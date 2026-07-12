"""
ميزة الإلغاء عبر Reply — منطق نقيّ معزول (§10 نسخة نهائية).

معزولة تمامًا عن مسارات المعالجة القائمة (try_absorb_*, absorb_fragment, try_group,
discount_pair, Sender Slot, FIFO): يعترضها process_inbox قبل _ingest فلا تمسّ أيّها.

- detect_cancellation: هل الرسالة أمر إلغاء؟ (كلمة إلغاء ككلمة كاملة) + استخراج السبب الإضافيّ.
- within_cancellation_window: عمر الصفقة (من created_at) بتوقيت ليبيا (UTC+2) ≤ 96 ساعة؟
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .constants import (
    CANCELLATION_KEYWORDS,
    CANCELLATION_WINDOW_HOURS,
    LIBYA_UTC_OFFSET_HOURS,
    OperationType,
)
from .models import Deal, WriteJob
from .parsing.normalize import normalize_ar

_LIBYA_TZ = timezone(timedelta(hours=LIBYA_UTC_OFFSET_HOURS))
_NORM_CANCEL = {normalize_ar(k) for k in CANCELLATION_KEYWORDS}


def _to_libya(dt: datetime) -> datetime:
    """يحوّل أي وقت (aware أو naive-يُفترَض UTC) إلى توقيت ليبيا (UTC+2)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_LIBYA_TZ)


def detect_cancellation(text: Optional[str]) -> Optional[str]:
    """هل الرسالة أمر إلغاء؟ يُرجع السبب الإضافيّ (نصّ بعد كلمة الإلغاء، قد يكون "") إن كانت
    إلغاءً، وإلّا None. المطابقة على **كلمة كاملة** بعد التطبيع (لا داخل كلمة) — كـ detect_control.

    عزل صريح: لا يستدعي ولا يعدّل detect_control/§10؛ مجموعة كلمات مستقلّة (CANCELLATION_KEYWORDS)."""
    if not text or not text.strip():
        return None
    norm_words = normalize_ar(text).split()
    hit_idx = next((i for i, w in enumerate(norm_words) if w in _NORM_CANCEL), None)
    if hit_idx is None:
        return None
    # السبب الإضافيّ = بقيّة الكلمات الأصلية (بلا كلمة الإلغاء) — نُعيد من النصّ الخام محافظةً عليه.
    raw_words = text.split()
    reason_words = [w for i, w in enumerate(raw_words)
                    if i >= len(norm_words) or norm_words[i] not in _NORM_CANCEL]
    return " ".join(reason_words).strip()


def within_cancellation_window(created_at: datetime, now: datetime,
                               hours: int = CANCELLATION_WINDOW_HOURS) -> bool:
    """عمر الصفقة (now − created_at) بتوقيت ليبيا (UTC+2) ≤ hours (96 = 4 أيام)؟

    المدّة ثابتة عبر المناطق الزمنية، لكن نُصرّح بالتحويل لليبيا التزامًا بالمواصفة."""
    age = _to_libya(now) - _to_libya(created_at)
    return age <= timedelta(hours=hours)


def build_cancellation_jobs(deal: Deal, now: datetime, ref: str,
                            note: Optional[str] = None) -> list[WriteJob]:
    """يبني القيود العكسية للإلغاء من **أطراف الصفقة** (لا من الدفتر) — معزول عن build_reversal
    (§10 القائم) الذي يبقى كما هو. القواعد (§4 من المواصفة):

    - بيع فقط → شراء عكسيّ: نفس الكود/المبلغ/السعر/الخزينة، ملاحظات «إلغاء {ref}».
    - بيع + خصم → شراء بالمبلغ **قبل الخصم** (leg.amount) والخصم **موجب** (abs) بلا سالب.
    - بيع + شراء → شراء (ببيانات البيع) order=0 ثم بيع (ببيانات الشراء) order=1.
    يعكس الصافي/العمولة **الحاليّين** (المحفوظين حيّاً في أطراف الصفقة بعد أي تعديلات §3).
    note: ملاحظة مخصّصة (تذكر التعديل السابق §6) — الافتراض «إلغاء {ref}».
    كلّها is_reversal=True (لا تُحسب «نُزّلت» في الحارس §9) بمحاولة واحدة (كالقيود العكسية)."""
    note = note or f"إلغاء {ref}"
    jobs: list[WriteJob] = []
    order = 0
    if deal.sell_leg is not None:
        disc = deal.sell_leg.commission
        rev = deal.sell_leg.model_copy(update={
            "operation": OperationType.BUY,
            "recipient_name": note,                       # خانة الملاحظات (§11.1-12)
            "commission": abs(disc) if disc else deal.sell_leg.commission,  # الخصم موجب (بلا سالب)
            "commission_rate": 0.0,
            "amount_after_discount": None,                # المبلغ قبل الخصم = leg.amount
        })
        jobs.append(WriteJob(
            job_id=f"{deal.deal_id}-cancel-{order}", deal_id=deal.deal_id,
            operation=OperationType.BUY, leg=rev, order_index=order,
            is_reversal=True, max_attempts=1, created_at=now,
        ))
        order += 1
    if deal.buy_leg is not None:
        rev = deal.buy_leg.model_copy(update={
            "operation": OperationType.SELL,
            "recipient_name": note,
        })
        jobs.append(WriteJob(
            job_id=f"{deal.deal_id}-cancel-{order}", deal_id=deal.deal_id,
            operation=OperationType.SELL, leg=rev, order_index=order,
            is_reversal=True, max_attempts=1, created_at=now,
        ))
    return jobs
