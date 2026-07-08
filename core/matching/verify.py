"""
بوابة الثقة + مطابقة المبلغ + تحقّق اسم الزبون — دوال نقيّة (§8.2, §11.2).

القاعدة الذهبية (§0): لو شكّيت لا تُنزّل — لكن العمليات السليمة تمشي فورًا.
البوابة «متساهلة»: تمشي طالما الكود + المبلغ مقروءان، وتعلّق فقط عند شكّ حقيقي.
"""
from __future__ import annotations

from typing import Optional

from ..logging_setup import get_logger
from ..models import ParsedLeg
from .fuzzy import names_match, normalize_ar

log = get_logger(__name__)

# فرق مقبول عند مقارنة المبالغ (مبالغ نقدية — تفاوت الفاصلة العائمة فقط)
_AMOUNT_EPSILON = 0.01


def amount_matches(central: float, room: float) -> bool:
    """
    المركزية هي المرجع دائمًا (§8.2). أي اختلاف = لا تطابق.
    مثال: 990 (مركزية) مقابل 980 (خزينة) → False.
    """
    if central is None or room is None:
        return False
    return abs(float(central) - float(room)) < _AMOUNT_EPSILON


def trust_gate(leg: ParsedLeg) -> tuple[bool, Optional[str]]:
    """
    بوابة الثقة (§8.2): (ok, reason).

    تمشي (True, None) طالما الكود + المبلغ مقروءان — يُتسامح مع خطأ إملاء الاسم.
    تعلّق (False, سبب) فقط عند:
      • مبلغ غير مقروء (None) أو غير صالح (≤ 0)،
      • تعارض صريح (المبلغ بعد الخصم أكبر من قبله — تناقض)،
      • كود ناقص + اسم ملتبس (فارغ/غير مقروء).
    """
    # 1) المبلغ غير مقروء → تعليق (§8.2)
    if leg.amount is None:
        return False, "مبلغ غير مقروء"
    if leg.amount <= 0:
        return False, "مبلغ غير صالح (≤ 0)"

    # 2) تعارض صريح في المبلغ: القيمة بعد الخصم أكبر من قبله
    if leg.amount_after_discount is not None and leg.amount_after_discount > leg.amount + _AMOUNT_EPSILON:
        return False, "تعارض صريح: المبلغ بعد الخصم أكبر من قبله"

    # 3) كود ناقص + اسم ملتبس → تعليق (الكود هو المرساة §1.1)
    if not leg.customer_code:
        if not normalize_ar(leg.customer_name or ""):
            return False, "كود ناقص واسم ملتبس"

    # الكود + المبلغ مقروءان → يمشي (يُتسامح مع خطأ إملاء الاسم §8.2)
    return True, None


def verify_customer_name(
    entered_code: str,
    displayed_name: str,
    expected_name: Optional[str],
) -> tuple[bool, str]:
    """
    §11.2: بعد إدخال الكود يُظهر MONEYADO اسم الزبون؛ يُتحقّق أنه معقول.

    - الاسم الظاهر فارغ/غريب تمامًا → (False, سبب): لا يكمل.
    - لا يوجد اسم متوقّع للمقارنة → (True): يُعتمد الكود (الكود أضمن §11.2).
    - تطابق تقريبي مع المتوقّع → (True).
    - مختلف تمامًا (ليس خطأ إملاء §8.2) → (False): لا يكمل.
    """
    shown = normalize_ar(displayed_name or "")
    if not shown:
        log.warning("تحقّق اسم الزبون: الكود %s لم يُظهر اسمًا — لا يكمل (§11.2)", entered_code)
        return False, "الاسم الظاهر فارغ — لا يكمل"

    # لا اسم متوقّع في الرسالة للمقارنة → الكود هو المرجع (§11.2)
    if not normalize_ar(expected_name or ""):
        return True, "لا اسم متوقّع للمقارنة — اعتُمد الكود"

    if names_match(displayed_name, expected_name):
        return True, "الاسم الظاهر مطابق تقريبًا"

    log.warning(
        "تحقّق اسم الزبون: الكود %s أظهر «%s» ويختلف تمامًا عن المتوقّع «%s» — لا يكمل (§8.2)",
        entered_code, displayed_name, expected_name,
    )
    return False, "الاسم الظاهر مختلف تمامًا عن المتوقّع — لا يكمل"
