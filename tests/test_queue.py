"""
اختبارات الطابور والتجميع والخصم (§6 §7). أوقات صريحة (datetime) — بلا اعتماد على الوقت الحقيقي.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.constants import Currency, OperationType, Status
from core.models import ParsedLeg, RawMessage, Deal, SupplierRef
from core.queue import (
    QueueService,
    compute_commission,
    compute_grouping_key,
    is_same_deal,
    is_stable,
    resolve_two_leg_treasury,
    should_ignore_as_noise,
)

NOW = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)

# نصوص §5.5 (A6779) لاختبار الاستقرار
TWO_LEG_SELL_TEXT = """A6779
01115233493
8.475 ج م
فود فون كاش
53 احمد العكاري 5.90"""


def _sell_leg(**kw) -> ParsedLeg:
    base = dict(
        operation=OperationType.SELL, reference_number="A6779",
        phone="01115233493", amount=8475.0, currency=Currency.EGP,
        expects_pair=True, source_message_key="sell-msg",
    )
    base.update(kw)
    return ParsedLeg(**base)


def _buy_leg(**kw) -> ParsedLeg:
    base = dict(
        operation=OperationType.BUY, reference_number="A6779",
        phone="01115233493", amount=8391.0, currency=Currency.EGP,
        supplier=SupplierRef(name="طه"), source_message_key="buy-msg",
    )
    base.update(kw)
    return ParsedLeg(**base)


# ─────────────────────────────────────────────────────────────────────────────
# التجميع (§7.3) — طرفا A6779 في صفقة واحدة
# ─────────────────────────────────────────────────────────────────────────────
def test_grouping_key_reference_plus_phone():
    assert compute_grouping_key(_sell_leg()) == "A6779|01115233493"


def test_grouping_key_phone_only_when_ref_missing():
    # الرقم الإشاري ناقص → الهاتف وحده يميّز (§7.3)
    assert compute_grouping_key(_sell_leg(reference_number=None)) == "01115233493"


def test_is_same_deal_matches_ref_and_phone():
    assert is_same_deal(_sell_leg(), _buy_leg(), within_seconds=120) is True


def test_is_same_deal_missing_ref_falls_back_to_phone():
    # طرف بلا رقم إشاري لكن نفس الهاتف = نفس الصفقة (§7.3)
    assert is_same_deal(_sell_leg(), _buy_leg(reference_number=None), 120) is True


def test_is_same_deal_different_phone_rejected():
    assert is_same_deal(_sell_leg(), _buy_leg(phone="09999"), 120) is False


async def test_two_legs_grouped_into_one_deal(db):
    svc = QueueService(db)
    d1 = await svc.try_group(_sell_leg(), NOW)
    assert d1.status == Status.WAITING_SECOND_LEG
    assert d1.is_two_legged is True
    assert d1.waiting_deadline == NOW + timedelta(seconds=90)

    d2 = await svc.try_group(_buy_leg(), NOW + timedelta(seconds=30))
    # نفس الصفقة — دُمج الطرف الثاني (§7.3)
    assert d2.deal_id == d1.deal_id
    assert d2.sell_leg is not None and d2.buy_leg is not None
    assert d2.status == Status.PARSED
    assert d2.waiting_deadline is None
    # صفقة واحدة فقط في القاعدة
    assert await db.deals.col.count_documents({}) == 1


async def test_manual_grouping_by_reply_key(db):
    # التجميع اليدوي: الطرف الثاني يحمل مفتاح رسالة الأول (Reply/Edit §7.3)
    svc = QueueService(db)
    d1 = await svc.try_group(_sell_leg(source_message_key="orig"), NOW)
    buy = _buy_leg(reference_number=None, phone=None, source_message_key="orig")
    d2 = await svc.try_group(buy, NOW + timedelta(seconds=20))
    assert d2.deal_id == d1.deal_id
    assert d2.buy_leg is not None
    assert await db.deals.col.count_documents({}) == 1


# ─────────────────────────────────────────────────────────────────────────────
# الخصم والعمولة (§6.2) — الفرق بالسالب
# ─────────────────────────────────────────────────────────────────────────────
def test_commission_si_negative():
    # صيغة SI: 3540 → 3505 = −35
    si = ParsedLeg(operation=OperationType.SELL, amount=3540.0, amount_after_discount=3505.0)
    assert compute_commission(si, None) == -35.0


def test_commission_format_a_negative():
    # صيغة A: 1000 − 990 → عمولة −10
    sell = ParsedLeg(operation=OperationType.SELL, amount=1000.0)
    buy = ParsedLeg(operation=OperationType.BUY, amount=990.0)
    assert compute_commission(sell, buy) == -10.0


def test_commission_sell_only_none():
    sell = ParsedLeg(operation=OperationType.SELL, amount=1000.0)
    assert compute_commission(sell, None) is None


def test_two_leg_treasury_discount_vs_net():
    sell = ParsedLeg(operation=OperationType.SELL, amount=1000.0)
    buy = ParsedLeg(operation=OperationType.BUY, amount=990.0)
    assert resolve_two_leg_treasury(sell, buy, has_discount=True) == "خصم 1%"
    assert resolve_two_leg_treasury(sell, buy, has_discount=False) == "صافي"


# ─────────────────────────────────────────────────────────────────────────────
# ترتيب أوامر الكتابة (§7.3 §11.4) — بيع=0 قبل شراء=1
# ─────────────────────────────────────────────────────────────────────────────
async def test_write_jobs_sell_before_buy(db):
    svc = QueueService(db)
    deal = Deal(
        deal_id="deal-1", sell_leg=_sell_leg(), buy_leg=_buy_leg(),
        is_two_legged=True, created_at=NOW, updated_at=NOW,
    )
    await db.deals.upsert(deal)
    jobs = await svc.build_write_jobs(deal)
    assert [j.order_index for j in jobs] == [0, 1]
    assert jobs[0].operation == OperationType.SELL
    assert jobs[1].operation == OperationType.BUY
    assert jobs[1].max_attempts == 1
    # الصفقة انتقلت إلى READY
    refreshed = await db.deals.get("deal-1")
    assert refreshed.status == Status.READY


# ─────────────────────────────────────────────────────────────────────────────
# تجاوز 90s → تصعيد (§7.3)
# ─────────────────────────────────────────────────────────────────────────────
async def test_sweep_finalizes_single_leg_after_ninety_seconds(db):
    # قرار المستخدم: بيع كامل (كود+سعر) ينتظر شراءً اختياريًا → عند تجاوز المهلة بلا شراء
    # يُنهى كطرف واحد ويُدخَل (لا تصعيد). (بيع كامل ≠ حوالة A ناقصة التي تُنبَّه وتُصعَّد.)
    svc = QueueService(db)
    await svc.try_group(_sell_leg(customer_code="53", price_normalized="5.90"), NOW)

    # قبل المهلة — لا شيء
    assert await svc.sweep_waiting(NOW + timedelta(seconds=60)) == []

    # بعد 90s — إنهاء كطرف واحد (PARSED)، لا ESCALATED
    finalized = await svc.sweep_waiting(NOW + timedelta(seconds=91))
    assert len(finalized) == 1
    assert finalized[0].status == Status.PARSED
    assert finalized[0].is_two_legged is False
    assert finalized[0].waiting_deadline is None


async def test_sweep_no_finalize_when_second_leg_arrived(db):
    svc = QueueService(db)
    await svc.try_group(_sell_leg(), NOW)
    await svc.try_group(_buy_leg(), NOW + timedelta(seconds=30))
    # اكتملت قبل المهلة (صارت PARSED بالدمج) → sweep لا يجد شيئًا منتظِرًا
    assert await svc.sweep_waiting(NOW + timedelta(seconds=120)) == []


# ─────────────────────────────────────────────────────────────────────────────
# الاستقرار «الحرف» (§7.2)
# ─────────────────────────────────────────────────────────────────────────────
def test_harf_waits_until_max():
    harf = RawMessage(message_key="h1", chat_jid="central", text="حرف", received_at=NOW)
    # قصيرة قابلة للتعديل — لم تستقر بعد 30s
    assert is_stable(harf, NOW + timedelta(seconds=30)) is False
    # بعد STABILIZE_MAX (90s) تُحسم
    assert is_stable(harf, NOW + timedelta(seconds=90)) is True


def test_harf_still_short_is_noise_after_deadline():
    harf = RawMessage(message_key="h1", chat_jid="central", text="دير", received_at=NOW)
    # لم تُحسم قبل المهلة
    assert should_ignore_as_noise(harf, NOW + timedelta(seconds=30)) is False
    # بقيت قصيرة بلا بنية بعد المهلة → هدرزة
    assert should_ignore_as_noise(harf, NOW + timedelta(seconds=91)) is True


def test_harf_edited_into_full_transfer_stabilizes_immediately():
    # «الحرف ثم Edit» — الموظف يضع الحوالة بتعديل الرسالة (§7.2)
    edited = RawMessage(
        message_key="h1", chat_jid="central", text=TWO_LEG_SELL_TEXT,
        received_at=NOW, edited_at=NOW + timedelta(seconds=20),
    )
    assert is_stable(edited, NOW + timedelta(seconds=21)) is True
    assert should_ignore_as_noise(edited, NOW + timedelta(seconds=200)) is False


def test_complete_transfer_stable_immediately():
    full = RawMessage(message_key="f1", chat_jid="central", text=TWO_LEG_SELL_TEXT, received_at=NOW)
    assert is_stable(full, NOW) is True


# ─────────────────────────────────────────────────────────────────────────────
# حارس انحراف الساعة (§7.2) — توقيت مستقبلي لا يجمّد المعالجة، مع تحذير (لا صمت)
# ─────────────────────────────────────────────────────────────────────────────
import logging  # noqa: E402

from core.queue.stabilization import _elapsed_seconds, looks_like_complete_transfer  # noqa: E402

# received_at من وقت خادم واتساب؛ ساعة الجهاز متأخّرة ~44 دقيقة → المرجع «في المستقبل»
FUTURE = NOW + timedelta(minutes=44)


def test_future_timestamp_elapsed_never_negative():
    # جوهر العطل سابقًا: elapsed سالب ⇒ تجمّد أبدي. الآن محدود بصفر فأكثر.
    raw = RawMessage(message_key="fut1", chat_jid="central", text="نصّ بلا بنية حوالة واضحة",
                     received_at=FUTURE)
    assert _elapsed_seconds(raw, NOW) >= 0.0


def test_future_timestamp_complete_transfer_not_frozen():
    # حوالة كاملة بتوقيت مستقبلي (ساعة متأخّرة) → تُعالَج فورًا، لا تتجمّد.
    full = RawMessage(message_key="fut2", chat_jid="central", text=TWO_LEG_SELL_TEXT,
                      received_at=FUTURE)
    assert is_stable(full, NOW) is True


def test_future_timestamp_logs_warning(caplog):
    raw = RawMessage(message_key="fut3", chat_jid="central", text="نصّ عاديّ للانتظار",
                     received_at=FUTURE)
    with caplog.at_level(logging.WARNING, logger="core.queue.stabilization"):
        _elapsed_seconds(raw, NOW)
    assert any("توقيت مستقبلي" in r.message for r in caplog.records)


def test_small_clock_jitter_within_tolerance_no_warning(caplog):
    # تأخّر بسيط (ثوانٍ) ضمن الهامش → لا تحذير مزعج، elapsed = 0.
    raw = RawMessage(message_key="fut4", chat_jid="central", text="نصّ",
                     received_at=NOW + timedelta(seconds=3))
    with caplog.at_level(logging.WARNING, logger="core.queue.stabilization"):
        assert _elapsed_seconds(raw, NOW) == 0.0
    assert not any("توقيت مستقبلي" in r.message for r in caplog.records)


# ─────────────────────────────────────────────────────────────────────────────
# تعرّف الحوالة الكاملة على المرجع القصير «A01» وكلمة العملة «مصري/مصرى» (§3.4)
# ─────────────────────────────────────────────────────────────────────────────
def test_complete_transfer_recognizes_short_ref_and_masri():
    # المثال الحقيقي الذي كان يعلَق: مرجع قصير A01 + مبلغ بعملة «مصري»
    text = "A01\nفودافون\n01097298988\n69.000 مصري\nصافي"
    assert looks_like_complete_transfer(text) is True
    raw = RawMessage(message_key="real1", chat_jid="central", text=text, received_at=NOW)
    assert is_stable(raw, NOW) is True  # فورًا، بلا انتظار


def test_complete_transfer_masri_spelling_variant():
    assert looks_like_complete_transfer("A05\nفودافون\n69.000 مصرى\nصافي") is True
