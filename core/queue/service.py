"""
QueueService — الطابور الصارم وتجميع الطرفين (§7). يستعمل DB، غير متزامن.

مبادئ (§7.1):
- الالتقاط منفصل عن المعالجة: capture يخزّن الخام فورًا؛ المعالجة تفرّغ بتأنٍّ.
- المعالجة تسلسلية صارمة صفقة-صفقة؛ الترتيب بختم الوصول.
- التعليق/التصعيد لا يوقف الطابور.
- الترتيب الصارم للكتابة: بيع أولًا (order_index=0) ثم شراء (order_index=1) — §7.3 §11.4.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from ..constants import (
    INCOMPLETE_DATA_ESCALATE_SECONDS,
    PENDING_REPLY_MAX_SECONDS,
    SECOND_LEG_MAX_SECONDS,
    SECOND_MESSAGE_LINK_SECONDS,
    Mark,
    OperationType,
    Status,
)
from ..db import Database
from ..logging_setup import get_logger
from ..models import Deal, ParsedLeg, RawMessage, SupplierRecord, SupplierRef, TreasuryRecord, TreasuryRef, WriteJob
from ..parsing import parse_completion_fragment
from ..parsing.resolve import resolve_treasury
from .commission import compute_commission
from .grouping import _norm_ref, compute_grouping_key, discount_pair

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_naive_utc(dt: datetime) -> datetime:
    """توحيد للمقارنة: القاعدة قد تُرجع أوقاتًا بلا منطقة (naive) — نقارن الجميع UTC-naive."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def is_completion_fragment(leg: ParsedLeg | None) -> bool:
    """
    رد خزينة/مورد يُكمِّل حوالة معلّقة (§7.3) — مثل «بلس» وحدها أو «صافي» وحدها أو
    «603 بن ناصر 5.77 / بلس». يحلّ خزينة أو موردًا لكنه ليس حوالة مستقلّة (بلا مبلغ ولا
    رقم إشاري)، فيُصنَّف حاليًا noise ويُهمَل — بينما هو الجزء المكمّل لرسالة أولى معلّقة.

    الشرط: يحلّ خزينة أو موردًا + بلا مبلغ + بلا رقم إشاري (وإلّا فهو حوالة/طرف مستقلّ
    يُجمَّع بالمفتاح المزدوج، لا رد مكمّل).
    """
    if leg is None:
        return False
    has_treasury_or_supplier = leg.treasury is not None or leg.is_supplier_counterpart
    return has_treasury_or_supplier and leg.amount is None and not leg.reference_number


def is_incomplete_first_message(leg: ParsedLeg | None) -> bool:
    """
    حوالة A ناقصة (§7.3، قرار المستخدم): الرسالة الأولى تحمل رقمًا إشاريًا + مبلغًا + هاتفًا
    + وسيلةً، وتنقصها **مرساة هوية الزبون** (لا كود ولا اسم مقروء) — وهي ما تحمله الرسالة
    الثانية (كود + اسم + سعر + خزينة). لا يمكن إدخالها كطرف واحد: تنتظر الرسالة الثانية، ثم
    تنبيه خفيف عند 90s، فتصعيد عند 15د.

    🔴 المرساة = هوية الزبون لا الخزينة (تصحيح بعد بلاغ إنتاج A11–A16): في الحوالات الحقيقية
    تصل الخزينة محلولة في الرسالة الأولى (مثل «صافي») بينما الكود/الاسم غائبان — فالاعتماد على
    «treasury is None» كان يُفوّت هذه الحالة ويمرّرها فورًا إلى trust_gate. لذا نقيس غياب الهوية
    بغضّ النظر عن الخزينة — وهو نفس شرط تعليق trust_gate (§8.2).

    تمييزها عن بيع كامل ينتظر شراءً اختياريًا: وجود كود أو اسم مقروء يُخرجها من التصنيف
    (فتُنهى كطرف واحد عبر sweep_waiting كالسابق).
    """
    if leg is None:
        return False
    from ..matching.fuzzy import normalize_ar  # استيراد محليّ: تفادي دورة استيراد الحزمة
    return not leg.customer_code and not normalize_ar(leg.customer_name or "")


