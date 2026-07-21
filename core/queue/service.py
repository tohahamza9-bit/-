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
    Currency,
    Mark,
    OperationType,
    Status,
)
from ..db import Database
from ..logging_setup import get_logger
from ..models import Deal, ParsedLeg, RawMessage, SupplierRecord, SupplierRef, TreasuryRecord, TreasuryRef, WriteJob
from ..parsing import extract_code_name_price_lines, parse_completion_fragment, parse_message
from ..parsing.normalize import normalize_ar, normalize_payment, normalize_price
from ..parsing.resolve import resolve_supplier, resolve_treasury
from .commission import compute_commission
from .grouping import _is_discount_identity_leg, _norm_ref, compute_grouping_key, discount_pair

log = get_logger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_naive_utc(dt: datetime) -> datetime:
    """توحيد للمقارنة: القاعدة قد تُرجع أوقاتًا بلا منطقة (naive) — نقارن الجميع UTC-naive."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _unresolved_treasury_tokens(text: str, treasuries: list[TreasuryRecord]) -> list[str]:
    """أسطر تبدو خزينةً لكنها لم تُحلّ: سطر **كلمة عربية واحدة** بلا رقم، ليست وسيلة دفع، ولا
    تُطابِق أي خزينة (§0 — الخزائن تامّة). يميّز «محموذ» (خطأ إملاء «محمود صفاقس») عن أسطر السعر/
    الاسم المتعدّدة الكلمات. يُستعمَل لالتقاط الرمز المجهول وربط الرسالة الثانية بدل إسقاطها."""
    out: list[str] = []
    for ln in (text or "").replace("/", "\n").splitlines():
        ln = ln.strip()
        if (ln and len(ln.split()) == 1 and normalize_ar(ln)
                and not any(ch.isdigit() for ch in ln)
                and not normalize_payment(ln)
                and resolve_treasury(ln, treasuries) is None):
            out.append(ln)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# (R1) المرجع الصريح مُلزِم مطلقًا — حارسٌ واحد لكلّ منتقيات الربط
# ═════════════════════════════════════════════════════════════════════════════
def _deal_ref(d: Deal) -> str:
    """الرقم الإشاريّ المُطبَّع لصفقة (من طرف البيع وإلّا الشراء). "" إن لا مرجع."""
    leg = d.sell_leg or d.buy_leg
    return _norm_ref(leg.reference_number) if leg is not None else ""


def _log_link_decision(stage: str, message_key: str | None, cands: list[Deal],
                       chosen: Deal | None, reason: str, text_ref: str | None = None) -> None:
    """(R3) **لماذا** اختير هذا الهدف دون غيره — لا «أنّ» ربطًا حدث فقط.

    فجوة رصد مؤكَّدة (2026-07-20): وصلت 6 مراجع خلال 20 ثانية كلُّها بلا خزينة، ورُبط اثنان
    منها خطأً — ولا سطر واحد في اللوق يذكر من كان المرشَّحون ولا لماذا فاز من فاز."""
    log.info("[link] stage=%s msg=%s text_ref=%s cands=%s chosen=%s reason=%s",
             stage, message_key or "-", text_ref or "-",
             [f"{d.deal_id[:8]}:{_deal_ref(d) or '؟'}" for d in cands],
             chosen.deal_id[:8] if chosen is not None else "-", reason)


def bind_by_reference(cands: list[Deal], text_ref: str | None) -> tuple[list[Deal], str] | None:
    """(R1) المرجع الصريح مُلزِم مطلقًا — الحارس الوحيد لكلّ مسارات الربط.

    ثلاثيّ الحالة **عمدًا**، فـ«الرفض» مُرمَّزٌ في قيمة الإرجاع لا في `if` كلّ مُستدعٍ:
      • `None`                        ⇒ لا مرجع في النصّ؛ امضِ لمنطقك (ذكاء ثمّ FIFO).
      • `([], "rejected-ref-mismatch")` ⇒ مرجعٌ صريح بلا صفقة مطابقة ⇒ **لا رَبْط بشيء**.
        لا سقوط لـFIFO: هذه بالضبط هي حادثة X1567 التي ابتلعتها X1566.
      • `([d], "ref-exact")`          ⇒ اربط بصفقته، بغضّ النظر عن أيّ شيء آخر.

    كلّ منتقٍ يُعيد هذه النتيجة **مباشرةً**، فلا يوجد كود FIFO بعد الحارس يمكن العودة إليه."""
    if not text_ref:
        return None
    nref = _norm_ref(text_ref)
    if not nref:
        return None
    matched = [d for d in cands if _deal_ref(d) == nref]
    return matched, ("ref-exact" if matched else "rejected-ref-mismatch")


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


def is_treasury_second_reply(leg: ParsedLeg | None) -> bool:
    """رسالة ثانية «رقم إشاري + خزينة» بلا هوية زبون (§7.3) — **المبلغ اختياري**. تُكمِّل صفقة
    معلّقة بنفس الرقم: خزينةً عاديةً، أو **تسوية خصم** إن حملت مبلغًا بعد الخصم (Aخصم §6.3).
    تمييزه عن is_completion_fragment: هذا **يحمل** رقمًا إشاريًا."""
    if leg is None:
        return False
    from ..matching.fuzzy import normalize_ar  # استيراد محليّ: تفادي دورة استيراد الحزمة
    # 🔴 (م: X1327، نفس مبدأ X1257) **الموضع يحدّد النوع**: رسالةٌ تحمل **وجهة المستلم**
    #    (مدينة/بلد) هي رسالة أولى قطعًا — مهما طابق أحدُ أسطرها اسمَ خزينة مسجّلة أو إملاءً
    #    بديلًا لها. اسم المستلم لا يُحوّل الرسالة إلى ردّ خزينة.
    #    الحادثة: «X1327 / +356… / 3000 دت / وليد / العاصمة» — «وليد»+«العاصمة» طابقتا الخزينة
    #    المسجّلة «وليد تونس العاصمة» (51) عبر تجريد لاحقة المدينة، فصُنّفت الرسالة الأولى
    #    **ثانيةً**، فلم تُنشَأ لها صفقة، فرَست تكملتها («طلال») على الجارة X1328 بخزينة خاطئة.
    #    الردّ الثاني الحقيقيّ (خزينة، أو تسوية خصم §6.3) يحمل مرجعًا وخزينةً ومبلغًا — وقد يحمل
    #    هاتفًا — لكنه **لا يحمل وجهةً** أبدًا؛ الوجهة معلومة الرسالة الأولى وحدها.
    if leg.country:
        return False
    return (
        bool(leg.reference_number) and leg.treasury is not None
        and not leg.customer_code and not normalize_ar(leg.customer_name or "")
    )


def is_treasury_only_reply(leg: ParsedLeg | None) -> bool:
    """رسالة ثانية «رقم إشاري + خزينة **فقط**» بلا مبلغ (§7.3) — رد خزينة بحت يُحفَظ ردًّا معلّقًا
    عند غياب صفقته. «بلا مبلغ» يميّزه عن تسوية الخصم (تحمل المبلغ بعد الخصم)."""
    return is_treasury_second_reply(leg) and leg.amount is None


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

    🔴 الصيغة التونسية (اسم مستلم منفرد بلا كود): وجود recipient_name = هوية مستلم مقروءة →
    ليست ناقصة (is_incomplete=False)، فلا تنبيه «أكمل البيانات» المبكر — تنتظر الرسالة الثانية
    (كود + سعر + خزينة) طبيعيًّا عبر انتظار الطرف الثاني (treasury=None → needs_pair).
    """
    if leg is None:
        return False
    from ..matching.fuzzy import normalize_ar  # استيراد محليّ: تفادي دورة استيراد الحزمة
    return (not leg.customer_code
            and not normalize_ar(leg.customer_name or "")
            and not normalize_ar(leg.recipient_name or ""))


