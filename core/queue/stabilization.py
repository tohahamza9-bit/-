"""
منطق انتظار الاستقرار (§7.2) — دوال نقيّة تُختبر بلا DB.

القاعدة (§7.2):
- البوت لا يحكم على الرسالة فور ظهورها؛ ينتظر استقرارها (دقيقة إلى دقيقة ونصف).
- «الحرف» (حجز مكان): كلمة قصيرة سريعة يعدّلها الموظف لاحقًا (Edit) فيضع فيها الحوالة
  → تنتظر حتى STABILIZE_MAX لالتقاط التعديل.
- حوالة كاملة واضحة (رقم إشاري/صيغة SI + مبلغ) → تُعالَج فورًا.
- بقيت ناقصة بلا بنية بعد المهلة → هدرزة → تُتجاهل بصمت.

معنى «استقرّت» هنا = «توقّف الانتظار، اتّخذ القرار الآن» (حوالة أو هدرزة يحسمه الفهم لاحقًا).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from ..constants import STABILIZE_MAX_SECONDS, STABILIZE_MIN_SECONDS
from ..logging_setup import get_logger
from ..models import RawMessage

log = get_logger(__name__)

# ── إشارات بنية الحوالة (heuristics — الفهم الدقيق مهمّة وحدة الـ parsing) ────
# رقم إشاري: A6779 / SI0464 / A01 (حرف/حرفان/ثلاثة + رقمان فأكثر — يشمل المرجع القصير)
_REF_RE = re.compile(r"\b[A-Za-z]{1,3}\s?\d{2,}\b")
# مبلغ بعملة: رقم يتبعه رمز/كلمة عملة (§3.4) — يشمل «مصري/مصرى» و«تونسي/تونسى»
_AMOUNT_CUR_RE = re.compile(
    r"\d[\d.,]*\s*(?:جنيه|ج\.?\s?م|ج|مصري|مصرى|دينار|تونسي|تونسى|د\.?\s?ت|دت)"
)
# عناوين صيغة SI الصريحة (§3.3)
_SI_LABELS = ("رقم العملية", "رقم المستلم", "القيمة", "السعر", "الخزينة", "اسم الزبون")

# «الحرف»: رسالة قصيرة قابلة للتعديل — حدّ الطول والكلمات
_HARF_MAX_CHARS = 15
_HARF_MAX_WORDS = 3


def _as_naive_utc(dt: datetime) -> datetime:
    """توحيد للمقارنة: القاعدة (mongomock/motor) قد تُرجع أوقاتًا بلا منطقة — نقارن الجميع UTC-naive."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _reference_time(raw: RawMessage) -> datetime:
    """آخر تعديل إن وُجد، وإلّا لحظة الوصول (§7.2 «الحرف ثم Edit»)."""
    return raw.edited_at or raw.received_at


# هامش تسامح لانحراف الساعة قبل إطلاق التحذير (ثوانٍ) — تأخّر بسيط طبيعي، لا نُزعج به.
_CLOCK_SKEW_TOLERANCE_SECONDS = 5


def _elapsed_seconds(raw: RawMessage, now: datetime) -> float:
    """
    الزمن المنقضي منذ آخر تعديل/وصول. 🔴 حارس انحراف الساعة (§7.2):
    `received_at` مصدره وقت خادم واتساب؛ لو ساعة الجهاز متأخّرة يصير المرجع «في المستقبل»
    فينتج elapsed سالب ⇒ الرسالة لا تستقرّ أبدًا وتُتخطّى صامتةً (تجمّد كامل).
    نحُدّ المرجع بـ min(reference, now) فلا يقلّ elapsed عن صفر، ونحذّر (لا صمت).
    """
    now_n = _as_naive_utc(now)
    ref_n = _as_naive_utc(_reference_time(raw))
    if ref_n > now_n:
        # المرجع في المستقبل: نحُدّه بـ now دائمًا (لا elapsed سالب → لا تجمّد).
        # نحذّر فقط إن تجاوز الفرق هامش التسامح (تأخّر بسيط بالثواني لا يستحقّ ضجيجًا).
        skew = (ref_n - now_n).total_seconds()
        if skew > _CLOCK_SKEW_TOLERANCE_SECONDS:
            log.warning(
                "توقيت مستقبلي للرسالة %s: المرجع=%s > now=%s (فرق %.0f ث) — "
                "غالبًا ساعة الجهاز متأخّرة عن وقت خادم واتساب؛ زامن الساعة (NTP/w32tm). "
                "أُحدّ المرجع بـ now لتفادي التجمّد.",
                raw.message_key, ref_n.isoformat(), now_n.isoformat(), skew,
            )
        ref_n = now_n
    return (now_n - ref_n).total_seconds()


def looks_like_complete_transfer(text: str) -> bool:
    """حوالة كاملة واضحة: رقم إشاري + مبلغ بعملة، أو حقول SI مُعنونة."""
    t = (text or "").strip()
    if not t:
        return False
    has_ref = bool(_REF_RE.search(t))
    has_amount = bool(_AMOUNT_CUR_RE.search(t))
    if has_ref and has_amount:
        return True
    # صيغة SI: حقلان مُعنونان صريحان على الأقل
    si_hits = sum(1 for lbl in _SI_LABELS if lbl in t)
    return si_hits >= 2


def is_short_placeholder(text: str) -> bool:
    """«الحرف» — كلمة قصيرة قابلة للتعديل (حجز مكان)، بلا بنية حوالة."""
    t = (text or "").strip()
    if not t or "\n" in t:
        return False
    if looks_like_complete_transfer(t):
        return False
    return len(t) <= _HARF_MAX_CHARS and len(t.split()) <= _HARF_MAX_WORDS


def is_stable(raw: RawMessage, now: datetime) -> bool:
    """
    هل استقرّت الرسالة ويمكن حسمها الآن؟ (§7.2)

    - حوالة كاملة واضحة → فورًا (بلا انتظار).
    - «الحرف» (قصيرة قابلة للتعديل) → تنتظر حتى STABILIZE_MAX (لالتقاط Edit).
    - غير ذلك → استقرّت إن مرّ STABILIZE_MIN على آخر تعديل/الوصول.
    """
    text = (raw.text or "").strip()
    if not text:
        # فارغة: تنتظر المهلة القصوى ثم تُحسم (غالبًا هدرزة)
        return _elapsed_seconds(raw, now) >= STABILIZE_MAX_SECONDS

    if looks_like_complete_transfer(text):
        return True

    if is_short_placeholder(text):
        return _elapsed_seconds(raw, now) >= STABILIZE_MAX_SECONDS

    return _elapsed_seconds(raw, now) >= STABILIZE_MIN_SECONDS


def should_ignore_as_noise(raw: RawMessage, now: datetime) -> bool:
    """
    هدرزة تُتجاهل بصمت: بقيت ناقصة بلا بنية حوالة بعد انتهاء المهلة (§7.2).

    لا حكم قبل انتهاء STABILIZE_MAX — قد يصل تعديل «الحرف» في أي لحظة.
    """
    if _elapsed_seconds(raw, now) < STABILIZE_MAX_SECONDS:
        return False
    if looks_like_complete_transfer(raw.text or ""):
        return False
    log.info("رسالة تُهمل كهدرزة بعد المهلة: %s", raw.message_key)
    return True
