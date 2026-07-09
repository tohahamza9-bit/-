"""
اختبارات صيغة SI المعنونة (§3.3) — بما فيها متغيّر «القيمة (صافي)» (Fix 5).
المصدر: pr.md المصدر ٤ (د) + الصور (٧ SI0855، ٩ SI0827).
"""
from __future__ import annotations

import pytest

from core.constants import Currency, TreasuryType
from core.models import SupplierRecord
from core.parsing import parse_message
from core.queue.commission import compute_commission
from core.writers.moneyado.fields import build_sell_fields


async def _treas(db):
    return await db.treasuries.all_active()


async def _suppliers(db):
    await db.suppliers.upsert(SupplierRecord(code="760", name="طه", aliases=["طه"]))
    return await db.suppliers.all_active()


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


async def test_si_saafi_treasury_is_ignored(db):
    """«الخزينة: صافي» ليست خزينة وجهة (تعني «المبلغ صافي») → treasury=None (تُعامَل كملاحظة §6.1)."""
    text = (
        "رقم العملية: SI1901\nرقم المستلم: 01093232832\n"
        "اسم الزبون: مروان الشاوش كود 1284\n"
        "القيمة (صافي): 3540 ج.م\nالسعر: 5.9\nنوع التحويل: فودافون كاش\nالخزينة: صافي"
    )
    leg = parse_message(text, await _treas(db), []).leg
    assert leg is not None
    assert leg.treasury is None                     # «صافي» ليست خزينة
    assert leg.customer_code == "1284"              # باقي الحقول سليمة


@pytest.mark.parametrize("text, code, name, price, treasury_code", [
    # أ) كود+اسم+سعر في سطر، ثم وسيلة دفع (تُتجاهَل)، ثم خزينة
    ("750 ايهاب ابو حميد 5.70\nفودافون كاش\nابو يوسف", "750", "ايهاب ابو حميد", "5.70", "77"),
    # ب) الخزينة أولًا ثم كود+اسم+سعر
    ("ابو يوسف\n750 ايهاب 5.70", "750", "ايهاب", "5.70", "77"),
    # ج) السعر أولًا والكود آخر السطر (يفشل مع التحليل سطرًا-بسطر)
    ("5.70 ايهاب ابو حميد 750\nبلاس فون", "750", "ايهاب ابو حميد", "5.70", "74"),
    # د) كود+اسم، عملة تونس، سعر بسطر مستقلّ، خزينة تونسية
    ("750 ايهاب\nتونس\n35.25\nوليد", "750", "ايهاب", "35.25", "51"),
])
async def test_completion_fragment_fishing(db, text, code, name, price, treasury_code):
    """الرسالة الثانية بمنهج pattern-fishing: تُستخرج code/name/price/treasury مهما اختلف الترتيب."""
    from core.parsing import parse_completion_fragment
    leg = parse_completion_fragment(text, await _treas(db), [])
    assert leg.customer_code == code
    assert leg.customer_name == name
    assert leg.price_raw == price
    assert leg.treasury is not None and leg.treasury.code == treasury_code


@pytest.mark.parametrize("val", [
    "793 حميد بن غارات",          # أ: الكود في البداية بلا «كود»
    "حميد بن غارات كود.793",      # ب: الكود في النهاية مع «كود.»
    "كود.793 حميد بن غارات",      # ج: الكود في البداية مع «كود.»
])
def test_extract_code_name_all_positions(val):
    """يستخرج الكود=793 والاسم='حميد بن غارات' في المواضع الثلاثة (بداية/نهاية، مع «كود.» أو بلا)."""
    from core.parsing.parser import _extract_code_name
    code, name = _extract_code_name(val)
    assert code == "793"
    assert name == "حميد بن غارات"


async def test_si_customer_code_merged_with_dot(db):
    """صيغة جديدة: كود مدموج بسطر الاسم بنقطة «حميد بن غارات كود.793» → كود=793، الاسم بلا الكود."""
    text = (
        "رقم العملية: SI1900\nرقم المستلم: 01093232832\n"
        "اسم الزبون: حميد بن غارات كود.793\n"
        "القيمة قبل الخصم: 3540 ج.م\nالقيمة بعد الخصم 1%: 3505 ج.م\n"
        "السعر: 5.9\nنوع التحويل: فودافون كاش\nالخزينة: بلاس فون"
    )
    leg = parse_message(text, await _treas(db), []).leg
    assert leg.customer_code == "793"
    assert leg.customer_name == "حميد بن غارات"


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


