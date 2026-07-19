"""
الأولوية 3 — صلابة الكاتب: الفورم البطيء/الناقص + النجاة من إغلاق MONEYADO أثناء الكتابة +
إعادة التشغيل اليدويّة + حل الموردين الجريء.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from core.constants import OperationType, Status, TreasuryType
from core.models import ParsedLeg, RawMessage, SupplierRecord, WriteResult
from core.writers.moneyado.screens import FormNotReadyError, MoneyadoScreen
from core.writers.moneyado.writer import _is_transient
from tests.test_moneyado_writer import make_job, make_screen, make_sell_leg, make_writer

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
EMP = "20100@s.whatsapp.net"


# ── بند 1: اكتمال بنية الفورم ─────────────────────────────────────────────────────────
def test_wait_structure_complete_full_vs_partial():
    """يكتمل عند بلوغ العدد المتوقّع؛ يبقى ناقصًا (False) إن ظلّ العدد أقلّ خلال المهلة."""
    scr = MoneyadoScreen({"sell_screen": {"expected_field_count": 21}})
    win = MagicMock()
    scr._window = win
    win.descendants.return_value = [0] * 21                       # مكتمل
    assert scr.wait_structure_complete(OperationType.SELL, timeout=0) is True
    win.descendants.return_value = [0] * 15                       # ناقص (15/21)
    assert scr.wait_structure_complete(OperationType.SELL, timeout=0) is False
    # بلا بصمة عدد → fail-open (لا فحص)
    scr2 = MoneyadoScreen({"sell_screen": {}})
    scr2._window = win
    assert scr2.wait_structure_complete(OperationType.SELL, timeout=0) is True


# ── بند 2: تصنيف الفشل ─────────────────────────────────────────────────────────────────
def test_is_transient_classification():
    assert _is_transient(FormNotReadyError("x"))
    assert _is_transient(RuntimeError("MONEYADO غير مرئي — افتحه أولًا"))
    assert _is_transient(RuntimeError("الشاشة الحالية ليست القائمة الرئيسية"))
    assert not _is_transient(RuntimeError("الحقل غير موجود عند الإحداثي (590,230)"))  # فشل حقيقيّ
    assert not _is_transient(ValueError("عشوائيّ"))


@pytest.mark.asyncio
async def test_write_requeue_on_form_not_ready(tmp_path):
    """(بند 2) الفورم لم تكتمل بنيته (FormNotReadyError قبل الضغط) → requeue، **لا** tech_failed."""
    screen = make_screen()
    screen.open_sell_screen.side_effect = FormNotReadyError("فورم «بيع عملة» لم تكتمل بنيته")
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    res = await writer.write(make_job(make_sell_leg()), commit=True)
    assert res.ok is False and res.requeue is True and res.store_unconfirmed is False


@pytest.mark.asyncio
async def test_write_requeue_on_moneyado_invisible(tmp_path):
    """(بند 2) MONEYADO غير مرئي أثناء الفتح (قبل الضغط) → requeue."""
    screen = make_screen()
    screen.open_sell_screen.side_effect = RuntimeError("MONEYADO غير مرئي — افتحه أولًا")
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    res = await writer.write(make_job(make_sell_leg()), commit=True)
    assert res.requeue is True


@pytest.mark.asyncio
async def test_write_store_unconfirmed_when_dim_not_seen(tmp_path):
    """(بند 2 استثناء) ضُغط «تخزين» ولم يُؤكَّد البهتان → store_unconfirmed (لا requeue، لا إعادة تلقائية)."""
    screen = make_screen()
    screen.wait_store_confirmed.return_value = False        # لم يُبيَهت الزرّ بعد الضغط
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    res = await writer.write(make_job(make_sell_leg()), commit=True)
    assert res.ok is False and res.store_unconfirmed is True and res.requeue is False


@pytest.mark.asyncio
async def test_write_store_unconfirmed_on_crash_after_press(tmp_path):
    """(بند 2 استثناء) انقطاع (استثناء) **بعد** ضغط «تخزين» → store_unconfirmed لا requeue."""
    screen = make_screen()
    screen.wait_store_confirmed.side_effect = RuntimeError("Invalid window handle")  # بعد الضغط
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    res = await writer.write(make_job(make_sell_leg()), commit=True)
    assert res.store_unconfirmed is True and res.requeue is False


# ── بند 2: الأنبوب — requeue → PARSED (يبقى قابلًا للتنفيذ) ──────────────────────────────
class _W:
    name = "w"

    def __init__(self, result):
        self._result = result

    async def write(self, job, *, commit):
        return self._result


class _RecBus:
    def __init__(self):
        self.central_jid = CENTRAL
        self.admin_jid = ADMIN
        self.admin_msgs = []
        self.central_msgs = []

    async def notify_admin(self, text, reply_to_key=None, forward_key=None):
        self.admin_msgs.append(text)

    async def reply_central(self, text, reply_to_key=None, *, is_alert=False):
        self.central_msgs.append(text)

    async def mark_central(self, key, emoji):
        pass

    async def flush_reactions(self):
        pass

    async def wait_for_reaction_sent(self, keys, timeout=5.0):
        pass

    async def react(self, chat_jid, key, emoji):
        pass


class _V:
    enabled = False

    async def verify_transaction(self, *a, **k):
        return (False, None)

    async def find_last_pending(self, *a, **k):
        return None


def _pipe(db, writer, bus=None):
    from core.pipeline import Pipeline
    return Pipeline(db, bus or _RecBus(), writer, _V(), customer_room_jids=[], treasury_room_jids=[])


async def _seed_deal(db, did, ref, status=Status.READY):
    from core.models import Deal, TreasuryRef
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code="100",
                    customer_name="زبون", amount=5000.0, price_normalized="6.0",
                    treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY),
                    sender_jid=EMP, source_message_key=f"{did}-a")
    d = Deal(deal_id=did, status=status, sell_leg=leg, created_at=NOW, updated_at=NOW,
             chat_jid=CENTRAL, source_message_keys=[f"{did}-a"])
    await db.deals.upsert(d)
    return d


async def _enable_storage(db):
    from core.models import BotControl
    await db.control.set(BotControl(storage_enabled=True, state="running"), "test")


async def test_pipeline_requeue_returns_to_parsed_no_tech_failed(db):
    """(بند 2) نتيجة requeue بلا قيد نازل → الصفقة PARSED (تُستأنَف تلقائيًّا)، **ليست** tech_failed."""
    await _enable_storage(db)
    deal = await _seed_deal(db, "d1", "X1")
    bus = _RecBus()
    pipe = _pipe(db, _W(WriteResult(ok=False, requeue=True, error="فشل عابر")), bus)
    from core.models import WriteJob
    job = WriteJob(job_id="j1", deal_id="d1", operation=OperationType.SELL, leg=deal.sell_leg,
                   created_at=NOW)
    out = await pipe._write_jobs(deal, [job], NOW)
    fresh = await db.deals.get("d1")
    assert fresh.status == Status.PARSED                          # عادت للطابور (لا tech_failed)
    assert any("تُستأنَف" in m or "باقية بالطابور" in m for m in bus.central_msgs)


async def test_pipeline_store_unconfirmed_escalates_no_retry(db):
    """(بند 2 استثناء) store_unconfirmed → tech_failed + تنبيه «غير مؤكّد» (لا إعادة تلقائية)."""
    await _enable_storage(db)
    deal = await _seed_deal(db, "d2", "X2")
    bus = _RecBus()
    pipe = _pipe(db, _W(WriteResult(ok=False, store_unconfirmed=True, needs_review=True,
                                    error="لم يتأكّد")), bus)
    from core.models import WriteJob
    job = WriteJob(job_id="j2", deal_id="d2", operation=OperationType.SELL, leg=deal.sell_leg,
                   created_at=NOW)
    await pipe._write_jobs(deal, [job], NOW)
    fresh = await db.deals.get("d2")
    assert fresh.status == Status.TECH_FAILED
    assert any("غير مؤكّد التخزين" in m for m in bus.admin_msgs)


# ── بند 3: إعادة تشغيل يدويّة ──────────────────────────────────────────────────────────
def test_detect_control_rerun():
    from core.parsing.classify import detect_control
    assert detect_control("أعد") == ("rerun", None)
    assert detect_control("أعدها") == ("rerun", None)
    assert detect_control("إلغاء") == ("cancel", None)          # لم يتغيّر


async def test_rerun_control_requeues_tech_failed(db):
    """(بند 3) Reply «أعد» من معتمد على tech_failed بلا قيد نازل → PARSED (تُعاد عبر النبضة)."""
    from core.models import EmployeeRecord
    await db.employees.upsert(EmployeeRecord(whatsapp_number=EMP, name="مسؤول", active=True))
    deal = await _seed_deal(db, "d3", "X3", status=Status.TECH_FAILED)
    bus = _RecBus()
    pipe = _pipe(db, _W(WriteResult(ok=True)), bus)
    raw = RawMessage(message_key="r1", chat_jid=CENTRAL, sender_jid=EMP, text="أعد",
                     reply_to_key="d3-a", received_at=NOW)
    await pipe._handle_control("rerun", None, raw, NOW)
    fresh = await db.deals.get("d3")
    assert fresh.status == Status.PARSED
    assert any("أُعيدت للطابور" in m for m in bus.central_msgs)


# ── بند 4: حل الموردين بالتشابه — 🔴 ملغى (قرار المالك 2026-07-19) ───────────────────────
async def test_supplier_similarity_disabled_no_guessing(db):
    """مورّد معنون لم يُحلّ صارمًا **لا يُخمَّن** بالتشابه: يبقى unresolved بلا تنبيه ولا تعلّم → تصعيد."""
    await db.suppliers.seed_if_missing([{"name": "أبو يوسف", "code": "1290", "aliases": []}])
    bus = _RecBus()
    pipe = _pipe(db, _W(WriteResult(ok=True)), bus)
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="X9", unresolved_supplier="ابو بوسف")
    raw = RawMessage(message_key="k", chat_jid=CENTRAL, sender_jid=EMP, text="...", received_at=NOW)
    await pipe._auto_resolve_supplier(leg, raw, await db.suppliers.all_active())
    assert leg.supplier is None and leg.unresolved_supplier == "ابو بوسف"
    assert not any("تشابه اسم مورّد" in m for m in bus.admin_msgs)
    from core.parsing.resolve import resolve_supplier
    assert resolve_supplier("ابو بوسف", await db.suppliers.all_active()) is None   # لم يُتعلَّم شيء
