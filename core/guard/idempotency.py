"""
الحارس — منع التكرار (Idempotency §9). 🔴

مصدر الحقيقة = دفتر البوت الداخلي (ما نزّله فعلًا) + ✅ — **ليس** الاسم+المبلغ،
**وليس** الرقم الإشاري وحده. المعرّف الأثبت = رقم رسالة واتساب (message key).

مفهومان منفصلان (§9):
- «التحقّق» (هل موجودة؟ بالاسم+المبلغ) — ليست مهمّة هذه الوحدة.
- «منع التكرار» (هل نزّلتها؟ بالدفتر + message key) — هذه الوحدة.

قاعدة ذهبية (§0): لو شكّيت — لا تُنزّل. الحكم النهائي دائمًا من الدفتر.
"""
from __future__ import annotations

from typing import Optional

from ..constants import CANCEL_KEYWORDS, OperationType
from ..logging_setup import get_logger
from ..models import Deal, ParsedLeg

log = get_logger(__name__)

# سماحية مقارنة المبالغ (float) — المبالغ أعداد صحيحة عادةً لكن نتحمّل خطأ التمثيل
_AMOUNT_EPS = 0.001


def _amounts_equal(a: Optional[float], b: Optional[float]) -> bool:
    """تساوي مبلغين مع سماحية float. None ≠ رقم."""
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= _AMOUNT_EPS


class Guard:
    """حارس منع التكرار — يعتمد الدفتر (db.ledger) والرسائل الخام (db.raw) وحدهما."""

    def __init__(self, db):
        self._db = db

    # ── (1) هل نُزّلت من قبل؟ الدفتر + message key (§9) ──────────────────────
    async def already_downloaded(self, message_key: str) -> bool:
        """المرجع = الدفتر + مفتاح الرسالة (§9). قيد غير عكسي بنفس المفتاح = نُزّلت."""
        return await self._db.ledger.was_downloaded(message_key)

    # ── (2) هل يوجد رد «إلغاء» عليها؟ (قبل أي تنزيل §9) ──────────────────────
    async def is_cancelled(self, message_key: str) -> bool:
        """
        هل توجد رسالة رد (Reply) على هذه الرسالة نصّها «إلغاء» (§10)؟
        الربط بمفتاح الأصل (reply_to_key) — يقين 100%. لا نُنزّل حوالة عليها إلغاء.
        """
        cur = self._db.raw.col.find({"reply_to_key": message_key})
        async for doc in cur:
            text = doc.get("text") or ""
            if any(kw in text for kw in CANCEL_KEYWORDS):
                return True
        return False

    # ── تصنيف الرقم الإشاري (§9) — الحكم النهائي من الدفتر ───────────────────
    async def classify_reference(
        self, reference_number: Optional[str], new_leg: ParsedLeg
    ) -> str:
        """
        نفس الرقم الإشاري (§9):
        - 'independent' : لا قيد بهذا الرقم في الدفتر → حوالة مستقلة.
        - 'duplicate'   : يوجد قيد **بنفس المحتوى** (زبون+مبلغ) → نسخة طبق الأصل، يُتجاهل الثاني.
        - 'two_legs'    : يوجد قيد **بمحتوى مختلف** (زبون/مبلغ) → طرفا صفقة واحدة.

        الرقم الإشاري وحده غير حاسم (يتكرّر لطرفَي صفقة أو بالغلط) — الحكم من الدفتر.
        """
        if not reference_number:
            return "independent"

        entries = await self._db.ledger.by_reference(reference_number)
        if not entries:
            return "independent"

        for e in entries:
            if e.is_reversal:
                continue  # القيود العكسية ليست تنزيلًا أصليًا
            same_customer = e.customer_code == new_leg.customer_code
            if same_customer and _amounts_equal(e.amount, new_leg.amount):
                return "duplicate"

        return "two_legs"

    # ── الفحص قبل أي تنزيل (§9 §15-4) ────────────────────────────────────────
    async def guard_before_write(self, deal: Deal) -> tuple[bool, Optional[str]]:
        """
        قبل أي تنزيل: (1) لم تُنزَّل من قبل (الدفتر)، (2) لا يوجد رد «إلغاء» عليها.
        يُرجع (allowed, reason). عند المنع reason = سبب مختصر يُسجَّل ويُصعَّد.

        يضمن عدم الإدخال المزدوج من جهة الدفتر (فحص SQL قبل إعادة المحاولة مهمّة وحدة أخرى).
        """
        keys = list(deal.source_message_keys or [])
        for leg in (deal.sell_leg, deal.buy_leg):
            if leg and leg.source_message_key and leg.source_message_key not in keys:
                keys.append(leg.source_message_key)

        if not keys:
            # لا مفتاح رسالة → لا يمكن ضمان عدم التكرار → القاعدة الذهبية: لا تُنزّل
            reason = f"الصفقة {deal.deal_id} بلا مفتاح رسالة — يُمنع التنزيل (§9)"
            log.warning(reason)
            return False, reason

        for key in keys:
            if await self.already_downloaded(key):
                reason = f"الرسالة {key} نُزّلت من قبل — منع تكرار (§9)"
                log.info(reason)
                return False, reason

        for key in keys:
            if await self.is_cancelled(key):
                reason = f"يوجد رد إلغاء على الرسالة {key} — لا تُنزّل (§10)"
                log.info(reason)
                return False, reason

        return True, None


# للاستخدام الداخلي في corrections.py أيضًا
_OPPOSITE = {OperationType.SELL: OperationType.BUY, OperationType.BUY: OperationType.SELL}
