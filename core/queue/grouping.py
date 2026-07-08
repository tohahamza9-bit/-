"""
تجميع الطرفين (§7.3) — دوال نقيّة.

التجميع التلقائي: نفس الرقم الإشاري + نفس الهاتف خلال المهلة = صفقة واحدة.
الرقم الإشاري ناقص/غير مقروء → الهاتف وحده يميّز (§7.3).
"""
from __future__ import annotations

import re

from ..models import ParsedLeg


def _norm_phone(phone: str | None) -> str:
    """يُبقي الأرقام فقط (يزيل مسافات/رموز ملتصقة §3.4)."""
    if not phone:
        return ""
    return re.sub(r"\D", "", phone)


def _norm_ref(ref: str | None) -> str:
    """توحيد الرقم الإشاري: بلا مسافات، حروف كبيرة."""
    if not ref:
        return ""
    return re.sub(r"\s+", "", ref).upper()


def compute_grouping_key(leg: ParsedLeg) -> str:
    """
    مفتاح التجميع التلقائي: «رقم إشاري|هاتف».
    إن غاب الرقم الإشاري → الهاتف وحده يميّز (§7.3). إن غاب الاثنان → مفتاح فارغ
    (لا تجميع تلقائي؛ يُعتمد على الربط اليقيني بمفتاح الرسالة Reply/Edit).
    """
    ref = _norm_ref(leg.reference_number)
    phone = _norm_phone(leg.phone)
    if ref and phone:
        return f"{ref}|{phone}"
    if phone:
        return phone
    if ref:
        return ref
    return ""


def _is_discount_identity_leg(leg: ParsedLeg) -> bool:
    """رسالة الهوية (msg1) لصيغة الخصم: كود الزبون + سعر + مبلغ (قبل الخصم)، بلا خزينة.

    الخزينة تأتي في رسالة التسوية (msg2). وجود الكود هو ما يميّز صيغة الخصم عن حوالة A
    الناقصة (رسالة أولى بلا كود) — القاعدة ١ مقابل ٢ في وصف الصيغة.
    """
    return bool(
        leg.customer_code
        and leg.price_normalized
        and leg.amount is not None
        and leg.treasury is None
    )


def _is_discount_settlement_leg(leg: ParsedLeg) -> bool:
    """رسالة التسوية (msg2) لصيغة الخصم: خزينة + مبلغ (بعد الخصم)، بلا كود زبون."""
    return bool(
        not leg.customer_code
        and leg.treasury is not None
        and leg.amount is not None
    )


def discount_pair(a: ParsedLeg, b: ParsedLeg) -> tuple[ParsedLeg, ParsedLeg] | None:
    """
    هل يشكّل الطرفان صيغةَ خصم من رسالتين (Aخصم)؟ — دالة نقيّة، مستقلّة عن ترتيب الوصول.

    - رسالة الهوية (msg1): كود الزبون + اسم + سعر + مبلغ **قبل** الخصم، بلا خزينة.
    - رسالة التسوية (msg2): **نفس الرقم الإشاري** + خزينة + مبلغ **بعد** الخصم، بلا كود.
    - الربط بالرقم الإشاري لا بالقرب الزمني.

    يُرجع (identity_leg, settlement_leg) عند التطابق، وإلا None (فتبقى الصيغ الأخرى كما هي).
    """
    for identity, settlement in ((a, b), (b, a)):
        if (
            _is_discount_identity_leg(identity)
            and _is_discount_settlement_leg(settlement)
            and _norm_ref(identity.reference_number)
            and _norm_ref(identity.reference_number) == _norm_ref(settlement.reference_number)
        ):
            return identity, settlement
    return None


def is_same_deal(a: ParsedLeg, b: ParsedLeg, within_seconds: int) -> bool:
    """
    هل الطرفان لصفقة واحدة؟ (§7.3)

    - الهاتف مميّز أساسي: إن توفّر في الطرفين وجب تطابقه.
    - إن توفّر الرقم الإشاري في الطرفين وجب تطابقه؛ إن غاب في أحدهما فالهاتف يكفي.
    - إن غاب الهاتف في أحدهما → نعتمد الرقم الإشاري إن توفّر يقينًا في الطرفين.

    ملاحظة: النافذة الزمنية (within_seconds ≤ دقيقتين §7.3) تُطبَّق في الخدمة
    عبر حالة الصفقة (WAITING_SECOND_LEG) و waiting_deadline — الطرف هنا بلا ختم وقت.
    """
    pa, pb = _norm_phone(a.phone), _norm_phone(b.phone)
    ra, rb = _norm_ref(a.reference_number), _norm_ref(b.reference_number)

    if pa and pb:
        if pa != pb:
            return False
        if ra and rb and ra != rb:
            return False
        return True

    # لا هاتف في أحدهما — يُعتمد الرقم الإشاري إن توفّر في الطرفين
    if ra and rb:
        return ra == rb
    return False
