"""
إصلاح ٤ (§ Reconciliation): التدقيق الدوري بين DB وMONEYADO — كشف انحراف الكتابة بعد الحدث.

يتحقّق: المقارنة النقيّة (كود/اسم/مبلغ/سعر/عمولة)؛ reconcile_recent يكتشف التعارض ويسجّل تقريرًا
ويُنبّه المسؤول؛ التطابق لا يُنبّه؛ SQL معطّل → لا شيء؛ عدم وجود صفّ → لا جزم؛ لا تكرار تنبيه؛
وأنّ SqlVerifier.reconcile استعلام قراءة‑فقط سليم. لا مساس بالمسار الحيّ (مهمّة مستقلّة).
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

from core.bus import Bus
from core.config import load_json_config
from core.constants import OperationType, Status
from core.db import utcnow
from core.models import Deal, ParsedLeg, TreasuryRef
from core.constants import TreasuryType
from core.verification.reconciliation import compare_leg_to_actual, reconcile_recent
from core.verification.sql_verifier import SqlVerifier

CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
CONFIG = Path(__file__).resolve().parent.parent / "config" / "sql_queries.example.json"


def _bus(db):
    return Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)


def _leg():
    return ParsedLeg(operation=OperationType.SELL, customer_code="612", customer_name="شركة الرائد",
                     amount=2825.0, price_normalized="5.98", commission=-28.0,
                     reference_number="A9078",
                     treasury=TreasuryRef(code="10", name="بلاس فون", type=TreasuryType.SELL_ONLY))


async def _add_completed_deal(db, leg):
    deal = Deal(deal_id="d-recon", status=Status.COMPLETED, sell_leg=leg,
                created_at=utcnow(), updated_at=utcnow())
    await db.deals.upsert(deal)   # upsert يضبط updated_at = utcnow() (حديث)
    return deal


class _FakeVerifier:
    def __init__(self, enabled: bool, record):
        self.enabled = enabled
        self._record = record

    async def reconcile(self, ref):
        return self._record


# ═════════════════════════════════════════════════════════════════════════════
# المقارنة النقيّة
# ═════════════════════════════════════════════════════════════════════════════
def test_compare_all_match_no_mismatch():
    actual = {"customer_code": "612", "customer_name": "شركة الرائد", "amount": 2825.0,
              "price": "5.98", "commission": -28.0}
    assert compare_leg_to_actual(_leg(), actual) == []


def test_compare_wrong_code_flagged():
    actual = {"customer_code": "603", "customer_name": "شركة الرائد", "amount": 2825.0,
              "price": "5.98", "commission": -28.0}
    ms = compare_leg_to_actual(_leg(), actual)
    assert any(m["field"] == "code" and m["sql_value"] == "603" for m in ms)


def test_compare_wrong_amount_and_price_flagged():
    actual = {"customer_code": "612", "customer_name": "شركة الرائد", "amount": 2797.0,
              "price": "5.92", "commission": -28.0}
    fields = {m["field"] for m in compare_leg_to_actual(_leg(), actual)}
    assert fields == {"amount", "price"}


def test_compare_missing_sql_field_not_flagged():
    # حقل غائب في MONEYADO (None) → لا يُقارَن (كشف محافظ)
    actual = {"customer_code": "612", "customer_name": None, "amount": 2825.0,
              "price": None, "commission": None}
    assert compare_leg_to_actual(_leg(), actual) == []


def test_compare_name_typo_tolerated():
    actual = {"customer_code": "612", "customer_name": "شركه الرائد", "amount": 2825.0,
              "price": "5.98", "commission": -28.0}
    assert all(m["field"] != "name" for m in compare_leg_to_actual(_leg(), actual))


# ═════════════════════════════════════════════════════════════════════════════
# reconcile_recent — الكشف والتنبيه والتسجيل
# ═════════════════════════════════════════════════════════════════════════════
async def test_reconcile_recent_detects_and_alerts(db):
    await _add_completed_deal(db, _leg())
    verifier = _FakeVerifier(True, {"customer_code": "603", "customer_name": "بن ناصر",
                                    "amount": 2825.0, "price": "5.98", "commission": -28.0})
    reported = await reconcile_recent(db, verifier, _bus(db), utcnow(), window_seconds=3600)
    assert len(reported) == 1 and reported[0]["reference_number"] == "A9078"
    # تقرير مسجَّل
    rep = await db.reconciliation.col.find_one({"deal_id": "d-recon"})
    assert rep is not None and any(m["field"] == "code" for m in rep["mismatches"])
    # تنبيه للمسؤول بـis_alert
    outs = await db.outgoing.next_unsent(50)
    alerts = [o for o in outs if o["chat_jid"] == ADMIN and "A9078" in (o.get("text") or "")]
    assert alerts and alerts[0].get("is_alert") is True


async def test_reconcile_recent_match_no_alert(db):
    await _add_completed_deal(db, _leg())
    verifier = _FakeVerifier(True, {"customer_code": "612", "customer_name": "شركة الرائد",
                                    "amount": 2825.0, "price": "5.98", "commission": -28.0})
    reported = await reconcile_recent(db, verifier, _bus(db), utcnow(), window_seconds=3600)
    assert reported == []
    assert await db.outgoing.next_unsent(50) == []


async def test_reconcile_recent_disabled_is_noop(db):
    await _add_completed_deal(db, _leg())
    verifier = _FakeVerifier(False, {"customer_code": "603"})
    reported = await reconcile_recent(db, verifier, _bus(db), utcnow(), window_seconds=3600)
    assert reported == [] and await db.outgoing.next_unsent(50) == []


async def test_reconcile_recent_not_found_skips(db):
    await _add_completed_deal(db, _leg())
    verifier = _FakeVerifier(True, None)   # غير موجود في SQL بعد
    reported = await reconcile_recent(db, verifier, _bus(db), utcnow(), window_seconds=3600)
    assert reported == [] and await db.outgoing.next_unsent(50) == []


async def test_reconcile_recent_no_duplicate_alert(db):
    await _add_completed_deal(db, _leg())
    verifier = _FakeVerifier(True, {"customer_code": "603", "amount": 2825.0, "price": "5.98"})
    first = await reconcile_recent(db, verifier, _bus(db), utcnow(), window_seconds=3600)
    second = await reconcile_recent(db, verifier, _bus(db), utcnow(), window_seconds=3600)
    assert len(first) == 1 and second == []   # لا تكرار تنبيه لنفس التعارض
    alerts = [o for o in await db.outgoing.next_unsent(50) if o["chat_jid"] == ADMIN]
    assert len(alerts) == 1


# ═════════════════════════════════════════════════════════════════════════════
# SqlVerifier.reconcile — استعلام قراءة فقط سليم + تحويل الصفّ
# ═════════════════════════════════════════════════════════════════════════════
async def test_sql_verifier_reconcile_maps_row():
    config = load_json_config(CONFIG)

    def _connect(dsn):
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = ("TX-9", "612", "شركة الرائد", 2825.0, "5.98", -28.0)
        conn.cursor.return_value = cur
        return conn

    verifier = SqlVerifier("DSN=x", config, enabled=True, connect=_connect)
    got = await verifier.reconcile("A9078")
    assert got == {"moneyado_ref": "TX-9", "customer_code": "612", "customer_name": "شركة الرائد",
                   "amount": 2825.0, "price": "5.98", "commission": -28.0}


async def test_sql_verifier_reconcile_disabled_none():
    config = load_json_config(CONFIG)
    verifier = SqlVerifier("DSN=x", config, enabled=False)
    assert await verifier.reconcile("A9078") is None