def missing_mandatory_fields(leg: ParsedLeg | None) -> list[str]:
    """الحقول الإلزامية الغائبة عن الصفقة (لرسالة «ناقص: …» الديناميكية §تنبيهات). يفحص كل حقل
    على حدة بنفس ما يعتبره الفهم/الحارس إلزاميًّا لإكمال الصفقة (كود+اسم+سعر+خزينة). لا يمسّ منطق
    الـsweep — دالة عرض فقط. الاسم يُعدّ حاضرًا بـ customer_name أو recipient_name (§الصيغة التونسية)."""
    labels = ["كود الزبون", "الاسم", "السعر", "الخزينة"]
    if leg is None:
        return labels
    from ..matching.fuzzy import normalize_ar  # استيراد محليّ: تفادي دورة استيراد الحزمة
    missing: list[str] = []
    if not leg.customer_code:
        missing.append("كود الزبون")
    if not normalize_ar(leg.customer_name or "") and not normalize_ar(leg.recipient_name or ""):
        missing.append("الاسم")
    if not (leg.price_normalized or leg.price_raw):
        missing.append("السعر")
    if leg.treasury is None:
        missing.append("الخزينة")
    return missing


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
        رقمها الإشاري صفقةَ بيعٍ معلّقةً بلا طرف شراء في نفس الغرفة → تُستخرج بيانات المورد بـ
        **pattern-fishing** (متينة للترتيب المختلف: المورد آخر السطر «طه 5.93»)، ومبلغها الأصغر =
        بعد الخصم. يُرجع الصفقة المكتملة أو None (فتتبع الرسالة المسار العادي try_group).

        🔴 حالة الأولى **بلا كود زبون** (مستلم فقط مثل «نجوى» §7.3): تُمتَص أيضًا لكن **فقط** إن
        حمل الطرفُ الثاني موردًا **مُدرَجًا** بالقائمة البيضاء (frag.supplier) — لا مجرّد كود — كي
        لا يُخلَط كودُ إكمالِ الزبون بمورد. مع كود زبون قائم يكفي كود رقمي أو مورد (السلوك القائم)."""
        if not raw.chat_jid or not leg.reference_number:
            return None
        ref = _norm_ref(leg.reference_number)
        horizon = _as_naive_utc(now) - timedelta(seconds=SECOND_MESSAGE_LINK_SECONDS)
        target: Deal | None = None
        for deal in await self.db.deals.waiting_in_room(raw.chat_jid):
            sell = deal.sell_leg
            if (sell is not None and deal.buy_leg is None
                    and sell.supplier is None
                    and _norm_ref(sell.reference_number) == ref and ref
                    and _as_naive_utc(deal.created_at) >= horizon):
                target = deal
                break
        if target is None:
            return None
        # المورد بـ pattern-fishing (يعالج الترتيب المختلف للرسالة الثانية: الكود/المورد آخر السطر…)
        frag = parse_completion_fragment(raw.text, treasuries, suppliers)
        # 🔴 (الموضع يحدّد النوع — X1587) سطرُ «كود+اسم+سعر» في رسالةٍ ثانيةٍ بنفس المرجع هو
        #   **المورّد وسعره** قطعًا، لا زبونًا. وpattern-fishing يصطاد أرقامًا من حيث لا ينبغي:
        #   في «X1587 / +20 122 1225261 / نجوى / 1.485مصري / طه6.07» التقط الكود «122» من **مقطع
        #   رقم الهاتف** واسمَ **المستلم** «نجوى»، فأُسنِد المورّد «122 نجوى» بدل «760 طه ».
        #   extract_code_name_price_lines يُعطي الجواب الصحيح سلفًا (يُطابق الاسم بالقائمة البيضاء
        #   ويستكمل كوده)، فنُقدّمه هنا. حارس الالتباس: لا نعتمده إلّا إذا انحلّ سطرٌ **واحد**
        #   بالضبط إلى مورّد مُدرَج — التعدّد أو الصفر يُترك للسلوك القائم بلا تخمين (§0).
        if frag.supplier is None:
            resolved = []
            for code, name, price in extract_code_name_price_lines(raw.text, suppliers):
                rec = resolve_supplier(name, suppliers)
                if rec is not None:
                    resolved.append((rec, price))
            if len(resolved) == 1:
                rec, price = resolved[0]
                frag.supplier = SupplierRef(code=rec.code, name=rec.name)
                frag.is_supplier_counterpart = True
                if price:
                    frag.price_raw, frag.price_normalized = normalize_price(
                        price, self._leg_currency(frag) or target.sell_leg.currency or Currency.EGP)
                log.info("(الموضع يحدّد النوع) سطر المورّد في الرسالة الثانية %s → «%s %s» بسعر %s "
                         "(بدل «%s %s» من اصطياد النمط)", raw.message_key, rec.code, rec.name,
                         frag.price_raw, frag.customer_code, frag.customer_name)
        # هوية المورد = كود رقمي في الرسالة («760 طه») **أو** اسم مُدرَج بالقائمة البيضاء بلا كود
        # («طه» وحده → يُحلّ كودُه من القائمة).
        # 🔴 لو صفقة الوجهة **بلا كود زبون** (رسالة أولى بمستلم فقط مثل «نجوى») نشترط مورداً
        #    **مُدرَجًا** (frag.supplier) صراحةً — كي لا يُخلَط كودُ إكمالِ زبونٍ بمورد (§6). أمّا مع
        #    كود زبون قائم فيكفي كود رقمي أو مورد مُدرَج (السلوك القائم). بلا الاثنين → مسار عادي.
        if target.sell_leg.customer_code:
            if not frag.customer_code and frag.supplier is None:
                return None
        elif frag.supplier is None:
            # صفقة الوجهة **بلا كود زبون** + مورد غير مُدرَج: اقبله طرفَ مورد **فقط** بإشارة خصم
            #   قويّة — رسالة نفس الرقم بمبلغ **أقصر** + «اسم سعر» في آخر سطر (§6) — فيُلتقَط المورد
            #   بالاسم (كودُه قد يكون None → معلّق للإسناد اليدويّ من اللوحة §4.5) بدل صفقة مكرّرة.
            #   بلا هذه الإشارة → مسار عادي (لا تخمين، فلا يُخلَط كودُ إكمالِ زبونٍ بمورد §0).
            is_discount_supplier = bool(
                frag.customer_name and frag.price_raw
                and frag.amount is not None and target.sell_leg.amount is not None
                and frag.amount < target.sell_leg.amount
            )
            if not is_discount_supplier:
                return None
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
        # المورد = المُحلّ من القائمة البيضاء (بكوده الصحيح) إن وُجد، وإلّا الكود+الاسم من الرسالة
        # («760 طه» بلا إدراج). يمنع فقدان كود القائمة حين يأتي المورد بالاسم وحده («طه»).
        sell.supplier = frag.supplier or SupplierRef(code=frag.customer_code, name=frag.customer_name)
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
            deal.deal_id, sell.supplier.code, sell.supplier.name,
            sell.reference_number, sell.amount_after_discount,
        )
        return deal

    async def try_absorb_treasury_second(
        self, leg: ParsedLeg, raw: RawMessage, now: datetime,
    ) -> Deal | None:
        """رسالة ثانية بنفس الرقم الإشاري تحمل **خزينة فقط** (بلا كود/اسم زبون): تُكمِّل خزينة
        صفقة معلّقة مطابقة للرقم في نفس الغرفة (§7.3) — لا صفقة جديدة ولا هدرزة.

        الشرط: للرسالة رقم إشاري + خزينة محلولة + بلا هوية زبون (is_treasury_second_reply)، وتُطابق
        صفقة WAITING بنفس الرقم خزينتها غائبة ضمن نافذة الربط. المطابقة **بالرقم الإشاري** (لا
        ref+phone) فتُدمَج حتى لو اختلف الهاتف بين الرسالتين. يُرجع الصفقة المكتملة أو None.

        🔴 لو شكّلت الرسالة والصفقة **زوج خصم** (Aخصم §6.3: هوية بمبلغ قبل + تسوية بمبلغ بعد بنفس
        الرقم) → تُدمَج كخصم (_merge_discount) لا كخزينة عادية، فلا يضيع المبلغ بعد الخصم ولا العمولة.
        (Fix 2 §7.3) لو وصلت خزينة-فقط (بلا مبلغ) قبل الأولى (لا صفقة معلّقة) → تُحفَظ ردًّا معلّقًا 90s
        (PENDING_REPLY_MAX_SECONDS) لتُربَط تلقائيًّا عند إنشاء الأولى — فالترتيب لا يهمّ."""
        if not raw.chat_jid or not is_treasury_second_reply(leg):
            return None
        ref = _norm_ref(leg.reference_number)
        if not ref:
            return None
        if not leg.source_message_key:
            leg.source_message_key = raw.message_key
        horizon = _as_naive_utc(now) - timedelta(seconds=SECOND_MESSAGE_LINK_SECONDS)
        for deal in await self.db.deals.waiting_in_room(raw.chat_jid):
            sell = deal.sell_leg
            if (sell is not None and sell.treasury is None
                    and _norm_ref(sell.reference_number) == ref
                    and _as_naive_utc(deal.created_at) >= horizon):
                pair = discount_pair(sell, leg)          # صيغة خصم بنفس الرقم (Aخصم §6.3)؟
                if pair is not None:
                    return await self._merge_discount(deal, pair[0], pair[1], now)
                sell.treasury = leg.treasury             # خزينة عادية: أكمل الخزينة فقط
                deal.status = Status.PARSED
                deal.waiting_deadline = None
                if raw.message_key and raw.message_key not in deal.source_message_keys:
                    deal.source_message_keys.append(raw.message_key)
                deal.updated_at = now
                await self.db.deals.upsert(deal)
                log.info("صفقة %s: رسالة ثانية بخزينة «%s» (نفس الرقم %s) → أُكملت الخزينة",
                         deal.deal_id, leg.treasury.name, ref)
                return deal
        # لا صفقة معلّقة بنفس الرقم — رسالة ثانية بمرجع صريح وصلت قبل أُولاها → احفظها ردًّا معلّقًا
        #   **بمرجعها** ريثما تصل الأولى (Fix 1ب). يشمل الآن **تسوية الخصم** (تحمل مبلغًا بعد الخصم)،
        #   لا الخزينة-فقط فحسب — كانت تسوية الخصم تسقط سابقًا فتصير صفقة headless تبتلع هويةً أجنبية
        #   (حادثة A9078). المفتاح مضبوط (سطر 384-385)؛ عند وصول الأولى يسحبها _pull_pending_reply
        #   بالمرجع فيُحتسَب الخصم. إن لم تصل خلال المهلة → تصعيد ⚠️ (sweep_expired) لا هدرزة صامتة.
        await self.db.pending_replies.add(
            message_key=raw.message_key, chat_jid=raw.chat_jid, leg=leg, received_at=now,
        )
        log.info("رسالة ثانية بمرجع %s (خزينة/تسوية) بلا صفقة معلّقة — حُفِظت ردًّا معلّقًا (%ss، تُربَط بأُولاها)",
                 ref, PENDING_REPLY_MAX_SECONDS)
        return None

    async def waiting_candidates_for_second(
        self, chat_jid: str | None, now: datetime,
    ) -> list[Deal]:
        """صفقات معلّقة (WAITING) في الغرفة خلال نافذة الربط (120s)، لها طرف بيع بلا مورد ولا
        طرف شراء — مرشّحة لرسالة ثانية بلا رقم إشاري (§7.3). تعدّدها = التباس → يُصعَّد في الأنبوب."""
        if not chat_jid:
            return []
        horizon = _as_naive_utc(now) - timedelta(seconds=SECOND_MESSAGE_LINK_SECONDS)
        return [
            d for d in await self.db.deals.waiting_in_room(chat_jid)
            if d.sell_leg is not None and d.buy_leg is None and d.sell_leg.supplier is None
            and _as_naive_utc(d.created_at) >= horizon
        ]

    async def absorb_customer_supplier(
        self, deal: Deal, customer: tuple, supplier: tuple, message_key: str,
        treasuries: list[TreasuryRecord], now: datetime,
    ) -> Deal:
        """رسالة ثانية بلا رقم إشاري بسطرين «كود+اسم+سعر»: السطر ١ (زبون) **يكمل** بيانات صفقة
        البيع الناقصة (كود/اسم/سعر — لا يُنشئ زبونًا)، والسطر ٢ (مورد) يُدمَج كطرف مورد (§7.3)."""
        ccode, cname, cprice = customer
        sell = deal.sell_leg
        # (١) السطر الأول يكمل الناقص فقط (لا يستبدل زبونًا موجودًا)
        if not sell.customer_code and ccode:
            sell.customer_code = ccode
        if not sell.customer_name and cname:
            sell.customer_name = cname
        if not sell.price_normalized and cprice:
            _raw, pnorm = normalize_price(cprice, sell.currency or Currency.EGP)
            sell.price_raw, sell.price_normalized = cprice, pnorm
        # (٢) السطر الثاني = المورد → نفس منطق طرف المورد (خزينة «فودافون بالخصم» 85 + اشتقاق الشراء)
        scode, sname, sprice = supplier
        frag = ParsedLeg(operation=OperationType.SELL, customer_code=scode,
                         customer_name=sname, price_raw=sprice)
        return await self._absorb_supplier_leg(deal, frag, message_key, treasuries, now)

    async def absorb_second_into(
        self, deal: Deal, raw: RawMessage, now: datetime,
        treasuries: list[TreasuryRecord], suppliers: list[SupplierRecord],
    ) -> tuple[Deal, bool] | None:
        """يدمج الرسالة الثانية `raw` في صفقة معلّقة **محدّدة** `deal` اختارتها خانة المُرسِل (§7.3)
        — يعيد استخدام مكانيكا الدمج القائمة حسب شكل الجزء، بلا مطابقة ref/عملة/تجاور (الخانة
        حسمت الاختيار حتمًا). يُرجع `(الصفقة، هل تُعالَج فورًا)` أو None إن لم يُطابِق الجزء أيّ شكل
        إكمال (فيسقط للمسار القديم fallback: try_absorb_*/الردود المعلّقة).

        🔴 قرار «فورًا» يطابق المسار القديم بدقّة: سطرَا كود+اسم+سعر والخزينة-بنفس-الرقم تُعالَج
        فورًا (كما كان try_absorb_*)، أمّا الجزء المكمّل («بلس»/«صافي») فيُربَط ويبقى PARSED لتُعالِجه
        النبضة (كما كان absorb_fragment لا يستدعي process_deal)."""
        text = raw.text or ""
        # (١) سطرَا «كود+اسم+سعر» (زبون + مورد) — يكمل الهوية ويدمج المورد (§7.3) → فورًا
        pairs = extract_code_name_price_lines(text, suppliers)
        if len(pairs) >= 2:
            merged = await self.absorb_customer_supplier(
                deal, pairs[0], pairs[1], raw.message_key, treasuries, now)
            return merged, True
        result = parse_message(text, treasuries, suppliers)
        leg = result.leg
        # (٢) «رقم إشاري + خزينة» بلا هوية — خزينة عادية أو تسوية خصم (Aخصم §6.3) → فورًا
        if leg is not None and is_treasury_second_reply(leg):
            sell = deal.sell_leg
            if sell is not None:
                # 🔴 (إصلاح ٢) سجّل مفتاح الرسالة الثانية قبل الدمج — كي يضيفه _merge_discount إلى
                #    source_message_keys. بدونه تفقد كل حوالة خصم تكتمل عبر خانة المُرسِل مفتاح رسالتها
                #    الثانية، فيفشل إلغاؤها/تعديلها بالرد عليها بـ«لم يُعثر» (A9030/A9062/A9079).
                if not leg.source_message_key:
                    leg.source_message_key = raw.message_key
                pair = discount_pair(sell, leg)
                if pair is not None:
                    merged = await self._merge_discount(deal, pair[0], pair[1], now)
                    return merged, True
                if sell.treasury is None:
                    sell.treasury = leg.treasury
                    deal.status = Status.PARSED
                    deal.waiting_deadline = None
                    if raw.message_key and raw.message_key not in deal.source_message_keys:
                        deal.source_message_keys.append(raw.message_key)
                    deal.updated_at = now
                    await self.db.deals.upsert(deal)
                    log.info("خانة المُرسِل: أُكملت خزينة «%s» للصفقة %s",
                             leg.treasury.name, deal.deal_id)
                    return deal, True
        # (٣) جزء مكمّل («بلس»/«صافي»/مورد بلا ref) — نفس مكانيكا absorb_fragment (_apply_fragment)،
        #     يُربَط ويبقى PARSED (تُعالِجه النبضة، لا فورًا) → يطابق السلوك القديم تمامًا.
        frag = parse_completion_fragment(text, treasuries, suppliers)
        frag.sender_jid = raw.sender_jid
        frag.source_message_key = raw.message_key
        if is_completion_fragment(frag):
            self._apply_fragment(deal, frag)
            deal.status = Status.PARSED
            deal.waiting_deadline = None
            if raw.message_key and raw.message_key not in deal.source_message_keys:
                deal.source_message_keys.append(raw.message_key)
            deal.updated_at = now
            await self.db.deals.upsert(deal)
            log.info("خانة المُرسِل: رُبط الجزء المكمّل بالصفقة %s (PARSED — تُعالِجه النبضة)",
                     deal.deal_id)
            return deal, False
        # (٤) 🔴 رسالة ثانية بهوية زبون واضحة (كود+اسم، بلا مبلغ/مرجع) **مع رمز خزينة لم يُحلّ**
        #     (سطر كلمة عربية واحدة غير مطابِق لأي خزينة — إملاء غريب مثل «محموذ»→«محمود صفاقس»؛
        #     الخزائن تُحلّ تامًّا §0). سابقًا تُسقَط صامتةً فتبقى الأولى معلّقةً وتسرق ثانيةَ التالية
        #     (انزياح FIFO §7.3، حادثة X850–X853). الآن: تُربَط الهويةُ بهدفها (فتخرج من طابور
        #     المرشّحين)، وتبقى الخزينة None فتُصعَّد لاحقًا «لا خزينة محلولة» لا تُسقَط، ويُلتقَط الرمز.
        #     🔴 القيد برمزٍ مجهولٍ صريح يمنع ابتلاع أسطر مبهمة (سطرَا سعر مثل «… 34.5 / النور 35.75»
        #     التي يُفترَض ألا تُحلَّ — golden shape_04#3).
        unresolved = _unresolved_treasury_tokens(text, treasuries)
        target_leg = deal.sell_leg or deal.buy_leg
        if (unresolved and frag.customer_code and (frag.customer_name or "").strip()
                and frag.amount is None and not frag.reference_number
                and target_leg is not None and not target_leg.customer_code):
            self._apply_fragment(deal, frag)          # يكمل كود/اسم/سعر؛ الخزينة تبقى None
            for tok in unresolved:
                await self.db.unknown_terms.record(tok, "treasury")
            deal.status = Status.PARSED
            deal.waiting_deadline = None
            if raw.message_key and raw.message_key not in deal.source_message_keys:
                deal.source_message_keys.append(raw.message_key)
            deal.updated_at = now
            await self.db.deals.upsert(deal)
            log.warning("خانة المُرسِل: رُبطت هوية «%s %s» بالصفقة %s لكن الخزينة «%s» غير محلولة "
                        "— تُصعَّد لاحقًا لا تُسقَط (§7.3)",
                        frag.customer_code, frag.customer_name, deal.deal_id, "، ".join(unresolved))
            return deal, False
        return None

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
        self, frag: ParsedLeg, chat_jid: str | None, message_key: str, now: datetime,
        text_ref: str | None = None,
    ) -> Deal | None:
        """
        رد خزينة/مورد بلا رقم إشاري («بلس»/«صافي» وحدها).

        (Fix 1) يصل بعد الحوالة: يُربَط بأقرب صفقة معلّقة في نفس الغرفة خلال دقيقتين
                (SECOND_MESSAGE_LINK_SECONDS) → تكتمل الصفقة وتمشي.
        (Fix 2) يصل قبل الحوالة: لا صفقة معلّقة → يُحفَظ «ردًّا معلّقًا» حتى 90s
                (PENDING_REPLY_MAX_SECONDS) ريثما تصل الأولى.

        `text_ref` (R1): مرجع صريح من **النصّ الخام** — يُلزِم الاختيار ويمنع FIFO.
        """
        frag.source_message_key = message_key
        deal = await self._find_recent_waiting(chat_jid, now, frag, text_ref,
                                               message_key=message_key)
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
        # (R1) وسمُه بمرجعه الصريح إن وُجد، ليطالبه _pull_pending_reply لاحقًا **بالمرجع** لا
        #      بـFIFO حين تصل صفقته. لا يُوسَم إلّا على هذا المسار (بلا هدف) — الوسم على مسار
        #      الالتصاق يُبطل شروط «غياب المرجع» في is_completion_fragment وغيرها.
        if text_ref and not frag.reference_number:
            frag.reference_number = text_ref
        await self.db.pending_replies.add(
            message_key=message_key, chat_jid=chat_jid or "", leg=frag, received_at=now,
        )
        log.info("رد خزينة/مورد %s بلا صفقة معلّقة — حُفِظ ردًّا معلّقًا (%ss)",
                 message_key, PENDING_REPLY_MAX_SECONDS)
        return None

    @staticmethod
    def _leg_currency(leg: ParsedLeg | None) -> Currency | None:
        """عملة الطرف: الصريحة إن وُجدت، وإلّا عملة خزينته (§4.1)."""
        if leg is None:
            return None
        if leg.currency is not None:
            return leg.currency
        return leg.treasury.currency if leg.treasury is not None else None

    @staticmethod
    def _currency_from_price(price_raw: str | None) -> Currency | None:
        """يستنبط عملة الرسالة الثانية من **السعر** حين لا خزينة/عملة صريحة (§3.6 §4.1): السعر
        التونسي ~0.xx (يُكتب «35»→0.35 أو «0.35») → TND؛ المصري ~5-6 («5.90») → EGP. يمنع ربط
        رسالة ثانية تونسية (سعر 35) بصفقة مصرية عند تعدّد المعلّقات (تلوّث العملة/الخزينة)."""
        if not price_raw:
            return None
        try:
            v = float(str(price_raw).replace("،", ".").replace(",", "."))
        except (ValueError, TypeError):
            return None
        if v <= 0:
            return None
        if v < 1 or v >= 10:          # «0.35» أو «35» → سعر تونسي (0.xx بعد التطبيع)
            return Currency.TND
        return Currency.EGP           # «5.90» (1 ≤ v < 10) → سعر مصري

    @classmethod
    def _currency_compatible(cls, deal: Deal, frag_cur: Currency | None) -> bool:
        """توافق عملة الرد مع الصفقة: رد بلا عملة → بلا تقييد؛ وإلّا يجب تطابق العملتين (§4.1)."""
        if frag_cur is None:
            return True
        deal_cur = cls._leg_currency(deal.sell_leg or deal.buy_leg)
        return deal_cur is None or deal_cur == frag_cur

    @staticmethod
    def _leg_sender(deal: Deal) -> str | None:
        """مُرسِل الصفقة (sender_jid لطرف البيع/الشراء) — لربط الرد بنفس المُرسِل (§7.3)."""
        leg = deal.sell_leg or deal.buy_leg
        return leg.sender_jid if leg is not None else None

    async def fragment_link_candidates(
        self, chat_jid: str | None, now: datetime, frag: ParsedLeg | None = None,
        text_ref: str | None = None, *, message_key: str | None = None,
    ) -> list[Deal]:
        """المرشّحون لربط الرسالة الثانية `frag` بصفقة معلّقة في نفس الغرفة، **بعد كل المميّزات**
        (§7.3، §0):

        (١) **رقم إشاري صريح** في الرسالة الثانية → يُطابَق **بالـref حصرًا**.
        (٢) بلا ref → تُصفّى بنفس **المُرسِل** (sender_jid) إن وُجد مطابق، ثم تبقى **العملة**.

        🔴 تُستبعَد الصفقة: مُصعَّدة (ESCALATED)، أو أقدم من 15د (INCOMPLETE_DATA_ESCALATE_SECONDS)،
        أو خارج نافذة الربط (SECOND_MESSAGE_LINK_SECONDS) — فرد قديم/مُصعَّد لا يُعاد ربطه فيُدخِل
        حوالة خاطئة (حادثة A8667: 258 → 996). 🔴 شرط العملة (§4.1): تطابق العملتين.

        تعدّد الناتج (>1) = **التباس** → يُصعّده الأنبوب بلا تخمين؛ الناتج الوحيد = ربط آمن."""
        if not chat_jid:
            return []
        # عملة الرد: الصريحة/خزينته، وإلّا **من سعره** (35→TND، 5.90→EGP §3.6) — كي لا تُربَط
        # رسالة ثانية تونسية بصفقة مصرية (تلوّث العملة) عند تعدّد المعلّقات المتزامنة.
        frag_cur = self._leg_currency(frag) or (
            self._currency_from_price(frag.price_raw) if frag is not None else None
        )
        horizon = _as_naive_utc(now) - timedelta(seconds=SECOND_MESSAGE_LINK_SECONDS)
        # حارس العمر الأقصى (15د): زائد فعليًّا فوق horizon الأضيق (120s)، لكن نُصرّح به صراحةً
        # كي يبقى الثابت صحيحًا لو وُسّعت النافذة مستقبلًا (لا يُربَط رد بصفقة أقدم من 15د).
        max_age = _as_naive_utc(now) - timedelta(seconds=INCOMPLETE_DATA_ESCALATE_SECONDS)
        candidates = [
            d for d in await self.db.deals.waiting_in_room(chat_jid)
            if self._needs_completion(d)
            and d.status != Status.ESCALATED                       # لا ربط بصفقة مُصعَّدة (§0)
            and _as_naive_utc(d.created_at) >= horizon             # داخل نافذة الربط (120s)
            and _as_naive_utc(d.created_at) >= max_age             # وليست أقدم من 15د
            and self._currency_compatible(d, frag_cur)
        ]
        if not candidates:
            return []
        # (الطبقة ١) رقم إشاري صريح → طابِق بالـref حصرًا (يشمل تسوية الخصم لصفقة ذات هوية بلا خزينة).
        # (R1) المصدر: `text_ref` من **النصّ الخام** أوّلًا — فلا يُسقِط فشلُ التفكيك المرجعَ معه
        #      (حادثة X1567). ثمّ مرجع الطرف المفكَّك. النتيجة مُلزِمة: لا مطابقة ⇒ [] ⇒ لا ربط.
        bound = bind_by_reference(
            candidates, text_ref or (frag.reference_number if frag is not None else None))
        if bound is not None:
            matched, reason = bound
            _log_link_decision("fragment_candidates", message_key, candidates,
                               matched[0] if matched else None, reason, text_ref)
            return matched
        # (الطبقة ٢) بلا ref: الرسالة الثانية تحمل الهوية → تخصّ صفقةً **ناقصة الهوية** فقط (لا صفقة
        #   ذات هوية تنتظر تسوية مرجعها). ثم نفس المُرسِل يميّز إن وُجد، والترتيب تصاعديّ (FIFO الأقدم).
        awaiting = [d for d in candidates if self._fragment_targets(d, frag)]
        sender = frag.sender_jid if frag is not None else None
        if sender:
            same_sender = [d for d in awaiting if self._leg_sender(d) == sender]
            if same_sender:
                _log_link_decision("fragment_candidates", message_key, same_sender,
                                   same_sender[0], "fifo-pool-same-sender")
                return same_sender
        _log_link_decision("fragment_candidates", message_key, awaiting,
                           awaiting[0] if awaiting else None, "fifo-pool")
        return awaiting

    async def _find_recent_waiting(
        self, chat_jid: str | None, now: datetime, frag: ParsedLeg | None = None,
        text_ref: str | None = None, *, message_key: str | None = None,
    ) -> Deal | None:
        """الصفقة المطابِقة للرسالة الثانية (§7.3 — تصميم حتميّ بطبقتين، بلا تجاور ولا تصعيد):

        • **الطبقة ١ (مرجع صريح):** الرسالة الثانية فيها ref → الصفقة ذات المرجع نفسه (fragment_link_candidates
          يُصفّي بالـref حصرًا)، بلا اعتبار للقرب/المُرسِل.
        • **الطبقة ٢ (FIFO):** بلا ref → **أقدم** صفقة مطابِقة (عملة + مُرسِل) لنفس المُرسِل — أول فتح أول
          قفل، يعكس عمل الموظف (رسالتان متتاليتان ثم ينتقل). لا التباس→None: التعدّد يُحسَم بالأقدم.

        fragment_link_candidates تُرجِع المرشّحين مرتّبين بـcreated_at تصاعديًّا (waiting_in_room)،
        فأوّلهم = الأقدم = هدف FIFO الصحيح. (استُبدلت آلية التجاور بالكامل بـFIFO — أحسم لنفس الهدف §0.)"""
        cands = await self.fragment_link_candidates(chat_jid, now, frag, text_ref,
                                                    message_key=message_key)
        return cands[0] if cands else None

    async def waiting_by_reference(self, chat_jid: str | None, ref: str | None,
                                   now: datetime) -> Deal | None:
        """الطبقة ١ (مرجع صريح §7.3): أقدم صفقة منتظِرة تحتاج إكمالاً بنفس الرقم الإشاري — بلا أي
        اعتبار للقرب/المُرسِل/الزمن. حسم فوريّ للشكل الجديد (المرجع مكرّر في الرسالتين)."""
        nref = _norm_ref(ref)
        if not chat_jid or not nref:
            return None
        max_age = _as_naive_utc(now) - timedelta(seconds=INCOMPLETE_DATA_ESCALATE_SECONDS)
        for d in await self.db.deals.waiting_in_room(chat_jid):   # مرتّبة created_at تصاعديًّا
            if not self._needs_completion(d) or _as_naive_utc(d.created_at) < max_age:
                continue
            leg = d.sell_leg or d.buy_leg
            if leg is not None and _norm_ref(leg.reference_number) == nref:
                return d
        return None

    async def oldest_waiting_for_sender(self, chat_jid: str | None, sender_jid: str | None,
                                        now: datetime, frag: ParsedLeg | None = None,
                                        text_ref: str | None = None, *,
                                        message_key: str | None = None) -> Deal | None:
        """الطبقة ٢ (FIFO §7.3): **أقدم** صفقة منتظِرة لنفس المُرسِل هي هدف صالح للرسالة الثانية `frag`
        (حسب _fragment_targets) ضمن نافذة الربط — أول فتح أول قفل. حتميّ بلا قرب/تجاور/تصعيد
        (يعكس عمل الموظف: رسالتان متتاليتان ثم التالية).

        (R1) يسبقها الحارس: مرجعٌ صريح ⇒ مطابقته **حصرًا**، ولا سقوط لـFIFO عند عدم المطابقة."""
        if not chat_jid or not sender_jid:
            return None
        horizon = _as_naive_utc(now) - timedelta(seconds=SECOND_MESSAGE_LINK_SECONDS)
        pool = [d for d in await self.db.deals.waiting_in_room(chat_jid)   # ASC = FIFO (الأقدم أولًا)
                if _as_naive_utc(d.created_at) >= horizon]
        # (R1) المرجع الصريح يعلو على المُرسِل والشكل معًا — كـwaiting_by_reference تمامًا.
        bound = bind_by_reference(pool, text_ref)
        if bound is not None:
            matched, reason = bound
            chosen = matched[0] if matched else None
            _log_link_decision("oldest_waiting_for_sender", message_key, pool, chosen, reason, text_ref)
            return chosen
        # 🔴 حارس العملة (تلوّث X1624، 2026-07-21): صفقةٌ تونسيّة (2500 د.ت) ابتلعت تكملةً
        #   مصريّة «131 الساعدي 6.02 / طه6.07» لأنّ هذا المسار كان يُصفّي بالشكل والمُرسِل فقط
        #   بلا عملة — بخلاف fragment_link_candidates. عملة التكملة من سعرها (6.02→EGP، 34→TND).
        #   incompatible ⇒ تُرفَض فتذهب لصفقةٍ بعملتها، وتلتقط الأولى تكملتها الصحيحة.
        frag_cur = self._leg_currency(frag) or (
            self._currency_from_price(frag.price_raw) if frag is not None else None)
        chosen = next((d for d in pool
                       if self._fragment_targets(d, frag) and self._leg_sender(d) == sender_jid
                       and self._currency_compatible(d, frag_cur)), None)
        _log_link_decision("oldest_waiting_for_sender", message_key, pool, chosen, "fifo-fallback")
        return chosen

    async def pending_candidates_for_sender(self, chat_jid: str | None, sender_jid: str | None,
                                            now: datetime, text_ref: str | None = None, *,
                                            frag: ParsedLeg | None = None,
                                            message_key: str | None = None) -> list[Deal]:
        """(البند 1، الربط أولاً) الصفقات المعلّقة لنفس المُرسِل ضمن نافذة الربط، **مرتّبة FIFO** —
        **بلا** تقييد نوع المحتوى (بخلاف oldest_waiting_for_sender الذي يشترط _fragment_targets).

        تُرجِع **القائمة** لا الأقدم وحدها، كي يحكّمها الأنبوب بالذكاء عند غياب المرجع (R2) بدل
        FIFO عمياء — حادثة X1571 التي سُرقت تكملتُها لصالح X1568 بلا أيّ فحص محتوى.
        (R1) مرجعٌ صريح ⇒ المصفّاة بالمرجع حصرًا؛ [] تعني **رفض ربط** لا «لا مرشّحين»."""
        if not chat_jid or not sender_jid:
            return []
        horizon = _as_naive_utc(now) - timedelta(seconds=SECOND_MESSAGE_LINK_SECONDS)
        pool = [d for d in await self.db.deals.waiting_in_room(chat_jid)   # ASC = FIFO
                if _as_naive_utc(d.created_at) >= horizon and self._leg_sender(d) == sender_jid]
        bound = bind_by_reference(pool, text_ref)
        if bound is not None:
            matched, reason = bound
            _log_link_decision("pending_for_sender", message_key, pool,
                               matched[0] if matched else None, reason, text_ref)
            return matched
        # 🔴 مرشِّح المحتوى الحتميّ **قبل** الذكاء وقبل FIFO (انحدار الدفعات 2026-07-20):
        #   كان هذا المسار متساهلًا عمدًا («بلا تقييد نوع المحتوى») كي لا تسقط تكملةٌ صامتةً —
        #   وهو سليمٌ حين تكون المعلّقة واحدة. لكن في دفعةٍ عميقة صار كلّ ردٍّ عديم‑مرجع يهبط
        #   على **أقدم** معلّقة أيًّا كان محتواه: «1277 شركة القت» (تحمل كودًا) هبطت على X1566
        #   وكودها 755 محجوزٌ سلفًا، بينما X1568 بلا كود هي صاحبتها الحقيقية.
        #   _fragment_targets يعرف القاعدة أصلًا: ردٌّ يحمل كودًا يخصّ صفقةً **بلا كود**.
        #   يُطبَّق تفضيلًا لا إقصاءً: إن لم يوافق أحدٌ نعود للمجموعة كاملةً، فتبقى ضمانة
        #   «لا سقوط صامت» قائمةً كما كانت.
        # 🔴 استبعادُ المولودة من إعادة استخدام مرجع (X1242) — حتميّ، بلا مرجع صريح فقط.
        #   تلك صفقةٌ نشأت من رسالةٍ **ثانية** انفصلت عن أُولاها، فمحتواها كاملٌ سلفًا ولا
        #   تنتظر تكملةً عديمة المرجع. جلوسها في الطابور يكسر تقابل ١:١ الذي تفترضه FIFO
        #   فتنزاح كلّ الروابط بواحد (X850‑X853، ودفعة 2026-07-20: k8 هبطت على X1566 وصاحبها
        #   X1568). المرجع الصريح ما زال يبلغها فوق هذا الاستبعاد عبر bind_by_reference أعلاه.
        #   إن أفرغ الاستبعادُ المجموعةَ، تُحفَظ الرسالة ردًّا معلّقًا ثمّ تُصعَّد ⚠️ — ظهورٌ
        #   صريح لا سقوط صامت (§0).
        # 🔴 حارس العملة (تلوّث X1624، 2026-07-21): تُستبعَد الصفقات المخالفة لعملة التكملة قبل
        #   أيّ ترشيح — تونسيّة لا تبتلع مصريّة والعكس. عملة التكملة من سعرها (6.02→EGP، 34→TND)؛
        #   None (مجهولة) ⇒ متوافقة (لا رفض كاذب).
        frag_cur = self._leg_currency(frag) or (
            self._currency_from_price(frag.price_raw) if frag is not None else None)
        eligible = [d for d in pool
                    if not d.born_from_ref_reuse and self._currency_compatible(d, frag_cur)]
        if len(eligible) != len(pool):
            _log_link_decision("pending_for_sender", message_key, pool, None,
                               f"excluded(reuse/currency):{len(pool) - len(eligible)}")
        targeted = [d for d in eligible if self._fragment_targets(d, frag)] if frag is not None else []
        if targeted:
            _log_link_decision("pending_for_sender", message_key, targeted,
                               targeted[0], "content-targeted")
            return targeted
        pool = eligible
        _log_link_decision("pending_for_sender", message_key, pool,
                           pool[0] if pool else None, "fifo-pool")
        return pool

    async def oldest_pending_for_sender(self, chat_jid: str | None, sender_jid: str | None,
                                        now: datetime, text_ref: str | None = None) -> Deal | None:
        """**أقدم** صفقة معلّقة لنفس المُرسِل (FIFO حتميّ — درس X850: سقوط/تعثّر وحدة لا يزيح الباقي).
        غلافٌ رفيع فوق pending_candidates_for_sender يحفظ التوقيع القائم."""
        cands = await self.pending_candidates_for_sender(chat_jid, sender_jid, now, text_ref)
        return cands[0] if cands else None

    @staticmethod
    def is_completion_shaped(frag: ParsedLeg | None) -> bool:
        """هل الرسالة **بشكل تكملة** (لا هدرزة/دردشة) حتى لو فشل حلّ اسمها؟ تحمل سعرًا أو خزينة/موردًا
        (محلولًا أو غير محلول) أو كودًا/اسمًا. تمنع ربط «شكرًا» بصفقة معلّقة (البند 1: نربط التكملات لا الدردشة)."""
        if frag is None:
            return False
        return bool(frag.price_normalized or frag.price_raw or frag.treasury is not None
                    or frag.supplier is not None or frag.is_supplier_counterpart
                    or frag.customer_code or (frag.customer_name or "").strip()
                    or frag.unresolved_treasury)

    async def apply_orphan_completion(self, target: Deal, frag: ParsedLeg,
                                      sender_jid: str | None, message_key: str,
                                      now: datetime) -> Deal:
        """يطبّق تكملةً على صفقةٍ **مختارةٍ سلفًا** ويُعيدها PARSED. فُصِل عن اختيار الهدف كي يتولّى
        الأنبوب التحكيم (مرجع مُلزِم / ذكاء / FIFO) ثمّ يستدعي التطبيق."""
        frag.sender_jid = sender_jid
        frag.source_message_key = message_key
        self._apply_fragment(target, frag)          # يطبّق الخزينة/المورد/الكود/السعر المحلول (إن وُجد)
        if message_key and message_key not in target.source_message_keys:
            target.source_message_keys.append(message_key)
        target.status = Status.PARSED
        target.waiting_deadline = None
        await self.db.deals.upsert(target)
        return target

    async def link_orphan_completion(self, frag: ParsedLeg, chat_jid: str | None,
                                     sender_jid: str | None, message_key: str,
                                     now: datetime, text_ref: str | None = None) -> Deal | None:
        """(البند 1) يربط رسالة **بشكل تكملة قد يفشل حلّ اسمها** بصفقة معلّقة لنفس المُرسِل — قبل
        وبغضّ النظر عن نجاح الحل. يطبّق ما انحلّ عبر _apply_fragment ويُعيدها PARSED (تُعالَج بالنبضة:
        مطابقة/كتابة إن اكتملت، أو تصعيد غنيّ إن بقيت ناقصة). None إن لا صفقة معلّقة (تُترك للمسار العادي).
        لا تمسّ حوالةً أولى مستقلّة (تُصنَّف transfer لا noise فلا تصل هنا).

        (R1) `text_ref` مُلزِم: مرجعٌ صريح بلا صفقة مطابقة ⇒ None (رفض ربط) لا أقدمَ معلّقة."""
        if not self.is_completion_shaped(frag):
            return None
        cands = await self.pending_candidates_for_sender(
            chat_jid, sender_jid, now, text_ref, frag=frag, message_key=message_key)
        if not cands:
            return None
        target = await self.apply_orphan_completion(cands[0], frag, sender_jid, message_key, now)
        _log_link_decision("orphan_completion", message_key, cands, target,
                           "ref-exact" if text_ref else "fifo-fallback", text_ref)
        log.info("(البند 1) رُبطت رسالة تكملة %s بالصفقة المعلّقة %s", message_key, target.deal_id)
        return target

    async def _pull_pending_reply(self, deal: Deal, chat_jid: str | None, now: datetime) -> bool:
        """رد معلّق سابق (الرسالة الثانية وصلت قبل الأولى) يُكمِّل هذه الصفقة الجديدة (Fix 2 + تصميم FIFO).
        يُرجع True إن اكتملت. القواعد الحتمية:
          • **الطبقة ١:** صفقة ذات مرجع → تسحب فقط ردًّا معلّقًا **مطابقًا لمرجعها** (لا رد لمرجع مختلف).
          • **الطبقة ٢:** صفقة ناقصة الهوية (is_incomplete) بلا مرجع مطابق → تسحب **أقدم** ردّ عديم‑مرجع
            لنفس المُرسِل (FIFO). صفقة **ذات هوية** (كود) بلا خزينة لا تسحب عديم‑المرجع إطلاقًا —
            تنتظر تسوية مرجعها (يُصلح A9015: تنتظر msg2 فيُحتَسَب الخصم عبر discount_pair)."""
        if not chat_jid:
            return False
        leg0 = deal.sell_leg or deal.buy_leg
        ref = _norm_ref(leg0.reference_number) if leg0 is not None else ""
        sender = self._leg_sender(deal)
        # الطبقة ١: ردّ معلّق بمرجع الصفقة نفسه (أولوية قصوى، بلا قرب) — **استحواذ ذرّي** (claim)
        #   يمنع أخذ صفقتين لنفس الرد تحت الضغط (Fix 1أ). فشل الحجز = محجوز لأخرى → لا يُعتبر «موجودًا».
        doc = await self.db.pending_replies.claim_by_reference(
            chat_jid, ref, now, PENDING_REPLY_MAX_SECONDS) if ref else None
        via_ref = doc is not None
        # (R3) قرار المطالبة يُسجَّل بسببه — كان هذا المسار كلّه بلا سطر لوق واحد، كنظيره في الربط.
        log.info("[pending] stage=claim_by_ref deal=%s ref=%s sender=%s → %s",
                 deal.deal_id[:8], ref or "-", sender or "-",
                 (doc.get("message_key") if doc else "لا مطابق"))
        # الطبقة ٢: ردّ معلّق عديم‑مرجع لنفس المُرسِل (FIFO، استحواذ ذرّي) — يُقبَل فقط إن كان هدفًا
        #   صالحًا للصفقة (frag يحمل هوية → لصفقة بلا كود؛ خزينة فقط → لصفقة تنتظر خزينة). غير ذلك يُطلَق سراحه.
        if doc is None:
            cand = await self.db.pending_replies.claim_fifo_for_sender(
                chat_jid, sender, now, PENDING_REPLY_MAX_SECONDS)
            if cand is not None:
                targets = self._fragment_targets(deal, ParsedLeg(**cand["leg"]))
                log.info("[pending] stage=claim_fifo deal=%s sender=%s cand=%s targets=%s",
                         deal.deal_id[:8], sender or "-", cand.get("message_key"), targets)
                if targets:
                    doc = cand
                else:
                    await self.db.pending_replies.release(cand.get("message_key"))
            else:
                log.info("[pending] stage=claim_fifo deal=%s sender=%s → لا مرشَّح",
                         deal.deal_id[:8], sender or "-")
        if doc is None:
            return False
        frag = ParsedLeg(**doc["leg"])
        mk = doc.get("message_key")
        if mk and not frag.source_message_key:
            frag.source_message_key = mk
        # 🔴 شرط العملة (§4.1): رد معلّق بعملة معروفة (من خزينته أو **سعره** 35→TND/5.90→EGP) لا
        #    يُسحَب إلى صفقة بعملة مختلفة — يمنع تلوّث حادثة A8755(TND)↔A8756(EGP). فشل التوافق بعد
        #    الحجز الذرّي → إطلاق سراح الرد (يبقى متاحًا لصفقة أصحّ) بدل ابتلاعه/ضياعه.
        frag_cur = self._leg_currency(frag) or self._currency_from_price(frag.price_raw)
        if not self._currency_compatible(deal, frag_cur):
            await self.db.pending_replies.release(mk)
            return False
        # 🔴 تسوية خصم بمرجع صريح (شكل جديد A9078): الرد المعلّق يحمل خزينة + مبلغ بعد الخصم بنفس مرجع
        #    الصفقة ذات الهوية → يُدمَج خصمًا (discount_pair→_merge_discount) فيُحتسَب المبلغ بعد الخصم
        #    والعمولة — لا مجرّد إكمال خزينة (_apply_fragment) الذي كان يُضيّع الخصم (Fix 1ب).
        pair = discount_pair(deal.sell_leg, frag) if (via_ref and deal.sell_leg is not None) else None
        if pair is not None:
            await self._merge_discount(deal, pair[0], pair[1], now)   # يضيف مفتاحي الرسالتين ويُخزّن
            log.info("رُبط الرد المعلّق %s بالصفقة الجديدة %s (تسوية خصم بمرجع)", mk, deal.deal_id)
            return True
        self._apply_fragment(deal, frag)
        deal.status = Status.PARSED
        deal.waiting_deadline = None
        if mk and mk not in deal.source_message_keys:
            deal.source_message_keys.append(mk)
        await self.db.deals.upsert(deal)
        log.info("رُبط الرد المعلّق %s بالصفقة الجديدة %s", mk, deal.deal_id)
        return True

    @classmethod
    def _fragment_targets(cls, deal: Deal, frag: ParsedLeg | None) -> bool:
        """هل الصفقة هدف صالح لرسالة ثانية **عديمة‑المرجع** `frag` (§7.3، الطبقة ٢)؟ القرار حسب نوع
        الرسالة الثانية (يمنع تلوّث A9015):
          • frag **يحمل كودًا** (هوية: «كود اسم سعر / خزينة») → يخصّ صفقةً **بلا كود** (يجلب الهوية)؛
            صفقة ذات كود لا تبتلعه أبدًا (كودها 1284 لا يُستبدَل بكود fragment آخر).
          • frag **بلا كود** (خزينة فقط «بلس»/«صافي») → يخصّ أي صفقة تنتظر **خزينة** (بلا خزينة)."""
        if not cls._needs_completion(deal):
            return False
        leg = deal.sell_leg or deal.buy_leg
        if leg is None:
            return False
        # 🔴 (البند 4، الموضع يحدّد النوع) سطر مورّدٍ صريحٍ محلول (اسم+سعر بلا كود، is_supplier_counterpart)
        #   يخصّ صفقةً تنتظر **موردًا** (بلا مورد ولا طرف شراء) — بصرف النظر عن كود الزبون. سطر المورّد
        #   لا يحمل هوية زبون فلا يُقاس بقاعدة «بلا كود» (X1277: الزبون 1277 في الأولى، «شركة البراق»
        #   في الثانية موردٌ لا هوية — كان يُرفَض لأنّ اسمه يُسرِّب لـcustomer_name فيبدو هويّةً).
        if frag is not None and frag.is_supplier_counterpart:
            return leg.supplier is None and deal.buy_leg is None
        # «يحمل هوية» = كود أو اسم زبون (بعض الأكواد أحاديّة الرقم تُفوَّت لكن الاسم يُلتقَط).
        frag_has_identity = frag is not None and bool(frag.customer_code or (frag.customer_name or "").strip())
        if frag_has_identity:
            return not leg.customer_code          # هوية → لصفقة بلا كود حصرًا (يمنع تلوّث A9015)
        return leg.treasury is None               # خزينة فقط («بلس») → لصفقة تنتظر خزينة

    @classmethod
    def narrow_by_content(cls, cands: list[Deal], frag: "ParsedLeg | None") -> list[Deal]:
        """(العرض 2-A) إقصاءٌ صارمٌ بالمحتوى لمرشّحي رسالةٍ ثانية `frag` عديمة‑المرجع — يُبقي فقط
        المتوافقين، **بلا رجوعٍ للطابور الكامل** (بخلاف السلوك القديم «تفضيل»):
          • **العملة** صارمة (`_currency_compatible`): تكملةٌ مصريّةٌ لا تلمس تونسيّة (عملتها من
            خزينتها أو سعرها 6.02→EGP/34→TND). مجهولةٌ ⇒ متوافقة (لا رفض كاذب).
          • **نوع المحتوى** صارم (`_fragment_targets`): كود→صفقة بلا كود، مورد→تنتظر موردًا،
            خزينة→تنتظر خزينة.
        ناتجٌ فارغ (0) = لا مطابق متوافق (يُعلَّق/يُحفَظ معلّقًا، لا يُخمَّن)؛ >1 = التباسٌ يُعلَّق+يُصعَّد.
        الهاتف/المبلغ: الجزء نادرًا يحملهما (أسطر خزينة/مورد/كود)؛ حين يحمل هاتفًا يبقى صارمًا في
        مطابقة الغرف المنفصلة، والمبلغ تسامحيّ (تمييزٌ لا إقصاء) فلا يُدرَج هنا."""
        if not cands:
            return cands
        frag_cur = cls._leg_currency(frag) or (
            cls._currency_from_price(frag.price_raw) if frag is not None else None)
        out = [d for d in cands if cls._currency_compatible(d, frag_cur)]
        if frag is not None:
            out = [d for d in out if cls._fragment_targets(d, frag)]
        return out

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
            # رد مورد = الطرف الثاني (شراء) لصفقة طرفين.
            # 🔴 (م: A845، دفاع بعمق) parse_completion_fragment يُرجِع operation=SELL افتراضيًّا؛
            #    طرفُ المورّد شراءٌ صراحةً (§5.3)، فنقلبه BUY هنا كي لا يُسجَّل الشراء بيعًا حتى لو
            #    وصل عبر مسار الجزء المكمِّل بدل _merge_second_leg.
            frag.operation = OperationType.BUY
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
                deal.mark = Mark.INCOMPLETE            # ❌ على المركزية (بلا تصعيد للمسؤول)
                deal.waiting_deadline = None
                deal.hold_reason = "حوالة A ناقصة — لم تصل الرسالة الثانية خلال 15 دقيقة"
                deal.updated_at = now
                await self.db.deals.upsert(deal)
                to_escalate.append(deal)
                log.warning(
                    "صفقة %s: حوالة A ناقصة تجاوزت 15 دقيقة بلا رسالة ثانية → ❌ على المركزية",
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
