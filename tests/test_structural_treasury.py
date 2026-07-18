"""
X1257 — الموضع البنيويّ يحدّد النوع: اسم المستلم (بعد «تسليم») لا يُطابَق خزينةً أبدًا حتى لو كان
لقبَ خزينة مسجّلة. الخزينة تُحلّ من موضعها البنيويّ فقط؛ الموضع الفارغ/الملتبس → لا خزينة (تصعيد).
"""
from __future__ import annotations

from core.constants import TreasuryType
from core.models import TreasuryRecord
from core.parsing import parse_message

# خزينتان يتصادم لقب إحداهما مع اسم مستلم شائع («محمد»)، والأخرى اسم مستلم شائع أيضًا («عمر»).
_TRS = [
    TreasuryRecord(name="محمد حمامات", code="79", type=TreasuryType.SELL_ONLY, aliases=["محمد"]),
    TreasuryRecord(name="عمر العاصمة", code="58", type=TreasuryType.SELL_ONLY, aliases=["عمر", "عمز"]),
]


def _leg(text):
    return parse_message(text, _TRS, []).leg


def test_recipient_after_deliver_excluded_treasury_taken_from_position():
    """«تسليم محمد … عمر» → «محمد» مستلم (لا خزينة رغم لقبه)، و«عمر» (الموضع البنيويّ) هي الخزينة."""
    leg = _leg("X1301\nارجو تسليم\nمحمد\n0921010929\nالقيمة 1000 دت\n149 انيس 35\nعمر")
    assert leg.recipient_name == "محمد"
    assert leg.treasury is not None and leg.treasury.name == "عمر العاصمة"   # code 58


def test_x1257_actual_no_explicit_treasury_gives_none():
    """(X1257 الحقيقيّة) لا خزينة صريحة → treasury=None (تصعيد لسؤال)، **لا** «محمد حمامات» الخاطئة."""
    leg = _leg("X1257\nارجو تسليم\nمحمد \n0921010929\n\nالقيمة 1.010 دت\n\n\nعاصمه\n\n149 انيس النفاتي 35")
    assert leg.recipient_name == "محمد"
    assert leg.treasury is None                                             # لم يعد يُخمّن الخزينة الخطأ


def test_deliver_amount_line_not_a_recipient_marker():
    """«تسليم 6070 ج.م» (تسليم مبلغ لا مستلم) → المقطع التالي «فودافون» يبقى قناة لا مستلمًا."""
    leg = _leg("A56\n01123325384\nتسليم 6070 ج.م\nفودافون")
    assert leg.recipient_name != "فودافون"                                  # لم يُبتلَع قناةً


def test_bare_city_not_matched_as_treasury():
    """مدينة وحدها («حمامات») لا تصير خزينة «محمد حمامات» — لا حلّ جريء على موضع غير خزينة."""
    leg = _leg("X1302\n0921010929\nالقيمة 500 دت\n149 زبون\nحمامات")
    assert leg.treasury is None


def test_recipient_name_alias_still_recipient_even_with_treasury_present():
    """الموضع يحسم: «محمد» بعد «تسليم» = مستلم دائمًا، حتى مع وجود «عمر» خزينةً — لا التباس نوع."""
    leg = _leg("X1303\nارجو تسليم\nمحمد\n0921010929\nالقيمة 200 دت\n149 انيس\nعمر")
    assert leg.recipient_name == "محمد" and leg.treasury.code == "58"
