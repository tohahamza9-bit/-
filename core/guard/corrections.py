"""
الإلغاء والتعديل والتصحيح (§10).

كلها تَرِد عبر Reply على رسالة الحوالة الأصلية → الربط بمفتاح الرسالة (يقين 100%).

القاعدة المحاسبية الحاكمة (§10):
- **قيود إلحاقية فقط (Append-only)** — البوت لا يعدّل قيدًا منزّلًا أبدًا (حفظ الأثر).
- عكس البيع = شراء، وعكس الشراء = بيع (متناظر). نفس الخزينة دائمًا (تصفير في مكانه).
- القيمة **تُحسب من دفتر البوت** (ما نزّله فعلًا) — لا من رسالة التصحيح وحدها.

| الحالة  | الفعل                                              |
|---------|----------------------------------------------------|
| إلغاء   | عكس **بكامل قيمة** كل طرف منزّل، نفس الخزينة (تصفير) |
| تعديل   | عكس **بالفرق فقط** على الطرف الرئيسي (البيع)         |
| تصحيح   | قيد **بالفرق** (زيادة نفس الاتجاه / نقص عكسي)        |

حالات لا يبنيها هذا المُنشئ (تُعالَج خارجيًا: تنبيه + مسؤول):
- تصحيح على «ملغاة» → البوت لا يتصرّف.
- خارج نافذة 15 يومًا (`is_out_of_active_window`) → نادر → مسؤول.
- إعادة إرسال بعد إلغاء برقم إشاري جديد → حوالة جديدة مستقلة (ليست تصحيحًا).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..constants import ACTIVE_WINDOW_DAYS, OperationType
from ..logging_setup import get_logger
from ..models import Deal, LedgerEntry, ParsedLeg, TreasuryRef, WriteJob
from .idempotency import _OPPOSITE, _amounts_equal

log = get_logger(__name__)

_NDIGITS = 2  # تقريب فروق المبالغ (تفادي خطأ float)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _opposite(op: OperationType) -> OperationType:
    return _OPPOSITE[op]


def _match_leg(deal: Deal, entry: LedgerEntry) -> Optional[ParsedLeg]:
    """الطرف الأصلي (ParsedLeg الكامل) المقابل لقيد الدفتر — للحصول على الخزينة الكاملة."""
    if entry.operation == OperationType.SELL and deal.sell_leg is not None:
        return deal.sell_leg
    if entry.operation == OperationType.BUY and deal.buy_leg is not None:
        return deal.buy_leg
    return deal.sell_leg or deal.buy_leg


def _reversal_leg(
    orig_leg: Optional[ParsedLeg], entry: LedgerEntry, amount: float, *, opposite: bool
) -> ParsedLeg:
    """
    يبني طرفًا للقيد الإلحاقي بنفس الخزينة والزبون والعملة.
    - opposite=True  → عكس العملية (إلغاء/تخفيض): بيع↔شراء.
    - opposite=False → نفس العملية (زيادة تصحيحية).
    العمولة/القيمة-بعد-الخصم تُصفَّر (قيد مبلغ صافٍ إلحاقي).
    """
    op = _opposite(entry.operation) if opposite else entry.operation

    if orig_leg is not None:
        # نحفظ بنية الطرف الأصلي (الخزينة الكاملة، الزبون، الهاتف، الرقم الإشاري)
        return orig_leg.model_copy(
            update={
                "operation": op,
                "amount": amount,
                "commission": None,
                "commission_rate": 0.0,
                "amount_after_discount": None,
            }
        )

    # احتياط: لا طرف أصلي — نبني الحد الأدنى من قيد الدفتر (نفس الخزينة بالكود)
    treasury = None
    if entry.treasury_code is not None:
        treasury = TreasuryRef(code=entry.treasury_code, name=entry.treasury_code, type="sell_only")
    log.warning(
        "corrections: بناء طرف عكسي من الدفتر بلا ParsedLeg أصلي (deal=%s) — بنية ناقصة",
        entry.deal_id,
    )
    return ParsedLeg(
        operation=op,
        customer_code=entry.customer_code,
        amount=amount,
        currency=entry.currency,
        treasury=treasury,
        reference_number=entry.reference_number,
    )


def _make_job(entry: LedgerEntry, leg: ParsedLeg, order_index: int, tag: str) -> WriteJob:
    """أمر كتابة لقيد إلحاقي (is_reversal=True) — محاولة واحدة، مرتبط بنفس الصفقة."""
    return WriteJob(
        job_id=f"{entry.entry_id}-{tag}-{order_index}",
        deal_id=entry.deal_id,
        operation=leg.operation,
        leg=leg,
        order_index=order_index,
        is_reversal=True,
        max_attempts=1,
        created_at=_now(),
    )


def _main_downloaded(downloaded: list[LedgerEntry]) -> LedgerEntry:
    """الطرف الرئيسي للتعديل/التصحيح = البيع (قيمة الحوالة)؛ وإلا أول قيد منزّل."""
    for e in downloaded:
        if e.operation == OperationType.SELL:
            return e
    return downloaded[0]


async def build_reversal(
    action: str, value: Optional[float], original_deal: Deal, db
) -> list[WriteJob]:
    """
    يبني القيود الإلحاقية (WriteJob) لإلغاء/تعديل/تصحيح صفقة منزّلة — **من دفتر البوت**.

    action: 'cancel' | 'edit' | 'correct'.
    value : القيمة الجديدة (للتعديل/التصحيح). تُهمَل عند الإلغاء.

    يُرجع قائمة WriteJob (قد تكون فارغة إن لا شيء يُعكس — يُعالَج خارجيًا بتنبيه/مسؤول).
    القيم كلها من الدفتر (entries_for_deal) — لا من رسالة التصحيح وحدها (§10).
    """
    act = (action or "").strip().lower()

    entries = await db.ledger.entries_for_deal(original_deal.deal_id)
    downloaded = [e for e in entries if not e.is_reversal]
    if not downloaded:
        # لا قيود منزّلة (مثلًا تصحيح على «ملغاة» أو صفقة لم تنزل) → البوت لا يتصرّف
        log.warning(
            "build_reversal(%s): لا قيود منزّلة للصفقة %s — لا عكس (تنبيه + مسؤول)",
            act, original_deal.deal_id,
        )
        return []

    # ── إلغاء: عكس كل طرف منزّل بكامل قيمته، نفس الخزينة (تصفير) ──────────────
    if act == "cancel":
        jobs: list[WriteJob] = []
        for idx, e in enumerate(downloaded):
            orig = _match_leg(original_deal, e)
            leg = _reversal_leg(orig, e, e.amount, opposite=True)
            jobs.append(_make_job(e, leg, idx, "cancel"))
        log.info("إلغاء الصفقة %s → %d قيد عكسي بكامل القيمة", original_deal.deal_id, len(jobs))
        return jobs

    # ── تعديل / تصحيح: قيد بالفرق على الطرف الرئيسي، يُحسب من الدفتر ──────────
    if act in ("edit", "correct"):
        if value is None:
            log.warning("build_reversal(%s): لا قيمة جديدة — لا قيد", act)
            return []

        main = _main_downloaded(downloaded)
        diff = round(main.amount - float(value), _NDIGITS)  # الأصل − الجديد

        if _amounts_equal(diff, 0.0):
            log.info("build_reversal(%s) الصفقة %s: لا فرق — لا قيد", act, original_deal.deal_id)
            return []

        orig = _match_leg(original_deal, main)
        # الجديد أقل (diff>0) → عكس بالفرق (تخفيض). الجديد أكبر (diff<0) → قيد إضافي نفس الاتجاه.
        opposite = diff > 0
        leg = _reversal_leg(orig, main, abs(diff), opposite=opposite)
        job = _make_job(main, leg, 0, act)
        # قيد بنفس الاتجاه (زيادة) ليس عكسيًا محاسبيًا
        job.is_reversal = opposite
        log.info(
            "%s الصفقة %s: الأصل %s الجديد %s → قيد %s بالفرق %s",
            act, original_deal.deal_id, main.amount, value,
            "عكسي" if opposite else "إضافي", abs(diff),
        )
        return [job]

    log.warning("build_reversal: فعل غير معروف '%s' — لا قيد", action)
    return []


def _as_naive_utc(dt: datetime) -> datetime:
    """توحيد للمقارنة: القاعدة (mongomock/motor) قد تُرجع أوقاتًا بلا/بمنطقة — نوحّد UTC-naive."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def is_out_of_active_window(original_created_at: datetime, now: datetime) -> bool:
    """
    أكثر من 15 يومًا (§10 / §12 — ACTIVE_WINDOW_DAYS) → خارج مدى الحوالات النشطة → نادر → مسؤول.
    نافذة النشاط تشمل 15 يومًا؛ ما بعدها فقط يُعدّ خارجًا.
    """
    return (_as_naive_utc(now) - _as_naive_utc(original_created_at)) > timedelta(days=ACTIVE_WINDOW_DAYS)
