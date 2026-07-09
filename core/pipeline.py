"""
الأنبوب — تدفّق البوت الكامل خطوة بخطوة (§15). يربط كل الوحدات:

  التقاط (§7.1) → استقرار (§7.2) → فهم (§3-§5) → حارس (§9) → تجميع (§7.3)
  → مطابقة الغرف (§8) → بوابة الثقة (§8.2) → خصم/خزينة الطرفين (§6)
  → Outbox → كتابة MONEYADO (§11) + Kill Switch (§13) → تحقّق SQL (§11.4)
  → دفتر (§9) → علامة ✅/⚠️/🔴 (§8.3)

القاعدة الذهبية (§0): لو شكّيت لا تُنزّل — علّق ⚠️، نبّه، وكمّل للتالية. الطابور لا يتوقّف.
كل مخرجات البوت تمرّ عبر Bus حصرًا (§2.2). لا silent catches (T5).

التصميم قابل للاختبار: كل خطوة دالة تستقبل `now` صراحةً (لا اعتماد على الساعة).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from .bus import Bus
from .constants import Currency, Mark, OperationType, RoomType, Status, TreasuryType
from .db import Database, utcnow
from .guard import Guard, build_reversal, is_out_of_active_window
from .logging_setup import get_logger
from .matching.service import MatchingService
from .models import Deal, LedgerEntry, ParsedLeg, RawMessage, WriteJob
from .parsing import detect_control, parse_message
from .parsing.normalize import normalize_price
from .parsing.resolve import resolve_treasury
from .queue.commission import compute_commission, resolve_two_leg_treasury
from .queue.service import QueueService, is_completion_fragment
from .queue.stabilization import is_stable
from .verification.sql_verifier import SqlVerifier
from .writers.base import Writer

log = get_logger(__name__)

# حالات لا يُقبل عليها «تم» بإعادة كتابة (§8.1 بند 7، §9): نهائية أو نازلة فعلًا.
_CONFIRM_BLOCKED = {
    Status.COMPLETED, Status.ESCALATED, Status.CANCELLED,
    Status.TECH_FAILED, Status.SELL_DONE,
}


class Pipeline:
    """منسّق المعالجة — يجمع الوحدات في تدفّق واحد صارم (§15)."""

    def __init__(
        self,
        db: Database,
        bus: Bus,
        writer: Writer,
        verifier: SqlVerifier,
        *,
        queue: Optional[QueueService] = None,
        matcher: Optional[MatchingService] = None,
        guard: Optional[Guard] = None,
        customer_room_jids: Optional[list[str]] = None,
        treasury_room_jids: Optional[list[str]] = None,
    ):
        self.db = db
        self.bus = bus
        self.writer = writer
        self.verifier = verifier
        self.queue = queue or QueueService(db)
        self.matcher = matcher or MatchingService(
            db, bus, customer_room_jids, treasury_room_jids
        )
        self.guard = guard or Guard(db)
        # seed من env/الإنشاء — fallback إن تعذّر قراءة تصنيف الغرف من DB. المصدر الحيّ
        # للتصنيف هو DB (hot-reload §شرط 5)؛ يُقرأ ديناميكيًّا في _matching_rooms_configured.
        self._has_rooms_seed = bool(
            (customer_room_jids or getattr(self.matcher, "_customer_rooms", []))
            or (treasury_room_jids or getattr(self.matcher, "_treasury_rooms", []))
        )

    async def _matching_rooms_configured(self) -> bool:
        """
        هل توجد غرف زبون/خزينة مصنّفة نشطة؟ DB مصدر الحقيقة (hot-reload)؛ env مجرّد seed.

        🔴 الإصلاح (بلاغ 🔸): البوّابة كانت تُحسب من env فقط عند الإنشاء، فإن كان env فارغًا
        (رغم تصنيف الغرف في DB/اللوحة) تُتخطّى المطابقة أبدًا. الآن تُقرأ من DB كل معالجة.
        """
        try:
            if await self.db.rooms.jids_of_type(RoomType.CUSTOMER.value) \
               or await self.db.rooms.jids_of_type(RoomType.TREASURY.value):
                return True
        except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونرجع للـ seed الآمن
            log.warning("تعذّر قراءة تصنيف الغرف من DB (%s) — استخدام seed env", exc)
        return self._has_rooms_seed

    # ═════════════════════════════════════════════════════════════════════════
    # (1) الالتقاط — §7.1 بند 1: تخزين خام فوري قبل أي معالجة
    # ═════════════════════════════════════════════════════════════════════════
    async def capture(self, raw: RawMessage) -> None:
        await self.queue.capture(raw)

    # ═════════════════════════════════════════════════════════════════════════
    # (2-4) معالجة الرسائل المستقرّة: فهم → حارس → تجميع
    # ═════════════════════════════════════════════════════════════════════════
    async def process_inbox(self, now: datetime) -> list[Deal]:
        """
        يمرّ على الرسائل الخام غير المعالَجة المستقرّة (§7.2)، يفكّكها ويجمّعها.
        يُرجع الصفقات التي أصبحت جاهزة للمعالجة (PARSED). لا يوقفه أي تعليق فردي.
        """
        ready: list[Deal] = []
        for raw in await self.db.raw.unprocessed():
            # رسائل التحكّم (Reply: إلغاء/تعديل/تصحيح/تم) إجراءات مكتملة متعمّدة — تُعالَج فورًا،
            # ولا تُعامَل كـ«حرف» placeholder ينتظر تعديلًا (§7.2 يخصّ حوالات جديدة قصيرة).
            is_control_reply = bool(raw.reply_to_key) and detect_control(raw.text) is not None
            if not is_control_reply and not is_stable(raw, now):
                # لم تستقرّ بعد — «الحرف» قد يُعدَّل (§7.2). لا تخطٍّ صامت (T5): نسجّل السبب.
                # استثناء (§7.3): رد خزينة/مورد مكمّل («بلس»/«صافي» وحدها) ليس حرفًا ينتظر
                # تعديلًا — يحلّ خزينة/موردًا صراحةً ويجب ربطه بحوالته المعلّقة فورًا (لا انتظار).
                if not await self._is_completion_reply(raw):
                    log.debug("تخطٍّ مؤقّت (لم تستقرّ بعد): %s", raw.message_key)
                    continue
            try:
                deal = await self._ingest(raw, now)
                if deal is not None and deal.status == Status.PARSED:
                    ready.append(deal)
            except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونكمل للتالية (الطابور لا يتوقّف)
                log.exception("فشل معالجة الرسالة %s: %s — تجاوز للتالية", raw.message_key, exc)
            finally:
                await self.db.raw.mark_processed(raw.message_key)
        return ready

    async def _is_completion_reply(self, raw: RawMessage) -> bool:
        """هل الرسالة رد خزينة/مورد مكمّل (§7.3)؟ يُعالَج فورًا بلا انتظار استقرار «الحرف»."""
        if raw.chat_jid != self.bus.central_jid or not (raw.text or "").strip():
            return False
        treasuries = await self.db.treasuries.all_active()
        suppliers = await self.db.suppliers.all_active()
        res = parse_message(raw.text, treasuries, suppliers)
        return res.kind == "noise" and is_completion_fragment(res.leg)

    async def _ingest(self, raw: RawMessage, now: datetime) -> Optional[Deal]:
        # الرسائل من غير المركزية = مصدر مطابقة صامت فقط (§2.2 §8) — لا تُفكَّك كحوالة
        if raw.chat_jid != self.bus.central_jid:
            return None

        # §10: رسائل التحكّم (Reply من موظف معتمد) — إلغاء/تعديل/تصحيح/تأكيد
        if raw.reply_to_key:
            ctrl = detect_control(raw.text)
            if ctrl is not None:
                await self._handle_control(ctrl[0], ctrl[1], raw, now)
                return None

        # الفهم (§3-§5)
        treasuries = await self.db.treasuries.all_active()
        suppliers = await self.db.suppliers.all_active()
        result = parse_message(raw.text, treasuries, suppliers)

        if result.kind == "silent_ignore":
            log.info("تجاهل صامت (صرف/قبض): %s", raw.message_key)
            return None
        if result.kind == "out_of_scope":
            # تسليم يدوي/باليد (§0): ليس حوالة وليس تجاهلًا → تصعيد لغرفة المسؤول للمعالجة اليدوية.
            log.warning("خارج النطاق (تسليم يدوي): %s — تصعيد لغرفة المسؤول", raw.message_key)
            await self.bus.notify_admin(
                f"🚫 خارج النطاق (تسليم يدوي) — مراجعة يدوية: {(raw.text or '').strip()[:80]}",
                raw.message_key,
            )
            return None
        if result.kind == "noise":
            # رد خزينة/مورد بلا رقم إشاري («بلس»/«صافي» وحدها) — ليس هدرزة بل جزء مكمّل
            # لحوالة معلّقة (§7.3): يُربَط بالمعلّقة (قرب زمني/نفس الغرفة) أو يُحفَظ ردًّا معلّقًا.
            if is_completion_fragment(result.leg):
                return await self.queue.absorb_fragment(
                    result.leg, raw.chat_jid, raw.message_key, now
                )
            log.info("هدرزة — تجاهل صامت: %s", raw.message_key)
            return None
        if result.kind == "control":
            # رسالة تحكّم بلا Reply (نادر) → لا مرجع → تحويل للمسؤول (§10)
            await self.bus.notify_admin(
                f"⚠️ رسالة تحكّم بلا مرجع (Reply): {result.control_action} — مراجعة.",
                raw.message_key,
            )
            return None

        leg = result.leg
        leg.source_message_key = raw.message_key

        # الحارس (§9): نُزّلت من قبل؟ → تجاهل (منع تكرار)
        if await self.guard.already_downloaded(raw.message_key):
            log.info("الحارس: %s نزلت من قبل — تجاهل (§9)", raw.message_key)
            return None

        # التجميع (§7.3): صفقة جديدة أو دمج طرف ثانٍ
        deal = await self.queue.try_group(leg, now, chat_jid=raw.chat_jid)
        return deal

    # ═════════════════════════════════════════════════════════════════════════
    # (5) الطرف الثاني: تصعيد المتأخّر + معالجة الصفقات الجاهزة
    # ═════════════════════════════════════════════════════════════════════════
    async def tick(self, now: datetime) -> None:
        """نبضة دورية: تصعيد المتأخّر (§7.3)، تذكير/تصعيد المطابقة (§8.1)، ومعالجة الجاهز."""
        # ردود خزينة/مورد معلّقة تجاوزت المهلة بلا حوالة تطابقها → تُسقَط كهدرزة (§7.3)
        from .constants import PENDING_REPLY_MAX_SECONDS
        await self.db.pending_replies.sweep_expired(now, PENDING_REPLY_MAX_SECONDS)

        # حوالة A ناقصة (رسالة أولى بلا رسالة ثانية §7.3، قرار المستخدم):
        #   90s → تنبيه خفيف في المركزية (تبقى منتظِرة)؛ 15 دقيقة → تصعيد لغرفة المسؤول.
        to_warn, to_escalate = await self.queue.sweep_incomplete_a(now)
        for deal in to_warn:
            await self.bus.reply_central(
                f"⚠️ {self._ref(deal)} — يُرجى إكمال البيانات "
                f"(كود الزبون + الاسم + السعر + الخزينة).",
                self._deal_key(deal),
            )
            log.info("تنبيه خفيف: حوالة A ناقصة %s تجاوزت 90s بلا رسالة ثانية", self._ref(deal))
        for deal in to_escalate:
            await self.bus.notify_admin(
                f"⚠️ حوالة A ({self._ref(deal)}) لم تكتمل خلال 15 دقيقة — لم تصل الرسالة "
                f"الثانية (كود الزبون + الاسم + السعر + الخزينة). مراجعة يدوية.",
                self._deal_key(deal),
            )
            log.warning("تصعيد: حوالة A ناقصة %s تجاوزت 15 دقيقة بلا رسالة ثانية", self._ref(deal))

        # صفقات تجاوزت 90s بلا طرف ثانٍ → تُنهى كطرف واحد (لا تصعيد) وتمشي مع PARSED أدناه (§7.3)
        for deal in await self.queue.sweep_waiting(now):
            log.info("صفقة %s أُنهيت كطرف واحد بعد المهلة — ستُعالَج الآن", self._ref(deal))

        # الصفقات الجاهزة (اكتمل فهمها/تجميعها، بما فيها ما أُنهي كطرف واحد) → المطابقة فالكتابة
        for deal in await self.db.deals.by_status(Status.PARSED):
            await self.process_deal(deal, now)

        # صفقات قيد المطابقة لم تظهر → تذكير/تصعيد (§8.1)
        for deal in await self.db.deals.by_status(Status.MATCHING, Status.HELD):
            updated = await self.matcher.escalation_tick(deal, now)
            # إن ظهرت متأخّرة يمكن إعادة محاولة المطابقة
            if updated.status == Status.MATCHING:
                await self.process_deal(updated, now)

    # ═════════════════════════════════════════════════════════════════════════
    # (6-10) معالجة صفقة: خصم/خزينة → مطابقة → ثقة → كتابة → تحقّق → دفتر → علامة
    # ═════════════════════════════════════════════════════════════════════════
    async def process_deal(self, deal: Deal, now: datetime) -> Deal:
        try:
            # (6·SI) حوالة SI معنونة خزينتها مذكورة صراحةً دائمًا — فإن لم تُحلّ (نادر جدًّا:
            # اسم خزينة خارج القوائم/خطأ إملائي) فهي حالة شاذّة تحتاج إنسانًا: تُصعَّد لغرفة
            # المسؤول فورًا بتنبيه واضح (لا HELD صامت، ولا مطابقة غرف بلا خزينة) — §0.
            leg0 = deal.sell_leg or deal.buy_leg
            if leg0 is not None and leg0.is_si_format and leg0.treasury is None:
                deal.status = Status.ESCALATED
                deal.mark = Mark.WARN
                deal.hold_reason = "SI بخزينة غير محلولة — تصعيد للمراجعة اليدوية"
                await self.db.deals.upsert(deal)
                await self.bus.notify_admin(
                    f"🚫 حوالة SI بخزينة غير محلولة ({self._ref(deal)}) — "
                    f"اسم الخزينة خارج القوائم أو خطأ إملائي؛ مراجعة يدوية فورية (§0).",
                    self._deal_key(deal),
                )
                log.warning("SI بخزينة غير محلولة %s — تصعيد لغرفة المسؤول", deal.deal_id)
                return deal

            # (6a) الخصم وخزينة الطرفين بمورد (§6) — تُحسم عند اكتمال صفقة طرفين بمورد فقط.
            #      🔴 محصور بطرف الشراء من مورد (supplier): طرف الشراء المشتقّ لخزينة sell_and_buy
            #      (يُخلَّق أدناه قبل الكتابة) عمولته محسومة مسبقًا فلا يُعاد حسابها هنا.
            if deal.is_two_legged and deal.sell_leg and deal.buy_leg and (
                deal.buy_leg.is_supplier_counterpart or deal.buy_leg.supplier is not None
            ):
                await self._resolve_two_leg(deal)

            # (6ب) مطابقة الغرف (§8.1) — إن كانت الغرف مُصنّفة (DB مصدر الحقيقة، hot-reload)
            if await self._matching_rooms_configured():
                deal = await self.matcher.match_in_rooms(deal, now)
                if deal.status != Status.MATCHED:
                    # لم تتطابق بعد → تبقى للتذكير/التصعيد (§8.1) — لا تُكتب الآن
                    return deal
            else:
                log.warning("لا غرف مطابقة مُصنّفة (DB/env) — تخطّي §8 (وضع مركزية فقط، مؤقّت).")

            # (7-8) بوابة الثقة (§8.2): الكود + المبلغ مقروءان؟
            leg = deal.sell_leg or deal.buy_leg
            ok, reason = self._trust_gate(deal)
            if not ok:
                deal.hold_reason = reason
                deal.status = Status.HELD
                deal.mark = Mark.WARN
                await self.db.deals.upsert(deal)
                await self.matcher.apply_mark(deal, Mark.WARN)  # ⚠️ Reply بالسبب
                log.warning("تعليق ⚠️ للصفقة %s: %s", deal.deal_id, reason)
                return deal

            # الحارس قبل الكتابة (§9): لم تُنزَّل + لا إلغاء
            allowed, greason = await self.guard.guard_before_write(deal)
            if not allowed:
                log.info("الحارس منع كتابة الصفقة %s: %s", deal.deal_id, greason)
                return deal

            # (8·ب) طرف الشراء المشتقّ لخزينة sell_and_buy (§6): بيع ثم شراء لنفس الخزينة الخارجية.
            #        يُخلَّق بعد المطابقة/الثقة على البيع الأصلي، وقبل بناء أوامر الكتابة مباشرة.
            self._maybe_synthesize_buy_leg(deal)

            # (9) Outbox + الكتابة (بيع order=0 ثم شراء order=1 — التسلسل: بيع→تخزين→شراء→تخزين)
            jobs = await self.queue.build_write_jobs(deal)
            return await self._write_jobs(deal, jobs, now)

        except Exception as exc:  # T5 — لا نبتلع؛ نصعّد ونكمل
            log.exception("فشل معالجة الصفقة %s: %s", deal.deal_id, exc)
            await self.bus.notify_admin(
                f"🔴 خطأ تقني أثناء معالجة الصفقة {self._ref(deal)}: {exc}",
                self._deal_key(deal),
            )
            return deal

    def _maybe_synthesize_buy_leg(self, deal: Deal) -> None:
        """
        خزينة الصفقة من نوع «بيع وشراء» (sell_and_buy: خصم1%/صافي/تونسي خارجي §6) → البوت يُسجّل
        عمليتين لنفس الخزينة الخارجية: بيع ثم شراء. طرف الشراء **مشتقّ من البيع** (لا رسالة ثانية):

          - amount = المبلغ بعد الخصم (amount_after_discount) أو المبلغ نفسه بلا خصم.
          - بلا عمولة (commission=None) — العمولة على طرف البيع وحده (§6.2).
          - نفس الخزينة والهاتف والرقم الإشاري والعملة والزبون (نسخة من البيع).

        يُخلَّق فقط إن لم يوجد طرف شراء (لا يمسّ مسار الطرفين بمورد §5.3). طرف مشتقّ بلا مورد
        فلا يُعاد حساب عمولته في _resolve_two_leg (محصور بالمورد أعلاه).

        🔴 مورد مذكور في البيع (SI «المورد: طه 5.72» §5/§6): طرف الشراء يُبنى **من المورد** لا
        نسخةً صرفة من البيع — الحساب = كود المورد، والسعر = سعر المورد (لا سعر البيع)، ويُعلَّم
        طرف مورد (is_supplier_counterpart) ليأخذ كود المورد في شاشة الشراء (§5.3، build_buy_fields).
        """
        sell = deal.sell_leg
        if deal.buy_leg is not None or sell is None or sell.treasury is None:
            return
        if sell.treasury.type != TreasuryType.SELL_AND_BUY:
            return
        net = sell.amount_after_discount if sell.amount_after_discount is not None else sell.amount
        update = {
            "operation": OperationType.BUY,
            "amount": net,
            "amount_after_discount": None,
            "commission": None,
            "commission_rate": 0.0,
        }
        if sell.supplier is not None:
            # سعر المورد → سعر طرف الشراء (مطبَّع حسب العملة §3.6)؛ الحساب = كود المورد.
            _raw, pnorm = normalize_price(sell.supplier_price_raw, sell.currency or Currency.EGP)
            update.update({
                "customer_code": sell.supplier.code,
                "customer_name": sell.supplier.name,
                "price_raw": sell.supplier_price_raw,
                "price_normalized": pnorm,
                "is_supplier_counterpart": True,
                "supplier_price_raw": None,          # استُهلك في بناء طرف الشراء
            })
        deal.buy_leg = sell.model_copy(update=update)
        deal.is_two_legged = True
        log.info(
            "صفقة %s: خزينة sell_and_buy «%s» → تخليق طرف شراء (مبلغ=%s، مورد=%s، سعر=%s، ref=%s)",
            deal.deal_id, sell.treasury.name, net,
            sell.supplier.name if sell.supplier else "—",
            sell.supplier_price_raw if sell.supplier else "—", sell.reference_number,
        )

    async def _resolve_two_leg(self, deal: Deal) -> None:
        """يحسب العمولة ويُسند خزينة «خصم1%/صافي» لطرفَي الصفقة (§6.1 §6.2)."""
        commission = compute_commission(deal.sell_leg, deal.buy_leg)
        has_discount = bool(commission is not None and abs(commission) > 1e-9)
        deal.sell_leg.commission = commission  # الفرق بالسالب (§6.2)
        deal.sell_leg.commission_rate = 0.0    # 0 دائمًا (§6.2)

        # الخزينة إن لم تكن محسومة بعد (§5.5): خصم1% عند خصم، صافي بدونه (§6.1)
        if deal.sell_leg.treasury is None or deal.buy_leg.treasury is None:
            tname = resolve_two_leg_treasury(deal.sell_leg, deal.buy_leg, has_discount)
            treasuries = await self.db.treasuries.all_active()
            trec = resolve_treasury(tname, treasuries)
            if trec is not None:
                from .models import TreasuryRef
                tref = TreasuryRef(
                    code=trec.code, name=trec.name, type=trec.type,
                    currency=trec.currency or deal.sell_leg.currency,
                )
                deal.sell_leg.treasury = deal.sell_leg.treasury or tref
                deal.buy_leg.treasury = deal.buy_leg.treasury or tref
                log.info("صفقة %s: خزينة الطرفين = %s، عمولة = %s",
                         deal.deal_id, tname, commission)
            else:
                log.warning("صفقة %s: تعذّر حلّ خزينة الطرفين «%s» (كود معلّق §ملحق ب-3)",
                            deal.deal_id, tname)
        await self.db.deals.upsert(deal)

    def _trust_gate(self, deal: Deal) -> tuple[bool, Optional[str]]:
        """بوابة الثقة على الطرف/الأطراف (§8.2). يستخدم منطق A3."""
        from .matching.verify import trust_gate
        for leg in (deal.sell_leg, deal.buy_leg):
            if leg is None:
                continue
            ok, reason = trust_gate(leg)
            if not ok:
                return False, reason
            # خزينة بلا كود = لا يمكن التنزيل (كود معلّق) → تعليق (§ملحق ب-3)
            if leg.treasury is None:
                return False, "لا خزينة محلولة — تعذّر تحديد الحساب"
            if leg.treasury.code is None:
                return False, f"خزينة «{leg.treasury.name}» بلا كود MONEYADO (معلّق)"
        return True, None

    # ═════════════════════════════════════════════════════════════════════════
    # الكتابة + التحقّق + الدفتر + العلامة (§9 §11 §11.4 §13)
    # ═════════════════════════════════════════════════════════════════════════
    async def _write_jobs(self, deal: Deal, jobs: list[WriteJob], now: datetime) -> Deal:
        control = await self.db.control.get()
        commit = control.storage_enabled  # Kill Switch (§13) — الافتراضي إيقاف
        key = self._deal_key(deal)
        dry_run_seen = False              # DRY_RUN: عُبّئت الشاشة بلا تخزين → ✅ بصري لاحقًا

        for job in sorted(jobs, key=lambda j: j.order_index):  # بيع أولًا (§7.3)
            # 🔴 صمّام منع الازدواج (§9، §11.4، §12): قبل أي «تخزين» نفحص SQL — إن كان هذا
            # الطرف محفوظًا فعلًا (تعطّل/إعادة معالجة/تم متأخّر) لا نُخزّن ثانية، نسجّل الدفتر فقط.
            if commit and self.verifier.enabled and not job.is_reversal:
                already, mref0 = await self.verifier.verify_transaction(
                    job.leg.reference_number or "", job.leg.amount or 0.0,
                    job.leg.customer_code or "", job.operation,
                )
                if already:
                    log.warning("§9: %s للصفقة %s محفوظ مسبقًا في SQL — لا إعادة إدخال، تسجيل الدفتر فقط.",
                                job.operation.value, deal.deal_id)
                    await self._append_ledger(deal, job, mref0, verified=True)
                    if job.operation == OperationType.SELL:
                        deal.status = Status.SELL_DONE
                        await self.db.deals.set_status(deal.deal_id, Status.SELL_DONE)
                    continue

            res = await self.writer.write(job, commit=commit)
            await self.db.outbox.increment_attempt(job.job_id)

            if not res.ok:
                # فشل/شكّ تقني → dead-letter + رسالة للمسؤول + وقف الصفقة (§11.3).
                # 🔴 لا تفاعل على المركزية عند الفشل (قرار المستخدم): العلامتان الوحيدتان على
                # المركزية 🔸/✅ فقط؛ الفشل يُبلَّغ في غرفة المسؤول وينتظر تدخّلًا يدويًا.
                await self.db.dead_letter.add(
                    deal.deal_id, res.error or "فشل كتابة",
                    screenshot_path=res.screenshot_path,
                    details={"job": job.job_id, "operation": job.operation.value},
                )
                deal.status = Status.SELL_DONE if job.operation == OperationType.BUY else Status.TECH_FAILED
                deal.mark = Mark.FAILED
                await self.db.deals.set_status(deal.deal_id, deal.status, mark=Mark.FAILED.value)
                await self.bus.notify_admin(
                    f"🔴 فشل {job.operation.value} للصفقة {self._ref(deal)}: {res.error} "
                    f"{'(لقطة محفوظة)' if res.screenshot_path else ''} — مراجعة يدوية.",
                    key,
                )
                return deal

            if res.dry_run:
                # DRY_RUN (مستقلّ عن Kill Switch): عُبّئت الشاشة وتُركت مفتوحة للمعاينة — لا تحقّق
                # SQL ولا دفتر (لم يُخزَّن شيء). ✅ «تمّ — DRY_RUN» تُوضع بعد كل الأطراف.
                dry_run_seen = True
                log.info("DRY_RUN — %s للصفقة %s عُبّئ ومُعروض (بلا تخزين/دفتر).",
                         job.operation.value, deal.deal_id)
                continue

            if not commit:
                # Kill Switch = إيقاف: عُبّئت الشاشة وتوقّفت عند «تخزين» (§13). لا دفتر، لا ✅.
                log.info("Kill Switch إيقاف — %s للصفقة %s عُبّئ بلا تخزين (انتظار التفعيل).",
                         job.operation.value, deal.deal_id)
                continue

            # commit=True → تحقّق SQL (§11.4) ثم قيد الدفتر (§9)
            verified, mref = await self._verify_and_record(deal, job)
            if not verified:
                deal.status = Status.TECH_FAILED if job.operation == OperationType.SELL else Status.SELL_DONE
                deal.mark = Mark.FAILED
                await self.db.deals.set_status(deal.deal_id, deal.status, mark=Mark.FAILED.value)
                await self.bus.notify_admin(
                    f"🔴 لم يتأكّد حفظ {job.operation.value} للصفقة {self._ref(deal)} في SQL — مراجعة (§11.4).",
                    key,
                )
                return deal
            if job.operation == OperationType.SELL:
                deal.status = Status.SELL_DONE  # بيع نزل (§11.4)
                await self.db.deals.set_status(deal.deal_id, Status.SELL_DONE)

        # DRY_RUN: عُبّئت كل الأطراف والشاشة مفتوحة للمعاينة — ✅ «تمّ — DRY_RUN» بصري فقط،
        # بلا حالة COMPLETED ولا دفتر (كي لا يُحجب التشغيل الحقيقي لاحقًا §9). لا يمسّ Kill Switch.
        # ✅ يُوضع مرّة واحدة (mark != DONE) فلا يتكرّر إن أُعيدت معالجة الصفقة في نبضة لاحقة.
        if dry_run_seen:
            if key and deal.mark != Mark.DONE:
                deal.mark = Mark.DONE
                await self.db.deals.upsert(deal)                # نحفظ العلامة فقط، لا الحالة
                await self.matcher.apply_mark(deal, Mark.DONE)  # ✅ صامت (§8.3)
                log.info("✅ DRY_RUN — الصفقة %s عُبّئت ومُعروضة (بلا تخزين).", deal.deal_id)
            return deal

        # اكتملت كل الأطراف بنجاح (أو Kill Switch إيقاف)
        if commit:
            deal.status = Status.COMPLETED
            deal.mark = Mark.DONE
            await self.db.deals.set_status(deal.deal_id, Status.COMPLETED, mark=Mark.DONE.value)
            if key:
                await self.matcher.apply_mark(deal, Mark.DONE)  # ✅ صامت (§8.3)
            log.info("✅ الصفقة %s تمّت وتأكّدت", deal.deal_id)
        return deal

    async def _verify_and_record(self, deal: Deal, job: WriteJob) -> tuple[bool, Optional[str]]:
        """تحقّق SQL (§11.4) ثم قيد الدفتر (§9). يُرجع (verified, moneyado_ref)."""
        leg = job.leg
        verified, mref = await self.verifier.verify_transaction(
            leg.reference_number or "", leg.amount or 0.0,
            leg.customer_code or "", job.operation,
        )
        if not self.verifier.enabled:
            # SQL معطّل: لا يمكن الجزم (§0). سُجّل القيد لمنع الازدواج (§9) لكن بلا ✅ مؤكّد.
            log.warning("SQL معطّل — قيد %s للصفقة %s يُسجَّل بلا تأكيد (فعّل SQL قبل التخزين الحقيقي).",
                        job.operation.value, deal.deal_id)
            await self._append_ledger(deal, job, mref, verified=False)
            return True, mref  # نمرّر (التخزين تمّ فعليًا)؛ التحذير مسجَّل
        if verified:
            await self._append_ledger(deal, job, mref, verified=True)
            return True, mref
        return False, None

    async def _append_ledger(self, deal: Deal, job: WriteJob, mref: Optional[str], verified: bool) -> None:
        leg = job.leg
        entry = LedgerEntry(
            entry_id=str(uuid.uuid4()),
            deal_id=deal.deal_id,
            message_key=leg.source_message_key or self._deal_key(deal) or deal.deal_id,
            reference_number=leg.reference_number,
            operation=job.operation,
            is_reversal=job.is_reversal,
            amount=leg.amount or 0.0,
            currency=leg.currency or Currency.EGP,
            customer_code=leg.customer_code,
            treasury_code=leg.treasury.code if leg.treasury else None,
            moneyado_ref=mref,
            status=Status.COMPLETED if verified else Status.SELL_DONE,
            sql_verified=verified,
            created_at=utcnow(),
        )
        try:
            await self.db.ledger.append(entry)
        except Exception as exc:  # backstop الفهرس الفريد (§9): قيد أصلي مكرّر → مُنع، لا انهيار
            if "duplicate" in str(exc).lower() or exc.__class__.__name__ == "DuplicateKeyError":
                log.error("🔴 مُنع قيد دفتر مكرّر (§9): %s/%s — الإدخال المزدوج محبَط.",
                          entry.message_key, job.operation.value)
            else:
                raise

    # ═════════════════════════════════════════════════════════════════════════
    # (§10) الإلغاء/التعديل/التصحيح/التأكيد — عبر Reply من موظف معتمد
    # ═════════════════════════════════════════════════════════════════════════
    async def _handle_control(self, action: str, value: Optional[float],
                              raw: RawMessage, now: datetime) -> None:
        # يُقبل التحكّم من الموظفين المعتمدين فقط (§8.3 §10)
        authorized = await self.db.employees.is_authorized(raw.sender_jid or "")
        if not authorized:
            log.warning("تحكّم «%s» من غير معتمد (%s) — رُفض (§8.3).", action, raw.sender_jid)
            return

        original = await self.db.deals.find_by_source_key(raw.reply_to_key or "")
        if original is None:
            await self.bus.notify_admin(
                f"⚠️ «{action}» على حوالة غير معروفة (Reply {raw.reply_to_key}) — مراجعة.",
                raw.message_key,
            )
            return

        # تأكيد «تم» = تجاوز بشري موثوق للمطابقة (§8.1 بند 6) — فقط لصفقة لم تُحسم بعد.
        if action == "confirm":
            # 🔴 حارس حالة (§8.1 بند 7، §9): «تم» على صفقة بحالة نهائية/نازلة لا يعيد الكتابة أبدًا،
            # حتى لو جاء متأخّرًا — يتفادى الإدخال المزدوج (مثال: TECH_FAILED بعد تخزين فعلي).
            if original.status in _CONFIRM_BLOCKED:
                await self.bus.notify_admin(
                    f"⚠️ «تم» على صفقة بحالة «{original.status.value}» ({self._ref(original)}) — "
                    f"البوت لا يعيد الكتابة (§8.1/§9)؛ المسؤول يتحقّق يدويًا.",
                    raw.message_key,
                )
                log.warning("«تم» مرفوض للصفقة %s (حالة نهائية %s)", original.deal_id, original.status)
                return
            original.matched_customer_room = True
            original.matched_treasury_room = True
            original.matched_supplier_room = True   # يغطّي أيضًا صفقة طرفين بلا غرفة مورد (§8)
            original.status = Status.MATCHED
            original.mark = Mark.MATCHED
            await self.db.deals.upsert(original)
            log.info("«تم» معتمد للصفقة %s — تجاوز المطابقة، ستُعالَج.", original.deal_id)
            await self.process_deal(original, now)
            return

        # خارج نافذة 15 يومًا → نادر جدًّا → مسؤول (§10)
        if is_out_of_active_window(original.created_at, now):
            await self.bus.notify_admin(
                f"⚠️ «{action}» على حوالة أقدم من 15 يومًا ({self._ref(original)}) — مراجعة يدوية (§10).",
                raw.message_key,
            )
            return

        # تصحيح على «ملغاة» → البوت لا يتصرّف (§10)
        if action == "correct" and original.status == Status.CANCELLED:
            await self.bus.notify_admin(
                f"⚠️ تصحيح على حوالة ملغاة ({self._ref(original)}) — البوت لا يتصرّف، مراجعة (§10).",
                raw.message_key,
            )
            return

        # بناء القيد العكسي (Append-only §10) — يُحسب من الدفتر لا من رسالة التصحيح
        jobs = await build_reversal(action, value, original, self.db)
        if not jobs:
            await self.bus.notify_admin(
                f"⚠️ «{action}» على صفقة بلا قيود منزّلة ({self._ref(original)}) — مراجعة (§10).",
                raw.message_key,
            )
            return

        # كتابة القيود العكسية (نفس منطق الكتابة/التحقّق)
        await self._write_jobs(original, jobs, now)
        if action == "cancel":
            await self.db.deals.set_status(original.deal_id, Status.CANCELLED)
        log.info("نُفّذ «%s» للصفقة %s بـ %d قيد عكسي", action, original.deal_id, len(jobs))

    # ═════════════════════════════════════════════════════════════════════════
    # مساعدات
    # ═════════════════════════════════════════════════════════════════════════
    @staticmethod
    def _deal_key(deal: Deal) -> Optional[str]:
        if deal.sell_leg and deal.sell_leg.source_message_key:
            return deal.sell_leg.source_message_key
        if deal.buy_leg and deal.buy_leg.source_message_key:
            return deal.buy_leg.source_message_key
        return deal.source_message_keys[0] if deal.source_message_keys else None

    @staticmethod
    def _ref(deal: Deal) -> str:
        leg = deal.sell_leg or deal.buy_leg
        return leg.reference_number if leg and leg.reference_number else deal.deal_id
