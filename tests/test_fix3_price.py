"""
إصلاح ٣ (§3): استخراج/فحص السعر — أعطال إنتاج حقيقية.

٣أ: مبلغ + مؤشّر خصم على نفس السطر («حول 13100 ج م … بدون خصم» — A9055) كان يُبتلَع بصمت،
    فيختفي المبلغ وتُهمَل الحوالة كاملة. الآن يُستخرج المبلغ ويُسجَّل مؤشّر الخصم كملاحظة.
٣ب: سعر ملتصق بالاسم بلا مسافة («603 بن ناصر5.98» — A9063/A9035) كان price=None. الآن يُفصَل.
٣ج: فحص السعر في trust_gate مغطّى في test_matching.py (طرف بيع الزبون بلا سعر → رفض 🔴).
"""
from __future__ import annotations

from core.parsing.parser import parse_message


def _leg(text):
    return parse_message(text, [], []).leg


# ═════════════════════════════════════════════════════════════════════════════
# ٣أ — المبلغ لا يضيع حين يشارك السطر مؤشّرَ خصم
# ═════════════════════════════════════════════════════════════════════════════
def test_amount_extracted_with_discount_word_same_line():
    """A9055: «حول 13100 ج م فودافون بدون خصم» → المبلغ 13100 يُستخرج (لا هدرزة صامتة)."""
    leg = _leg("A9055\nحول 13100 ج م فودافون بدون خصم\n\n01022355604")
    assert leg is not None and leg.amount == 13100.0

def test_pure_discount_indicator_not_amount():
    """مؤشّر خصم بحت («بدون خصم» بلا مبلغ) لا يُلتقَط مبلغًا ولا اسم مستلم."""
    leg = _leg("A9071\nبدون خصم\n\n01022355604")
    assert leg is None or leg.amount is None

def test_safi_with_amount_still_works():
    """«5000 ج م صافي» على سطر واحد → المبلغ 5000 (بلا انحدار)."""
    leg = _leg("A9070\n5000 ج م صافي\n\n01022355604")
    assert leg is not None and leg.amount == 5000.0


# ═════════════════════════════════════════════════════════════════════════════
# ٣ب — السعر الملتصق بالاسم بلا مسافة
# ═════════════════════════════════════════════════════════════════════════════
def test_price_glued_to_name_extracted():
    """A9063: «603 بن ناصر5.98» → كود 603 + اسم «بن ناصر» + سعر 5.98 (لا price=None)."""
    leg = _leg("A9063\n01205807643\n60500 ج\n603 بن ناصر5.98")
    assert leg is not None
    assert leg.customer_code == "603" and leg.customer_name == "بن ناصر"
    assert leg.price_raw == "5.98"

def test_price_spaced_still_works():
    """الفراغ الطبيعي «603 بن ناصر 5.98» يبقى سليمًا."""
    leg = _leg("A9063\n01205807643\n60500 ج\n603 بن ناصر 5.98")
    assert leg.customer_code == "603" and leg.customer_name == "بن ناصر" and leg.price_raw == "5.98"

def test_name_without_price_stays_none():
    """اسم بلا سعر لا يُخترَع له سعر («603 بن ناصر» → price=None)."""
    leg = _leg("A9072\n01205807643\n60500 ج\n603 بن ناصر")
    assert leg.customer_code == "603" and leg.customer_name == "بن ناصر" and leg.price_raw is None

def test_arabic_comma_price_glued():
    """الفاصلة العربية في سعر ملتصق («ناصر5،98») → تُطبَّع عشريًّا 5.98."""
    leg = _leg("A9073\n01205807643\n60500 ج\n603 بن ناصر5،98")
    assert leg.customer_name == "بن ناصر" and leg.price_raw == "5.98"
