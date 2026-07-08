"""
اختبارات وحدة الفهم (§3، §4، §5، §6، §10) — أمثلة حقيقية من المواصفات.
fixtures: `examples` (نصوص §3/§5) و`db` (خزائن افتراضية) من conftest.
"""
from __future__ import annotations

import pytest

from core.constants import Currency, OperationType, TreasuryType
from core.models import SupplierRecord
from core.parsing import detect_control, parse_message
from core.parsing.normalize import normalize_price, parse_amount


# ── مُساعدات ─────────────────────────────────────────────────────────────────
async def _treasuries(db):
    return await db.treasuries.all_active()


async def _suppliers(db):
    """يزرع الموردين المعتمدين (§5.4) ثم يُرجعهم — القائمة فارغة في seed conftest."""
    await db.suppliers.upsert(SupplierRecord(code="760", name="طه", aliases=["طه"]))
    await db.suppliers.upsert(SupplierRecord(code="900", name="مؤمن", aliases=["مؤمن", "مومن"]))
    return await db.suppliers.all_active()


# ═════════════════════════════════════════════════════════════════════════════
# §3.2 — الصيغة A (1208 فداء شاكونه)
# ═════════════════════════════════════════════════════════════════════════════
async def test_format_a_simple(examples, db):
    res = parse_message(examples["format_a_simple"], await _treasuries(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.customer_code == "1208"
    assert leg.customer_name == "فداء شاكونه"
    assert leg.price_raw == "5.84"
    assert leg.price_normalized == "5.84"          # EGP كما هو (§3.6)
    assert leg.amount == 1600.0
    assert leg.currency == Currency.EGP
    assert leg.reference_number == "A5169"
    assert leg.phone == "010954227116"
    assert leg.payment_method == "فودافون كاش"     # §3.4
    # الخزينة «بلاس» → بلاس فون (74) بيع فقط (§4.5) — خزينة حقيقية تُحلّ عاديًا
    assert leg.treasury is not None
    assert leg.treasury.code == "74"
    assert leg.treasury.name == "بلاس فون"
    assert leg.treasury.type == TreasuryType.SELL_ONLY
    assert leg.notes == "بدون خصم"                 # مؤشّر الخصم → ملاحظات لا خزينة (§3.2)
    # نوع العملية: بيع افتراضي، لا انتظار طرف ثانٍ (§5)
    assert leg.operation == OperationType.SELL
    assert leg.explicit_operation is False
    assert leg.is_supplier_counterpart is False
    assert leg.expects_pair is False


# ═════════════════════════════════════════════════════════════════════════════
# §3.3/§6 — الصيغة SI (SI0464 مروان الشاوش) + القيمة قبل/بعد الخصم
# ═════════════════════════════════════════════════════════════════════════════
async def test_format_si(examples, db):
    res = parse_message(examples["format_si"], await _treasuries(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "SI0464"
    assert leg.phone == "01093232832"
    assert leg.customer_code == "1284"
    assert leg.customer_name == "مروان الشاوش"
    assert leg.amount == 3540.0                     # القيمة قبل الخصم (§6.2)
    assert leg.amount_after_discount == 3505.0      # القيمة بعد الخصم (§6)
    assert leg.price_raw == "5.9"
    assert leg.price_normalized == "5.9"            # EGP كما هو
    assert leg.currency == Currency.EGP
    assert leg.payment_method == "فودافون كاش"
    assert leg.treasury.code == "74"                # بلاس فون
    assert leg.operation == OperationType.SELL
    assert leg.expects_pair is False


# ═════════════════════════════════════════════════════════════════════════════
# §5.5 — طرفان (A6779): البيع (زبون) + الشراء (مورد طه)
# ═════════════════════════════════════════════════════════════════════════════
async def test_two_leg_sell(examples, db):
    res = parse_message(examples["two_leg_sell"], await _treasuries(db), await _suppliers(db))
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "A6779"
    assert leg.phone == "01115233493"
    assert leg.amount == 8475.0
    assert leg.currency == Currency.EGP
    assert leg.customer_code == "53"
    assert leg.customer_name == "احمد العكاري"
    assert leg.price_raw == "5.90"
    assert leg.payment_method == "فودافون كاش"
    # زبون (ليس موردًا) → بيع، لا انتظار (الخزينة تُحسم لاحقًا §6)
    assert leg.operation == OperationType.SELL
    assert leg.is_supplier_counterpart is False
    assert leg.supplier is None
    assert leg.expects_pair is False


async def test_two_leg_buy_supplier(examples, db):
    res = parse_message(examples["two_leg_buy"], await _treasuries(db), await _suppliers(db))
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "A6779"
    assert leg.amount == 8391.0
    assert leg.customer_code == "760"
    # الطرف «طه» = مورد ⇒ شراء من مورد (§5.3)
    assert leg.operation == OperationType.BUY
    assert leg.is_supplier_counterpart is True
    assert leg.supplier is not None
    assert leg.supplier.name == "طه"
    assert leg.supplier.code == "760"
    assert leg.price_raw == "5.86"
    assert leg.expects_pair is True                 # شراء → ينتظر الطرف الأول


# ═════════════════════════════════════════════════════════════════════════════
# §5.6 — بيع فقط + خصم تونسي (A6604): وليد خزينة، السعر 35.75→0.3575
# ═════════════════════════════════════════════════════════════════════════════
async def test_tnd_sell_only(examples, db):
    res = parse_message(examples["tnd_sell_only"], await _treasuries(db), await _suppliers(db))
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "A6604"
    assert leg.customer_code == "526"
    assert leg.customer_name == "بكر همالي"
    assert leg.amount == 2000.0
    assert leg.currency == Currency.TND
    assert leg.price_raw == "35.75"
    assert leg.price_normalized == "0.3575"         # تونسي → 0.xxxx (§3.6)
    # «وليد» = خزينة بيع فقط (51) — خزينة حقيقية تُحلّ عاديًا (ليست مؤشّر خصم)
    assert leg.treasury is not None
    assert leg.treasury.code == "51"
    assert leg.treasury.name == "وليد تونس العاصمة"
    assert leg.treasury.type == TreasuryType.SELL_ONLY
    assert leg.treasury.currency == Currency.TND
    assert leg.operation == OperationType.SELL
    assert leg.is_supplier_counterpart is False
    assert leg.expects_pair is False
    assert leg.country == "العاصمة"
    assert leg.recipient_name == "محمد عبدالرحيم"
    assert leg.phone == "0925135252"


# ═════════════════════════════════════════════════════════════════════════════
# §3.5 — قواعد المبلغ (الفاصلة = فاصل آلاف يُشال) 🔴
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5.000", 5000.0),
        ("5،000", 5000.0),      # فاصلة عربية
        ("5'000", 5000.0),      # apostrophe
        ("17.400", 17400.0),
        ("50.000", 50000.0),
        ("5.802", 5802.0),
        ("1600", 1600.0),
        ("2000", 2000.0),
    ],
)
def test_amount_thousands_rule(raw, expected):
    assert parse_amount(raw) == expected


# ═════════════════════════════════════════════════════════════════════════════
# §3.6 — السعر حسب العملة 🔴
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("35.25", "0.3525"),
        ("35.7", "0.357"),
        ("33", "0.33"),
        ("0.3525", "0.3525"),   # كما هو
        ("35.75", "0.3575"),
    ],
)
def test_price_tnd_normalization(raw, expected):
    assert normalize_price(raw, Currency.TND)[1] == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("5.9", "5.9"),
        ("6.01", "6.01"),
        ("5.84", "5.84"),
    ],
)
def test_price_egp_as_is(raw, expected):
    assert normalize_price(raw, Currency.EGP)[1] == expected