class QueueService:
    """خدمة الطابور — تنسّق الالتقاط والتجميع والتصعيد وبناء أوامر الكتابة."""

    def __init__(self, db: Database):
        self.db = db

    # ── الالتقاط (§7.1 بند 1) ────────────────────────────────────────────────
    async def capture(self, raw: RawMessage) -> None:
        """تخزين الرسالة الخام فورًا قبل أي معالجة — انقطاع الكهرباء لا يضيّعها."""
        await self.db.raw.insert(raw)
        log.info("التقاط خام: %s (%s)", raw.message_key, raw.chat_jid)

    # ── التجميع (§7.3) ───────────────────────────────────────────────────────
    async def try_group(self, leg: ParsedLeg, now: datetime, *, chat_jid: str | None = None) -> Deal:
        """
        ينشئ صفقة جديدة أو يدمج الطرف الثاني في صفقة منتظِرة (§7.3).

        1) الربط اليقيني: Reply/تعديل يدوي → صفقة تحمل مفتاح رسالة هذا الطرف.
        2) التجميع التلقائي: رقم إشاري + هاتف على صفقة منتظِرة طرفًا ثانيًا.
        3) غير ذلك → صفقة جديدة.
        """
        # 1) ربط يقيني بمفتاح الرسالة (§7.3 التجميع اليدوي / idempotency)
        if leg.source_message_key:
            existing = await self.db.deals.find_by_source_key(leg.source_message_key)
            if existing is not None:
                if self._slot_filled(existing, leg):
                    return existing  # موجود مسبقًا — لا ازدواج
                return await self._merge_second_leg(existing, leg, now)

        # 2) التجميع التلقائي بالرقم الإشاري + الهاتف (§7.3)
        key = compute_grouping_key(leg)
        if key:
            waiting = await self.db.deals.find_by_grouping_key(key)
            if waiting is not None:
                if not self._slot_filled(waiting, leg):
                    return await self._merge_second_leg(waiting, leg, now)
                # الخانة (بيع) ممتلئة بنفس نوع العملية — قد تكون رسالة تسوية خصم بنفس الرقم
                # الإشاري (صيغة Aخصم §6.3): رسالة أولى بالهوية + رسالة ثانية بالخزينة والمبلغ بعد
                # الخصم. غير ذلك (ازدواج فعليّ) → يسقط للسلوك السابق (صفقة جديدة).
                pair = discount_pair(waiting.sell_leg, leg) if waiting.sell_leg is not None else None
                if pair is not None:
                    return await self._merge_discount(waiting, pair[0], pair[1], now)

        # 3) صفقة جديدة
        return await self._create_deal(leg, key or None, now, chat_jid)

    async def _create_deal(self, leg: ParsedLeg, key: str | None, now: datetime,
                           chat_jid: str | None = None) -> Deal:
        deal = Deal(
            deal_id=str(uuid.uuid4()),
            status=Status.PARSED,
            created_at=now,
            updated_at=now,
            chat_jid=chat_jid,
            grouping_key=key,
            source_message_keys=[leg.source_message_key] if leg.source_message_key else [],
        )
        self._apply_leg(deal, leg)

        # الخزينة/الكلمة الصريحة تحدّد انتظار الطرف الثاني (§5.2 §5.3).
        # تكاملٌ (§5.5, §7.3): الطرف البائع في صفقة طرفين قد يصل أولًا بخزينة غير
        # مصرَّح بها (تُحسم ضمنيًّا خصم1%/صافي عند اكتمال الصفقة §6.1). لذا ننتظر أيضًا
        # حين تكون الخزينة غير محلولة (غموض = ننتظر الطرف الثاني — القاعدة الذهبية §0).
        # الخزينة «بيع فقط» المحلولة (بلاس/وليد...) تمشي فورًا ولا تنتظر (§5.2).
        # 🔴 استثناء (Fix SI): حوالة SI معنونة مكتملة برسالة واحدة، لا تنتظر طرفًا ثانيًا أبدًا.
        #    خزينتها غير المحلولة تُعلَّق فورًا (HELD في بوابة الثقة) بدل انتظار 90s بلا جدوى.
        # 🔴 تصحيح (بلاغ إنتاج A11–A16): حوالة A ناقصة الهوية (رسالة أولى بخزينة محلولة مثل
        #    «صافي» لكن بلا كود/اسم) يجب أن تنتظر الرسالة الثانية أيضًا — لا أن تمرّ فورًا إلى
        #    trust_gate فتُعلَّق «كود ناقص». المرساة = هوية الزبون لا الخزينة (is_incomplete_first_message).
        needs_pair = leg.expects_pair or (
            not leg.is_si_format
            and (leg.treasury is None or is_incomplete_first_message(leg))
        )
        if needs_pair:
            deal.is_two_legged = leg.expects_pair  # يتأكّد نهائيًا عند وصول طرف ثانٍ فعلًا
            deal.status = Status.WAITING_SECOND_LEG
            deal.waiting_deadline = now + timedelta(seconds=SECOND_LEG_MAX_SECONDS)
            # (Fix 2) رد/رسالة ثانية وصلت قبل هذه الحوالة (معلّقة)؟ اربطها الآن ليكتمل فورًا.
            # يشمل الآن حالة الهوية الناقصة بخزينة محلولة (بلاغ A11–A16)، لا الخزينة الناقصة فقط.
            merged = False
            if leg.treasury is None or is_incomplete_first_message(leg):
                merged = await self._pull_pending_reply(deal, chat_jid, now)
            if merged:
                log.info("صفقة %s اكتملت فورًا بربط رد معلّق سابق", deal.deal_id)
            else:
                log.info("صفقة %s تنتظر الطرف الثاني حتى %s (expects_pair=%s, treasury=%s)",
                         deal.deal_id, deal.waiting_deadline, leg.expects_pair,
                         leg.treasury.name if leg.treasury else None)
        await self.db.deals.upsert(deal)
        return deal

    async def _merge_second_leg(self, deal: Deal, leg: ParsedLeg, now: datetime) -> Deal:
        """دمج الطرف الثاني — اكتملت الصفقة، تنتقل للمطابقة (§8) لاحقًا."""
        self._apply_leg(deal, leg)
        deal.is_two_legged = True
        deal.waiting_deadline = None
        deal.status = Status.PARSED
        if leg.source_message_key and leg.source_message_key not in deal.source_message_keys:
            deal.source_message_keys.append(leg.source_message_key)
        await self.db.deals.upsert(deal)
        log.info("اكتملت الصفقة %s (بيع+شراء)", deal.deal_id)
        return deal

    async def _merge_discount(
        self, deal: Deal, identity: ParsedLeg, settlement: ParsedLeg, now: datetime
    ) -> Deal:
        """
        صيغة الخصم من رسالتين (Aخصم §6.3): تُدمَج رسالة الهوية (كود+اسم+سعر+مبلغ قبل الخصم)
        مع رسالة التسوية (خزينة + مبلغ بعد الخصم) في **طرف بيع واحد**:

        - المبلغ الأجنبي = مبلغ الهوية (قبل الخصم) — يُدخَل في خانة «المبلغ الأجنبي» (§6.2).
        - amount_after_discount = مبلغ التسوية (بعد الخصم) — يحسبه MONEYADO بعد العمولة.
        - العمولة = بعد − قبل (سالبة، §6.2) → خانة العمولة. المبلغ المخصوم النهائي = بعد الخصم.
        - الخزينة من رسالة التسوية. الصفقة **ليست طرفين** (لا شراء) → لا يمسّها _resolve_two_leg،
          لذا تُضبط العمولة هنا صراحةً (كما SI تُحسب من amount_after_discount).
        """
        merged = identity.model_copy(update={
            "amount_after_discount": settlement.amount,
            "treasury": settlement.treasury,
            "currency": identity.currency or settlement.currency,
            "commission_rate": 0.0,
            "phone": identity.phone or settlement.phone,
            "payment_method": identity.payment_method or settlement.payment_method,
        })
        merged.commission = compute_commission(merged, None)  # بعد − قبل (سالبة §6.2)

        deal.sell_leg = merged
        deal.buy_leg = None
        deal.is_two_legged = False
        deal.status = Status.PARSED
        deal.waiting_deadline = None
        for mk in (identity.source_message_key, settlement.source_message_key):
            if mk and mk not in deal.source_message_keys:
                deal.source_message_keys.append(mk)
        deal.updated_at = now
        await self.db.deals.upsert(deal)
        log.info(
            "صفقة %s: صيغة خصم (Aخصم) — قبل=%s بعد=%s عمولة=%s خزينة=%s",
            deal.deal_id, merged.amount, merged.amount_after_discount,
            merged.commission, merged.treasury.name if merged.treasury else None,
        )
        return deal

    async def try_absorb_supplier_second(
        self, leg: ParsedLeg, raw: RawMessage, now: datetime,
        treasuries: list[TreasuryRecord], suppliers: list[SupplierRecord],
    ) -> Deal | None:
        """رسالة ثانية بنفس الرقم الإشاري لصفقة معلّقة (WAITING) في نفس الغرفة تحمل **موردًا**
        (كود+اسم+سعر+مبلغ بعد الخصم §6): تُدمَج كطرف مورد في تلك الصفقة — لا صفقة جديدة (§6).

        🔴 المشكلة: الرسالة الثانية تبدأ بنفس الرقم الإشاري فتُقرأ كحوالة جديدة. الحلّ: إن طابق
        رقمها الإشاري صفقةً لها طرف بيع بكود زبون (الرسالة الأولى) وبلا طرف شراء في نفس الغرفة →
        تُستخرج بيانات المورد بـ **pattern-fishing** (متينة للترتيب المختلف)، ومبلغها الأصغر =
        بعد الخصم. يُرجع الصفقة المكتملة أو None (فتتبع الرسالة المسار العادي try_group)."""
        if not raw.chat_jid or not leg.reference_number:
            return None
        ref = _norm_ref(leg.reference_number)
        horizon = _as_naive_utc(now) - timedelta(seconds=SECOND_MESSAGE_LINK_SECONDS)
        target: Deal | None = None
        for deal in await self.db.deals.waiting_in_room(raw.chat_jid):
            sell = deal.sell_leg
            if (sell is not None and deal.buy_leg is None and sell.customer_code
                    and sell.supplier is None
                    and _norm_ref(sell.reference_number) == ref and ref
                    and _as_naive_utc(deal.created_at) >= horizon):
                target = deal
                break
        if target is None:
            return None
        # المورد بـ pattern-fishing (يعالج الترتيب المختلف للرسالة الثانية: الكود آخرًا…)
        frag = parse_completion_fragment(raw.text, treasuries, suppliers)
        if not frag.customer_code:
            return None                          # لا هوية مورد في الرسالة الثانية → ليست طرف مورد
        return await self._absorb_supplier_leg(target, frag, raw.message_key, treasuries, now)

    async def _absorb_supplier_leg(
        self, deal: Deal, frag: ParsedLeg, message_key: str, treasuries: list[TreasuryRecord],
        now: datetime,
    ) -> Deal:
        """يضبط المورد وسعره والمبلغ بعد الخصم على طرف البيع المعلّق + خزينة sell_and_buy
        افتراضية، فيُشتقّ طرف الشراء لاحقًا (pipeline._maybe_synthesize_buy_leg §6.1)."""
        sell = deal.sell_leg
        # المبلغ الأصغر في الرسالة الثانية = المبلغ بعد الخصم (للعمولة والطرف المشتقّ §6.2)
        if frag.amount is not None and sell.amount is not None and frag.amount < sell.amount:
            sell.amount_after_discount = frag.amount
        # كود+اسم الرسالة الثانية = المورد (لا زبون جديد)
        sell.supplier = SupplierRef(code=frag.customer_code, name=frag.customer_name)
        sell.supplier_price_raw = frag.price_raw
        sell.commission = compute_commission(sell, None)  # بعد − قبل (سالبة §6.2)
        sell.commission_rate = 0.0
        # خزينة sell_and_buy الافتراضية لطرف مورد حوالة A الثانية = «فودافون بالخصم» (85) **دائمًا**
        # — سواء وُجد خصم أو لا (قرار صاحب العمل §6.1) — كي يُشتقّ منها طرف الشراء بكود قابل للكتابة.
        if sell.treasury is None:
            rec = resolve_treasury("فودافون بالخصم", treasuries)
            if rec is not None:
                sell.treasury = TreasuryRef(
                    code=rec.code, name=rec.name, type=rec.type,
                    currency=rec.currency or sell.currency,
                )
        deal.is_two_legged = True
        deal.status = Status.PARSED
        deal.waiting_deadline = None
        if message_key and message_key not in deal.source_message_keys:
            deal.source_message_keys.append(message_key)
        deal.updated_at = now
        await self.db.deals.upsert(deal)
        log.info(
            "صفقة %s: رسالة ثانية بمورد «%s %s» (نفس الرقم %s) → دُمجت كطرف مورد (بعد الخصم=%s)",
            deal.deal_id, frag.customer_code, frag.customer_name,
            sell.reference_number, sell.amount_after_discount,
        )
        return deal

    @staticmethod
    def _apply_leg(deal: Deal, leg: ParsedLeg) -> None:
        if leg.operation == OperationType.BUY:
            deal.buy_leg = leg
        else:
            deal.sell_leg = leg

    @staticmethod
    def _slot_filled(deal: Deal, leg: ParsedLeg) -> bool:
        if leg.operation == OperationType.BUY:
            return deal.buy_leg is not None
        return deal.sell_leg is not None

    # ── ردود الخزينة/المورد المكمّلة (§7.3 — الرسالة الثانية بلا رقم إشاري) ────
    async def absorb_fragment(
        self, frag: ParsedLeg, chat_jid: str | None, message_key: str, now: datetime
    ) -> Deal | None:
        """
        رد خزينة/مورد بلا رقم إشاري («بلس»/«صافي» وحدها).

        (Fix 1) يصل بعد الحوالة: يُربَط بأقرب صفقة معلّقة في نفس الغرفة خلال دقيقتين
                (SECOND_MESSAGE_LINK_SECONDS) → تكتمل الصفقة وتمشي.
        (Fix 2) يصل قبل الحوالة: لا صفقة معلّقة → يُحفَظ «ردًّا معلّقًا» حتى 90s
                (PENDING_REPLY_MAX_SECONDS) ريثما تصل الأولى.
        """
        frag.source_message_key = message_key
        deal = await self._find_recent_waiting(chat_jid, now)
        if deal is not None:
            self._apply_fragment(deal, frag)
            deal.status = Status.PARSED
            deal.waiting_deadline = None
            if message_key and message_key not in deal.source_message_keys:
                deal.source_message_keys.append(message_key)
            await self.db.deals.upsert(deal)
            log.info("رُبط رد الخزينة/المورد %s بالصفقة المعلّقة %s (قرب زمني، نفس الغرفة)",
                     message_key, deal.deal_id)
            return deal
        # لا صفقة معلّقة → احفظه ردًّا معلّقًا (Fix 2)
        await self.db.pending_replies.add(
            message_key=message_key, chat_jid=chat_jid or "", leg=frag, received_at=now,
        )
        log.info("رد خزينة/مورد %s بلا صفقة معلّقة — حُفِظ ردًّا معلّقًا (%ss)",
                 message_key, PENDING_REPLY_MAX_SECONDS)
        return None

    async def _find_recent_waiting(self, chat_jid: str | None, now: datetime) -> Deal | None:
        """أحدث صفقة معلّقة تنتظر خزينتها في نفس الغرفة خلال نافذة الربط (§7.3)."""
        if not chat_jid:
            return None
        horizon = _as_naive_utc(now) - timedelta(seconds=SECOND_MESSAGE_LINK_SECONDS)
        candidates = [
            d for d in await self.db.deals.waiting_in_room(chat_jid)
            if self._needs_completion(d) and _as_naive_utc(d.created_at) >= horizon
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda d: _as_naive_utc(d.created_at), reverse=True)
        return candidates[0]

    async def _pull_pending_reply(self, deal: Deal, chat_jid: str | None, now: datetime) -> bool:
        """رد معلّق سابق في نفس الغرفة يُكمِّل هذه الصفقة الجديدة (Fix 2). يُرجع True إن اكتملت."""
        if not chat_jid:
            return False
        doc = await self.db.pending_replies.find_recent(chat_jid, now, PENDING_REPLY_MAX_SECONDS)
        if doc is None:
            return False
        frag = ParsedLeg(**doc["leg"])
        self._apply_fragment(deal, frag)
        deal.status = Status.PARSED
        deal.waiting_deadline = None
        mk = doc.get("message_key")
        if mk and mk not in deal.source_message_keys:
            deal.source_message_keys.append(mk)
        await self.db.pending_replies.consume(mk)
        log.info("رُبط الرد المعلّق %s بالصفقة الجديدة %s", mk, deal.deal_id)
        return True

    @staticmethod
    def _needs_completion(deal: Deal) -> bool:
        """
        صفقة تنتظر إكمالًا — هدف صالح لرسالة ثانية مكمّلة:
          • خزينة غير محلولة (الرسالة الثانية تحمل الخزينة)، أو
          • هوية زبون ناقصة بخزينة محلولة (الرسالة الثانية تحمل الكود/الاسم — بلاغ A11–A16).
        """
        for leg in (deal.sell_leg, deal.buy_leg):
            if leg is not None and (leg.treasury is None or is_incomplete_first_message(leg)):
                return True
        return False

    @staticmethod
    def _apply_fragment(deal: Deal, frag: ParsedLeg) -> None:
        """يدمج رد الخزينة/المورد في الصفقة المعلّقة: يكمّل الخزينة وبيانات الزبون الناقصة."""
        target = deal.sell_leg or deal.buy_leg
        # خزينة الرد تُكمِّل الطرف المنتظِر إن كان بلا خزينة
        if target is not None and target.treasury is None and frag.treasury is not None:
            target.treasury = frag.treasury
            if target.currency is None:
                target.currency = frag.treasury.currency
        if frag.is_supplier_counterpart and frag.supplier is not None and deal.buy_leg is None:
            # رد مورد = الطرف الثاني (شراء) لصفقة طرفين
            deal.buy_leg = frag
            deal.is_two_legged = True
        elif target is not None:
            # رد زبون/خزينة يكمّل بيانات البيع الناقصة (كود/اسم/سعر)
            if target.customer_code is None and frag.customer_code:
                target.customer_code = frag.customer_code
            if not target.customer_name and frag.customer_name:
                target.customer_name = frag.customer_name
            if target.price_normalized is None and frag.price_normalized:
                target.price_raw = frag.price_raw
                target.price_normalized = frag.price_normalized

    # ── إنهاء المهلة: طرف واحد لا تصعيد (§7.3، قرار المستخدم) ─────────────────
    async def sweep_waiting(self, now: datetime) -> list[Deal]:
        """
        الصفقات التي تجاوزت SECOND_LEG_MAX (90s) بلا طرف ثانٍ (مورد) → **تُنهى كطرف واحد**
        وتمشي للمعالجة (PARSED) — لا تصعيد. نافذة الانتظار كانت فرصة اختيارية لدمج شراء.
        (أُلغي التصعيد القديم: بيع مفرد شرعيّ لا يُعامَل خطأً.)
        يُرجع الصفقات التي أُنهيت كطرف واحد (للتسجيل/المتابعة).

        🔴 استثناء (§7.3، قرار المستخدم): حوالة A ناقصة (رسالة أولى بلا كود/سعر/خزينة) لا
        تُنهى كطرف واحد إطلاقًا — تُدار عبر sweep_incomplete_a (تنبيه خفيف ثم تصعيد).
        """
        finalized: list[Deal] = []
        for deal in await self.db.deals.by_status(Status.WAITING_SECOND_LEG):
            if is_incomplete_first_message(deal.sell_leg or deal.buy_leg):
                continue  # حوالة A ناقصة — لا إدخال مفرد؛ تنبيه/تصعيد في sweep_incomplete_a
            if deal.waiting_deadline is not None and _as_naive_utc(now) >= _as_naive_utc(deal.waiting_deadline):
                deal.status = Status.PARSED          # يمشي كطرف واحد
                deal.is_two_legged = False           # لم يصل شراء → مفرد
                deal.waiting_deadline = None
                deal.updated_at = now
                await self.db.deals.upsert(deal)
                finalized.append(deal)
                log.info(
                    "صفقة %s: انتهت مهلة الطرف الثاني (%ss) بلا شراء → إدخال كطرف واحد",
                    deal.deal_id, SECOND_LEG_MAX_SECONDS,
                )
        return finalized

    # ── حوالة A ناقصة: تنبيه خفيف عند 90s، تصعيد عند 15د (§7.3، قرار المستخدم) ─
    async def sweep_incomplete_a(self, now: datetime) -> tuple[list[Deal], list[Deal]]:
        """
        حوالة A ناقصة (رسالة أولى: رقم إشاري + مبلغ + هاتف + وسيلة فقط) تنتظر الرسالة
        الثانية (كود + اسم + سعر + خزينة). القياس من لحظة الإنشاء (created_at):

          - تجاوزت 90s ولم تصل الرسالة الثانية ولم تُنبَّه → **تنبيه خفيف في المركزية**
            (تبقى WAITING_SECOND_LEG — لا تُدخَل).
          - تجاوزت 15 دقيقة بلا رسالة ثانية → **تصعيد لغرفة المسؤول** (ESCALATED).

        يُرجع (to_warn, to_escalate) لينفّذ الأنبوب الإرسال (QueueService بلا Bus §2.2).
        الإرسال الفعليّ في pipeline.tick. التصعيد يُخرجها من WAITING فلا يتكرّر؛ التنبيه
        محميّ بعلم incomplete_warned فلا يتكرّر كل نبضة.
        """
        to_warn: list[Deal] = []
        to_escalate: list[Deal] = []
        for deal in await self.db.deals.by_status(Status.WAITING_SECOND_LEG):
            if not is_incomplete_first_message(deal.sell_leg or deal.buy_leg):
                continue
            elapsed = (_as_naive_utc(now) - _as_naive_utc(deal.created_at)).total_seconds()
            if elapsed >= INCOMPLETE_DATA_ESCALATE_SECONDS:
                deal.status = Status.ESCALATED
                deal.mark = Mark.WARN
                deal.waiting_deadline = None
                deal.hold_reason = "حوالة A ناقصة — لم تصل الرسالة الثانية خلال 15 دقيقة"
                deal.updated_at = now
                await self.db.deals.upsert(deal)
                to_escalate.append(deal)
                log.warning(
                    "صفقة %s: حوالة A ناقصة تجاوزت 15 دقيقة بلا رسالة ثانية → تصعيد للمسؤول",
                    deal.deal_id,
                )
            elif elapsed >= SECOND_LEG_MAX_SECONDS and not deal.incomplete_warned:
                deal.incomplete_warned = True
                deal.updated_at = now
                await self.db.deals.upsert(deal)
                to_warn.append(deal)
                log.info(
                    "صفقة %s: حوالة A ناقصة تجاوزت 90s بلا رسالة ثانية → تنبيه خفيف في المركزية",
                    deal.deal_id,
                )
        return to_warn, to_escalate

    # ── بناء أوامر الكتابة (§7.3 §11.4) ──────────────────────────────────────
    async def build_write_jobs(self, deal: Deal) -> list[WriteJob]:
        """
        يبني أوامر الكتابة بالترتيب الصارم: بيع أولًا (order_index=0) ثم شراء (=1).
        الشراء max_attempts=1 (§11.4). تُوضع في الـ Outbox وتُعاد للمنادي.
        """
        now = _utcnow()
        jobs: list[WriteJob] = []

        if deal.sell_leg is not None:
            jobs.append(WriteJob(
                job_id=f"{deal.deal_id}:sell",
                deal_id=deal.deal_id,
                operation=OperationType.SELL,
                leg=deal.sell_leg,
                order_index=0,
                max_attempts=1,   # لا يُعاد البيع أبدًا (§11.4)
                created_at=now,
            ))
        if deal.buy_leg is not None:
            jobs.append(WriteJob(
                job_id=f"{deal.deal_id}:buy",
                deal_id=deal.deal_id,
                operation=OperationType.BUY,
                leg=deal.buy_leg,
                order_index=1,
                max_attempts=1,   # الشراء: محاولة واحدة (§11.4)
                created_at=now,
            ))

        for job in jobs:
            await self.db.outbox.enqueue(job)
        # الصفقة صارت في Outbox جاهزة للكتابة (§15.9)
        await self.db.deals.set_status(deal.deal_id, Status.READY)
        return jobs
