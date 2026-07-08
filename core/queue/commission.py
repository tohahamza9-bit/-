"""
الخصم والعمولة (§6) — دوال نقيّة.

القواعد (§6.2):
- المبلغ الأجنبي = المبلغ قبل الخصم.
- نسبة العمولة = 0 دائمًا (على الطرف نفسه).
- العمولة = الفرق بالسالب (MONEYADO يقبل السالب ✅).
"""
from __future__ import annotations

from typing import Optional

from ..models import ParsedLeg


def compute_commission(
    sell_leg: Optional[ParsedLeg], buy_leg: Optional[ParsedLeg]
) -> Optional[float]:
    """
    العمولة = الفرق بالسالب (§6.2).

    - صيغة SI: القيمة بعد الخصم صريحة على طرف البيع
      → العمولة = amount_after_discount − amount (مثال: 3505 − 3540 = −35).
    - صيغة A: الفرق بين مبلغَي الطرفين
      → العمولة = مبلغ الشراء − مبلغ البيع (مثال: 990 − 1000 = −10).
    - بيع فقط بلا طرف ثانٍ ولا قيمة بعد خصم → لا عمولة (None).
    """
    if sell_leg is not None:
        # صيغة SI: الفرق مكتوب صريحًا (§6.2)
        if sell_leg.amount_after_discount is not None and sell_leg.amount is not None:
            return round(sell_leg.amount_after_discount - sell_leg.amount, 2)
        # صيغة A: الفرق المحسوب بين الرسالتين (§6.2) — يلتقط خطأ الجنيه/الجنيهين
        if buy_leg is not None and sell_leg.amount is not None and buy_leg.amount is not None:
            return round(buy_leg.amount - sell_leg.amount, 2)
    return None


def resolve_two_leg_treasury(
    sell_leg: Optional[ParsedLeg], buy_leg: Optional[ParsedLeg], has_discount: bool
) -> str:
    """
    خزينة صفقة الطرفين (§6.1):
    - وجود خصم (فودافون/إنستاباي — جنيه مصري) → «خصم 1%» (الطرفان في نفس الخزينة، الفرق ربح).
    - بدون خصم → «صافي».
    """
    return "خصم 1%" if has_discount else "صافي"
