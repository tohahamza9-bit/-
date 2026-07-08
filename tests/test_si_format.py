"""
اختبارات صيغة SI المعنونة (§3.3) — بما فيها متغيّر «القيمة (صافي)» (Fix 5).
المصدر: pr.md المصدر ٤ (د) + الصور (٧ SI0855، ٩ SI0827).
"""
from __future__ import annotations

from core.constants import Currency
from core.parsing import parse_message
from core.queue.commission import compute_commission
from core.writers.moneyado.fields import build_sell_fields


async def _treas(db):
    return await db.treasuries.all_active()


# نصّ SI1417 (خصم 1%): قبل=20475، بعد=20271 → عمولة −204، خزينة «ابو يوسف» (77)
SI1417_TEXT = (
    "رقم العملية: SI1417\n"
    "رقم المستلم: 01037354643\n"
    "اسم الزبون: 570 ايهاب ابو حميد\n"
    "القيمة قبل الخصم: 20475 ج.م\n"
    "القيمة بعد الخصم 1%: 20271 ج.م\n"
    "السعر: 5.9\n"
    "نوع التحويل: فودافون كاش\n"
    "الخزينة: ابو يوسف"
)


async def test_si0464_labeled(db):
    text = (
        "رقم العملية: SI0464\nرقم المستلم: 01093232832\n"
        "اسم الزبون: مروان الشاوش كود 1284\n"
        "القيمة قبل الخصم: 3540 ج.م\nالقيمة بعد الخصم 1%: 3505 ج.م\n"
        "السعر: 5.9\nنوع التحويل: فودافون كاش\nالخزينة: بلاس فون"
    )
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "SI0464"
    assert leg.customer_code == "1284"
    assert leg.amount == 3540
    assert leg.amount_after_discount == 3505
    assert leg.price_normalized == "5.9"
    assert leg.treasury is not None and leg.treasury.name == "بلاس فون"


async def test_si0855_before_after(db):
    text = (
        "لبستنا\nرقم العملية: SI0855\nرقم المستلم: 01278497844\n"
        "اسم الزبون: عبدالله معيتيق كود 1300\n"
        "القيمة قبل الخصم: 2020 ج.م\nالقيمة بعد الخصم 1%: 2000 ج.م\n"
        "السعر: 5.83\nنوع التحويل: فودافون كاش\nالخزينة: بلاس فون"
    )
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.reference_number == "SI0855"
    assert res.leg.amount == 2020
    assert res.leg.amount_after_discount == 2000
    assert res.leg.price_normalized == "5.83"
    assert res.leg.currency == Currency.EGP


async def test_si0827_saafi_bracket_variant(db):
    # Fix 5: «القيمة (صافي): 820» يُلتقط مبلغًا (كان يفشل → noise)
    text = (
        "لبستنا\nرقم العملية: SI0827\nرقم المستلم: 01091203309\n"
        "اسم الزبون: الهادي لخبولي كود1201\n"
        "القيمة (صافي): 820 ج.م\nالسعر: 5.77\n"
        "نوع التحويل: فودافون كاش\nالخزينة: بلاس فون"
    )
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.reference_number == "SI0827"
    assert res.leg.amount == 820
    assert res.leg.customer_code == "1201"
    assert res.leg.price_normalized == "5.77"


# ═════════════════════════════════════════════════════════════════════════════
# SI مع خصم (SI1417) — التحقّق (١): الفهم يستخرج القيمتين والخزينة
# ═════════════════════════════════════════════════════════════════════════════
async def test_si1417_discount_parses_amounts_and_treasury(db):
    res = parse_message(SI1417_TEXT, await _treas(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.is_si_format is True
    assert leg.reference_number == "SI1417"
    assert leg.customer_code == "570"
    assert leg.customer_name == "ايهاب ابو حميد"
    assert leg.amount == 20475                  # قبل الخصم
    assert leg.amount_after_discount == 20271   # بعد الخصم
    assert leg.price_normalized == "5.9"
    assert leg.currency == Currency.EGP
    assert leg.treasury is not None and leg.treasury.code == "77"   # ابو يوسف


async def test_si1417_commission_is_derivable_minus_204(db):
    # الدالة النقيّة تعطي العمولة الصحيحة (بعد − قبل = −204) — قيمة مشتقّة صالحة.
    leg = parse_message(SI1417_TEXT, await _treas(db), []).leg
    assert compute_commission(leg, None) == -204.0


# ── العمولة تُضبط للطرف المفرد (SI بخصم) وتصل خانة MONEYADO (§6.2) ────────────
async def test_si1417_discount_sets_commission(db):
    leg = parse_message(SI1417_TEXT, await _treas(db), []).leg
    assert leg.commission == -204.0
    assert leg.commission_rate == 0.0


async def test_si1417_discount_commission_written_to_field(db):
    # خانة العمولة في شاشة البيع = −204 (لا 0)، والمبلغ الأجنبي = قبل الخصم.
    leg = parse_message(SI1417_TEXT, await _treas(db), []).leg
    ops = {op.key: op.value for op in build_sell_fields(leg)}
    assert ops["foreign_amount"] == "20475"     # المبلغ الأجنبي = قبل الخصم
    assert ops["commission"] == "-204"          # الخصم يُطبَّق


# ── SI عادية (بلا «بعد الخصم») لم تتأثر: لا عمولة، لا مبلغ بعد خصم ─────────────
async def test_si_plain_no_discount_has_no_commission(db):
    # SI0827 «القيمة (صافي)» بلا سطر «بعد الخصم» → صافٍ بلا خصم.
    text = (
        "لبستنا\nرقم العملية: SI0827\nرقم المستلم: 01091203309\n"
        "اسم الزبون: الهادي لخبولي كود1201\n"
        "القيمة (صافي): 820 ج.م\nالسعر: 5.77\n"
        "نوع التحويل: فودافون كاش\nالخزينة: بلاس فون"
    )
    leg = parse_message(text, await _treas(db), []).leg
    assert leg.amount == 820
    assert leg.amount_after_discount is None
    assert leg.commission is None               # بلا خصم → بلا عمولة
    ops = {op.key: op.value for op in build_sell_fields(leg)}
    assert ops["commission"] == "0"             # تُكتب 0 (لا خصم)
