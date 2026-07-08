"""
اختبارات صيغة الخصم من رسالتين (Aخصم) — إضافة لا تكسر الصيغ القائمة (§6.3).

الصيغة:
  رسالة ١ (الهوية):  A8010 + هاتف + مبلغ **قبل** الخصم (8550) + وسيلة + «كود اسم سعر».
  رسالة ٢ (التسوية): A8010 (نفس الرقم الإشاري) + هاتف + مبلغ **بعد** الخصم (8465) + وسيلة + خزينة فقط.
الربط بالرقم الإشاري لا بالقرب الزمني. الحساب:
  الخصم = 8550 − 8465 = 85، العمولة = −85 (خانة العمولة)، المبلغ الأجنبي = 8550 (قبل)،
  والمبلغ المخصوم النهائي = 8465 (يحسبه MONEYADO من المبلغ − العمولة).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.constants import OperationType, Status, TreasuryType
from core.models import ParsedLeg, TreasuryRef
from core.parsing import parse_message
from core.queue.grouping import discount_pair
from core.queue.service import QueueService, is_completion_fragment
from core.writers.moneyado.fields import build_sell_fields

NOW = datetime(2026, 7, 8, 12, 0, 0, tzinfo=timezone.utc)
JID = "central@g.us"

MSG_IDENTITY = "A8010\n01012345678\n8550 ج م\nفودافون\n570 ايهاب ابو حميد 5.70"
MSG_SETTLEMENT = "A8010\n01012345678\n8465 ج م\nفودافون\nبلس"


async def _treas(db):
    return await db.treasuries.all_active()


# ── الكشف النقيّ (discount_pair) ─────────────────────────────────────────────
def test_discount_pair_pure_detection_and_negatives():
    identity = ParsedLeg(
        operation=OperationType.SELL, customer_code="570", customer_name="ايهاب ابو حميد",
        price_normalized="5.70", amount=8550, reference_number="A8010", phone="01012345678",
    )
    settlement = ParsedLeg(
        operation=OperationType.SELL, amount=8465, reference_number="A8010", phone="01012345678",
        treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY),
    )
    # يُكتشف بأي ترتيب (الربط بالرقم الإشاري لا الوصول)
    assert discount_pair(identity, settlement) == (identity, settlement)
    assert discount_pair(settlement, identity) == (identity, settlement)

    # سلبيات: رقم إشاري مختلف → لا زوج
    assert discount_pair(identity, settlement.model_copy(update={"reference_number": "A9999"})) is None
    # التسوية فيها كود زبون → ليست تسوية (ازدواج/طرف مستقلّ)
    assert discount_pair(identity, settlement.model_copy(update={"customer_code": "999"})) is None
    # الهوية فيها خزينة (بيع مفرد مكتمل) → لا انتظار تسوية
    assert discount_pair(identity.model_copy(update={"treasury": settlement.treasury}), settlement) is None
    # الهوية بلا كود (حوالة A ناقصة عادية) → ليست صيغة خصم
    assert discount_pair(identity.model_copy(update={"customer_code": None}), settlement) is None


# ── الفهم: كل رسالة على حدة ──────────────────────────────────────────────────
async def test_identity_and_settlement_parse_as_expected(db):
    treas = await _treas(db)

    leg1 = parse_message(MSG_IDENTITY, treas, []).leg
    assert leg1.reference_number == "A8010"
    assert leg1.customer_code == "570"
    assert leg1.customer_name == "ايهاب ابو حميد"
    assert leg1.amount == 8550
    assert leg1.price_normalized  # سعر ملتقَط
    assert leg1.treasury is None  # الخزينة تأتي في رسالة التسوية

    leg2 = parse_message(MSG_SETTLEMENT, treas, []).leg
    assert leg2.reference_number == "A8010"
    assert leg2.customer_code is None       # التسوية بلا كود
    assert leg2.amount == 8465
    assert leg2.treasury is not None and leg2.treasury.code == "74"  # بلاس فون


# ── الدمج (الترتيب المعتاد: الهوية ثم التسوية) ────────────────────────────────
async def test_discount_merge_forward_order(db):
    svc = QueueService(db)
    treas = await _treas(db)

    leg1 = parse_message(MSG_IDENTITY, treas, []).leg
    leg1.source_message_key = "d-m1"
    d1 = await svc.try_group(leg1, NOW, chat_jid=JID)
    assert d1.status == Status.WAITING_SECOND_LEG   # ينتظر (خزينة None)

    leg2 = parse_message(MSG_SETTLEMENT, treas, []).leg
    leg2.source_message_key = "d-m2"
    d2 = await svc.try_group(leg2, NOW + timedelta(seconds=20), chat_jid=JID)

    assert d2.deal_id == d1.deal_id
    assert d2.status == Status.PARSED
    assert d2.is_two_legged is False and d2.buy_leg is None
    sell = d2.sell_leg
    assert sell.customer_code == "570"
    assert sell.customer_name == "ايهاب ابو حميد"
    assert sell.amount == 8550                  # قبل الخصم → خانة المبلغ الأجنبي
    assert sell.amount_after_discount == 8465   # بعد الخصم → يحسبه MONEYADO
    assert sell.commission == -85.0             # بعد − قبل
    assert sell.commission_rate == 0.0
    assert sell.treasury.code == "74"           # بلاس فون من التسوية
    assert {"d-m1", "d-m2"} <= set(d2.source_message_keys)


# ── الدمج (الترتيب العكسي: التسوية ثم الهوية) — الربط بالرقم الإشاري ──────────
async def test_discount_merge_reverse_order(db):
    svc = QueueService(db)
    treas = await _treas(db)

    leg2 = parse_message(MSG_SETTLEMENT, treas, []).leg
    leg2.source_message_key = "d-m2"
    d_a = await svc.try_group(leg2, NOW, chat_jid=JID)
    assert d_a.status == Status.WAITING_SECOND_LEG   # ناقصة الهوية → تنتظر

    leg1 = parse_message(MSG_IDENTITY, treas, []).leg
    leg1.source_message_key = "d-m1"
    d_b = await svc.try_group(leg1, NOW + timedelta(seconds=20), chat_jid=JID)

    assert d_b.deal_id == d_a.deal_id
    assert d_b.status == Status.PARSED
    sell = d_b.sell_leg
    assert sell.customer_code == "570"
    assert sell.amount == 8550
    assert sell.amount_after_discount == 8465
    assert sell.commission == -85.0
    assert sell.treasury.code == "74"


# ── التعيين النهائي في خانات MONEYADO (§11.1 §6.2) ──────────────────────────
async def test_discount_maps_to_moneyado_fields(db):
    svc = QueueService(db)
    treas = await _treas(db)

    leg1 = parse_message(MSG_IDENTITY, treas, []).leg
    leg1.source_message_key = "d-m1"
    await svc.try_group(leg1, NOW, chat_jid=JID)
    leg2 = parse_message(MSG_SETTLEMENT, treas, []).leg
    leg2.source_message_key = "d-m2"
    deal = await svc.try_group(leg2, NOW + timedelta(seconds=20), chat_jid=JID)

    ops = {op.key: op.value for op in build_sell_fields(deal.sell_leg)}
    assert ops["foreign_amount"] == "8550"   # المبلغ الأجنبي = قبل الخصم
    assert ops["commission"] == "-85"        # العمولة سالبة
    assert ops["commission_rate"] == "0"
    assert ops["foreign_account"] == "74"    # حساب الخزينة (بلاس فون)


# ── لا كسر: رسالة خزينة «بلس» وحدها (بلا رقم إشاري/مبلغ) تبقى إكمال خزينة لا خصمًا ──
async def test_bare_treasury_fragment_is_not_discount(db):
    svc = QueueService(db)
    treas = await _treas(db)

    leg1 = parse_message(
        "A8011\n01012345678\n8550 ج م\nفودافون\n570 ايهاب ابو حميد 5.70", treas, []
    ).leg
    leg1.source_message_key = "f-m1"
    d1 = await svc.try_group(leg1, NOW, chat_jid=JID)
    assert d1.status == Status.WAITING_SECOND_LEG

    frag = parse_message("بلس", treas, [])
    assert frag.kind == "noise" and is_completion_fragment(frag.leg) is True
    d2 = await svc.absorb_fragment(frag.leg, JID, "f-m2", NOW + timedelta(seconds=20))

    assert d2.deal_id == d1.deal_id
    sell = d2.sell_leg
    assert sell.treasury.code == "74"
    assert sell.amount == 8550
    assert sell.amount_after_discount is None   # لا خصم
    assert sell.commission is None              # لا عمولة (تُكتب 0 في MONEYADO)