# ═════════════════════════════════════════════════════════════════════════════
# §7.2 — تسليم/صرف/قبض → تجاهل صامت
# ═════════════════════════════════════════════════════════════════════════════
async def test_silent_ignore(db):
    # «تسليم» صار «خارج النطاق» (تصعيد يدوي) لا تجاهلًا صامتًا (قرار المستخدم)
    res = parse_message("تسليم 5000 لأحمد", await _treasuries(db), [])
    assert res.kind == "out_of_scope"


async def test_silent_ignore_sarf(db):
    res = parse_message("صرف نقدي", await _treasuries(db), [])
    assert res.kind == "silent_ignore"


# ═════════════════════════════════════════════════════════════════════════════
# §7.2 — هدرزة بلا بنية → noise
# ═════════════════════════════════════════════════════════════════════════════
async def test_noise(db):
    res = parse_message("صباح الخير يا شباب ازيكم", await _treasuries(db), [])
    assert res.kind == "noise"


async def test_empty_is_noise(db):
    res = parse_message("   ", await _treasuries(db), [])
    assert res.kind == "noise"


# ═════════════════════════════════════════════════════════════════════════════
# §3.2 — حوالة مرتكزها الرقم الإشاري بلا كود زبون (A06/صافي) → transfer لا noise
# ═════════════════════════════════════════════════════════════════════════════
async def test_transfer_reference_anchored_no_customer_code(db):
    # النص الحقيقي الذي كان يُصنّف «هدرزة» خطأً: مرجع A06 + مبلغ بعملة + خزينة، بلا كود زبون
    text = "A06\nفودافون\n01097298988\n69.000 مصري\nصافي"
    res = parse_message(text, await _treasuries(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "A06"          # المرساة = الرقم الإشاري
    assert leg.customer_code is None              # لا كود زبون في هذه الصيغة
    assert leg.amount == 69000.0                  # 69.000 → فاصل آلاف مُشال (§3.5)
    assert leg.currency == Currency.EGP           # «مصري» → EGP (§3.4)
    # 🔴 تصحيح (بلاغ A11–A16): الرسالة الأولى لا تُحلّ خزينة — «صافي» ملاحظة لا خزينة (§7.3)
    assert leg.treasury is None
    assert leg.notes is not None and "صافي" in leg.notes


async def test_chat_with_bare_number_stays_noise(db):
    # حارس false-positive: دردشة حقيقية (تحية + رقم بلا عملة ولا رقم إشاري) تبقى هدرزة.
    res = parse_message("صباح الخير يا شباب معايا 500 النهاردة", await _treasuries(db), [])
    assert res.kind == "noise"                    # لا مبلغ بعملة ولا مرجع → ليست حوالة


async def test_bare_amount_without_reference_stays_noise(db):
    # مبلغ بعملة لكن بلا أي مرساة (كود/مرجع) → لا يُصنّف حوالة (يمنع ابتلاع أرقام عابرة).
    res = parse_message("تعالوا ناخد 200 جنيه شاي", await _treasuries(db), [])
    assert res.kind == "noise"


# ═════════════════════════════════════════════════════════════════════════════
# §10 — الإلغاء/التعديل/التأكيد/التصحيح
# ═════════════════════════════════════════════════════════════════════════════
def test_detect_control_cancel():
    assert detect_control("إلغاء") == ("cancel", None)
    assert detect_control("الغاء الحوالة") == ("cancel", None)


def test_detect_control_edit_value():
    assert detect_control("تعديل 9000") == ("edit", 9000.0)


def test_detect_control_confirm():
    assert detect_control("تم") == ("confirm", None)


def test_detect_control_correct():
    assert detect_control("تصحيح 10500") == ("correct", 10500.0)


def test_detect_control_none():
    assert detect_control("1208 فداء شاكونه 5.84") is None


async def test_parse_message_control_cancel(db):
    res = parse_message("إلغاء", await _treasuries(db), [])
    assert res.kind == "control"
    assert res.control_action == "cancel"


async def test_parse_message_control_edit(db):
    res = parse_message("تعديل 9000", await _treasuries(db), [])
    assert res.kind == "control"
    assert res.control_action == "edit"
    assert res.control_value == 9000.0