# ═════════════════════════════════════════════════════════════════════════════
# SI مع مورد (بيع + شراء §5/§6): «المورد: طه 5.72» بدل «الخزينة:»
# ═════════════════════════════════════════════════════════════════════════════
SI1417_SUPPLIER_TEXT = (
    "رقم العملية: SI1417\n"
    "رقم المستلم: 01037354643\n"
    "اسم الزبون: 570 ايهاب ابو حميد\n"
    "القيمة قبل الخصم: 20475 ج.م\n"
    "القيمة بعد الخصم 1%: 20271 ج.م\n"
    "السعر: 5.9\n"
    "نوع التحويل: فودافون كاش\n"
    "المورد: طه 5.72"
)


async def test_si_supplier_line_captures_name_and_rate(db):
    # (١) «المورد: طه 5.72» → اسم المورد وسعره يُلتقطان
    leg = parse_message(SI1417_SUPPLIER_TEXT, await _treas(db), await _suppliers(db)).leg
    assert leg.supplier is not None
    assert leg.supplier.name == "طه"
    assert leg.supplier.code == "760"           # حُلّ من القائمة البيضاء
    assert leg.supplier_price_raw == "5.72"


async def test_si_supplier_default_sell_and_buy_treasury(db):
    # (٢+٣) «المورد:» بلا «الخزينة:» → خزينة sell_and_buy افتراضية (خصم 1% لوجود «بعد الخصم»)
    leg = parse_message(SI1417_SUPPLIER_TEXT, await _treas(db), await _suppliers(db)).leg
    assert leg.is_si_format is True
    assert leg.treasury is not None
    assert leg.treasury.type == TreasuryType.SELL_AND_BUY
    assert leg.treasury.code == "72"            # «خصم 1%» (الافتراضية لـ SI مع مورد)
    # الزبون يبقى طرف بيع (لا يُحوَّل لشراء بسبب المورد)
    from core.constants import OperationType
    assert leg.operation == OperationType.SELL


async def test_si_supplier_keeps_customer_amounts_and_commission(db):
    # القيمتان والعمولة كما في SI بخصم عادية (المورد لا يغيّرهما)
    leg = parse_message(SI1417_SUPPLIER_TEXT, await _treas(db), await _suppliers(db)).leg
    assert leg.customer_code == "570"
    assert leg.customer_name == "ايهاب ابو حميد"
    assert leg.amount == 20475
    assert leg.amount_after_discount == 20271
    assert leg.commission == -204.0
    assert leg.currency == Currency.EGP


async def test_si_supplier_no_discount_defaults_saafi(db):
    # بلا سطر «بعد الخصم» + مورد → خزينة sell_and_buy الافتراضية «صافي» (لا خصم)
    text = (
        "رقم العملية: SI1500\nرقم المستلم: 01037354643\n"
        "اسم الزبون: 570 ايهاب ابو حميد\n"
        "القيمة (صافي): 20000 ج.م\nالسعر: 5.9\n"
        "نوع التحويل: فودافون كاش\nالمورد: طه 5.72"
    )
    leg = parse_message(text, await _treas(db), await _suppliers(db)).leg
    assert leg.supplier is not None and leg.supplier.name == "طه"
    assert leg.treasury is not None
    assert leg.treasury.type == TreasuryType.SELL_AND_BUY
    assert leg.treasury.name == "صافي"
    assert leg.amount_after_discount is None


async def test_si_supplier_explicit_treasury_takes_precedence(db):
    # (٤) «الخزينة:» مذكورة أيضًا → تُقرأ كالمعتاد ولا تُستبدَل بالافتراضية؛ المورد يبقى ملتقَطًا
    text = SI1417_SUPPLIER_TEXT + "\nالخزينة: ابو يوسف"
    leg = parse_message(text, await _treas(db), await _suppliers(db)).leg
    assert leg.treasury is not None and leg.treasury.code == "77"   # ابو يوسف (لا الافتراضية)
    assert leg.supplier is not None and leg.supplier.name == "طه"
    assert leg.supplier_price_raw == "5.72"


async def test_si_supplier_unresolved_name_kept(db):
    # مورد خارج القائمة البيضاء → يُحتفظ بالاسم (code=None) بلا فشل
    text = SI1417_SUPPLIER_TEXT.replace("المورد: طه 5.72", "المورد: مجهول 5.72")
    leg = parse_message(text, await _treas(db), await _suppliers(db)).leg
    assert leg.supplier is not None
    assert leg.supplier.name == "مجهول" and leg.supplier.code is None
    assert leg.supplier_price_raw == "5.72"


async def test_si_plain_unaffected_by_supplier_feature(db):
    # SI بخصم عادية (بـ«الخزينة:» بلا «المورد:») لم تتأثر: بلا مورد، خزينة كما هي
    leg = parse_message(SI1417_TEXT, await _treas(db), await _suppliers(db)).leg
    assert leg.supplier is None
    assert leg.supplier_price_raw is None
    assert leg.treasury is not None and leg.treasury.code == "77"   # ابو يوسف كما كانت
