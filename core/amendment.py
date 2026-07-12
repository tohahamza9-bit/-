"""
ميزة التعديل عبر Reply «تعديل X» — منطق نقيّ معزول (موازٍ لـ cancellation.py).

الموظف يردّ «تعديل 9000» على رسالة الحوالة الأصلية؛ X = المبلغ الجديد المطلوب (لا الفرق).
معزولة عن مسارات المعالجة القائمة: يعترضها process_inbox قبل _ingest.

- detect_amendment: هل الرسالة أمر تعديل؟ + المبلغ الجديد + السبب الإضافيّ.
- amendment_ratio / floor_commission: نسبة خصم الحوالة **الأصلية** (لا نسبة آخر تعديل) + FLOOR.
- build_amendment_jobs: قيد التعديل (شراء/بيع بمقدار الفرق) على نفس خزينة البيع + ملاحظة.
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Optional

from .constants import AMENDMENT_KEYWORDS, OperationType
from .models import Deal, WriteJob
from .parsing.normalize import normalize_ar, parse_amount

_NORM_AMEND = {normalize_ar(k) for k in AMENDMENT_KEYWORDS}
_NUM_RE = re.compile(r"\d[\d.,ّ'،\s]*\d|\d")


def detect_amendment(text: Optional[str]) -> Optional[dict]:
    """هل الرسالة أمر تعديل «تعديل X»؟ يُرجع {'amount': X|None, 'reason': str} إن كانت تعديلًا،
    وإلّا None. «تعديل 9000» → amount=9000؛ «تعديل» بلا رقم → amount=None (🔴 لاحقًا في المعالجة).
    المطابقة على **كلمة كاملة** (تعديل/تعدل) بعد التطبيع — مستقلّة عن detect_control/§10."""
    if not text or not text.strip():
        return None
    norm_words = normalize_ar(text).split()
    if not any(w in _NORM_AMEND for w in norm_words):
        return None
    m = _NUM_RE.search(text)
    amount = parse_amount(m.group()) if m else None
    # السبب = بقيّة الكلمات (بلا كلمة التعديل وبلا الرقم) من النصّ الخام
    num_tokens = set((m.group().split() if m else []))
    reason_words = [w for i, w in enumerate(text.split())
                    if (i >= len(norm_words) or norm_words[i] not in _NORM_AMEND)
                    and w not in num_tokens and not _NUM_RE.fullmatch(w.strip())]
    return {"amount": amount, "reason": " ".join(reason_words).strip()}


def amendment_ratio(deal: Deal) -> float:
    """نسبة خصم الحوالة **الأصلية** = |العمولة الأصلية| ÷ المبلغ الأصلي (لا نسبة آخر تعديل §5).
    الأصلي = amendments[0].old_* إن وُجدت تعديلات، وإلّا القيم الحاليّة (= الأصلية قبل أول تعديل)."""
    if deal.amendments:
        orig_amt = deal.amendments[0].get("old_net")
        orig_comm = deal.amendments[0].get("old_commission")
    else:
        leg = deal.sell_leg or deal.buy_leg
        orig_amt = leg.amount if leg else None
        orig_comm = leg.commission if leg else None
    if not orig_amt or not orig_comm:
        return 0.0
    return abs(orig_comm) / abs(orig_amt)


def floor_commission(new_net: float, ratio: float) -> float:
    """العمولة الجديدة = FLOOR(X × نسبة الخصم) — تُقرَّب لأسفل دائمًا (§4). مقدار موجب."""
    return float(math.floor(new_net * ratio))


def build_amendment_jobs(deal: Deal, current_net: float, new_net: float,
                         new_commission: Optional[float], note: str,
                         now: datetime) -> list[WriteJob]:
    """قيد التعديل من طرف البيع (§6): الفرق = current_net − new_net على **نفس خزينة البيع**.

    - تخفيض (diff>0) → **شراء** عكسيّ بمقدار الفرق (is_reversal=True).
    - زيادة   (diff<0) → **بيع** إضافيّ بمقدار |الفرق| (نفس الاتجاه، ليس عكسيًّا).
    - لو فيها خصم → خانة العمولة = العمولة الجديدة (FLOOR). ملاحظات = «تعديل {ref}: old ← new»."""
    leg = deal.sell_leg or deal.buy_leg
    if leg is None:
        return []
    diff = round(current_net - new_net, 2)
    if abs(diff) < 1e-9:
        return []                                    # لا فرق → لا قيد
    reduce = diff > 0
    comm = None
    if new_commission is not None and leg.commission:
        # خانة العمولة = **فرق** العمولة (|old| − new)، لا العمولة الجديدة الكاملة — فيُعكَس جزء
        # الخصم المقابل للفرق فقط، فيبقى الصافي = X بعمولته الجديدة.
        #   مثال: أصلي 10100 (خصم 101) → تعديل 7070 (خصم جديد 70) → فرق العمولة = 101−70 = 31.
        # 🔴 الإشارة: شاشة «شراء عملة» (نقصان) → **موجب دائمًا** (قاعدة السالب خاصة بالبيع فقط)؛
        #    شاشة «بيع عملة» (زيادة) → سالب (كإدخال الخصم بالبيع).
        diff_comm = abs(abs(leg.commission) - new_commission)   # مقدار الفرق (موجب)
        comm = diff_comm if reduce else -diff_comm
    rev = leg.model_copy(update={
        "operation": OperationType.BUY if reduce else OperationType.SELL,
        "amount": abs(diff),
        "recipient_name": note,                      # خانة الملاحظات (§11.1-12)
        "commission": comm,
        "commission_rate": 0.0,
        "amount_after_discount": None,
    })
    return [WriteJob(
        job_id=f"{deal.deal_id}-amend-{len(deal.amendments)}", deal_id=deal.deal_id,
        operation=rev.operation, leg=rev, order_index=0,
        is_reversal=reduce, max_attempts=1, created_at=now,
    )]
