"""
اختبارات التحقّق من SQL Server والاسترجاع (§11.4 §12 §9) — بلا pyodbc حقيقي.

كل شيء يُحقَن: SqlVerifier يأخذ `connect` وهميًّا (mock connection/cursor) فلا يتصل فعلًا،
وrecover_pending يأخذ verifier وهميًّا. لا يوجد SQL Server ولا pyodbc في هذه البيئة.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.config import load_json_config
from core.constants import Currency, OperationType, Status
from core.models import Deal, LedgerEntry, ParsedLeg
from core.verification import recover_pending
from core.verification.sql_verifier import SqlVerifier, _assert_read_only

CONFIG = Path(__file__).resolve().parent.parent / "config" / "sql_queries.example.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def queries_config() -> dict:
    """إعداد الاستعلامات من المثال المرفق (config/sql_queries.example.json)."""
    return load_json_config(CONFIG)


def _connect_returning(row):
    """مصنع اتصال وهمي: cursor.fetchone() يُرجع الصف المُعطى (أو None)."""
    def factory(dsn):
        conn = MagicMock(name="conn")
        cursor = MagicMock(name="cursor")
        cursor.fetchone.return_value = row
        conn.cursor.return_value = cursor
        return conn
    return factory


# ─────────────────────────────────────────────────────────────────────────────
# SqlVerifier.verify_transaction
# ─────────────────────────────────────────────────────────────────────────────
async def test_verify_transaction_row_match_returns_true_and_ref(queries_config):
    verifier = SqlVerifier(
        "DSN=test", queries_config, enabled=True, connect=_connect_returning(("TX-777",))
    )
    ok, ref = await verifier.verify_transaction("A6779", 8475.0, "53", OperationType.SELL)
    assert ok is True
    assert ref == "TX-777"


async def test_verify_transaction_no_row_returns_false_none(queries_config):
    verifier = SqlVerifier(
        "DSN=test", queries_config, enabled=True, connect=_connect_returning(None)
    )
    ok, ref = await verifier.verify_transaction("A6779", 8475.0, "53", OperationType.SELL)
    assert ok is False
    assert ref is None


async def test_verify_transaction_disabled_is_safe_neutral(queries_config):
    """enabled=False → نتيجة محايدة آمنة بلا أي اتصال بقاعدة البيانات (§0)."""
    spy = MagicMock(name="connect", side_effect=AssertionError("يجب ألا يُتصل وقد عُطّل"))
    verifier = SqlVerifier("DSN=test", queries_config, enabled=False, connect=spy)
    ok, ref = await verifier.verify_transaction("A6779", 8475.0, "53", OperationType.SELL)
    assert (ok, ref) == (False, None)
    spy.assert_not_called()


async def test_verify_transaction_passes_expected_params(queries_config):
    """يتأكّد أن الاستعلام مرّر (رقم إشاري + مبلغ + كود زبون + نوع)."""
    factory = _connect_returning(("TX-1",))
    captured = {}

    def wrap(dsn):
        conn = factory(dsn)
        orig = conn.cursor.return_value.execute

        def exec_spy(sql, params):
            captured["sql"] = sql
            captured["params"] = params
            return orig(sql, params)

        conn.cursor.return_value.execute.side_effect = exec_spy
        return conn

    verifier = SqlVerifier("DSN=test", queries_config, enabled=True, connect=wrap)
    await verifier.verify_transaction("A6779", 8475.0, "53", OperationType.SELL)
    assert captured["params"] == ("A6779", 8475.0, "53", "sell")
    assert captured["sql"].lstrip().upper().startswith("SELECT")


# ─────────────────────────────────────────────────────────────────────────────
# SqlVerifier.find_last_pending
# ─────────────────────────────────────────────────────────────────────────────
async def test_find_last_pending_returns_dict(queries_config):
    verifier = SqlVerifier(
        "DSN=test", queries_config, enabled=True,
        connect=_connect_returning(("TX-1", "A6779", 1600.0)),
    )
    result = await verifier.find_last_pending("A6779")
    assert result == {"ref": "TX-1", "refnum": "A6779", "amount": 1600.0}


async def test_find_last_pending_none_when_missing(queries_config):
    verifier = SqlVerifier(
        "DSN=test", queries_config, enabled=True, connect=_connect_returning(None)
    )
    assert await verifier.find_last_pending("A9999") is None


async def test_find_last_pending_disabled_returns_none(queries_config):
    verifier = SqlVerifier("DSN=test", queries_config, enabled=False)
    assert await verifier.find_last_pending("A6779") is None


# ─────────────────────────────────────────────────────────────────────────────
# قراءة فقط (§11.4) — لا INSERT/UPDATE/DELETE إطلاقًا
# ─────────────────────────────────────────────────────────────────────────────
def test_all_rendered_queries_are_read_only(queries_config):
    verifier = SqlVerifier("DSN=test", queries_config, enabled=True, connect=_connect_returning(None))
    assert verifier._sql, "يجب أن تُبنى استعلامات"
    forbidden = ("INSERT", "UPDATE", "DELETE", "DROP", "MERGE", "EXEC", "TRUNCATE", "ALTER")
    for key, sql in verifier._sql.items():
        upper = sql.upper()
        assert upper.lstrip().startswith("SELECT"), f"{key} ليس SELECT"
        for word in forbidden:
            assert word not in upper, f"{key} يحوي كلمة كتابة ممنوعة {word}"


def test_raw_config_queries_are_read_only(queries_config):
    """حتى قوالب الإعداد الخام خالية من كلمات الكتابة."""
    forbidden = ("INSERT", "UPDATE", "DELETE", "DROP", "MERGE", "TRUNCATE")
    for key, template in queries_config["queries"].items():
        upper = template.upper()
        for word in forbidden:
            assert word not in upper, f"{key} خام يحوي {word}"


def test_write_query_rejected_at_init():
    """استعلام كتابة يفشل بوضوح عند التهيئة (لا يُبنى SqlVerifier أصلًا)."""
    bad = {
        "tables": {"transactions": "dbo.Transactions"},
        "columns": {"amount": "ForeignAmount"},
        "queries": {"evil": "UPDATE {transactions} SET {amount} = 0"},
    }
    with pytest.raises(ValueError):
        SqlVerifier("DSN=test", bad, enabled=True, connect=_connect_returning(None))


def test_assert_read_only_helper_rejects_delete():
    with pytest.raises(ValueError):
        _assert_read_only("DELETE FROM dbo.Transactions", "x")
    # SELECT سليم لا يرمي
    _assert_read_only("SELECT TOP 1 x FROM dbo.T WHERE a = ?", "ok")


# ─────────────────────────────────────────────────────────────────────────────
# verifier وهمي للاسترجاع
# ─────────────────────────────────────────────────────────────────────────────
class _StubVerifier:
    """verifier وهمي: نتيجة قابلة للضبط لكل نوع عملية + تتبّع الاستدعاءات."""

    def __init__(self, enabled=True, sell=(True, "TX-SELL"), buy=(True, "TX-BUY")):
        self.enabled = enabled
        self._sell = sell
        self._buy = buy
        self.calls: list = []

    async def verify_transaction(self, ref, amount, customer_code, operation):
        self.calls.append((ref, amount, customer_code, operation))
        return self._sell if operation == OperationType.SELL else self._buy

    async def find_last_pending(self, ref):
        return None


def _sell_leg(ref="A6779", amount=8475.0, code="53"):
    return ParsedLeg(
        operation=OperationType.SELL, customer_code=code, amount=amount,
        currency=Currency.EGP, reference_number=ref, source_message_key="msg-1",
    )


def _buy_leg(ref="A6779", amount=8391.0, code="760"):
    return ParsedLeg(
        operation=OperationType.BUY, customer_code=code, amount=amount,
        currency=Currency.EGP, reference_number=ref, source_message_key="msg-2",
    )


async def _make_deal(db, *, status, sell_leg, buy_leg=None, two_legged=False):
    deal = Deal(
        deal_id="deal-1", status=status, sell_leg=sell_leg, buy_leg=buy_leg,
        is_two_legged=two_legged, created_at=_now(), updated_at=_now(),
        source_message_keys=["msg-1"],
    )
    await db.deals.upsert(deal)
    return deal


async def _append_sell_ledger(db, sql_verified=True):
    await db.ledger.append(LedgerEntry(
        entry_id="led-sell", deal_id="deal-1", message_key="msg-1",
        reference_number="A6779", operation=OperationType.SELL, amount=8475.0,
        currency=Currency.EGP, customer_code="53", status=Status.SELL_DONE,
        sql_verified=sql_verified, created_at=_now(),
    ))


# ─────────────────────────────────────────────────────────────────────────────
# recover_pending (§12 §11.4 §9)
# ─────────────────────────────────────────────────────────────────────────────
async def test_recover_sell_done_never_resells(db):
    """قيد SELL_DONE في الدفتر + verifier يؤكّد الحفظ → لا يعيد البيع أبدًا."""
    await _make_deal(db, status=Status.SELL_DONE, sell_leg=_sell_leg())
    await _append_sell_ledger(db, sql_verified=True)

    verifier = _StubVerifier(enabled=True)
    reports = await recover_pending(db, verifier)

    assert len(reports) == 1
    assert reports[0]["sell"] == "already_landed_no_resell"
    # لم يُضَف قيد بيع ثانٍ (لا ازدواج §9)
    entries = await db.ledger.entries_for_deal("deal-1")
    sells = [e for e in entries if e.operation == OperationType.SELL and not e.is_reversal]
    assert len(sells) == 1


async def test_recover_half_failure_buy_needs_completion(db):
    """بيع نزل، شراء لم ينزل → لا يعيد البيع، يُعلّم الشراء لإكمال محاولة واحدة (§11.4)."""
    await _make_deal(
        db, status=Status.SELL_DONE, sell_leg=_sell_leg(),
        buy_leg=_buy_leg(), two_legged=True,
    )
    await _append_sell_ledger(db, sql_verified=True)

    verifier = _StubVerifier(enabled=True, buy=(False, None))
    reports = await recover_pending(db, verifier)

    assert reports[0]["sell"] == "already_landed_no_resell"
    assert reports[0]["buy"] == "needs_completion_one_retry"
    # البيع مؤكّد سابقًا فلا يُستعلم عنه؛ الاستعلام الوحيد للشراء
    assert verifier.calls == [("A6779", 8391.0, "760", OperationType.BUY)]


async def test_recover_ready_found_in_sql_records_no_reentry(db):
    """READY (لا قيد بالدفتر) لكن SQL يؤكّد الحفظ → يُسجَّل بلا إعادة إدخال (§9)."""
    await _make_deal(db, status=Status.READY, sell_leg=_sell_leg())

    verifier = _StubVerifier(enabled=True, sell=(True, "TX-9"))
    reports = await recover_pending(db, verifier)

    assert reports[0]["sell"] == "found_in_sql_recorded_no_reentry"
    entries = await db.ledger.entries_for_deal("deal-1")
    assert len(entries) == 1
    assert entries[0].sql_verified is True
    assert entries[0].moneyado_ref == "TX-9"
    assert entries[0].status == Status.SELL_DONE


async def test_recover_ready_not_saved_safe_to_retry(db):
    await _make_deal(db, status=Status.READY, sell_leg=_sell_leg())

    verifier = _StubVerifier(enabled=True, sell=(False, None))
    reports = await recover_pending(db, verifier)

    assert reports[0]["sell"] == "not_saved_safe_to_retry"
    # لم يُسجَّل شيء (لم يُحفظ)
    assert await db.ledger.entries_for_deal("deal-1") == []


async def test_recover_ready_verifier_disabled_escalates(db):
    """READY وSQL معطّل → لا يمكن الجزم → تصعيد (القاعدة الذهبية §0)."""
    await _make_deal(db, status=Status.READY, sell_leg=_sell_leg())

    verifier = _StubVerifier(enabled=False)
    reports = await recover_pending(db, verifier)

    assert reports[0]["sell"] == "cannot_verify_escalate"


async def test_recover_no_pending_returns_empty(db):
    assert await recover_pending(db, _StubVerifier()) == []
