"""
اختبارات الطبقة الثلاثية للتحقّق قبل قرار الإلغاء (م: SI2891) — لا إلغاء بلا يقين كامل.

الطبقات: ١) DB (الحالة)  ٢) الدفتر (قيد أصليّ)  ٣) MONEYADO/SQL (تأكيد فعليّ).
أيّ غموض → HELD + تنبيه المالك، لا قرار أوتوماتيكيّ.
"""
from __future__ import annotations

from datetime import timedelta

from core.constants import Currency, OperationType, Status, TreasuryType
from core.models import BotControl, Deal, ParsedLeg, TreasuryRef

from tests.test_integration import (
    ADMIN, PAST, FakeWriter, StubVerifier, _enable_storage, _make_pipeline, _raw,
)

_A_TRANSFER = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"


def _leg():
    return ParsedLeg(
        operation=OperationType.SELL, customer_code="1208", customer_name="فداء شاكونه",
        amount=1600.0, currency=Currency.EGP, reference_number="A5169", phone="010954227116",
        treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY, currency=Currency.EGP),
        source_message_key="m1")


async def _insert_deal(db, status):
    deal = Deal(deal_id=f"d-{status.value}", status=status, sell_leg=_leg(),
                created_at=PAST, updated_at=PAST, source_message_keys=["m1"])
    await db.deals.upsert(deal)
    return deal.deal_id


async def _cancel(pipe, at):
    await pipe.capture(_raw("c1", "إلغاء", reply="m1", at=at))
    await pipe.process_inbox(at + timedelta(seconds=60))


def _admin_alerts(outs):
    return [o for o in outs if o["chat_jid"] == ADMIN and o.get("is_alert")]


# ── ١) PARSED → إلغاء مباشر (سلوك قائم) ──────────────────────────────────────
async def test_parsed_direct_cancel(db):
    did = await _insert_deal(db, Status.PARSED)
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)
    await _cancel(pipe, PAST + timedelta(seconds=5))
    d = await db.deals.col.find_one({"deal_id": did})
    assert d["status"] == Status.CANCELLED.value, "PARSED → إلغاء مباشر"
    assert not any(c[0] == "buy" for c in writer.calls), "لا عكس (لم تُكتب)"


# ── ٢) COMPLETED + دفتر + SQL مؤكَّد → إلغاء طبيعيّ + عكس ─────────────────────
async def test_completed_ledger_sql_confirmed_normal_cancel(db):
    await _enable_storage(db)
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(enabled=True), rooms=False)
    await pipe.capture(_raw("m1", _A_TRANSFER))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)
    d = await db.deals.find_by_source_key("m1")
    assert d.status == Status.COMPLETED
    assert any(not e.is_reversal for e in await db.ledger.entries_for_deal(d.deal_id))

    await _cancel(pipe, now + timedelta(seconds=5))
    d2 = await db.deals.find_by_source_key("m1")
    assert d2.status == Status.CANCELLED, "مؤكَّد → إلغاء طبيعيّ"
    assert any(c[0] == "buy" for c in writer.calls), "قيد عكسيّ كُتب"


# ── ٣) COMPLETED + دفتر + SQL معطّل → HELD + تنبيه 🟡 ────────────────────────
async def test_completed_ledger_sql_disabled_holds_yellow(db):
    # وضع sql_required: SQL معطّل → HELD 🟡 (السلوك القديم، اختياريّ الآن عبر cancellation_mode).
    await _enable_storage(db)
    await db.control.set(BotControl(storage_enabled=True, cancellation_mode="sql_required",
                                    state="running"), "test")
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(enabled=False), rooms=False)
    await pipe.capture(_raw("m1", _A_TRANSFER))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)
    d = await db.deals.find_by_source_key("m1")
    assert d.status == Status.COMPLETED and len(await db.ledger.entries_for_deal(d.deal_id)) >= 1

    reversal_before = [c for c in writer.calls if c[0] == "buy"]
    await _cancel(pipe, now + timedelta(seconds=5))
    d2 = await db.deals.find_by_source_key("m1")
    assert d2.status == Status.HELD, "SQL معطّل → HELD لا CANCELLED"
    alerts = _admin_alerts(await db.outgoing.next_unsent(50))
    assert any("🟡" in (o.get("text") or "") and "غير مؤكَّد" in (o.get("text") or "") for o in alerts)
    assert [c for c in writer.calls if c[0] == "buy"] == reversal_before, "لا عكس بلا تأكيد"


async def test_completed_immediate_mode_reverses_without_sql(db):
    """الوضع الافتراضيّ (immediate): قيد بالدفتر → عكس فوريّ + 🚫 حتى لو SQL معطّل (لا شرط SQL)."""
    await _enable_storage(db)                                # الوضع الافتراضيّ = immediate
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(enabled=False), rooms=False)
    await pipe.capture(_raw("m1", _A_TRANSFER))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)
    assert (await db.deals.find_by_source_key("m1")).status == Status.COMPLETED

    await _cancel(pipe, now + timedelta(seconds=5))
    d2 = await db.deals.find_by_source_key("m1")
    assert d2.status == Status.CANCELLED, "immediate: عكس فوريّ بلا شرط SQL"
    assert any(c[0] == "buy" for c in writer.calls), "قيد عكسيّ كُتب"


async def test_completed_manual_mode_holds_no_reverse(db):
    """الوضع manual: القيد بالدفتر موجود لكن لا عكس تلقائيّ → HELD 🟡 + تصعيد يدويّ."""
    await _enable_storage(db)
    await db.control.set(BotControl(storage_enabled=True, cancellation_mode="manual",
                                    state="running"), "test")
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(enabled=True), rooms=False)
    await pipe.capture(_raw("m1", _A_TRANSFER))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)
    reversal_before = [c for c in writer.calls if c[0] == "buy"]

    await _cancel(pipe, now + timedelta(seconds=5))
    d2 = await db.deals.find_by_source_key("m1")
    assert d2.status == Status.HELD, "manual → HELD (لا عكس تلقائيّ)"
    assert [c for c in writer.calls if c[0] == "buy"] == reversal_before, "لا عكس في الوضع اليدويّ"
    assert any("يدويّ" in (o.get("text") or "")
               for o in _admin_alerts(await db.outgoing.next_unsent(50)))


# ── ٤) COMPLETED + لا دفتر → HELD + تنبيه 🔴 ─────────────────────────────────
async def test_completed_no_ledger_holds_red(db):
    did = await _insert_deal(db, Status.COMPLETED)   # مُدرَجة يدويًّا بلا أي قيد دفتر
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(enabled=True), rooms=False)
    await _cancel(pipe, PAST + timedelta(seconds=5))
    d = await db.deals.col.find_one({"deal_id": did})
    assert d["status"] == Status.HELD.value, "لا قيد → HELD (خطر عكس فراغ)"
    alerts = _admin_alerts(await db.outgoing.next_unsent(50))
    assert any("🔴" in (o.get("text") or "") and "قيد مفقود بالسجل" in (o.get("text") or "") for o in alerts)
    assert not any(c[0] == "buy" for c in writer.calls), "لا عكس فراغ"
