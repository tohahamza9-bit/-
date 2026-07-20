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

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Optional

from .amendment import (
    amendment_ratio,
    build_amendment_jobs,
    detect_amendment,
    floor_commission,
)
from .bus import Bus
from .config import get_settings
from .cancellation import (
    build_cancellation_jobs,
    detect_cancellation,
    within_cancellation_window,
)
from .constants import (
    BATCH_PAIR_SECONDS,
    CANCELLATION_WINDOW_HOURS,
    INCOMPLETE_DATA_ESCALATE_SECONDS,
    SENDER_SLOT_WINDOW_SECONDS,
    Currency,
    Mark,
    OperationType,
    RoomType,
    Status,
    TreasuryType,
)
from .db import Database, utcnow
from .guard import Guard, build_reversal, is_out_of_active_window
from .logging_setup import get_logger
from .matching.service import MatchingService
from .models import (
    Deal, LedgerEntry, ParsedLeg, RawMessage, SupplierRef, TreasuryRef, WriteJob,
)
from .parsing import (
    detect_control,
    extract_code_name_price_lines,
    parse_completion_fragment,
    parse_message,
)
from .ai_context import build_sender_context
from .ai_understanding import (
    OpenRouterClient, _num_in_text, apply_text_corrections, join_pair, validate_corrections,
    validate_line_parse, validate_link_choice, validate_postal, validate_proposal,
    validate_text_corrections, with_ephemeral_alias,
)
from .ambiguity import detect as detect_ambiguity
from .ambiguity import treasury_role_conflict
from .fx_rates import ingest_price_message, price_room_currency
from .parsing.normalize import normalize_price
from .parsing.parser import _has_reference, first_reference
from .parsing.resolve import resolve_bold, resolve_treasury
from .queue.commission import compute_commission, resolve_two_leg_treasury
from .queue.service import (
    QueueService,
    _as_naive_utc,
    _log_link_decision,
    bind_by_reference,
    is_completion_fragment,
    is_incomplete_first_message,
    is_treasury_only_reply,
    is_treasury_second_reply,
    missing_mandatory_fields,
)
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
        ai_client: Optional[object] = None,
    ):
        # (الفهم الذكي) عميل قابل للحقن في الاختبارات؛ None ⇒ يُبنى حيًّا من .env + إعداد اللوحة.
        self._ai_client = ai_client
        self.db = db
        self.bus = bus
        self.writer = writer
        self.verifier = verifier
        self.queue = queue or QueueService(db)
        self.matcher = matcher or MatchingService(
            db, bus, customer_room_jids, treasury_room_jids
        )
        self.guard = guard or Guard(db)
        # 🔴 قفل تسلسليّ لمعالجة الوارد (§7.3): يمنع تشابك استدعاءات process_inbox المتزامنة
        #    فتُعالَج كل رسالة وتُكتب بالكامل قبل بدء التالية (سباق: الثانية تقرأ DB قبل كتابة الأولى).
        self._inbox_lock = asyncio.Lock()
        # seed من env/الإنشاء — fallback إن تعذّر قراءة تصنيف الغرف من DB. المصدر الحيّ
        # للتصنيف هو DB (hot-reload §شرط 5)؛ يُقرأ ديناميكيًّا في _matching_rooms_configured.
        self._has_rooms_seed = bool(
            (customer_room_jids or getattr(self.matcher, "_customer_rooms", []))
            or (treasury_room_jids or getattr(self.matcher, "_treasury_rooms", []))
        )
        # 🔴 بوابة صحّة MONEYADO (§11.3): توقف سحب الكتابة عند مغلق/مصغّر/نسختين — بدل فشل الحوالات
        #    واحدة-واحدة صمتًا. الحالة تُحفَظ على الأنبوب (كائن حيّ): علَم الإيقاف + خنق التنبيه.
        _s = get_settings()
        self._gate_enabled = getattr(_s, "moneyado_gate_enabled", True)
        self._gate_throttle = getattr(_s, "moneyado_gate_alert_throttle", 300.0)
        self._moneyado_paused = False
        self._last_gate_alert: Optional[datetime] = None
        # (البند 5) خنق الرسائل الإلزامية لكل مرجع: كامل مرّتين ثم مختصرة (ممنوع صفر). ذاكرة حيّة.
        self._alert_counts: dict[str, int] = {}
        # 🔴 (قرار المالك 2026-07-19) الحلّ التلقائي بالتشابه **ملغى**: default=off ولا يُفعَّل.
        #    الاسم الذي لا يُحلّ صارمًا (أو من القروب) → تصعيد بالرسالة الإلزامية، لا تخمين.
        self._bold_resolve_enabled = getattr(_s, "bold_resolve_enabled", False)
        self._AUTO_RESOLVE_THRESHOLD = 70   # يبقى للتوثيق — لا أثر له والمسار معطَّل
        # (طبقة قروب الخزينة) — من الإعدادات (مفتاح المالك + النافذة).
        self._room_match_enabled = getattr(_s, "room_match_enabled", True)
        self._room_match_window = getattr(_s, "room_match_window_seconds", 50.0)

    async def _auto_resolve_treasury(self, leg: Optional[ParsedLeg], raw: RawMessage,
                                     treasuries: list) -> None:
        """(الجزء 1، قرار المالك) خزينةٌ لم تُحلّ بالمطابقة الصارمة → **حلّ جريء** (WRatio ≥ ٧٠ بعد
        التطبيع) على قائمة الخزائن وحدها: يأخذ أفضل مرشّح، يطبّقه، يُنزّل الحوالة فورًا، ويُنبّه المسؤول
        + يسجّل في الصفقة (deviation_log) + يتعلّم alias (نفس الغلطة تُحلّ صارمًا لاحقًا). لا مرشّح فوق
        العتبة → يبقى unresolved (يُلتقط مجهولًا ويُصعَّد). الخطّان الأحمران داخل resolve_bold.

        🔴 **ملغى بقرار المالك (2026-07-19)**: المفتاح bold_resolve_enabled=False افتراضًا ولا يُفعَّل.
           التخمين في الأسماء أنزل حوالات على خزائن خاطئة → عاد النظام لِما قبل التشابه: تصعيد."""
        if not self._bold_resolve_enabled:
            return                              # ملغى — لا تخمين، الاسم يبقى unresolved فيُصعَّد
        if leg is None or not leg.unresolved_treasury:
            return
        original = leg.unresolved_treasury
        rec, score = resolve_bold(original, treasuries, threshold=self._AUTO_RESOLVE_THRESHOLD)
        if rec is None:
            return                              # لا مرشّح ≥ العتبة → لا تخمين (يُصعَّد لاحقًا)
        from .models import TreasuryRef
        from .parsing.normalize import normalize_ar
        leg.treasury = TreasuryRef(code=rec.code, name=rec.name, type=rec.type, currency=rec.currency)
        if leg.currency is None:
            leg.currency = rec.currency
        leg.unresolved_treasury = None
        leg.deviation_log.append({"field": "treasury", "raw_value": original,
                                  "extracted_value": rec.name, "method": "similarity",
                                  "confidence": score})
        ref = leg.reference_number or "؟"
        await self.bus.notify_admin(
            f"⚠️ {ref} نُزّلت — تشابه اسم خزينة: «{original}» ← «{rec.name}» (score {score}). "
            f"للتراجع: احذف الإملاء المتعلَّم من الداشبورد.", raw.message_key)
        learned = normalize_ar(original)
        if learned and learned != normalize_ar(rec.name):
            await self.db.treasuries.add_alias(rec.name, learned)   # source=auto (حيّ فورًا)
        log.info("(الجزء 1) حلّ خزينة جريء: «%s» ← «%s» (%d) — نُزّلت وتُعلّم", original, rec.name, score)

    async def _auto_resolve_supplier(self, leg: Optional[ParsedLeg], raw: RawMessage,
                                     suppliers: list) -> None:
        """(بند 4، متابعة الأولوية 2) مورّدٌ معنون لم يُحلّ صارمًا (`unresolved_supplier`) → **حلّ جريء**
        (WRatio ≥ ٧٠) نظيرَ الخزينة: يطبّق أفضل مرشّح + تنبيه المسؤول + deviation_log + تعلّم alias.
        الخطّان الأحمران داخل resolve_bold (الموردون قائمة مستقلّة، والعامّ وحده لا يطابق). «فكهتني».

        🔴 **ملغى بقرار المالك (2026-07-19)** — نظير الخزينة: bold_resolve_enabled=False، تصعيد لا تخمين."""
        if not self._bold_resolve_enabled:
            return                              # ملغى — لا تخمين، الاسم يبقى unresolved فيُصعَّد
        if leg is None or not leg.unresolved_supplier:
            return
        original = leg.unresolved_supplier
        rec, score = resolve_bold(original, suppliers, threshold=self._AUTO_RESOLVE_THRESHOLD)
        if rec is None:
            return
        from .models import SupplierRef
        from .parsing.normalize import normalize_ar
        leg.supplier = SupplierRef(code=rec.code, name=rec.name)
        leg.unresolved_supplier = None
        leg.deviation_log.append({"field": "supplier", "raw_value": original,
                                  "extracted_value": rec.name, "method": "similarity",
                                  "confidence": score})
        ref = leg.reference_number or "؟"
        await self.bus.notify_admin(
            f"⚠️ {ref} نُزّلت — تشابه اسم مورّد: «{original}» ← «{rec.name}» (score {score}). "
            f"للتراجع: احذف الإملاء المتعلَّم من الداشبورد.", raw.message_key)
        learned = normalize_ar(original)
        if learned and learned != normalize_ar(rec.name):
            await self.db.suppliers.add_alias(rec.name, learned)
        log.info("(بند 4) حلّ مورّد جريء: «%s» ← «%s» (%d) — نُزّلت وتُعلّم", original, rec.name, score)

    @staticmethod
    def _value_matches(room_amt: Optional[float], deal_amt: Optional[float]) -> bool:
        """قيمة القروب ≈ الإجمالي **أو** ≈ ×0.99 (صافي خصم 1%)، بهامش تقريب بسيط للكسور (§قروب الخزينة)."""
        if room_amt is None or deal_amt is None:
            return False
        for target in (deal_amt, deal_amt * 0.99):
            if abs(room_amt - target) <= max(1.0, target * 0.005):
                return True
        return False

    async def _room_match_treasury(self, deal: Deal, now: datetime, treasuries: list,
                                   suppliers: list, window: Optional[float] = None) -> bool:
        """(طبقة قروب الخزينة، توجيه المالك) خزينةٌ لم تُحلّ من النص → ابحث رسائل **قروبات الخزائن**
        ضمن النافذة (±room_match_window) عن رسالة تطابق **هاتف + قيمة** الحوالة (الإجمالي أو ×0.99
        صافي، بهامش تقريب). المرجع تعزيزٌ لا شرط. المطابقة → خزينة القروب + تنزيل + تنبيه المسؤول
        «حُلّت من قروب» + resolved_by=room_match. تعدّد القروبات → الأقرب زمنيًّا + ملاحظة للمراجعة.
        يُرجِع True إن حُلّت. الاسم الصريح في النص يغلب (لا نصل هنا إلا وخزينة الطرف None). المفتاح
        والنافذة من إعداد الداشبورد (db.detection، hot-reload)؛ `window` يتجاوزهما إن مُرِّر (للاختبار)."""
        _cfg = await self.db.detection.get()
        if not getattr(_cfg, "room_match_enabled", True):
            return False
        leg = deal.sell_leg or deal.buy_leg
        if leg is None or leg.treasury is not None or not leg.phone or leg.amount is None:
            return False
        tphone = self._digits(leg.phone)[-9:]
        if not tphone:
            return False
        anchor = _as_naive_utc(deal.first_received_at or deal.created_at or now)
        _w = window if window is not None else getattr(_cfg, "room_match_window_seconds", self._room_match_window)
        win = timedelta(seconds=_w)
        lo, hi = anchor - win, anchor + win
        rooms = [r for r in await self.db.rooms.all()
                 if r.type == RoomType.TREASURY and r.active and r.treasury_code]
        if not rooms:
            return False
        by_code = {t.code: t for t in treasuries}
        matches = []   # (|Δt|, treasury_code, room_name)
        for room in rooms:
            cur = self.db.raw.col.find(
                {"chat_jid": room.jid, "received_at": {"$gte": lo, "$lte": hi}})
            async for m in cur:
                rl = parse_message(m.get("text") or "", treasuries, suppliers).leg
                if rl is None or not rl.phone or rl.amount is None:
                    continue
                if self._digits(rl.phone)[-9:] != tphone or not self._value_matches(rl.amount, leg.amount):
                    continue
                dt = abs((_as_naive_utc(m.get("received_at")) - anchor).total_seconds())
                matches.append((dt, room.treasury_code, room.name))
        if not matches:
            return False
        matches.sort(key=lambda x: x[0])            # الأقرب زمنيًّا أولًا (لا يوقف الحوالة)
        dt, code, rname = matches[0]
        trec = by_code.get(code)
        if trec is None:
            return False
        leg.treasury = TreasuryRef(code=trec.code, name=trec.name, type=trec.type, currency=trec.currency)
        if leg.currency is None:
            leg.currency = trec.currency
        leg.unresolved_treasury = None
        leg.deviation_log.append({"field": "treasury", "raw_value": "room_match",
                                  "extracted_value": trec.name, "method": "room_match", "confidence": 100})
        await self.db.deals.upsert(deal)
        distinct = {c for _, c, _ in matches}
        multi = f" (وُجد في {len(distinct)} قروبات — راجعه إن شئت)" if len(distinct) > 1 else ""
        await self.bus.notify_admin(
            f"⚠️ {leg.reference_number or '؟'} — حُلّت الخزينة من قروب «{rname}»{multi} "
            f"(مطابقة هاتف+قيمة، resolved_by=room_match).", self._deal_key(deal))
        log.info("(قروب الخزينة) حُلّت %s ← قروب «%s» (code %s، Δ%.1fs)",
                 leg.reference_number, rname, code, dt)
        return True

    async def _deal_source_text(self, deal: Deal) -> Optional[str]:
        """نصّ الرسالة (أو الرسالتين) التي وُلِدت منها الصفقة — مدخل الفهم الذكي والتحقّق من الأرقام."""
        keys: list[str] = []
        for lg in (deal.sell_leg, deal.buy_leg):
            if lg is not None and lg.source_message_key:
                keys.append(lg.source_message_key)
        for k in deal.source_message_keys:
            if k not in keys:
                keys.append(k)
        parts: list[str] = []
        for k in keys:
            try:
                raw = await self.db.raw.get(k)
            except Exception as exc:      # T5 — لا نبتلع؛ بلا نصّ ⇒ لا فهم ذكيّ (تصعيد عاديّ)
                log.warning("(الفهم الذكي) تعذّر جلب نصّ الرسالة %s: %s", k, exc)
                continue
            if raw is not None and (raw.text or "").strip():
                parts.append(raw.text.strip())
        return "\n".join(parts) if parts else None

    # ═════════════════════════════════════════════════════════════════════════
    # AI-first للحالات الغامضة (§1-§3، قرار المالك 2026-07-20)
    # ═════════════════════════════════════════════════════════════════════════
    def _pair_partner(self, raw: RawMessage, batch: list[RawMessage],
                      treasuries: list, suppliers: list,
                      window_seconds: Optional[float] = None) -> Optional[RawMessage]:
        """تكملة الرسالة `raw` من نفس المُرسِل داخل الدفعة — أو None إن لم تصل بعد.

        🔴 القيد الحاسم (بلاغ إنتاج): **رسالةٌ تحمل مرجعًا (X/A/SI####) هي بداية صفقة
           جديدة لا تكملة** — مهما كان توقيتها. التكملة الحقيقيّة لا تحمل مرجعًا في الغالب
           (سعر + خزينة فقط: «390 العربي34.5 / محمود»).
           هذا يُفحص بـ`_has_reference` على النصّ **مستقلًّا عن نجاح التفكيك**. الصياغة
           السابقة كانت تشترط `kind=="transfer"` أيضًا، فرسالةٌ أولى تحمل مرجعًا لكنها
           تُفكَّك «noise» (شائع تحت الـburst) كانت تُقبَل تكملةً خطأً — فيرى الذكاء صفقتين
           مخلوطتين. الاستثناء الوحيد: **نفس** المرجع (الشكل الموثَّق: المرجع مكرَّر في
           الرسالتين).

        🔴 وحدٌ زمنيّ: المرشّح يجب أن يصل خلال نافذة الانتظار نفسها؛ بلا هذا القيد كانت
           رسالةٌ بعد دقائق (في نفس الدفعة) تُقبَل تكملةً.
        """
        own_ref = None
        try:
            own = parse_message(raw.text or "", treasuries, suppliers)
            own_ref = own.leg.reference_number if own.leg is not None else None
        except Exception:      # noqa: BLE001 — التفكيك لا يوقف الضمّ
            pass
        base = _as_naive_utc(raw.received_at)
        # ترتيب زمنيّ صريح: «الأولى بعدها» يجب ألّا تتعلّق بترتيب الدفعة.
        for cand in sorted(batch, key=lambda c: _as_naive_utc(c.received_at)):
            if cand.message_key == raw.message_key or cand.is_from_me:
                continue
            if cand.chat_jid != raw.chat_jid or cand.sender_jid != raw.sender_jid:
                continue
            gap = (_as_naive_utc(cand.received_at) - base).total_seconds()
            if gap <= 0:
                continue
            if window_seconds is not None and gap > float(window_seconds):
                return None    # خارج نافذة الانتظار — ليست تكملتها
            text = cand.text or ""
            if _has_reference(text) and not (own_ref and own_ref in text):
                # بداية صفقة جديدة (X1328/SI4721) — لا تُضمّ، ولا نتخطّاها إلى ما بعدها:
                # التكملة تلي أصلها مباشرةً، وأيّ قفزٍ فوق صفقة أخرى تخمينٌ في الهويّة.
                return None
            return cand
        return None

    async def _ai_pair_wait(self, raw: RawMessage, batch: list[RawMessage], now: datetime,
                            lists: tuple[list, list]) -> bool:
        """(§2) هل نؤجّل هذه الرسالة انتظارًا لتكملتها؟ True ⇒ تُترك للدورة التالية.

        🔴 لا `asyncio.sleep`: كل `process_inbox` داخل `_inbox_lock`، فنومُ 8 ثوانٍ يجمّد الوارد
           كلّه لكل رسالة غامضة. نستعمل بدلها آليّة التأجيل القائمة (نفس أسلوب `is_stable`):
           الرسالة تبقى غير معالَجة فتُلتقَط بعد ثانيتين، حتى تصل التكملة أو تنقضي المهلة.
        """
        cfg = await self.db.detection.get()
        if not getattr(cfg, "ai_first_enabled", False) or not getattr(cfg, "ai_enabled", False):
            return False
        treasuries, suppliers = lists
        try:
            result = parse_message(raw.text or "", treasuries, suppliers)
        except Exception:      # noqa: BLE001 — فشل التفكيك يُعالَج في _ingest
            return False
        verdict = detect_ambiguity(
            raw.text or "", result, treasuries, suppliers,
            min_confidence=float(getattr(cfg, "ai_min_parse_confidence", 0.7)))
        if not verdict.ambiguous:
            return False
        wait = float(getattr(cfg, "ai_pair_wait_seconds", 3.0))
        if self._pair_partner(raw, batch, treasuries, suppliers, window_seconds=wait) is not None:
            return False       # التكملة حاضرة ⇒ لا انتظار، تُضمّان الآن
        waited = (_as_naive_utc(now) - _as_naive_utc(raw.received_at)).total_seconds()
        if waited >= wait:
            return False       # انقضت المهلة ⇒ تمضي وحدها للذكاء
        log.debug("(AI-first) تأجيل %s انتظارًا للتكملة (%.1fث/%.1fث) — %s",
                  raw.message_key, waited, wait, verdict.as_note())
        return True

    async def _ai_first(self, raw: RawMessage, result, verdict, treasuries: list,
                        suppliers: list, partner: Optional[RawMessage], now: datetime):
        """(§3) مسار الذكاء: الرسالتان معًا → اقتراح → تحقّق حتميّ → تطبيق أو تصعيد.

        يُرجِع `ParseResult` مُصحَّحًا عند النجاح، أو None فيمضي المسار الحتميّ كما هو (fail-open §6).
        """
        cfg = await self.db.detection.get()
        text = (raw.text or "").strip()
        if not text:
            return None
        texts = [text] + ([partner.text.strip()] if partner is not None
                          and (partner.text or "").strip() else [])
        combined = join_pair(texts)
        is_pair = len(texts) > 1

        client = self._ai_client or OpenRouterClient(
            api_key=get_settings().openrouter_api_key,
            model=getattr(cfg, "ai_model", "google/gemini-2.5-flash"),
            timeout=float(getattr(cfg, "ai_timeout_seconds", 10.0)))
        sctx = await build_sender_context(self.db, raw.sender_jid, _as_naive_utc(raw.received_at))
        try:
            prop = await client.propose(combined, treasuries, suppliers,
                                        sender_context=sctx, pair=is_pair)
        except Exception as exc:      # §6 fail-open — لا رسالة تتوقّف على API خارجيّ
            log.warning("(AI-first) استثناء في النداء (%s): %s — مسار حتميّ", raw.message_key, exc)
            return None
        if prop is None:
            log.info("(AI-first) لا اقتراح (فشل/مهلة) لـ%s — مسار حتميّ", raw.message_key)
            return None

        threshold = float(getattr(cfg, "ai_confidence_threshold", 0.9))
        # 🔴 التحقّق الحتميّ (§3د) — الكود هو الحَكَم: كل كيان مسجَّل بمطابقة تامّة، وكل رقم
        #    موجود حرفيًّا في **النصّ المضموم** (أيّ من الرسالتين). لا استثناء لأيّ حقل.
        av = validate_proposal(prop, combined, treasuries, suppliers, threshold, require=())

        # 🔴 حارس الدور (حادثة XI1321): خزينةٌ اشتُقّت من رمزٍ مستهلَك في دورٍ آخر تُرفض،
        #    مهما بلغت ثقتها ومهما كانت مسجَّلة. التحقّق الحتميّ وحده لا يرى هذا الخطأ.
        if av.ok and av.treasury is not None:
            _tok = self._treasury_source_token(prop, av.treasury, combined)
            _conflict = treasury_role_conflict(_tok, combined,
                                               result.leg if result is not None else None)
            if _conflict is not None:
                av = type(av)(False, f"الرمز «{_tok}» مستهلَك في دور «{_conflict}» — "
                                     f"لا يصلح خزينةً (حارس الدور، XI1321)")

        if not av.ok:
            await self.bus.notify_admin(
                f"⚠️ حوالة غامضة ({verdict.as_note()}) — الذكاء اقترح ولم يجتز التحقّق: "
                f"{av.reason}. الاقتراح: {prop.summary()}. لم تُطبَّق — يرجى المراجعة.",
                raw.message_key, forward_key=raw.message_key)
            log.info("(AI-first) رُفض اقتراح %s: %s", raw.message_key, av.reason)
            return None

        # إعادة التفكيك حتميًّا بعد إضافة الإملاءات المُتحقَّق منها — الأرقام والأدوار
        # يستخرجها المفكِّك من النصّ، لا النموذج.
        fixes = validate_corrections(prop, combined, treasuries, suppliers, threshold)
        t_list, s_list = treasuries, suppliers
        for c in fixes:
            if c.entity_type == "supplier":
                s_list = with_ephemeral_alias(s_list, c.record, c.raw)
            else:
                t_list = with_ephemeral_alias(t_list, c.record, c.raw)
        res = parse_message(text, t_list, s_list)
        if res.kind != "transfer" or res.leg is None:
            log.info("(AI-first) إعادة التفكيك لم تُنتج حوالة (%s) — مسار حتميّ", raw.message_key)
            return None
        if res.leg.amount is not None and not _num_in_text(res.leg.amount, combined):
            log.warning("(AI-first) رُفض: المبلغ %s ليس في نصّ أيّ من الرسالتين", res.leg.amount)
            return None

        # deviation_log (§6): سبب التصنيف غامضةً + المدخل + المخرج + الثقة
        res.leg.deviation_log.append({
            "field": "ai_first",
            "raw_value": verdict.as_note(),
            "extracted_value": prop.summary(),
            "method": "ai_first",
            "confidence": int(threshold * 100),
            "model": prop.model,
            "stage": verdict.stage,
            "paired": is_pair,
        })
        await self.bus.notify_admin(
            f"⚠️ {res.leg.reference_number or '؟'} فُهمت بالذكاء الاصطناعي ({prop.model}) — "
            f"سبب التصنيف غامضةً: {verdict.as_note()}"
            f"{'؛ فُهمت مع رسالتها الثانية معًا' if is_pair else ''}. "
            f"الناتج: {prop.summary()}. راجعها.",
            raw.message_key, forward_key=raw.message_key)
        log.info("(AI-first) طُبِّق على %s (سبب=%s، مزدوجة=%s، نموذج=%s)",
                 raw.message_key, verdict.as_note(), is_pair, prop.model)
        return res

    @staticmethod
    def _treasury_source_token(prop, rec, text: str) -> Optional[str]:
        """الرمز في نصّ الرسالة الذي اشتُقّت منه الخزينة المقترحة — مدخل حارس الدور.

        الأولويّة: تصحيحٌ صريح من النموذج (يحمل `raw` كما ورد)، وإلّا أوّل صيغة من صيغ
        السجلّ (الاسم الرسميّ أو إملاء بديل) تظهر فعلًا في النصّ. None ⇒ لا رمز يمكن
        نسبته (فلا يعمل الحارس، ويبقى التحقّق الحتميّ وحده).
        """
        from .parsing.resolve import normalize_arabic_for_matching as _nrm

        items = (prop.data or {}).get("corrections")
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                if (it.get("entity_type") or "").strip().lower() != "treasury":
                    continue
                raw_tok = (it.get("raw") or "").strip()
                if raw_tok:
                    return raw_tok
        ntext = _nrm(text or "")
        for form in [getattr(rec, "name", None)] + list(getattr(rec, "aliases", []) or []):
            nf = _nrm(form or "")
            if nf and nf in ntext:
                return form
        return None

    async def _ai_link_choice(self, raw: RawMessage, cands: list[Deal],
                              treasuries: list, suppliers: list, now: datetime):
        """(§5) يعرض التكملة + بيانات الصفقتين المعلّقتين على النموذج ويطلب اختيارًا مبرَّرًا.

        يُرجِع `LinkChoice` أو None (⇒ السلوك الحتميّ القائم). لا يُطبَّق أيّ اختيار قبل التحقّق
        من أن الفهرس ضمن المدى فعلًا؛ ودون عتبة الحسم الدنيا يُترك القرار لـFIFO كما كان.
        """
        cfg = await self.db.detection.get()
        if not getattr(cfg, "ai_first_enabled", False) or not getattr(cfg, "ai_enabled", False):
            return None
        lines = []
        for i, d in enumerate(cands):
            lg = d.sell_leg or d.buy_leg
            lines.append(
                f"{i}) مرجع {self._ref(d) or '؟'} — زبون {(lg.customer_code if lg else None) or '؟'} "
                f"{(lg.customer_name if lg else None) or ''} — مبلغ "
                f"{(lg.amount if lg else None) or '؟'} "
                f"{getattr(getattr(lg, 'currency', None), 'value', '') or ''} — "
                f"وصلت {_as_naive_utc(d.first_received_at or d.created_at)}")
        ask = (
            f"{(raw.text or '').strip()}\n\n"
            f"الصفقات المعلّقة المرشّحة (اختر واحدة بفهرسها):\n" + "\n".join(lines) +
            "\n\nأضِف إلى JSON حقلين: \"link_index\": <فهرس الصفقة>، "
            "\"link_confidence\": <0.0-1.0>. إن لم تترجّح واحدة بوضوح فاجعل الثقة منخفضة."
        )
        client = self._ai_client or OpenRouterClient(
            api_key=get_settings().openrouter_api_key,
            model=getattr(cfg, "ai_model", "google/gemini-2.5-flash"),
            timeout=float(getattr(cfg, "ai_timeout_seconds", 10.0)))
        sctx = await build_sender_context(self.db, raw.sender_jid, _as_naive_utc(raw.received_at))
        try:
            prop = await client.propose(ask, treasuries, suppliers, sender_context=sctx)
        except Exception as exc:      # §6 fail-open
            log.warning("(AI-first §5) استثناء في نداء الربط: %s — FIFO الحتميّ", exc)
            return None
        if prop is None:
            return None
        choice = validate_link_choice(
            prop, len(cands),
            auto_threshold=float(getattr(cfg, "ai_link_auto_threshold", 0.95)),
            min_threshold=float(getattr(cfg, "ai_link_min_threshold", 0.80)))
        log.info("(AI-first §5) قرار الربط: %s (فهرس=%s، ثقة=%.2f، %s)",
                 choice.action, choice.index, choice.confidence, choice.reason)
        return choice

    async def _choose_link_target(self, raw: RawMessage, cands: list[Deal],
                                  treasuries: list, suppliers: list, now: datetime, *,
                                  text_ref: str | None = None, stage: str = "link") -> Deal | None:
        """(R1+R2+R3) نقطة التحكيم **الوحيدة** لاختيار هدف التكملة.

        الترتيب صارم: مرجعٌ صريح ⇒ حسمٌ فوريّ (القائمة مُصفّاة بالمرجع سلفًا في طبقة الطابور،
        و[] تعني رفض ربط لا «لا مرشّحين»). ثمّ مرشَّحٌ واحد ⇒ هو. ثمّ **الذكاء يرجّح بالمحتوى**
        عند التعدّد (حادثة X1571: FIFO أعطت تكملتَها لـX1568 بلا فحص محتوى). وأخيرًا FIFO
        احتياطًا حين يكون الذكاء مطفأً/فاشلًا/دون العتبة — السلوك القائم بلا تغيير."""
        if not cands:
            _log_link_decision(stage, raw.message_key, [], None,
                               "rejected-ref-mismatch" if text_ref else "no-candidates", text_ref)
            return None
        # (R1) يُعاد تطبيق الحارس هنا **عمدًا** رغم أنّ طبقة الطابور تُصفّي سلفًا: الاعتماد على
        #      «القائمة مُصفّاة» يجعل صحّة الربط رهنًا بانضباط كلّ مُستدعٍ. أثبت اختبار التحوّل
        #      (تعطيل الحارس في الطابور) أنّ هذه الدالة كانت تُسلّم cands[0] بثقةٍ عمياء.
        bound = bind_by_reference(cands, text_ref)
        if bound is not None:
            matched, reason = bound
            chosen = matched[0] if matched else None
            _log_link_decision(stage, raw.message_key, cands, chosen, reason, text_ref)
            return chosen
        if len(cands) == 1:
            _log_link_decision(stage, raw.message_key, cands, cands[0], "single-candidate")
            return cands[0]
        choice = await self._ai_link_choice(raw, cands, treasuries, suppliers, now)
        if choice is not None and choice.action == "auto":
            chosen = cands[choice.index]
            _log_link_decision(stage, raw.message_key, cands, chosen, f"ai-auto:{choice.confidence:.2f}")
            await self.bus.notify_admin(
                f"⚠️ تكملة بلا رقم إشاري + {len(cands)} صفقات معلّقة — رُبطت بالذكاء "
                f"(ثقة {choice.confidence:.2f}) بصفقة {self._ref(chosen) or '؟'}. راجعها.",
                raw.message_key, forward_key=raw.message_key)
            return chosen
        if choice is not None and choice.action == "ask":
            _log_link_decision(stage, raw.message_key, cands, cands[choice.index],
                               f"ai-ask:{choice.confidence:.2f}")
            opts = "\n".join(
                f"{i+1}) {self._ref(c) or '؟'} — "
                f"{(c.sell_leg.customer_name if c.sell_leg else None) or '؟'}"
                for i, c in enumerate(cands))
            await self.bus.notify_admin(
                f"⚠️ تكملة بلا رقم إشاري + {len(cands)} صفقات معلّقة. الذكاء يرجّح "
                f"الخيار {choice.index + 1} بثقة {choice.confidence:.2f} (دون الحسم). "
                f"أيّها؟ **رد برقم الخيار**:\n{opts}",
                raw.message_key, forward_key=raw.message_key)
            return None
        _log_link_decision(stage, raw.message_key, cands, cands[0], "fifo-fallback")
        return cands[0]

    async def _ai_rescue_parse(self, raw: RawMessage, treasuries: list, suppliers: list):
        """(الفهم الذكي — توسعة X1325) رسالة أولى تحمل مرجعًا وفشل تفكيكها → صحّح كلمة العملة
        ثم أعِد التفكيك حتميًّا. يُرجِع ParseResult ناجحًا أو None (فيمضي التصعيد كما هو).

        🔴 الضوابط: النموذج **لا يعيد صياغة النصّ** — يقترح استبدالات رمزيّة فقط، والكود يتحقّق
           أن الخام موجود حرفيًّا وأن البديل من قائمة عملات مغلقة وأنه امتداد للخام (جني→جنيه).
           وبعد إعادة التفكيك: **المبلغ المستخرَج يجب أن يكون موجودًا في النصّ الأصليّ** — فلا
           يخلق «تصحيحٌ» رقمًا لم يكتبه الموظّف.
        """
        cfg = await self.db.detection.get()
        if not getattr(cfg, "ai_enabled", False):
            return None
        text = (raw.text or "").strip()
        if not text:
            return None
        client = self._ai_client or OpenRouterClient(
            api_key=get_settings().openrouter_api_key,
            model=getattr(cfg, "ai_model", "google/gemini-2.5-flash"),
            timeout=float(getattr(cfg, "ai_timeout_seconds", 10.0)))
        # السياق الحيّ (قاموس حيّ + تاريخ المُرسِل) — يُبنى من DB لحظة النداء، بلا cache.
        sctx = await build_sender_context(self.db, raw.sender_jid, _as_naive_utc(raw.received_at))
        try:
            prop = await client.propose(text, treasuries, suppliers, sender_context=sctx)
        except Exception as exc:      # T5 — الصمود: لا رسالة تتوقّف على API خارجيّ
            log.warning("(الفهم الذكي) استثناء في نداء إنقاذ التفكيك: %s", exc)
            return None
        if prop is None:
            return None
        threshold = float(getattr(cfg, "ai_confidence_threshold", 0.9))

        # ═══ (ج) السطر الملتصق: «1160عبد السلام زكري6.04» → {كود، اسم، سعر} ═══
        # التحقّق الحتميّ يضمن أن كل جزء **مقطعٌ من السطر نفسه** (تفكيك لا اختراع). نطبّقه
        # كفصلٍ بالمسافات على النصّ، ثم يُعيد المفكِّك الحتميّ قراءته كأنه كُتب مفصولًا.
        lines = validate_line_parse(prop, text, threshold, entities=suppliers + treasuries)
        if lines:
            spaced = text
            for ln in lines:
                repl = " ".join(p for p in (ln.code, ln.name, ln.price) if p)
                spaced = spaced.replace(ln.raw, repl, 1)
            if spaced != text:
                res_l = parse_message(spaced, treasuries, suppliers)
                if res_l.kind == "transfer" and res_l.leg is not None \
                        and res_l.leg.amount is not None and _num_in_text(res_l.leg.amount, text):
                    _names = "، ".join(f"«{l.raw}» → {l.code} {l.name} {l.price or ''}".strip()
                                       for l in lines)
                    await self.bus.notify_admin(
                        f"⚠️ {res_l.leg.reference_number or '؟'} فُهمت بالذكاء الاصطناعي "
                        f"({prop.model}): فُكّ سطر ملتصق — {_names}.", raw.message_key)
                    log.info("(الفهم الذكي) فُكّ سطر ملتصق في %s: %s", raw.message_key, _names)
                    return res_l

        fixes = validate_text_corrections(prop, text, threshold)
        if not fixes:
            # ═══ حوالة بريد (بلا رقم مستلم) — يُحسم بتاريخ المُرسِل لا بالتخمين ═══
            postal = validate_postal(prop, threshold)
            if postal is True and not sctx.is_new_sender() and sctx.no_phone_ratio > 0:
                log.info("(الفهم الذكي) %s: نمط «بريد بلا رقم» مرجَّح بتاريخ المُرسِل "
                         "(نسبة %.0f%%) — تُكمَل بلا هاتف", raw.message_key,
                         sctx.no_phone_ratio * 100)
                await self.bus.notify_admin(
                    f"⚠️ فُهمت بالذكاء الاصطناعي ({prop.model}): حوالة **بريد بلا رقم مستلم** "
                    f"(المُرسِل {sctx.no_phone_ratio:.0%} من حوالاته بريد) — تُسجَّل بلا هاتف. راجعها.",
                    raw.message_key, forward_key=raw.message_key)
                return None      # الحسم للمسار الحتميّ؛ هذا تنبيهٌ لا تعديل بيانات
            if postal is not None or prop.data.get("is_postal") is not None:
                await self.bus.notify_admin(
                    f"⚠️ رسالة بلا رقم مستلم ({raw.message_key}) — **هل هذه حوالة بريد؟** "
                    f"(المُرسِل {'جديد بلا تاريخ' if sctx.is_new_sender() else 'تاريخه لا يرجّح البريد'})"
                    f" — لم تُكمَل تلقائيًّا.", raw.message_key, forward_key=raw.message_key)
            return None
        fixed_text = apply_text_corrections(text, fixes)
        if fixed_text == text:
            return None
        res = parse_message(fixed_text, treasuries, suppliers)
        if res.kind != "transfer" or res.leg is None or res.leg.amount is None:
            log.info("(الفهم الذكي) تصحيح العملة لم يُنتج حوالةً مفكَّكة (%s) — تصعيد عاديّ",
                     raw.message_key)
            return None
        # 🔴 الخط الأحمر: المبلغ الناتج موجود في النصّ **الأصليّ** (لا رقم مخترَع)
        if not _num_in_text(res.leg.amount, text):
            log.warning("(الفهم الذكي) رُفض إنقاذ التفكيك: المبلغ %s ليس في النصّ الأصليّ",
                        res.leg.amount)
            return None
        names = "، ".join(f"«{a}» ← «{b}»" for a, b in fixes)
        await self.bus.notify_admin(
            f"⚠️ {res.leg.reference_number or '؟'} فُهمت بالذكاء الاصطناعي ({prop.model}): "
            f"صُحّحت كلمة العملة {names}، ثم أُعيد التفكيك حتميًّا — "
            f"مبلغ {res.leg.amount:g} {getattr(res.leg.currency, 'value', '؟')}.",
            raw.message_key)
        log.info("(الفهم الذكي) أُنقِذ تفكيك %s بتصحيح %s (مبلغ=%s)",
                 raw.message_key, names, res.leg.amount)
        return res

    async def _ai_replay_with_corrections(self, deal: Deal, corrections: list,
                                          prop, now: datetime) -> bool:
        """(المسار أ) يعيد تفكيك **الرسالة الثانية** بعد تصحيح إملاء الكيان — حتميًّا بالكامل.

        الحالة النموذجيّة (م: XI1321): رسالة ثانية بسطرَي «كود+اسم+سعر» (زبون + مورد) فشل فيها
        سطرُ المورد لخطأ إملائيّ («عد الدين العجيلي» ← «عز الدين»)، فاستُخرج سطرٌ واحد بدل سطرين،
        فلم يُبنَ طرف الشراء ولا اشتُقّت الخزينة ⇒ «لا خزينة محلولة» وتصعيد.

        العلاج: نضيف النصّ الخام إملاءً بديلًا **مؤقّتًا في الذاكرة فقط**، ثم نستدعي نفس الدالة
        التي كان المسار العاديّ سيستدعيها (`absorb_customer_supplier`) — فتُحسَب الأسعار والعمولة
        والخزينة بالمنطق الحتميّ القائم، لا بالنموذج. يُرجِع True إن نجحت الإعادة فعلًا.
        """
        sup_fixes = [c for c in corrections if c.entity_type == "supplier"]
        if not sup_fixes or deal.sell_leg is None:
            return False
        treasuries = await self.db.treasuries.all_active()
        suppliers = await self.db.suppliers.all_active()
        for c in sup_fixes:
            suppliers = with_ephemeral_alias(suppliers, c.record, c.raw)

        # الرسالة الثانية = آخر مفتاح مصدر يحمل نصًّا بسطرَي كود+اسم+سعر بعد التصحيح
        for key in reversed(deal.source_message_keys or []):
            raw = await self.db.raw.get(key)
            if raw is None or not (raw.text or "").strip():
                continue
            pairs = extract_code_name_price_lines(raw.text, suppliers)
            if len(pairs) < 2:
                continue
            before_code = deal.sell_leg.customer_code
            merged = await self.queue.absorb_customer_supplier(
                deal, pairs[0], pairs[1], key, treasuries, now)
            merged = await self.db.deals.get(merged.deal_id) or merged
            lg = merged.sell_leg or merged.buy_leg
            if lg is None or lg.treasury is None:
                # الإعادة لم تُنتج خزينة ⇒ لا إنقاذ (التصعيد يمضي). لا نصمت (T5).
                log.info("(الفهم الذكي) إعادة التفكيك لم تُنتج خزينة للصفقة %s — تصعيد عاديّ",
                         deal.deal_id)
                return False
            names = "، ".join(f"«{c.raw}» ← «{c.official}»" for c in sup_fixes)
            lg.deviation_log.append({
                "field": "treasury", "raw_value": c.raw, "extracted_value": lg.treasury.name,
                "method": "ai", "confidence": int(c.confidence * 100), "model": prop.model,
                "correction": names,
            })
            await self.db.deals.upsert(merged)
            await self.bus.notify_admin(
                f"⚠️ {self._ref(merged)} فُهمت بالذكاء الاصطناعي ({prop.model}): "
                f"صُحّح الإملاء {names}، ثم أُعيد التفكيك حتميًّا — "
                f"زبون {merged.sell_leg.customer_code or '؟'} "
                f"{merged.sell_leg.customer_name or ''}، خزينة «{lg.treasury.name}».",
                self._deal_key(merged),
            )
            log.info("(الفهم الذكي) أُعيد تفكيك %s بعد تصحيح %s (زبون %s→%s، خزينة %s، نموذج %s)",
                     deal.deal_id, names, before_code, merged.sell_leg.customer_code,
                     lg.treasury.name, prop.model)
            # حدِّث المرجع الحيّ في المُستدعي
            deal.sell_leg, deal.buy_leg = merged.sell_leg, merged.buy_leg
            deal.is_two_legged, deal.status = merged.is_two_legged, merged.status
            return True
        return False

    async def _ai_rescue(self, deal: Deal, now: datetime) -> bool:
        """(طبقة الفهم الذكي — إنقاذ فقط) الصفقة على وشك التصعيد لعدم حلّ الخزينة؟ اسأل النموذج
        عبر OpenRouter، ثم **تحقّق حتميًّا** من اقتراحه قبل استعماله.

        🔴 الحدود المطلقة (§0): لا تُستدعى إلا على مسارٍ كان سيُصعَّد أصلًا؛ لا تلمس حقلًا حسمه الفهم
           الحتميّ (تُملأ الخزينة فقط إن كانت None)؛ الكيان لازم مسجَّل؛ الرقم لازم في النصّ؛ الثقة
           فوق العتبة. أيّ إخفاق (نداء/ردّ/تحقّق) → False ⇒ التصعيد يمضي كأن الطبقة غير موجودة.

        يُرجِع True إن أُنقِذت الصفقة (خزينة محلولة + تنبيه المسؤول + resolved_by=ai).
        """
        cfg = await self.db.detection.get()          # hot-reload من اللوحة (بلا إعادة تشغيل)
        if not getattr(cfg, "ai_enabled", False):
            return False
        leg = deal.sell_leg or deal.buy_leg
        if leg is None or leg.treasury is not None:  # لا شيء لإنقاذه
            return False
        text = await self._deal_source_text(deal)
        if not text:
            log.info("(الفهم الذكي) بلا نصّ مصدر للصفقة %s — تصعيد عاديّ", deal.deal_id)
            return False

        client = self._ai_client or OpenRouterClient(
            api_key=get_settings().openrouter_api_key,
            model=getattr(cfg, "ai_model", "google/gemini-2.5-flash"),
            timeout=float(getattr(cfg, "ai_timeout_seconds", 10.0)),
        )
        # 🔴 القوائم تُقرأ من DB **لحظة النداء** (لا cache): خزينة/مورد أُضيف قبل ثانية يظهر هنا.
        treasuries = await self.db.treasuries.all_active()
        suppliers = await self.db.suppliers.all_active()
        _sender = (leg.sender_jid if leg is not None else None) or deal.chat_jid
        sctx = await build_sender_context(self.db, _sender, _as_naive_utc(now))
        try:
            prop = await client.propose(text, treasuries, suppliers, sender_context=sctx)
        except Exception as exc:      # T5 — الصمود: لا حوالة تتوقّف على API خارجيّ
            log.warning("(الفهم الذكي) استثناء غير متوقّع في النداء: %s — تصعيد عاديّ", exc)
            return False
        if prop is None:
            return False

        threshold = float(getattr(cfg, "ai_confidence_threshold", 0.9))

        # ═══ المسار (أ) — تصحيح إملاء + **إعادة التفكيك الحتميّ** (المفضَّل) ═══
        # النموذج يردّ الاسم المكتوب خطأً إلى سجلّ مسجَّل؛ ثم نُعيد تشغيل نفس مكانيكا الدمج
        # القائمة على النصّ الأصليّ كما لو كتبه الموظّف صحيحًا. النموذج لا يقدّم أيّ رقم هنا:
        # المبالغ والأسعار والأدوار والخزينة كلها من المفكِّك الحتميّ (§0).
        corrections = validate_corrections(prop, text, treasuries, suppliers, threshold)
        if corrections and await self._ai_replay_with_corrections(deal, corrections, prop, now):
            return True

        # ═══ المسار (ب) — تعبئة الخزينة مباشرةً، **مقيَّدة بشدّة** ═══
        # 🔴 قاعدة مبدئيّة (بعد ملاحظة حيّة على XI1321): النموذج **لا يُسنِد دورًا** أبدًا، بل
        #    يصحّح إملاء رمزٍ أسنَد المفكِّكُ الحتميّ دورَه سلفًا. في XI1321 اقترح النموذج بثقة 1.0
        #    خزينة «صالح جربة تونس» من سطر الموقع «جربه/ميدون» — وهي ليست خزينة الحوالة. لذلك
        #    لا نقبل خزينةً من النموذج إلا إذا كان المفكِّك قد وسم رمزًا بأنه **اسم خزينة تعذّر
        #    حلّه** (unresolved_treasury) — أي الدور محسوم حتميًّا والنموذج يصحّح الهجاء فقط.
        if not (leg.unresolved_treasury or "").strip():
            log.info("(الفهم الذكي) لا رمز خزينة موسوم للصفقة %s — لا تُقبَل خزينة من النموذج "
                     "(منع إسناد الأدوار) ⇒ تصعيد عاديّ", deal.deal_id)
            return False

        verdict = validate_proposal(prop, text, treasuries, suppliers, threshold,
                                    require=("treasury",))
        if not verdict.ok:
            # فشل التحقّق → تصعيد **مع إرفاق اقتراح النموذج** مساعدةً للمسؤول (بند 3).
            log.warning("(الفهم الذكي) رُفض اقتراح %s للصفقة %s: %s",
                        prop.model, deal.deal_id, verdict.reason)
            await self.bus.notify_admin(
                f"🤖 {self._ref(deal)} — الفهم الذكي لم يُعتمَد ({verdict.reason}).\n"
                f"اقتراح النموذج ({prop.model}) للاستئناس فقط: {prop.summary()}",
                self._deal_key(deal),
            )
            return False

        trec = verdict.treasury
        leg.treasury = TreasuryRef(code=trec.code, name=trec.name,
                                   type=trec.type, currency=trec.currency)
        if leg.currency is None:
            leg.currency = verdict.currency or trec.currency
        leg.unresolved_treasury = None
        if verdict.supplier is not None and leg.supplier is None:
            leg.supplier = SupplierRef(code=verdict.supplier.code, name=verdict.supplier.name)
        leg.deviation_log.append({
            "field": "treasury", "raw_value": "ai", "extracted_value": trec.name,
            "method": "ai", "confidence": int(threshold * 100), "model": prop.model,
        })
        await self.db.deals.upsert(deal)
        await self.bus.notify_admin(
            f"⚠️ {self._ref(deal)} فُهمت بالذكاء الاصطناعي ({prop.model}): {prop.summary()}",
            self._deal_key(deal),
        )
        log.info("(الفهم الذكي) أُنقِذت %s ← خزينة «%s» (نموذج %s، %dms)",
                 deal.deal_id, trec.name, prop.model, prop.latency_ms)
        return True

    async def _mandatory_alert(self, deal: Deal, detail: str, reply_key: Optional[str]) -> None:
        """(البند 5) رسالة سبب **إلزامية** للمُرسِل مع كل ⚠️/🔴/❌ — مربوطةٌ بمرجع الصفقة، is_alert=True
        (تُعفى من warm-up فلا تُحجب). خنقٌ لكل مرجع: النصّ الكامل أوّل مرّتين، ثم مختصر «(تكرار N)» من
        الثالثة — **ممنوع صفر رسائل**."""
        ref = self._ref(deal)
        n = self._alert_counts.get(ref, 0) + 1
        self._alert_counts[ref] = n
        if n <= 2:
            text = f"⚠️ {ref} — {detail}\nالمطلوب من المُرسِل: أرسِل التصحيح/التكملة مربوطًا بالحوالة."
        else:
            text = f"⚠️ {ref} (تكرار {n}) — {detail}"
        await self.bus.reply_central(text, reply_key, is_alert=True)

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
    # بوابة صحّة MONEYADO (§11.3) — إيقاف سحب الكتابة عند مغلق/مصغّر/نسختين + استئناف تلقائيّ
    # ═════════════════════════════════════════════════════════════════════════
    def _moneyado_ready(self) -> tuple[bool, str]:
        """(جاهز, السبب) من الـwriter — fail-open إن لم يدعم الفحص (اختبارات مبسّطة/كاتب آخر)."""
        fn = getattr(self.writer, "moneyado_ready", None)
        if fn is None:
            return True, ""
        try:
            return fn()
        except Exception:                               # فحص متعذّر → جاهز (لا نحجب)
            return True, ""

    async def _pending_write_count(self) -> int:
        """عدد الحوالات المنتظِرة للكتابة (لنصّ التنبيه) — PARSED/MATCHING/READY/SELL_DONE."""
        try:
            return await self.db.deals.col.count_documents(
                {"status": {"$in": [Status.PARSED.value, Status.MATCHING.value,
                                    Status.READY.value, Status.SELL_DONE.value]}})
        except Exception:
            return 0

    async def _moneyado_gate(self, now: datetime) -> bool:
        """البوابة قبل أي كتابة (§11.3). تُرجِع **True = موقوف** (MONEYADO غير جاهز) → لا تُكتب
        الصفقة وتبقى قابلة للتنفيذ (لا tech_failed)؛ False = جاهز (تُكتب).
          - غير جاهز + شغل منتظر → تنبيه 🔴 **مخنوق زمنيًّا** (لا لكل حوالة) + رفع علَم الإيقاف.
          - رجوع الجاهزية بعد إيقاف → رسالة ✅ «رجع — جاري تنزيل N» + خفض العلَم (استئناف تلقائيّ)."""
        if not self._gate_enabled:
            return False
        ready, reason = self._moneyado_ready()
        if ready:
            if self._moneyado_paused:                   # كنّا موقوفين → استئناف تلقائيّ + إشعار
                self._moneyado_paused = False
                self._last_gate_alert = None
                n = await self._pending_write_count()
                await self.bus.notify_admin(
                    f"✅ MONEYADO رجع — جاري تنزيل {n} حوالة منتظرة (استئناف تلقائيّ).")
                log.info("بوابة MONEYADO: رجعت الجاهزية — استئناف تلقائيّ (%d منتظرة).", n)
            return False
        # غير جاهز → إيقاف. التنبيه مخنوق: أوّل مرّة، ثم كل _gate_throttle ثانية.
        fire = (self._last_gate_alert is None
                or (_as_naive_utc(now) - _as_naive_utc(self._last_gate_alert)).total_seconds()
                >= self._gate_throttle)
        if fire:
            n = await self._pending_write_count()
            await self.bus.notify_admin(
                f"🔴 {reason} — {n} حوالة منتظرة، الكتابة موقوفة (تُستأنَف تلقائيًّا عند التوفّر).")
            self._last_gate_alert = now
            log.warning("بوابة MONEYADO: %s — إيقاف سحب الكتابة (%d منتظرة).", reason, n)
        self._moneyado_paused = True
        return True

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
        يمرّ على الرسائل الخام غير المعالَجة المستقرّة (§7.2)، يفكّكها ويجمّعها، ثم **يعالج
        الصفقات الجاهزة مرتّبةً بختم وصول رسالتها الأولى** (first_received_at) لا بوقت اكتمالها.
        يُرجع الصفقات التي عولجت هذه الدورة. لا يوقفه أي تعليق فردي.

        🔴 الجدولة بترتيب الوصول (§7.3): زوجٌ (A + ثانيتها) قد يكتمل متأخّرًا في نفس الدفعة —
        فلو عولج لحظة اكتماله لتجاوز رسالةً مستقلّة (SI) وصلت **قبل** رسالته الأولى. الحلّ: نجمع
        الجاهز للكتابة أثناء المرور، ثم **نفرزه بـ first_received_at ونكتبه بعد فرز الدفعة كلها**.
        الرسالة الأولى الناقصة الهوية (رد مكمّل §7.3) تبقى لنبضة tick (ليست جاهزة — _ready_to_write_now).

        🔴 محميّ بقفل تسلسليّ (_inbox_lock): استدعاءان متزامنان لا يتشابكان — تُعالَج كل رسالة
        وتُكتب في DB بالكامل قبل بدء التالية، فلا تقرأ الثانية (try_absorb) قبل كتابة الأولى (§7.3).
        """
        async with self._inbox_lock:
            to_process: list[Deal] = []
            # 🔴 FIFO صارم بخانة المُرسِل (§7.3): كل الحوالات في غرفة المركزية، فحجب الغرفة كاملةً
            #    يوقف الطابور خلف أوّل رسالة غير مستقرّة (A8975 غير المستقرّة تحجب A8976 وSI). الحلّ:
            #    الحجب بمفتاح **المُرسِل** لا الغرفة — إن لم تستقرّ رسالةُ مُرسِلٍ تُؤجَّل رسائله **هو**
            #    التالية هذه الدورة فقط (حفظ ترتيبه التامّ)، ومُرسِلون مختلفون مستقلّون تمامًا لا ينتظرون
            #    بعضهم. الرسائل مرتّبة حتميًّا (§db) فالترتيب داخل المُرسِل محفوظ عبر الدورات.
            #    يُستثنى من الحجب: ردود التحكّم/الإكمال (إجراءات فورية على صفقات قائمة)، وحوالة SI
            #    المكتملة (مستقلّة برسالة واحدة §4.5 — لا تفتح خانة مُرسِل ولا تملؤها) → تنزل فور استقرارها.
            blocked_senders: set[str] = set()
            # ميزة الإلغاء/التعديل (§10 نسخة نهائية): تُجمَع رسائل التحكّم (إلغاء/تعديل) وتُنفَّذ **بعد**
            # فرز/كتابة الدفعة — فيُسجَّل البيع أولًا (لا تتقدّم على أحد)، وتُنفَّذ بترتيب received_at وكلٌّ
            # يقرأ الصفقة من DB لحظة تنفيذه (§3). معزولة: تتخطّى _ingest كليًّا.
            control_ops: list[RawMessage] = []
            batch = await self.db.raw.unprocessed()
            # القوائم للكاشف (§1-§2) — قراءة واحدة لكل دفعة (لا لكل رسالة). _ingest يقرأ نسخته
            # الحيّة كالمعتاد؛ هذه للفحص المسبق فقط فلا تغيّر مصدر الحقيقة.
            ai_treasuries = await self.db.treasuries.all_active()
            ai_suppliers = await self.db.suppliers.all_active()
            # نظام الأسعار (الربط أ): إعداد الأسعار يُقرأ مرّة لكل دفعة (hot-reload) — لتمييز غرفتَي
            #   الأسعار. فارغ افتراضيًّا ⇒ price_room_currency=None دائمًا ⇒ الفرع أدناه لا يُنفَّذ.
            fx_cfg = await self.db.fx_config.get()
            for raw in batch:
                # 🔴 رسائل البوت نفسه (is_from_me): Reply/تفاعل/تأكيد كتبها البوت — ليست مدخلات
                #    (§2.2). تُعلَّم معالَجة بلا أي معالجة **كأوّل شرط**، فلا يقرأ البوت رسائله ولا
                #    تُفسَّر كحوالة/إلغاء (مثلاً ردّ «… مُلغاة مسبقًا» لا يُطلق كشف الإلغاء).
                if raw.is_from_me:
                    log.debug("تخطٍّ: رسالة من البوت نفسه (is_from_me) %s", raw.message_key)
                    await self.db.raw.mark_processed(raw.message_key)
                    continue
                # ═══ نظام الأسعار (الربط أ) — اعتراض معزول لرسائل غرفتَي الأسعار ═══
                # رسالة من غرفة أسعار (يحدّدها المالك في FxRatesConfig) → تُبتلَع لتحديث fx_rate_history
                # **بلا دخول _ingest إطلاقًا** (لا خانة مُرسِل، لا مطابقة، لا طابور، لا حوالة، لا ردّ).
                # قبل فحص العمر عمدًا: تسجيل السعر صامت (لا يردّ) فلا يقلقه استرجاع §12؛ ورسائل الأسعار
                # الفائتة تُسجَّل بترتيبها الزمنيّ (§10). آمنة: الشرط لا يتحقّق ما لم يضبط المالك JIDs
                # الغرفتين (فارغة افتراضيًّا ⇒ price_room_currency=None ⇒ سلوك اليوم مطابق بايت-ببايت).
                if price_room_currency(raw.chat_jid, fx_cfg) is not None:
                    try:
                        await ingest_price_message(self.db, raw, fx_cfg)
                    except Exception as exc:      # best-effort (T5): تسجيل السعر لا يوقف معالجة الحوالات
                        log.warning("تعذّر استيعاب رسالة أسعار %s (متابعة): %s", raw.message_key, exc)
                    await self.db.raw.mark_processed(raw.message_key)
                    continue
                # 🔴 استرجاع بعد الإطفاء (§12): رسالة عمرها > 15 دقيقة (INCOMPLETE_DATA_ESCALATE_SECONDS)
                #    — قديمة من قبل التوقّف → تُعلَّم معالَجة **بلا معالجة ولا ردود**، فلا يردّ البوت
                #    على رسائل قديمة عند إعادة التشغيل. متماثل مع حجْر الصفقات القديمة (expire_stale).
                age = (_as_naive_utc(now) - _as_naive_utc(raw.received_at)).total_seconds()
                if age > INCOMPLETE_DATA_ESCALATE_SECONDS:
                    log.info("تخطٍّ نهائيّ (رسالة أقدم من 15د — استرجاع §12): %s", raw.message_key)
                    await self.db.raw.mark_processed(raw.message_key)
                    continue
                # ═══ ميزة الإلغاء/التعديل عبر Reply — اعتراض معزول قبل _ingest ═══
                # رسالة تحكّم في المركزية (إلغاء/تعديل ككلمة كاملة) → تُجمَع للتنفيذ بعد الدفعة، ولا
                # تدخل _ingest إطلاقًا (لا تفتح خانة مُرسِل، لا تكمل معلّقة، لا تُعامَل حوالةً). محصورة
                # بالمركزية فلا تمسّ رسائل الغرف الأخرى (مصدر مطابقة صامت). لا يتأثّر أيّ مسار آخر.
                if raw.chat_jid == self.bus.central_jid and (
                    detect_cancellation(raw.text) is not None
                    or detect_amendment(raw.text) is not None
                ):
                    # 🔴 إلغاء بلا Reply (م: X515): يُتجاهَل تمامًا — لا يُعالَج تحكّمًا (لا يدخل
                    #    control_ops/_handle_cancellation) ولا يدخل _ingest، فلا يُسقط أيّ حوالة قائمة.
                    #    رد توجيهيّ فقط. الإلغاء الفعليّ يتطلّب Reply على رسالة الحوالة المراد إلغاؤها.
                    if detect_cancellation(raw.text) is not None and raw.reply_to_key is None:
                        await self.bus.reply_central(
                            "🔴 الإلغاء يتطلب الرد (Reply) على رسالة الحوالة المراد إلغاؤها",
                            raw.message_key, is_alert=True)
                        await self.db.raw.mark_processed(raw.message_key)
                        continue
                    control_ops.append(raw)
                    await self.db.raw.mark_processed(raw.message_key)
                    continue
                # رسائل التحكّم (Reply: إلغاء/تعديل/تصحيح/تم) إجراءات مكتملة متعمّدة — تُعالَج فورًا،
                # ولا تُعامَل كـ«حرف» placeholder ينتظر تعديلًا (§7.2 يخصّ حوالات جديدة قصيرة).
                is_control_reply = bool(raw.reply_to_key) and detect_control(raw.text) is not None
                # §7.3-أ زوج متلاحق: رسالة يتلوها في نفس الغرفة رسالة خلال 3ث (نفس الدفعة) استقرّت
                # فورًا (لا تعديل قادم) → تُعالَج هذه الدورة فيُربَط الزوج بلا انتظار الاستقرار/النبضة.
                stable = is_control_reply or is_stable(raw, now) or self._has_rapid_followup(raw, batch)
                # رد خزينة/مورد مكمّل («بلس»/«صافي» §7.3): ليس «حرفًا» ينتظر تعديلًا — يُربَط بحوالته
                # المعلّقة فورًا. يُحسَب (بوصول DB) مرّة فقط حين تكون غير مستقرّة وليست تحكّمًا.
                completion = (not stable) and await self._is_completion_reply(raw)
                immediate = is_control_reply or completion   # إجراء فوريّ لا يخضع لترتيب الحوالات الجديدة
                sender_key = f"{raw.chat_jid}|{raw.sender_jid or ''}"

                # مُرسِل محجوب (سبقته رسالةٌ **له** لم تستقرّ) → أجّل رسائله التالية، إلا الإجراءات
                # الفورية (تحكّم/إكمال) وحوالة SI المستقلّة (لا تخضع لترتيب خانة المُرسِل §4.5).
                if sender_key in blocked_senders and not immediate \
                        and not await self._is_independent_si(raw):
                    log.debug("حجب ترتيبيّ (رسالة أسبق لنفس المُرسِل لم تستقرّ): %s", raw.message_key)
                    continue

                # ═══ (§2) انتظار الرسالة الثانية عند كشف غموض — تأجيل لا نوم ═══
                # الرسالة الغامضة تُترك غير معالَجة حتى تصل تكملتها أو تنقضي ai_pair_wait_seconds،
                # فتذهبان للذكاء **معًا** (90% من الحوالات ثنائيّة). تحجب مُرسِلها كبقيّة التأجيلات
                # حفاظًا على ترتيب FIFO. مطفأة ما لم يُفعِّل المالك ai_first_enabled.
                if stable and not immediate and not completion:
                    try:
                        if await self._ai_pair_wait(raw, batch, now, (ai_treasuries, ai_suppliers)):
                            blocked_senders.add(sender_key)
                            continue
                    except Exception as exc:  # §6 fail-open — الانتظار لا يوقف الطابور
                        log.warning("(AI-first) تعذّر فحص الانتظار لـ%s (متابعة): %s",
                                    raw.message_key, exc)

                # لم تستقرّ بعد وليست ردّ إكمال → تأجيل + حجب رسائل **نفس المُرسِل** التالية حتى تستقرّ
                # أو تنتهي مهلتها (§7.2). SI المستقلّة تُؤجَّل لاستقرارها لكن **لا تحجب** المُرسِل (لا
                # تفتح خانة/ترتيبًا §4.5). لا تخطٍّ صامت (T5): نسجّل السبب.
                if not stable and not completion:
                    log.debug("تخطٍّ مؤقّت (لم تستقرّ بعد): %s", raw.message_key)
                    if not await self._is_independent_si(raw):
                        blocked_senders.add(sender_key)
                    continue
                try:
                    deal = await self._ingest(raw, now, batch)
                    if deal is not None:
                        # ختم وصول الرسالة الأولى (§7.3): يُضبط مرّة عند أول ظهور للصفقة، ويُحفَظ
                        # عبر دمج الطرف الثاني (الدمج يقرأ الصفقة من DB فيبقى الختم الأقدم، لا يُستبدَل
                        # بختم الرسالة الثانية) — فيبقى معيار الجدولة = وصول الأولى.
                        if deal.first_received_at is None:
                            deal.first_received_at = raw.received_at
                            await self.db.deals.upsert(deal)
                        # جاهزة للكتابة الآن؟ اجمعها للفرز؛ وإلا (ناقصة/معلّقة) تبقى لنبضة tick.
                        if self._ready_to_write_now(deal):
                            to_process.append(deal)
                except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونكمل للتالية (الطابور لا يتوقّف)
                    log.exception("فشل معالجة الرسالة %s: %s — تجاوز للتالية", raw.message_key, exc)
                finally:
                    await self.db.raw.mark_processed(raw.message_key)

            # 🔴 المعالجة بترتيب وصول الرسالة الأولى (§7.3): الفرز مستقرّ (Python) والطابور مرتّب
            #    حتميًّا فيكسر التعادل عند تساوي الختم (دقّة الثانية) — فلا تجاوز عشوائيّ.
            to_process.sort(key=lambda d: _as_naive_utc(d.first_received_at or now))
            processed: list[Deal] = []
            for deal in to_process:
                processed.append(await self.process_deal(deal, now))

            # ميزة الإلغاء/التعديل: تُنفَّذ **بعد** كتابة كل صفقات الدفعة (فالبيع مسجَّل أولًا)، بترتيب
            # received_at (فتنفَّذ «تعديل ثم إلغاء» بالترتيب)، وكلٌّ يقرأ الصفقة من DB لحظة تنفيذه (§3).
            for raw in sorted(control_ops, key=lambda r: _as_naive_utc(r.received_at)):
                try:
                    amend = detect_amendment(raw.text)
                    if amend is not None:
                        await self._handle_amendment(raw, amend["amount"], amend["reason"], now)
                    else:
                        await self._handle_cancellation(raw, now)
                except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونكمل (الطابور لا يتوقّف)
                    log.exception("فشل تنفيذ تحكّم (إلغاء/تعديل) %s: %s", raw.message_key, exc)
            return processed

    @staticmethod
    def _ready_to_write_now(deal: Deal) -> bool:
        """هل الصفقة جاهزة لتُعالَج (تُكتب) في دورة process_inbox هذه، مرتّبةً بوصولها؟

        الجاهزة = PARSED باكتمالٍ فعليّ: صفقة طرفين مكتملة، أو صفقة مفردة/SI تحمل مرساة هوية
        (كود/اسم). الرسالة الأولى الناقصة الهوية (رد مكمّل بلا كود/اسم §7.3، أو ترويسة تنتظر
        ثانيتها) ليست جاهزة — تبقى لنبضة tick التي تُعلّقها/تُصعّدها كالسابق (لا كتابة/تعليق مبكر)."""
        if deal.status != Status.PARSED:
            return False
        if deal.is_two_legged and deal.sell_leg is not None and deal.buy_leg is not None:
            return True
        leg = deal.sell_leg or deal.buy_leg
        return leg is not None and not is_incomplete_first_message(leg)

    @staticmethod
    def _has_rapid_followup(raw: RawMessage, batch: list[RawMessage]) -> bool:
        """§7.3-أ: هل يتلو `raw` في **نفس الغرفة** رسالةٌ خلال BATCH_PAIR_SECONDS (<3ث) ضمن نفس
        الدفعة؟ لو نعم فقد استقرّت (لا تعديل قادم — المُرسِل انتقل للرسالة التالية)، فتُعالَج فورًا
        كي يُربَط الزوج (الأولى+الثانية) في نفس دورة process_inbox بلا انتظار الاستقرار/النبضة."""
        t0 = _as_naive_utc(raw.received_at)
        for other in batch:
            if other.message_key != raw.message_key and other.chat_jid == raw.chat_jid:
                dt = (_as_naive_utc(other.received_at) - t0).total_seconds()
                if 0 < dt < BATCH_PAIR_SECONDS:
                    return True
        return False

    async def _is_completion_reply(self, raw: RawMessage) -> bool:
        """هل الرسالة رد خزينة/مورد مكمّل (§7.3)؟ يُعالَج فورًا بلا انتظار استقرار «الحرف»."""
        if raw.chat_jid != self.bus.central_jid or not (raw.text or "").strip():
            return False
        # رسالة ثانية بلا رقم إشاري (سطرا «كود+اسم+سعر»: زبون+مورد) — تُربَط بحوالة معلّقة فورًا
        # (لا تنتظر استقرار «الحرف» ولا نبضة تالية). سطر المورد قد يأتي بلا كود → يُطابَق بـ suppliers.
        suppliers = await self.db.suppliers.all_active()
        if len(extract_code_name_price_lines(raw.text, suppliers)) >= 2:
            return True
        treasuries = await self.db.treasuries.all_active()
        res = parse_message(raw.text, treasuries, suppliers)
        # رد مكمّل بلا رقم («بلس»/«صافي»)، **أو** رسالة ثانية «رقم إشاري + خزينة» بلا هوية زبون
        # (تُربَط بالرقم فورًا في _ingest) → تُعالَج فورًا بلا انتظار استقرار «الحرف» (§7.3).
        return (res.kind == "noise" and is_completion_fragment(res.leg)) \
            or is_treasury_second_reply(res.leg)

    async def _is_independent_si(self, raw: RawMessage) -> bool:
        """هل الرسالة حوالة SI معنونة مكتملة (§3.3)؟ SI مستقلّة تمامًا: لا تفتح خانة مُرسِل ولا
        تملؤها ولا تنتظر طرفًا ثانيًا (§4.5) — فلا تُحجَب بترتيب خانة المُرسِل ولا تحجبه (§7.3).
        تُفحَص فقط لرسائل المركزية الحاملة نصًّا (غيرها لا يُفكَّك كحوالة أصلًا)."""
        if raw.chat_jid != self.bus.central_jid or not (raw.text or "").strip():
            return False
        treasuries = await self.db.treasuries.all_active()
        suppliers = await self.db.suppliers.all_active()
        res = parse_message(raw.text, treasuries, suppliers)
        return res.leg is not None and res.leg.is_si_format

    async def _ingest(self, raw: RawMessage, now: datetime,
                      batch: Optional[list[RawMessage]] = None) -> Optional[Deal]:
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

        # ═══ (طبقة الفهم الذكي — توسعة: الرسالة الأولى الفاشلة تفكيكًا، م: X1325) ═══
        # رسالةٌ تحمل مرجعًا لكن التفكيك أخفق ⇒ مصيرها التصعيد حتمًا (لا صفقة تُنشَأ، وتكملتها
        # تصير يتيمة فتنزاح على جارتها). فرصة أخيرة **قبل** أيّ تفرّع: النموذج يصحّح كلمة العملة
        # فقط (قائمة مغلقة، استبدال رمزيّ إضافيّ)، ثم يُعاد التفكيك **حتميًّا** على النصّ المصحَّح.
        # نجح ⇒ يمضي المسار الطبيعيّ كأن الموظّف كتبها صحيحة. أخفق ⇒ لا شيء يتغيّر (تصعيد عاديّ).
        if result.kind != "transfer" and _has_reference(raw.text or ""):
            _fixed = await self._ai_rescue_parse(raw, treasuries, suppliers)
            if _fixed is not None:
                result = _fixed

        # ═══ AI-first: أيّ شكّ → ذكاء فورًا بالرسالتين معًا (§1-§3) ═══
        # يسبق تصعيد «المبلغ الملتبس» أدناه عمدًا: ذاك أحد إشارات الغموض، فيُعطى الذكاء فرصته
        # قبل التصعيد لا بعده. مطفأ ما لم يُفعَّل ai_first_enabled (سلوك اليوم مطابق حين يكون off).
        _cfg_af = await self.db.detection.get()
        if getattr(_cfg_af, "ai_first_enabled", False) and getattr(_cfg_af, "ai_enabled", False) \
                and (result.kind == "transfer" or _has_reference(raw.text or "")):
            _verdict = detect_ambiguity(
                raw.text or "", result, treasuries, suppliers,
                min_confidence=float(getattr(_cfg_af, "ai_min_parse_confidence", 0.7)))
            if _verdict.ambiguous:
                _partner = self._pair_partner(
                    raw, batch or [], treasuries, suppliers,
                    window_seconds=float(getattr(_cfg_af, "ai_pair_wait_seconds", 3.0)))
                try:
                    _res = await self._ai_first(raw, result, _verdict, treasuries, suppliers,
                                                _partner, now)
                except Exception as exc:  # §6 fail-open — المسار الحتميّ يمضي كما هو
                    log.warning("(AI-first) استثناء (%s): %s — مسار حتميّ", raw.message_key, exc)
                    _res = None
                if _res is not None:
                    result = _res

        # 🔴 مبلغ بصيغة «ألف/آلاف» (§3.5): وُسِّع تلقائيًّا (32 ألف→32000) ويُسجَّل بالمبلغ المصحَّح
        #    عبر المسار العادي — تنبيه المركزية (Reply) + المسؤول للتأكيد/التصحيح. حسابٌ مع إشعار، بلا إيقاف.
        if result.alf_expansions and result.kind == "transfer":
            _note = "؛ ".join(
                f"{e['value']:,} (من «{e['original']}»)" for e in result.alf_expansions
            )
            _code = result.leg.reference_number if result.leg else None
            await self.bus.reply_central(
                f"⚠️ تم تسجيل المبلغ: {_note}", raw.message_key, is_alert=True)
            await self.bus.notify_admin(
                f"⚠️ تنبيه: حوالة {_code or '؟'} — الزبون كتب مبلغًا بصيغة «ألف»؛ "
                f"سُجِّل تلقائيًّا {_note}. يرجى التأكيد أو التصحيح.",
                raw.message_key, forward_key=raw.message_key,
            )

        # 🔴 (فيكس د) أرقام مجرّدة متعدّدة مرشّحة للمبلغ بلا حسم واثق (§0): يُصعَّد للمسؤول بدل التخمين
        #    الصامت (المبلغ بقي None فالحوالة ليست «واضحة» — لا تُسجَّل قيمة مخمَّنة). قبل أي ربط/تصنيف.
        if result.leg is not None and result.leg.ambiguous_amount:
            _amts = "، ".join(f"{a:,.0f}" for a in result.leg.ambiguous_amount)
            await self.bus.notify_admin(
                f"⚠️ تنبيه: حوالة {result.leg.reference_number or '؟'} — أرقام مبلغ محتملة متعدّدة "
                f"({_amts}) بلا كلمة عملة؛ تعذّر الحسم. يرجى تحديد المبلغ الصحيح.",
                raw.message_key, forward_key=raw.message_key,
            )
            return None

        # 🔴 (قرار المالك 2026-07-19) الحلّ بالتشابه **ملغى** — الاستدعاءان يخرجان فورًا ما لم يُفعَّل
        #    المفتاح (default=off، ولا يُفعَّل). الاسم غير المحلول صارمًا/من القروب → تصعيد إلزاميّ.
        await self._auto_resolve_treasury(result.leg, raw, treasuries)
        await self._auto_resolve_supplier(result.leg, raw, suppliers)

        # ═══ ربط الرسالة الثانية — حتميّ بطبقتين (§7.3، بلا قرب/تجاور/تصعيد) ═══
        # الطبقة ١ (مرجع صريح): الرسالة تحمل ref يطابق صفقة منتظِرة → تُربَط به مباشرة (الشكل الجديد،
        #   المرجع مكرّر في الرسالتين). الطبقة ٢ (FIFO): بلا ref → أقدم صفقة منتظِرة لنفس المُرسِل (أول
        #   فتح أول قفل). SI لا تدخل هنا (§4.5). الهدف يُشتقّ من الصفقات (مصدر الحقيقة)، لا من خانة مفردة.
        #   absorb_second_into يُرجِع None لو لم تكن الرسالة جزءًا مكمّلاً (حوالة أولى جديدة) → يسقط للتالي.
        # (R1) المرجع الصريح يُقرأ من **النصّ الخام** لا من ناتج التفكيك وحده: رسالةٌ فشل تفكيكها
        #      تفقد مرجعها فتنزلق إلى FIFO فتُبتلَع (X1567 داخل X1566، 2026-07-20). يُحسب مرّةً
        #      واحدة هنا ويُلزِم كلّ الفروع أدناه.
        txt_ref = (result.leg.reference_number if result.leg is not None else None) \
            or first_reference(raw.text or "")
        if raw.sender_jid and not (result.leg is not None and result.leg.is_si_format):
            if txt_ref:                                    # الطبقة ١ — بالمرجع حصرًا (مُلزِم)
                target = await self.queue.waiting_by_reference(raw.chat_jid, txt_ref, now)
                _link_reason = "مرجع" if target is not None else "رفض — مرجع بلا مطابقة"
            else:                                          # الطبقة ٢ — FIFO لنفس المُرسِل (حسب نوع الرسالة)
                frag2 = parse_completion_fragment(raw.text, treasuries, suppliers)
                target = await self.queue.oldest_waiting_for_sender(
                    raw.chat_jid, raw.sender_jid, now, frag2, message_key=raw.message_key)
                _link_reason = "FIFO مُرسِل"
            if target is not None and target.status == Status.WAITING_SECOND_LEG:
                absorbed = await self.queue.absorb_second_into(
                    target, raw, now, treasuries, suppliers)
                if absorbed is not None:
                    merged, _immediate = absorbed   # المعالجة مؤجَّلة للفرز — لا حاجة للعلَم هنا
                    log.info("ربط الرسالة الثانية %s بالصفقة %s (%s)", raw.message_key,
                             merged.deal_id, _link_reason)
                    return merged

        # 🔴 ربط الرسالة الثانية بلا رقم إشاري (سطرا «كود+اسم+سعر»: زبون ثم مورد §7.3) بصفقة معلّقة
        #    في نفس الغرفة خلال النافذة — **قبل التصنيف** كي لا تُسقَط noise/خارج-النطاق. لو نجح الربط
        #    → return فورًا؛ وإلا نكمل المسار العادي. حماية الالتباس: تعدّد المعلّقات → تصعيد لا تخمين (§0).
        pairs = extract_code_name_price_lines(raw.text, suppliers)
        if len(pairs) >= 2:
            cands = await self.queue.waiting_candidates_for_second(raw.chat_jid, now)
            # (R1) مرجعٌ صريح في النصّ ⇒ يُصفّي المرشّحين حصرًا؛ بلا مطابقة ⇒ **لا ربط** (لا ذكاء
            #      ولا FIFO) — تسقط الرسالة لتصعيد has_ref أدناه بدل أن تلوّث صفقةً أجنبية.
            _bound = bind_by_reference(cands, txt_ref)
            if _bound is not None:
                cands, _reason = _bound
                _log_link_decision("second_pairs", raw.message_key, cands,
                                   cands[0] if cands else None, _reason, txt_ref)
                if not cands:
                    return None
            if len(cands) == 1:
                merged = await self.queue.absorb_customer_supplier(
                    cands[0], pairs[0], pairs[1], raw.message_key, treasuries, now)
                return merged   # المعالجة مؤجَّلة لفرز الدفعة بـ first_received_at (§7.3)
            if len(cands) > 1:
                # ═══ (§5) تعدّد المعلّقات + تكملة واحدة → الذكاء يرى الكل ويقرّر ═══
                choice = await self._ai_link_choice(raw, cands, treasuries, suppliers, now)
                if choice is not None and choice.action == "auto":
                    merged = await self.queue.absorb_customer_supplier(
                        cands[choice.index], pairs[0], pairs[1], raw.message_key, treasuries, now)
                    await self.bus.notify_admin(
                        f"⚠️ تكملة بلا رقم إشاري + {len(cands)} صفقات معلّقة — رُبطت بالذكاء "
                        f"(ثقة {choice.confidence:.2f}) بصفقة {self._ref(merged) or '؟'}. راجعها.",
                        raw.message_key, forward_key=raw.message_key)
                    log.info("(AI-first §5) ربط تلقائيّ لتكملة %s بالصفقة #%d (ثقة %.2f)",
                             raw.message_key, choice.index, choice.confidence)
                    return merged
                if choice is not None and choice.action == "ask":
                    opts = "\n".join(
                        f"{i+1}) {self._ref(c) or '؟'} — "
                        f"{(c.sell_leg.customer_name if c.sell_leg else None) or '؟'}"
                        for i, c in enumerate(cands))
                    await self.bus.notify_admin(
                        f"⚠️ تكملة بلا رقم إشاري + {len(cands)} صفقات معلّقة. الذكاء يرجّح "
                        f"الخيار {choice.index + 1} بثقة {choice.confidence:.2f} (دون الحسم). "
                        f"أيّها؟ **رد برقم الخيار**:\n{opts}",
                        raw.message_key, forward_key=raw.message_key)
                    log.info("(AI-first §5) تصعيد بخيارات لتكملة %s (ثقة %.2f)",
                             raw.message_key, choice.confidence)
                    return None
                # choice is None أو action == "fifo" ⇒ السلوك الحتميّ كما كان (تصعيد §0)
                await self.bus.notify_admin(
                    f"⚠️ رسالة ثانية بلا رقم إشاري وتعدّد صفقات معلّقة ({len(cands)}) في الغرفة "
                    f"— تعذّر الربط التلقائي؛ مراجعة يدوية: {(raw.text or '').strip()[:60]}",
                    raw.message_key,
                )
                log.warning("رسالة ثانية بلا رقم + تعدّد معلّقات (%d) — تصعيد (§0).", len(cands))
                return None

        # 🔴 خزينة SI معنونة لم تُحلّ (§4.5): تُلتقط مجهولةً للإسناد اليدويّ من اللوحة (بلا تخمين §0).
        if result.leg is not None and result.leg.unresolved_treasury:
            await self.db.unknown_terms.record(result.leg.unresolved_treasury, "treasury")

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

        # 🔴 رسالة ثانية بنفس الرقم الإشاري تحمل **خزينة فقط** (بلا هوية زبون): تُكمِّل خزينة الصفقة
        #    المعلّقة المطابقة للرقم — **قبل** تصنيف noise/transfer كي لا تُنشأ صفقة جديدة (حين تحمل
        #    مبلغًا) ولا تُسقَط هدرزة (بلا مبلغ). تُعالَج فورًا (بلا انتظار sweep). مثل try_absorb_supplier_second.
        if result.leg is not None:
            completed = await self.queue.try_absorb_treasury_second(result.leg, raw, now)
            if completed is not None:
                return completed   # المعالجة مؤجَّلة لفرز الدفعة بـ first_received_at (§7.3)
            if is_treasury_second_reply(result.leg):
                # رسالة ثانية بمرجع صريح (خزينة عادية أو تسوية خصم) لم تجد صفقتها → حُفِظت ردًّا معلّقًا
                #   بمرجعها (Fix 1ب) — لا تُنشأ صفقة headless تبتلع هويةً أجنبية (A9078). تُربَط عند
                #   وصول أُولاها، أو تُصعَّد ⚠️ إن انتهت المهلة (sweep_expired).
                return None

        if result.kind == "noise":
            # رد خزينة/مورد بلا رقم إشاري («بلس»/«صافي» وحدها) — ليس هدرزة بل جزء مكمّل
            # لحوالة معلّقة (§7.3): يُربَط بالمعلّقة (قرب زمني/نفس الغرفة) أو يُحفَظ ردًّا معلّقًا.
            if is_completion_fragment(result.leg):
                # 🔴 الرسالة الثانية تُعاد قراءتها بـ pattern-fishing (لا سطرًا-بسطر) — أمتن
                #    للصيغ المتنوّعة (الكود آخر السطر، السعر بسطر مستقلّ…) — تلتقط كود/اسم/سعر/خزينة.
                frag = parse_completion_fragment(raw.text, treasuries, suppliers)
                frag.sender_jid = raw.sender_jid          # مُرسِل الرد (لربطه بصفقة نفس المُرسِل §7.3)
                frag.source_message_key = raw.message_key
                # ربط حتميّ (§7.3، بلا تجاور/تصعيد): مرجع صريح → بالمرجع؛ وإلا → أقدم صفقة مطابِقة FIFO
                #    (absorb_fragment → _find_recent_waiting). fallback حين لا تلتقطه طبقتا خانة المُرسِل
                #    (رد وصل قبل حوالته/عبر مُرسِل مختلف). تعدّد المرشّحين يُحسَم بالأقدم لا بالتصعيد.
                return await self.queue.absorb_fragment(
                    frag, raw.chat_jid, raw.message_key, now, text_ref=txt_ref
                )
            # 🔴 (البند 1، الربط أولاً) لم تُصنَّف جزءًا **محلولًا** (فشل حلّ الخزينة/المورد: غريب/جديد/
            #    إملائيّة خاطئة)، لكنّها **بشكل تكملة** (سعر/خزينة/مورد/كود) والمُرسِل عنده صفقة معلّقة →
            #    تُربَط بأقدمها بغضّ النظر عن نجاح الحل (لا سقوط صامت). ما انحلّ يُطبَّق؛ ما بقي ناقصًا
            #    يُصعَّد برسالة إلزامية مربوطة بمرجع الصفقة (اسم غير معروف: <حرفيًّا>) — روح 08adce8 للربط كلّه.
            if raw.sender_jid:
                frag2 = parse_completion_fragment(raw.text, treasuries, suppliers)
                merged = None
                # (R1+R2) هذا هو مسار حادثتَي 2026-07-20 بالضبط: كان يُلصِق الرسالة بأقدم معلّقة
                #   بلا قراءة مرجع (X1567→X1566) وبلا فحص محتوى (تكملة X1571→X1568). الآن:
                #   المرجع يُلزِم، وعند غيابه يرجّح الذكاءُ بالمحتوى، وFIFO آخر الاحتياطات.
                if self.queue.is_completion_shaped(frag2):
                    cands = await self.queue.pending_candidates_for_sender(
                        raw.chat_jid, raw.sender_jid, now, txt_ref,
                        frag=frag2, message_key=raw.message_key)
                    target = await self._choose_link_target(
                        raw, cands, treasuries, suppliers, now,
                        text_ref=txt_ref, stage="orphan_completion")
                    if target is not None:
                        merged = await self.queue.apply_orphan_completion(
                            target, frag2, raw.sender_jid, raw.message_key, now)
                if merged is not None:
                    missing = missing_mandatory_fields(merged.sell_leg or merged.buy_leg)
                    if missing:
                        await self._mandatory_alert(
                            merged,
                            f"وصلت تكملةٌ من الرسالة «{(raw.text or '').strip()[:60]}» وربطتُها، لكن ما "
                            f"زال ناقصًا: {'، '.join(missing)} (اسم غير معروف/لم يُحلّ)",
                            raw.message_key)
                    log.info("(البند 1) رُبطت رسالة تكملة غير محلولة %s بالصفقة %s (ناقص=%s)",
                             raw.message_key, merged.deal_id, missing)
                    return merged
            # 🔴 «حوالة محتملة فشل استخراجها» (م: A292): رسالة تحمل مرجعًا (Axxxx/SIxxxx) لكنها
            #    سقطت noise (خطأ إملاء عملة/حقل) → **لا سقوط صامت**: تنبيه المالك (is_alert) + ⚠️.
            #    الهدرزة الحقيقية (بلا مرجع) تبقى تجاهلًا صامتًا كالسابق.
            has_ref = bool((result.leg is not None and result.leg.reference_number)
                           or _has_reference(raw.text or ""))
            if has_ref:
                ref = (result.leg.reference_number if result.leg else None) or "؟"
                log.warning("⚠️ رسالة تبدو حوالة (مرجع %s) فشل استخراجها — تنبيه المالك: %s",
                            ref, raw.message_key)
                # مرحلة أ (لا خسارة صامتة §0): مرجع + قيمة مالية → لا تُسقط أبدًا. تصعيد غنيّ:
                #   النصّ الخام + ما استُخرج + سبب عدم الاكتمال. 🔴 لو بلا مبلغ (فشل كامل)، وإلا ⚠️.
                _l = result.leg
                _ext = (f"مبلغ={getattr(_l, 'amount', None)} عملة={getattr(_l, 'currency', None)} "
                        f"هاتف={getattr(_l, 'phone', None)} كود={getattr(_l, 'customer_code', None)}"
                        if _l is not None else "لا شيء")
                _sev = "⚠️" if (_l is not None and _l.amount is not None) else "🔴"
                await self.bus.notify_admin(
                    f"{_sev} رسالة تبدو حوالة لكن فشل استخراجها (مرجع {ref}) — راجعها يدويًا:\n"
                    f"سبب: {result.reason or '؟'}\n"
                    f"استُخرج: {_ext}\n"
                    f"النصّ الخام:\n{(raw.text or '').strip()[:200]}",
                    raw.message_key, forward_key=raw.message_key,
                )
                if raw.chat_jid == self.bus.central_jid:   # علامة ⚠️ على المركزية (إن كانت منها)
                    await self.bus.mark_central(raw.message_key, Mark.WARN.value)
                    await self.bus.flush_reactions()
                return None
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
        leg.sender_jid = raw.sender_jid          # مُرسِل الرسالة الأولى (لربط الرد بنفس المُرسِل §7.3)

        # مرحلة أ: تنبيهات best-effort (القيمة المالية أولوية) — لا تمسّ الربط/المطابقة/الكتابة.
        await self._phase_a_alerts(leg, raw)

        # الحارس (§9): نُزّلت من قبل؟ → تجاهل (منع تكرار)
        if await self.guard.already_downloaded(raw.message_key):
            log.info("الحارس: %s نزلت من قبل — تجاهل (§9)", raw.message_key)
            return None

        # 🔴 رسالة ثانية بنفس الرقم الإشاري لصفقة معلّقة في نفس الغرفة تحمل موردًا (§6): تُدمَج
        #    كطرف مورد لا كصفقة جديدة — قبل try_group كي لا تُنشأ صفقة منفصلة، وتُعالَج فورًا.
        second = await self.queue.try_absorb_supplier_second(leg, raw, now, treasuries, suppliers)
        if second is not None:
            return second   # المعالجة مؤجَّلة لفرز الدفعة بـ first_received_at (§7.3)

        # 🔴 dedup **بالمحتوى** (§9، الجزء 2): بلا نافذة زمنيّة. نفس المرجع + نفس الجوهر (هاتف/مبلغ/كود)
        #    = إعادة إرسال حقيقيّة → تجاهل **مع ريأكشن** (لا صمت) — يسدّ X1243 (نزلت مرّتين بفارق 10د).
        #    نفس المرجع + جوهر مختلف = المُرسِل أعاد استخدام المرجع → صفقة جديدة تُعالَج + تنبيه المسؤول —
        #    يسدّ X1242 (تجاهل تامّ لحوالة ٤٠ ألف). الرسائل الثانية (WAITING) مرّت أعلاه فلا تتأثّر.
        _reuse = await self._reference_reuse_action(leg, now)
        if _reuse == "duplicate":
            log.info("تكرار حقيقيّ (نفس المرجع+الجوهر) — تجاهل + ريأكشن: %s ref=%s",
                     raw.message_key, leg.reference_number)
            if raw.chat_jid == self.bus.central_jid:
                await self.bus.mark_central(raw.message_key, "🔁")   # ريأكشن لا صمت (§9)
                await self.bus.flush_reactions()
            return None
        if _reuse == "reused":
            log.warning("مرجع %s مُعاد استخدامه بمحتوى مختلف — صفقة جديدة + تنبيه (X1242): %s",
                        leg.reference_number, raw.message_key)
            await self.bus.notify_admin(
                f"⚠️ المرجع {leg.reference_number} مُستخدَم سابقًا بمحتوى مختلف — عُولِجت كحوالة "
                f"جديدة (لا تجاهل): {(raw.text or '').strip()[:60]}",
                raw.message_key, forward_key=raw.message_key)
            # يسقط للتجميع العادي (صفقة جديدة)

        # التجميع (§7.3): صفقة جديدة أو دمج طرف ثانٍ
        deal = await self.queue.try_group(leg, now, chat_jid=raw.chat_jid)
        # (X1242) وسمُ المولودة من إعادة الاستخدام — تُستبعَد لاحقًا من ترشّح التكملات عديمة
        #   المرجع. يُوسَم **بعد** التجميع كي لا يُوسَم دمجُ طرفٍ ثانٍ في صفقةٍ قائمة سليمة.
        if _reuse == "reused" and deal is not None and not deal.born_from_ref_reuse:
            deal.born_from_ref_reuse = True
            await self.db.deals.upsert(deal)
        # افتح خانة مُرسِل إن بقيت الصفقة تنتظر طرفًا ثانيًا (§7.3): الرسالة الثانية من نفس المُرسِل
        # خلال النافذة ستملؤها حتمًا. SI لا تفتح خانة (لا تنتظر ثانيًا §4.5) — والحالة WAITING تمنعها.
        if (deal is not None and deal.status == Status.WAITING_SECOND_LEG
                and raw.sender_jid and not leg.is_si_format):
            await self.db.sender_slots.open(
                chat_jid=raw.chat_jid, sender_jid=raw.sender_jid, deal_id=deal.deal_id,
                first_message_key=raw.message_key, now=now,
                window_seconds=SENDER_SLOT_WINDOW_SECONDS,
            )
        return deal

    @staticmethod
    def _digits(s: Optional[str]) -> str:
        return "".join(c for c in (s or "") if c.isdigit())

    @classmethod
    def _same_essential_content(cls, a: Optional[ParsedLeg], b: Optional[ParsedLeg]) -> bool:
        """جوهر متطابق (§9 dedup بالمحتوى): الهاتف (آخر ٩ أرقام، تجاهل مفتاح الدولة) + المبلغ (مقرَّب)
        + كود الزبون. تطابقُها = نفس الحوالة (إعادة إرسال حقيقيّة)."""
        if a is None or b is None:
            return False
        ph_a, ph_b = cls._digits(a.phone)[-9:], cls._digits(b.phone)[-9:]
        amt_a = round(a.amount) if a.amount is not None else None
        amt_b = round(b.amount) if b.amount is not None else None
        code_a, code_b = (a.customer_code or "").strip(), (b.customer_code or "").strip()
        return ph_a == ph_b and amt_a == amt_b and code_a == code_b

    async def _reference_reuse_action(self, leg: Optional[ParsedLeg], now: datetime) -> str:
        """(§9 dedup بالمحتوى، **بلا نافذة زمنيّة**) قرار المرجع المُعاد:
          • 'duplicate' — صفقة سابقة بنفس المرجع و**نفس الجوهر** (هاتف/مبلغ/كود) → إعادة إرسال حقيقيّة.
          • 'reused'    — صفقة سابقة بنفس المرجع لكن **جوهر مختلف** → المُرسِل أعاد استخدام المرجع.
          • 'new'       — لا صفقة سابقة بالمرجع (تُستثنى WAITING الشرعيّة والملغاة CANCELLED).
        **قراءة فقط** — لا يمسّ المطابقة/الكتابة/الإلغاء/التعديل."""
        ref = leg.reference_number if leg is not None else None
        if not ref:
            return "new"
        doc = await self.db.deals.col.find_one(
            {"status": {"$nin": [Status.WAITING_SECOND_LEG.value, Status.CANCELLED.value]},
             "$or": [{"sell_leg.reference_number": ref}, {"buy_leg.reference_number": ref}]},
            sort=[("updated_at", -1)])
        if doc is None:
            return "new"
        prior = Deal(**doc)
        return "duplicate" if self._same_essential_content(leg, prior.sell_leg or prior.buy_leg) else "reused"

    async def _phase_a_alerts(self, leg: ParsedLeg, raw: RawMessage) -> None:
        """مرحلة أ (best-effort، **لا يمسّ** الربط/المطابقة/الكتابة): تنبيهات القيمة المالية أولوية.
        - مبلغ موجود بلا رقم مستلم (الطبقة ٤) → 🔴 خطر ماليّ، راجع فورًا (recipient=None محفوظ).
        - رقم غير مؤكَّد الدولة (الطبقة ٣ uncertain) → ⚠️ سُجّل كما هو، راجع.
        - مبلغ سالب (abs) وغيره من deviation_log → ⚠️ استخراج بتخمين، راجع.
        الأخطاء تُبتلَع (best-effort) فلا توقف المعالجة."""
        ref = leg.reference_number or "؟"
        try:
            if leg.amount is not None and not leg.phone:
                cur = leg.currency.value if leg.currency else ""
                await self.bus.notify_admin(
                    f"🔴 {ref} — بلا رقم مستلم مؤكَّد، القيمة {leg.amount:g} {cur} — خطر ماليّ، راجع فورًا",
                    raw.message_key, forward_key=raw.message_key)
            elif leg.phone_confidence == "uncertain":
                await self.bus.notify_admin(
                    f"⚠️ {ref} — رقم هاتف غير مؤكَّد الدولة ({leg.phone})، سُجّل كما هو — راجع",
                    raw.message_key)
            neg = [d for d in (leg.deviation_log or []) if d.get("method") == "abs_negative"]
            if neg:
                vals = "، ".join(str(d.get("extracted_value")) for d in neg)
                await self.bus.notify_admin(
                    f"⚠️ {ref} — مبلغ سالب سُجّل كموجب (abs): {vals} — راجع", raw.message_key)
            # مرحلة ب — أولويّة ٤: مبلغ موجود بلا عملة مؤكَّدة (لا صريحة/خزينة/هاتف) → تنبيه للمراجعة
            if leg.amount is not None and leg.currency is None:
                await self.bus.notify_admin(
                    f"⚠️ {ref} — مبلغ {leg.amount:g} بلا عملة مؤكَّدة (تعذّر الاستنتاج) — راجع",
                    raw.message_key)
        except Exception as exc:      # best-effort: لا يوقف المعالجة (T5 — نسجّل فقط)
            log.warning("تنبيه مرحلة أ فشل (%s) — تجاهل best-effort: %s", raw.message_key, exc)

    # ═════════════════════════════════════════════════════════════════════════
    # (5·أ) حجْر الإقلاع: صفقات معلّقة قديمة لا تُعالَج تلقائيًّا بعد إعادة التشغيل
    # ═════════════════════════════════════════════════════════════════════════
    async def expire_stale_on_startup(self, now: datetime) -> list[Deal]:
        """عند بدء تشغيل النواة: صفقة معلّقة (WAITING_SECOND_LEG / PARSED / MATCHING / HELD) —
        - عمرها (من created_at) **≤ 15 دقيقة** (INCOMPLETE_DATA_ESCALATE_SECONDS) → **تبقى كما هي**
          فيلتقطها العامل (tick/sweep_waiting) ويُعالَجها طبيعيًّا (استرجاع رسائل وقت العطل §12).
        - عمرها **> 15 دقيقة** → **تُحجَر** (ESCALATED) بلا معالجة ولا كتابة، مع تنبيه المسؤول (§7.3).

        السبب: بعد إطفاء قصير (≤15د) النافذة ما زالت مفتوحة فنُكمل المعالجة لا نُهدرها؛ أمّا الأقدم
        فمعالجتها تلقائيًّا تُدخِل حوالة قديمة/مكرّرة → القاعدة الذهبية (§0): عند الشكّ نصعّد للإنسان.
        🔴 تشمل MATCHING/HELD أيضًا: tick يُعيد معالجتها (process_deal/escalation_tick)، فبلا حجْرها
        تُعالَج صفقة عمرها ساعات عند الإقلاع. تُستدعى مرّة قبل تشغيل العامل فلا يلتقطها tick قبل الفرز."""
        cutoff = _as_naive_utc(now) - timedelta(seconds=INCOMPLETE_DATA_ESCALATE_SECONDS)
        quarantined: list[Deal] = []
        for deal in await self.db.deals.by_status(
            Status.WAITING_SECOND_LEG, Status.PARSED, Status.MATCHING, Status.HELD
        ):
            if _as_naive_utc(deal.created_at) >= cutoff:
                continue                      # ≤15د → تبقى، يُعيد العامل معالجتها (§12)
            deal.status = Status.ESCALATED
            deal.mark = Mark.WARN
            deal.hold_reason = (
                "صفقة معلّقة قديمة عند إعادة التشغيل — لم تُعالَج تفاديًا لإدخال حوالة قديمة (§7.3)"
            )
            await self.db.deals.upsert(deal)  # حجْر صامت (بلا كتابة في MONEYADO)
            await self.bus.notify_admin(
                f"⏰ صفقة معلّقة قديمة ({self._ref(deal)}) عند إعادة التشغيل — لم تُعالَج تلقائيًّا "
                f"(عمرها > {INCOMPLETE_DATA_ESCALATE_SECONDS // 60} دقيقة)؛ مراجعة يدوية.",
                self._deal_key(deal),
            )
            quarantined.append(deal)
            log.warning("حجْر الإقلاع: صفقة %s قديمة → ESCALATED بلا معالجة", deal.deal_id)
        if quarantined:
            log.info("حجْر الإقلاع (§7.3): %d صفقة معلّقة قديمة حُجِرت بلا كتابة", len(quarantined))
        return quarantined

    async def rebuild_sender_slots(self, now: datetime) -> int:
        """Recovery خانات المُرسِل (§7.3): الخانة مشتقّة (cache) — تُعاد اشتقاقها عند الإقلاع لا
        تُستعاد. تُمسح كلها، ثم تُفتح خانة لكل صفقة WAITING عمرها ≤ النافذة ولها مُرسِل معروف؛
        الأقدم يبقى للصفقة عبر expire_stale_on_startup (لا خانة له → لا ربط فوريّ خاطئ بعد الإقلاع).
        تُستدعى **بعد** حجْر الإقلاع فلا تُعيد بناء خانة لصفقة حُجِرت. يُرجع عدد الخانات المُعاد بناؤها."""
        await self.db.sender_slots.clear_all()
        rebuilt = 0
        cutoff = _as_naive_utc(now) - timedelta(seconds=SENDER_SLOT_WINDOW_SECONDS)
        for deal in await self.db.deals.by_status(Status.WAITING_SECOND_LEG):
            leg = deal.sell_leg or deal.buy_leg
            sender = leg.sender_jid if leg is not None else None
            if not sender or not deal.chat_jid:
                continue
            if _as_naive_utc(deal.created_at) < cutoff:
                continue                          # أقدم من النافذة → لا خانة (المسار القديم fallback)
            await self.db.sender_slots.open(
                chat_jid=deal.chat_jid, sender_jid=sender, deal_id=deal.deal_id,
                first_message_key=self._deal_key(deal), now=now,
                window_seconds=SENDER_SLOT_WINDOW_SECONDS,
            )
            rebuilt += 1
        if rebuilt:
            log.info("Recovery خانات المُرسِل (§7.3): أُعيد بناء %d خانة من صفقات معلّقة حديثة", rebuilt)
        return rebuilt

    # ═════════════════════════════════════════════════════════════════════════
    # (5) الطرف الثاني: تصعيد المتأخّر + معالجة الصفقات الجاهزة
    # ═════════════════════════════════════════════════════════════════════════
    async def tick(self, now: datetime) -> None:
        """نبضة دورية: تصعيد المتأخّر (§7.3)، تذكير/تصعيد المطابقة (§8.1)، ومعالجة الجاهز."""
        # ردود خزينة/مورد معلّقة تجاوزت المهلة بلا حوالة تطابقها → تُسقَط (§7.3). الردود **ذات المرجع**
        #   المنتهية = رسالة ثانية بمرجع صريح لم تصل أُولاها → تصعيد ⚠️ للمركزية لا هدرزة صامتة (Fix 1ب).
        from .constants import PENDING_REPLY_MAX_SECONDS
        for exp in await self.db.pending_replies.sweep_expired(now, PENDING_REPLY_MAX_SECONDS):
            await self.bus.reply_central(
                f"⚠️ {exp['reference_number']} — وصلت رسالة ثانية (خزينة/تسوية) بلا رسالتها الأولى "
                f"خلال المهلة؛ لم تُربَط. مراجعة يدوية.",
                exp.get("message_key"), is_alert=True,
            )
            log.warning("رد معلّق بمرجع %s تجاوز المهلة بلا رسالته الأولى → تصعيد ⚠️", exp["reference_number"])

        # حوالة A ناقصة (رسالة أولى بلا رسالة ثانية §7.3، قرار المستخدم):
        #   90s → تنبيه خفيف في المركزية (تبقى منتظِرة)؛ 15 دقيقة → تصعيد لغرفة المسؤول.
        to_warn, to_escalate = await self.queue.sweep_incomplete_a(now)
        for deal in to_warn:
            # رسالة ديناميكية: تذكر **الحقول الناقصة فعلًا** فقط (لا نصّ ثابت). تنبيه حرج → is_alert.
            missing = missing_mandatory_fields(deal.sell_leg or deal.buy_leg)
            detail = "، ".join(missing) if missing else "كود الزبون، الاسم، السعر، الخزينة"
            await self.bus.reply_central(
                f"⚠️ {self._ref(deal)} — ناقص: {detail}.",
                self._deal_key(deal), is_alert=True,
            )
            log.info("تنبيه خفيف: حوالة A ناقصة %s تجاوزت 90s (ناقص: %s)", self._ref(deal), detail)
        for deal in to_escalate:
            # حوالة A ناقصة 15د بلا رسالة ثانية (قرار المستخدم): ❌ على المركزية فقط — بلا تصعيد
            # للمسؤول. الصفقة صارت ESCALATED (نهائية) في sweep_incomplete_a فلا تُعاد معالجتها.
            await self.matcher.apply_mark(deal, Mark.INCOMPLETE)
            # 🔴 (البند 5) ❌ لا تُترك بلا نصّ: رسالة سبب إلزامية للمُرسِل (المطلوب لإصلاحها) — كانت
            #    تفاعلًا صامتًا (react-بلا-send، pipeline.py:720 سابقًا).
            _miss = missing_mandatory_fields(deal.sell_leg or deal.buy_leg)
            _detail = "، ".join(_miss) if _miss else "بيانات الطرف الثاني"
            await self._mandatory_alert(
                deal, f"لم تصل الرسالة الثانية خلال ١٥ دقيقة؛ ناقص: {_detail}", self._deal_key(deal))
            log.warning("حوالة A ناقصة %s تجاوزت 15 دقيقة بلا رسالة ثانية → ❌ على المركزية",
                        self._ref(deal))

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
            # (0) 🔴 بوابة صحّة MONEYADO (§11.3): مغلق/مصغّر/نسختين → **لا نُعالِج ولا نكتب**؛ الصفقة
            #     تبقى بحالتها (PARSED/MATCHING) فتُعيد النبضةُ محاولتَها، وتُستأنَف تلقائيًّا عند التوفّر.
            #     يمنع تحوّلها tech_failed بسبب عدم توفّر التطبيق (بدل الفشل واحدة-واحدة صمتًا).
            if await self._moneyado_gate(now):
                return deal
            # (طبقة قروب الخزينة، توجيه المالك) خزينةٌ غير محلولة من النص → جرّبها من **قروبات الخزائن**
            #   (هاتف+قيمة، ±نافذة) **قبل** أيّ تصعيد. النافذة (~50s) أقصر من مهلة التصعيد (90s) فلا توقف
            #   شيئًا؛ ضمنها بلا مطابقة → انتظار (النبضة تعيد)؛ بعدها بلا مطابقة → المسار الحاليّ (تصعيد).
            _lt = deal.sell_leg or deal.buy_leg
            _rmcfg = await self.db.detection.get()   # يُقرأ حيًّا من الداشبورد (hot-reload، بلا إعادة تشغيل)
            if (getattr(_rmcfg, "room_match_enabled", True)
                    and _lt is not None and _lt.treasury is None and _lt.phone):
                _win = getattr(_rmcfg, "room_match_window_seconds", 50.0)
                _trs = await self.db.treasuries.all_active()
                _sup = await self.db.suppliers.all_active()
                if await self._room_match_treasury(deal, now, _trs, _sup, _win):
                    deal = await self.db.deals.get(deal.deal_id) or deal   # حُلّت → تابع المسار العادي
                elif not _lt.is_si_format:      # الانتظار للمسار العام (مكرر ثم قروب)؛ SI خزينتها معنونة
                    _anchor = _as_naive_utc(deal.first_received_at or deal.created_at or now)
                    if (_as_naive_utc(now) - _anchor).total_seconds() < _win:
                        return deal            # ضمن النافذة، رسالة القروب لم تصل بعد → انتظر النبضة

            # ═══ (طبقة الفهم الذكي — إنقاذ فقط، بديل التخمين الملغى) ═══
            # وصلنا هنا وخزينة الطرف غير محلولة ⇒ فشِل الحلّ الصارم **وطبقة القروب**، والمسار التالي
            # تصعيدٌ حتميّ (SI أدناه، أو بوابة الثقة). فرصةٌ أخيرة: النموذج يقترح، والكود يتحقّق.
            # نجاح التحقّق → تُحلّ الخزينة ويكمل المسار العاديّ؛ أيّ إخفاق → التصعيد يمضي كما هو.
            # مطفأة افتراضيًّا (ai_enabled=False) ⇒ سلوك اليوم مطابق تمامًا ما لم يشغّلها المالك.
            _at = deal.sell_leg or deal.buy_leg
            if _at is not None and _at.treasury is None:
                try:
                    if await self._ai_rescue(deal, now):
                        deal = await self.db.deals.get(deal.deal_id) or deal
                except Exception as exc:   # T5 — الصمود: الطبقة لا تُسقط حوالة أبدًا
                    log.warning("(الفهم الذكي) أخفقت الطبقة للصفقة %s: %s — تصعيد عاديّ",
                                deal.deal_id, exc)

            # (6·SI) حوالة SI معنونة خزينتها مذكورة صراحةً دائمًا — فإن لم تُحلّ (نادر جدًّا:
            # اسم خزينة خارج القوائم/خطأ إملائي) فهي حالة شاذّة تحتاج إنسانًا: تُصعَّد لغرفة
            # المسؤول فورًا بتنبيه واضح (لا HELD صامت، ولا مطابقة غرف بلا خزينة) — §0.
            leg0 = deal.sell_leg or deal.buy_leg
            if leg0 is not None and leg0.is_si_format and leg0.treasury is None:
                deal.status = Status.ESCALATED
                deal.mark = Mark.FAILED
                deal.hold_reason = "لا خزينة (SI بخزينة غير محلولة)"
                await self.db.deals.upsert(deal)
                await self.matcher.apply_mark(deal, Mark.FAILED)   # 🔴 على المركزية (§8.3)
                await self.bus.notify_admin(
                    f"🔴 فشل: {self._ref(deal)} — لا خزينة (اسم الخزينة خارج القوائم أو خطأ إملائي §0).",
                    self._deal_key(deal), forward_key=self._deal_key(deal),
                )
                log.warning("SI بخزينة غير محلولة %s — 🔴 + تصعيد لغرفة المسؤول", deal.deal_id)
                return deal

            # (6a) الخصم وخزينة الطرفين بمورد (§6) — تُحسم عند اكتمال صفقة طرفين بمورد فقط.
            #      🔴 محصور بطرف الشراء من مورد (supplier): طرف الشراء المشتقّ لخزينة sell_and_buy
            #      (يُخلَّق أدناه قبل الكتابة) عمولته محسومة مسبقًا فلا يُعاد حسابها هنا.
            if deal.is_two_legged and deal.sell_leg and deal.buy_leg and (
                deal.buy_leg.is_supplier_counterpart or deal.buy_leg.supplier is not None
            ):
                await self._resolve_two_leg(deal)

            # (6ب) مطابقة الغرف (§8.1) — إن كانت الغرف مُصنّفة (DB مصدر الحقيقة، hot-reload).
            # 🔴 «وضع التلقائي» (auto_trust §13، قابل للتبديل من اللوحة): يتخطّى مطابقة الغرف
            #    ويذهب لبوابة الثقة مباشرة — لا انتظار ظهور الحوالة في غرفة الخزينة/الزبون.
            control = await self.db.control.get()
            if control.auto_trust:
                log.info("وضع التلقائي (auto_trust): تخطّي مطابقة الغرف → بوابة الثقة مباشرة (صفقة %s).",
                         deal.deal_id)
            elif await self._matching_rooms_configured():
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
                # «لا خزينة» (non-SI): خزينة غير محلولة أصلًا → 🔴 فشل + تصعيد (قرار المستخدم).
                # غير ذلك (كود/مبلغ ناقص، أو خزينة بلا كود) → ⚠️ تعليق كالسابق.
                no_treasury = any(
                    lg is not None and lg.treasury is None for lg in (deal.sell_leg, deal.buy_leg)
                )
                if no_treasury:
                    deal.status = Status.ESCALATED
                    deal.mark = Mark.FAILED
                    await self.db.deals.upsert(deal)
                    await self.matcher.apply_mark(deal, Mark.FAILED)   # 🔴 على المركزية (§8.3)
                    await self.bus.notify_admin(
                        f"🔴 فشل: {self._ref(deal)} — لا خزينة محلولة (تعذّر تحديد الحساب).",
                        self._deal_key(deal), forward_key=self._deal_key(deal),
                    )
                    log.warning("🔴 لا خزينة (non-SI) للصفقة %s — تصعيد لغرفة المسؤول", deal.deal_id)
                    return deal
                deal.status = Status.HELD
                deal.mark = Mark.MATCHED   # 🟡 «قيد المراجعة» — انتظار لا فشل (قرار المالك)
                await self.db.deals.upsert(deal)
                await self.matcher.apply_mark(deal, Mark.MATCHED)  # 🟡 على الأولى (§8.3)
                await self.bus.reply_central(f"🟡 قيد المراجعة: {reason}",
                                             self._deal_key(deal), is_alert=True)  # يُبقي السبب نصًّا
                log.warning("🟡 تعليق للصفقة %s: %s", deal.deal_id, reason)
                return deal

            # الحارس قبل الكتابة (§9): لم تُنزَّل + لا إلغاء
            allowed, greason = await self.guard.guard_before_write(deal)
            if not allowed:
                log.info("الحارس منع كتابة الصفقة %s: %s", deal.deal_id, greason)
                return deal

            # (8·ب) طرف الشراء المشتقّ لخزينة sell_and_buy (§6): بيع ثم شراء لنفس الخزينة الخارجية.
            #        يُخلَّق بعد المطابقة/الثقة على البيع الأصلي، وقبل بناء أوامر الكتابة مباشرة.
            had_buy = deal.buy_leg is not None
            self._maybe_synthesize_buy_leg(deal)
            if deal.buy_leg is not None and not had_buy:
                await self.db.deals.upsert(deal)   # احفظ الطرف المشتقّ في السجلّ قبل الكتابة

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
        خزينة sell_and_buy (خصم1%/صافي/تونسي خارجي §6) **مع مورد مذكور صراحةً** في الرسالة
        («المورد: طه 5.72» §5/§6) → البوت يشتقّ طرف شراء من المورد (بيع ثم شراء):

          - amount = المبلغ الصافي (بعد الخصم أو المبلغ نفسه بلا خصم).
          - الحساب = كود المورد، والسعر = سعر المورد (لا سعر البيع)، ويُعلَّم طرف مورد
            (is_supplier_counterpart) ليأخذ كود المورد في شاشة الشراء (§5.3، build_buy_fields).
          - بلا عمولة (commission=None) — العمولة على طرف البيع وحده (§6.2).

        🔴 بلا «المورد:» (sell.supplier is None) → **لا تخليق**: بيع فقط حتى لو نوع الخزينة
        sell_and_buy (قرار صاحب العمل). ويُخلَّق فقط إن لم يوجد طرف شراء بعد (لا يمسّ مسار
        الطرفين برسالتين §5.3).
        """
        sell = deal.sell_leg
        if deal.buy_leg is not None or sell is None or sell.treasury is None:
            return
        # 🔴 تخليق الشراء محصور بـ sell_and_buy **مع مورد صريح**؛ بلا مورد = بيع فقط.
        if sell.treasury.type != TreasuryType.SELL_AND_BUY or sell.supplier is None:
            return
        net = sell.amount_after_discount if sell.amount_after_discount is not None else sell.amount
        # سعر المورد → سعر طرف الشراء (مطبَّع حسب العملة §3.6)؛ الحساب = كود المورد.
        _raw, pnorm = normalize_price(sell.supplier_price_raw, sell.currency or Currency.EGP)
        deal.buy_leg = sell.model_copy(update={
            "operation": OperationType.BUY,
            "amount": net,
            "amount_after_discount": None,
            "commission": None,
            "commission_rate": 0.0,
            "customer_code": sell.supplier.code,
            "customer_name": sell.supplier.name,
            "price_raw": sell.supplier_price_raw,
            "price_normalized": pnorm,
            "is_supplier_counterpart": True,
            "supplier_price_raw": None,          # استُهلك في بناء طرف الشراء
        })
        # 🔴 (حكم المالك النهائي — م: X1322) قيد **البيع** للحوالة المخصومة يُكتب **بالإجمالي
        #    (GROSS) + عمولة سالبة**، وMONEYADO يشتقّ الصافي (GROSS − |عمولة| = NET). لا يُحوَّل
        #    البيع للصافي ولا تُلغى عمولته.
        #    نُسِخ هذا الموضع سابقًا لقاعدة «الطرفان NET» (2680f6f) المبنيّة على افتراض أن خانة
        #    العمولة توثيقيّة — وقد أثبت a1e8e7c أن MONEYADO **يطرحها فعليًّا**، فسقط الافتراض.
        #    الآن القاعدة موحّدة مع خصم البيع-فقط (§6.2) ومع الإلغاء والتعديل (كلاهما GROSS).
        #    طرف الشراء المشتقّ يبقى بالصافي بلا عمولة (عرف MONEYADO القائم — بلا تغيير).
        if sell.amount_after_discount is not None:
            sell.commission = compute_commission(sell, None)   # بعد − قبل (سالبة §6.2)
        sell.commission_rate = 0.0
        deal.is_two_legged = True
        log.info(
            "صفقة %s: خزينة sell_and_buy «%s» مع مورد «%s» → تخليق طرف شراء (مبلغ=%s، سعر=%s، ref=%s)",
            deal.deal_id, sell.treasury.name, sell.supplier.name, net,
            sell.supplier_price_raw, sell.reference_number,
        )

    async def _resolve_two_leg(self, deal: Deal) -> None:
        """يحسب العمولة ويُسند خزينة «خصم1%/صافي» لطرفَي الصفقة (§6.1 §6.2)."""
        commission = compute_commission(deal.sell_leg, deal.buy_leg)
        has_discount = bool(commission is not None and abs(commission) > 1e-9)
        # 🔴 (حكم المالك النهائي — م: X1322) قيد **البيع** للحوالة المخصومة الثنائية يُكتب
        #    **بالإجمالي (GROSS) + عمولة سالبة**، وMONEYADO يشتقّ الصافي. مثال X1322:
        #    المبلغ الأجنبي 8700 والعمولة −87 ⇒ المخصوم 8613 (لا كتابة 8613 مباشرةً بلا عمولة).
        #
        #    تاريخ القاعدة: 2680f6f أدخل «الطرفان NET» بافتراض أن خانة العمولة **توثيقيّة**؛ ثم
        #    أثبت a1e8e7c بتأكيد محاسبيّ من MONEYADO أنها **تُطرَح فعليًّا**، وأعاد الإلغاء إلى
        #    GROSS لكنه ترك هذا الموضع. حكم المالك الآن يوحّد القاعدة: البيع المخصوم GROSS+سالبة
        #    في كل المسارات (بيع-فقط §6.2، ثنائية §6.1، الإلغاء، التعديل).
        #    has_discount يُحسَب **قبل** أيّ تعديل فلا يتأثّر اختيار الخزينة.
        if has_discount:
            deal.sell_leg.commission = commission          # سالبة (بعد − قبل)
        else:
            deal.sell_leg.commission = None
        deal.sell_leg.commission_rate = 0.0    # 0 دائمًا (§6.2)
        if deal.buy_leg is not None:
            deal.buy_leg.commission = None

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

    async def _set_mark(self, deal: Deal, mark: Mark) -> None:
        """يحدّث **علامة** الصفقة فقط في DB (بلا لمس الحالة) — نقطة إصدار علامة لا قرار.
        🔴 مهم: لا نستخدم upsert للصفقة كاملة هنا كي لا تُدهَس الحالة (READY) بحالة قديمة في الذاكرة."""
        deal.mark = mark
        await self.db.deals.col.update_one(
            {"deal_id": deal.deal_id}, {"$set": {"mark": mark.value}})

    async def _mark_pending(self, deal: Deal) -> None:
        """🟡 «بانتظار التأكيد» على كل رسائل الصفقة (نفس تغطية ✅ لكن بلا يقين: تخزين موقوف/
        dry_run/بلا تأكيد SQL). نقطة إصدار علامة فقط — لا تغيّر حالة الصفقة ولا قرار الكتابة/المطابقة."""
        keys = list(deal.source_message_keys)
        if not keys:
            k = self._deal_key(deal)
            keys = [k] if k else []
        for key in keys:
            if key:
                await self.bus.mark_central(key, Mark.MATCHED.value)   # 🟡
        if keys:
            await self.bus.wait_for_reaction_sent(keys)

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
                unconfirmed = res.store_unconfirmed
                # (بند 2) فشل **عابر** قبل الكتابة (توفّر MONEYADO/فورم غير جاهز) → الحوالة تبقى قابلة
                #   للتنفيذ: PARSED فتُستأنَف تلقائيًّا (البوابة + النبضة، FIFO محفوظ) — **ممنوع tech_failed**.
                #   شرط الأمان: لا قيد نزل بعد لهذه الصفقة (وإلا إعادتها تُعيد كتابة ما نزل → عاملها غير مؤكّد).
                if res.requeue:
                    wrote = await self.db.ledger.entries_for_deal(deal.deal_id)
                    if not wrote:
                        deal.status = Status.PARSED
                        await self.db.deals.set_status(
                            deal.deal_id, Status.PARSED, hold_reason="فشل عابر — تُستأنَف تلقائيًّا")
                        await self._mandatory_alert(
                            deal, f"تعذّرت الكتابة مؤقّتًا ({res.error}) — باقية بالطابور، تُستأنَف "
                            f"تلقائيًّا عند توفّر MONEYADO.", key)
                        log.info("(بند 2) فشل عابر — %s تعود PARSED (تُستأنَف): %s", deal.deal_id, res.error)
                        return deal
                    unconfirmed = True   # قيد سابق نزل → لا نعيد؛ نعامله غير مؤكّد (تصعيد يدويّ)
                if unconfirmed:
                    # (بند 2 استثناء) غير مؤكّد التخزين → تصعيد يدويّ، **لا إعادة تلقائية** (خطر ازدواج).
                    deal.status = Status.SELL_DONE if job.operation == OperationType.BUY else Status.TECH_FAILED
                    deal.mark = Mark.FAILED
                    await self.db.deals.set_status(
                        deal.deal_id, deal.status, mark=Mark.FAILED.value,
                        hold_reason="غير مؤكّد التخزين — مراجعة يدويّة (لا إعادة تلقائية)")
                    await self.matcher.apply_mark(deal, Mark.FAILED)
                    await self.bus.flush_reactions()
                    await self.bus.notify_admin(
                        f"🔴 {self._ref(deal)} — {job.operation.value} **غير مؤكّد التخزين** "
                        f"({res.error}). راجعه يدويًّا قبل أيّ إعادة (خطر ازدواج).",
                        key, forward_key=self._deal_key(deal))
                    return deal
                # فشل/شكّ تقنيّ **حقيقيّ** غير قابل للإعادة → dead-letter + tech_failed + تصعيد (§11.3).
                await self.db.dead_letter.add(
                    deal.deal_id, res.error or "فشل كتابة",
                    screenshot_path=res.screenshot_path,
                    details={"job": job.job_id, "operation": job.operation.value},
                )
                deal.status = Status.SELL_DONE if job.operation == OperationType.BUY else Status.TECH_FAILED
                deal.mark = Mark.FAILED
                await self.db.deals.set_status(deal.deal_id, deal.status, mark=Mark.FAILED.value)
                await self.matcher.apply_mark(deal, Mark.FAILED)   # 🔴 على الرسالتين (§8.3)
                await self.bus.flush_reactions()                   # دفع فوري قبل الحوالة التالية (§8.3)
                await self.bus.notify_admin(
                    f"🔴 فشل: {self._ref(deal)} — فشل {job.operation.value}: {res.error} "
                    f"{'(لقطة محفوظة)' if res.screenshot_path else ''}.",
                    key, forward_key=self._deal_key(deal),
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
                await self.matcher.apply_mark(deal, Mark.FAILED)   # 🔴 على الرسالتين (§8.3)
                await self.bus.flush_reactions()                   # دفع فوري قبل الحوالة التالية (§8.3)
                await self.bus.notify_admin(
                    f"🔴 فشل: {self._ref(deal)} — لم يتأكّد حفظ {job.operation.value} في SQL (§11.4).",
                    key, forward_key=self._deal_key(deal),
                )
                return deal
            if job.operation == OperationType.SELL:
                deal.status = Status.SELL_DONE  # بيع نزل (§11.4)
                await self.db.deals.set_status(deal.deal_id, Status.SELL_DONE)

        # DRY_RUN: عُبّئت كل الأطراف والشاشة مفتوحة للمعاينة — ✅ «تمّ — DRY_RUN» بصري فقط،
        # بلا حالة COMPLETED ولا دفتر (كي لا يُحجب التشغيل الحقيقي لاحقًا §9). لا يمسّ Kill Switch.
        # ✅ يُوضع مرّة واحدة (mark != DONE) فلا يتكرّر إن أُعيدت معالجة الصفقة في نبضة لاحقة.
        if dry_run_seen:
            # 🟡 لا ✅: DRY_RUN عُبّئ بلا تخزين فعليّ → «بانتظار» لا «تمّ» (قاعدة المالك: ✅ عند اليقين فقط).
            # الحالة تبقى MATCHED (عُبّئ ومُعروض)؛ المطابقة أصلًا تضع 🟡، فلا نُكرّرها إن كانت موجودة.
            if key:
                already_pending = (deal.mark == Mark.MATCHED)
                deal.mark = Mark.MATCHED
                await self.db.deals.upsert(deal)                # يعيد الحالة MATCHED (بعد أن جعلها build READY)
                if not already_pending:
                    await self._mark_pending(deal)              # 🟡 إن لم تضعها المطابقة
                log.info("🟡 DRY_RUN — الصفقة %s عُبّئت ومُعروضة (بلا تخزين).", deal.deal_id)
            return deal

        # اكتملت كل الأطراف بنجاح (أو Kill Switch إيقاف)
        if commit:
            deal.status = Status.COMPLETED                       # 🔴 قرار الحالة كما هو (لا تغيير منطق)
            if self.verifier.enabled:
                # ✅ فقط عند تأكيد SQL الفعليّ (§11.4) — اليقين الوحيد أن MONEYADO حفظت السجل.
                deal.mark = Mark.DONE
                await self.db.deals.set_status(deal.deal_id, Status.COMPLETED, mark=Mark.DONE.value)
                if key:
                    await self.matcher.apply_mark(deal, Mark.DONE)  # ✅ صامت (§8.3)
                    await self.bus.flush_reactions()
                log.info("✅ الصفقة %s تمّت وتأكّدت (SQL §11.4)", deal.deal_id)
            else:
                # SQL معطّل: خُزِّنت واجهيًّا (ok=True) لكن **بلا يقين** → 🟡 «بانتظار التأكيد» لا ✅
                # (قاعدة المالك: ✅ عند اليقين ١٠٠٪ فقط). فعّل SQL لتظهر ✅.
                deal.mark = Mark.MATCHED
                await self.db.deals.set_status(deal.deal_id, Status.COMPLETED, mark=Mark.MATCHED.value)
                if key:
                    await self._mark_pending(deal)                # 🟡 على كل الرسائل
                log.info("🟡 الصفقة %s خُزِّنت بلا تأكيد SQL — بانتظار التأكيد (فعّل SQL للـ✅)", deal.deal_id)
        else:
            # Kill Switch إيقاف: عُبّئت الشاشة بلا تخزين (المنطقة الميتة سابقًا: بلا أي علامة) →
            # 🟡 «عُبّئت — بانتظار تفعيل التخزين» كي يرى الموظف إشعارًا في الوضع الآمن. (علامة فقط، مرّة.)
            if key and deal.mark != Mark.MATCHED:
                await self._set_mark(deal, Mark.MATCHED)          # علامة فقط (يُبقي READY — لا كتابة مزدوجة)
                await self._mark_pending(deal)                    # 🟡 على كل الرسائل
                log.info("🟡 الصفقة %s عُبّئت والتخزين موقوف — بانتظار التفعيل", deal.deal_id)
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
            # 🔴 (قاعدة الرسائل الإلزامية) الرفض الصامت ممنوع: كان أمر التحكّم يُبتلَع بلا أيّ ردّ،
            #    فيظنّ المُرسِل أن البوت معطَّل (م: «أعد» على X1323 رُفضت مرّتين بصمت 21:29 و21:30).
            #    الآن: ردّ للمُرسِل بالسبب + إشعار المسؤول يحمل **معرّف المُرسِل حرفيًّا** كي يُضاف
            #    للمعتمدين بنسخٍ مباشر (المعرّف قد يكون @lid لا رقم هاتف — انظر _handle_control).
            sender = raw.sender_jid or "؟"
            log.warning("تحكّم «%s» من غير معتمد (%s) — رُفض (§8.3) + إبلاغ.", action, sender)
            await self.bus.reply_central(
                f"🔴 «{action}» مرفوض — المُرسِل غير مُدرَج في «الموظفين المعتمدين».",
                raw.message_key, is_alert=True)
            await self.bus.notify_admin(
                f"🔴 أمر تحكّم «{action}» رُفض: المُرسِل غير معتمد.\n"
                f"معرّفه: {sender}\n"
                f"لاعتماده: الإعدادات ← الموظفون المعتمدون ← أضِف هذا المعرّف حرفيًّا.",
                raw.message_key, forward_key=raw.message_key)
            return

        original = await self.db.deals.find_by_source_key(raw.reply_to_key or "")
        if original is None:
            await self.bus.notify_admin(
                f"⚠️ «{action}» على حوالة غير معروفة (Reply {raw.reply_to_key}) — مراجعة.",
                raw.message_key,
            )
            return

        # (بند 3) إعادة تشغيل يدويّة سهلة (Reply «أعد») لصفقة فاشلة/عالقة — بدل جلسات الفحص. الأمان:
        #   لا قيد نازل بعد (وإلا الإعادة تُحدث ازدواجًا) → تُرفض وتُطلَب مراجعة يدويّة. لا قيد → PARSED.
        if action == "rerun":
            wrote = await self.db.ledger.entries_for_deal(original.deal_id)
            if wrote:
                await self.bus.reply_central(
                    f"⚠️ {self._ref(original)} — لها قيد نازل بالفعل؛ الإعادة قد تُحدث ازدواجًا — "
                    f"راجعها يدويًّا (لم تُعَد).", raw.message_key, is_alert=True)
                return
            await self.db.deals.set_status(
                original.deal_id, Status.PARSED, hold_reason="إعادة تشغيل يدويّة (§بند 3)")
            await self.bus.reply_central(
                f"🔄 {self._ref(original)} — أُعيدت للطابور؛ ستُنفَّذ عند توفّر MONEYADO.",
                raw.message_key, is_alert=True)
            log.info("(بند 3) إعادة تشغيل يدويّة: %s (%s) → PARSED", original.deal_id, original.status.value)
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

        # 🔴 بوابة MONEYADO (§11.3): مغلق/مصغّر/نسختين → لا نكتب القيد العكسيّ (لا فشل صامت). نُخطر
        #    المُرسِل بإعادة الإرسال عند التوفّر (الإلغاء/التعديل رسالة تحكّم لمرّة — يُعاد إرسالها).
        if await self._moneyado_gate(now):
            await self.bus.reply_central(
                f"🔴 MONEYADO غير جاهز الآن — «{action}» {self._ref(original)} لم يُنفَّذ؛ أعد الإرسال عند التوفّر.",
                raw.message_key, is_alert=True)
            return
        # كتابة القيود العكسية (نفس منطق الكتابة/التحقّق)
        await self._write_jobs(original, jobs, now)
        if action == "cancel":
            await self.db.deals.set_status(original.deal_id, Status.CANCELLED)
        log.info("نُفّذ «%s» للصفقة %s بـ %d قيد عكسي", action, original.deal_id, len(jobs))

    # ═════════════════════════════════════════════════════════════════════════
    # ميزة الإلغاء عبر Reply (§10 نسخة نهائية) — مسار معزول تمامًا عن باقي المسارات
    # ═════════════════════════════════════════════════════════════════════════
    async def _cancel_sql_confirmed(self, deal: Deal) -> bool:
        """طبقة ٣ للإلغاء (قراءة فقط): هل قيد الصفقة مؤكَّد فعليًّا في MONEYADO عبر SQL؟
        SQL معطّل ⇒ **غير مؤكَّد** (False) — لا يُتّخذ قرار عكس تلقائيّ بلا يقين (§11.4)."""
        if not self.verifier.enabled:
            return False
        leg = deal.sell_leg or deal.buy_leg
        if leg is None:
            return False
        verified, _ = await self.verifier.verify_transaction(
            leg.reference_number or "", leg.amount or 0.0,
            leg.customer_code or "", leg.operation)
        return bool(verified)

    async def _handle_cancellation(self, raw: RawMessage, now: datetime) -> None:
        """يُلغي حوالة بـ Reply عليها (كلمة إلغاء). معزول: لا يمسّ _ingest ولا مسارات الربط.

        - بلا Reply → 🔴 «يجب الإلغاء عبر Reply».            - لم تُوجد → 🔴.
        - ملغاة مسبقًا → 🔴 (حالة نهائية، لا استرجاع).       - > 96س (توقيت ليبيا) → 🔴.
        - WAITING/PARSED (لم تُكتب) → إلغاء بلا MONEYADO.    - COMPLETED → قيد عكسي في MONEYADO.
        لا فحص للمُرسِل: أيّ شخص في المركزية يقدر يُلغي (استثناء خاص بالإلغاء فقط)."""
        reason = detect_cancellation(raw.text) or ""
        # (أ) لا Reply → إرشاد صريح للموظف (لا هدرزة صامتة)
        if not raw.reply_to_key:
            await self.bus.reply_central(
                "🔴 يجب الإلغاء عبر Reply على رسالة الحوالة الأصلية.", raw.message_key, is_alert=True)
            return
        # (ب) ابحث عن الصفقة بمفتاح الرسالة المُردود عليها ضمن source_message_keys
        deal = await self.db.deals.find_by_source_key(raw.reply_to_key)
        if deal is None:
            await self.bus.reply_central(
                "🔴 لم يُعثر على الحوالة المطلوب إلغاؤها.", raw.message_key, is_alert=True)
            return
        ref = self._ref(deal)
        # (ج) ملغاة مسبقًا (يشمل إلغاء إلغاء) → حالة نهائية، لا استرجاع
        if deal.status in (Status.CANCELLED, Status.CANCELLING):
            await self.bus.reply_central(f"🔴 الحوالة {ref} مُلغاة مسبقًا.", raw.message_key, is_alert=True)
            return
        # (د) نافذة الإلغاء: عمر الصفقة من created_at بتوقيت ليبيا (UTC+2) ≤ 96 ساعة
        if not within_cancellation_window(deal.created_at, now):
            await self.bus.reply_central(
                f"🔴 الحوالة {ref} تجاوزت مدة الإلغاء المسموح بها "
                f"({CANCELLATION_WINDOW_HOURS // 24} أيام).", raw.message_key, is_alert=True)
            return
        # (هـ) لم تُكتب في MONEYADO بعد (WAITING/PARSED) → إلغاء بلا قيد عكسي
        if deal.status in (Status.WAITING_SECOND_LEG, Status.PARSED):
            await self._finalize_cancellation(deal, raw, reason, now, written=False)
            await self.bus.reply_central(
                f"✅ أُلغيت الحوالة {ref} (لم تكن مُدخَلة في MONEYADO).", raw.message_key)
            return
        # (و) COMPLETED → قرار الإلغاء حسب cancellation_mode (§10، افتراضي immediate). القيد بالدفتر
        #     شرطٌ دائمٌ (منع عكس الفراغ)؛ SQL يبقى للتدقيق الدوري ولا يؤثّر على القرار إلا في وضع
        #     sql_required. immediate = عكس فوريّ؛ sql_required = يشترط تأكيد MONEYADO؛ manual = تصعيد يدويّ.
        if deal.status == Status.COMPLETED:
            mode = (await self.db.control.get()).cancellation_mode
            # شرط دائم: قيد أصليّ (غير عكسيّ) موجود بالدفتر؟ لا → 🔴 «قيد مفقود» (خطر عكس فراغ).
            entries = await self.db.ledger.entries_for_deal(deal.deal_id)
            if not any(not e.is_reversal for e in entries):
                await self.db.deals.set_status(
                    deal.deal_id, Status.HELD,
                    hold_reason="إلغاء مطلوب — قيد مفقود بالسجل (خطر عكس فراغ)")
                await self.bus.notify_admin(
                    f"🔴 {ref} — طُلب إلغاؤها لكن القيد مفقود بالسجل — خطر، راجع يدويًا قبل الإلغاء.",
                    raw.message_key, forward_key=self._deal_key(deal))
                log.error("إلغاء %s: قيد مفقود بالسجل → HELD + تصعيد (لا عكس فراغ)", ref)
                return
            # وضع «يدويّ»: لا عكس تلقائيّ إطلاقًا → تصعيد للمراجعة اليدوية.
            if mode == "manual":
                await self.db.deals.set_status(
                    deal.deal_id, Status.HELD, hold_reason="إلغاء مطلوب — وضع يدويّ (manual)")
                await self.bus.notify_admin(
                    f"🟡 {ref} — طُلب إلغاؤها (وضع الإلغاء «يدويّ») — نفّذ القيد العكسي يدويًا.",
                    raw.message_key, forward_key=self._deal_key(deal))
                log.info("إلغاء %s: وضع يدويّ → HELD + تصعيد", ref)
                return
            # وضع «SQL مطلوب»: يشترط تأكيد MONEYADO/SQL قبل العكس (السلوك القديم، اختياريّ الآن).
            if mode == "sql_required" and not await self._cancel_sql_confirmed(deal):
                await self.db.deals.set_status(
                    deal.deal_id, Status.HELD,
                    hold_reason="إلغاء مطلوب — القيد غير مؤكَّد بـMONEYADO (sql_required)")
                await self.bus.notify_admin(
                    f"🟡 {ref} — طُلب إلغاؤها، القيد بالسجل لكن غير مؤكَّد بـMONEYADO (وضع SQL) — "
                    f"راجع يدويًا ثم أكّد.", raw.message_key, forward_key=self._deal_key(deal))
                log.warning("إلغاء %s: القيد غير مؤكَّد بـSQL (sql_required) → HELD + تصعيد", ref)
                return
            # immediate (افتراضيّ) أو SQL مؤكَّد → العكس الفوريّ + 🚫 (القيد بالدفتر مضمون أعلاه).
            if not await self.db.deals.begin_cancelling(deal.deal_id):
                await self.bus.reply_central(f"🔴 الحوالة {ref} مُلغاة مسبقًا.", raw.message_key, is_alert=True)
                return
            # ملاحظة الإلغاء: تذكر التعديل السابق إن وُجد (§6) — «إلغاء {ref} — شامل تعديل سابق: …».
            note = self._cancellation_note(deal, ref)
            jobs = build_cancellation_jobs(deal, utcnow(), ref, note=note)
            if not jobs:
                await self.bus.notify_admin(
                    f"⚠️ إلغاء {ref}: لا أطراف للعكس — مراجعة يدوية (الحالة cancelling).",
                    raw.message_key, forward_key=self._deal_key(deal))
                return
            if not await self._execute_reversal_jobs(deal, jobs, now, "الإلغاء"):
                return  # فشل الكتابة — نُبّه المسؤول داخل الدالة، تبقى cancelling للمراجعة
            await self._finalize_cancellation(deal, raw, reason, now, written=True)
            await self.bus.reply_central(
                f"✅ أُلغيت الحوالة {ref} وسُجّل القيد العكسي في MONEYADO.", raw.message_key)
            return
        # حالات وسطى (MATCHING/HELD/READY/SELL_DONE/ESCALATED/TECH_FAILED) → مراجعة يدوية (§0)
        await self.bus.notify_admin(
            f"⚠️ إلغاء {ref} على صفقة بحالة «{deal.status.value}» — تحتاج مراجعة يدوية (§0).",
            raw.message_key, forward_key=self._deal_key(deal))
        log.warning("إلغاء على حالة وسطى %s للصفقة %s — تصعيد", deal.status, deal.deal_id)

    async def _execute_reversal_jobs(
        self, deal: Deal, jobs: list[WriteJob], now: datetime, label: str = "الإلغاء"
    ) -> bool:
        """يكتب قيود الإلغاء/التعديل بنفس آلية الكتابة/التحقّق العادية (retry داخل الكاتب +
        تحقّق SQL + دفتر is_reversal + dead-letter). يُرجع True عند نجاح كل الأطراف.
        label = «الإلغاء» أو «التعديل» لرسائل المسؤول."""
        control = await self.db.control.get()
        commit = control.storage_enabled  # Kill Switch (§13)
        for job in sorted(jobs, key=lambda j: j.order_index):  # بيع/شراء بالترتيب
            res = await self.writer.write(job, commit=commit)
            if not res.ok:
                await self.db.dead_letter.add(
                    deal.deal_id, res.error or f"فشل كتابة {label}",
                    screenshot_path=res.screenshot_path,
                    details={"job": job.job_id, "operation": job.operation.value, "reversal": label},
                )
                await self.bus.notify_admin(
                    f"🔴 فشل {label} {self._ref(deal)} — فشل كتابة {job.operation.value}: {res.error}.",
                    self._deal_key(deal), forward_key=self._deal_key(deal),
                )
                log.error("فشل كتابة قيد %s للصفقة %s (%s)", label, deal.deal_id, res.error)
                return False
            if res.dry_run or not commit:
                continue  # معاينة/Kill Switch — لا تحقّق/دفتر
            verified, _mref = await self._verify_and_record(deal, job)
            if not verified:
                await self.bus.notify_admin(
                    f"🔴 {label} {self._ref(deal)} — لم يتأكّد حفظ {job.operation.value} في SQL (§11.4).",
                    self._deal_key(deal), forward_key=self._deal_key(deal),
                )
                return False
        return True

    async def _finalize_cancellation(self, deal: Deal, raw: RawMessage, reason: str,
                                     now: datetime, *, written: bool) -> None:
        """يثبّت حالة الإلغاء ويضع التفاعلات (§6): ✅ على رسالة الإلغاء، 🚫 على رسائل الحوالة الأصلية."""
        deal.status = Status.CANCELLED
        deal.cancelled_at = now
        deal.cancelled_by_key = raw.message_key
        deal.cancellation_reason = reason or None
        await self.db.deals.upsert(deal)
        # ✅ على رسالة الإلغاء
        await self.bus.mark_central(raw.message_key, Mark.DONE.value)
        # 🚫 على كل رسائل الحوالة الأصلية (source_message_keys)
        for key in deal.source_message_keys:
            await self.bus.mark_central(key, Mark.CANCELLED.value)
        await self.bus.flush_reactions()
        await self.bus.wait_for_reaction_sent([raw.message_key, *deal.source_message_keys])
        log.info("أُلغيت الصفقة %s (%s) عبر %s — %s",
                 deal.deal_id, self._ref(deal), raw.message_key,
                 "قيد عكسي" if written else "بلا كتابة")

    def _cancellation_note(self, deal: Deal, ref: str) -> str:
        """ملاحظة الإلغاء (§6): تذكر التعديل السابق إن وُجد — «إلغاء {ref} — شامل تعديل سابق: …»."""
        if not deal.amendments:
            return f"إلغاء {ref}"
        original = deal.amendments[0].get("old_net")
        leg = deal.sell_leg or deal.buy_leg
        net = leg.amount if leg else None
        return (f"إلغاء {ref} — شامل تعديل سابق: أصلي {self._amt(original)} ← "
                f"تعديل إلى {self._amt(net)} ← ملغاة")

    # ═════════════════════════════════════════════════════════════════════════
    # ميزة التعديل عبر Reply «تعديل X» (§10 نسخة نهائية) — مسار معزول موازٍ للإلغاء
    # ═════════════════════════════════════════════════════════════════════════
    async def _handle_amendment(self, raw: RawMessage, amount: Optional[float],
                                reason: str, now: datetime) -> None:
        """يُعدّل مبلغ حوالة بـ Reply «تعديل X» (X = المبلغ الجديد، لا الفرق). معزول، ويقرأ الصفقة
        من DB **لحظة التنفيذ** (§3) فيرى آخر صافي بعد أي تعديل/إلغاء سابق بالطابور.

        - بلا رقم → 🔴 «التعديل يحتاج مبلغ». بلا Reply → 🔴. لم تُوجد → 🔴. ملغاة → 🔴. >96س → 🔴.
          طرفين (بيع+شراء) → 🔴. WAITING/PARSED → تعديل مباشر بالـ DB. COMPLETED → قيد الفرق في MONEYADO."""
        if not raw.reply_to_key:
            await self.bus.reply_central(
                "🔴 يجب التعديل عبر Reply على رسالة الحوالة الأصلية.", raw.message_key, is_alert=True)
            return
        if amount is None:
            await self.bus.reply_central(
                "🔴 التعديل يحتاج مبلغ، مثال: تعديل 9000", raw.message_key, is_alert=True)
            return
        deal = await self.db.deals.find_by_source_key(raw.reply_to_key)   # §3 قراءة عند التنفيذ
        if deal is None:
            await self.bus.reply_central(
                "🔴 لم يُعثر على الحوالة المطلوب تعديلها.", raw.message_key, is_alert=True)
            return
        ref = self._ref(deal)
        if deal.status in (Status.CANCELLED, Status.CANCELLING):
            await self.bus.reply_central(
                f"🔴 الحوالة {ref} مُلغاة، لا يمكن تعديلها.", raw.message_key, is_alert=True)
            return
        if not within_cancellation_window(deal.created_at, now):
            await self.bus.reply_central(
                f"🔴 الحوالة {ref} تجاوزت مدة التعديل المسموح بها "
                f"({CANCELLATION_WINDOW_HOURS // 24} أيام).", raw.message_key, is_alert=True)
            return
        if deal.is_two_legged and deal.sell_leg is not None and deal.buy_leg is not None:
            await self.bus.reply_central(
                "🔴 التعديل غير مدعوم لحوالات بيع+شراء — الرجاء الإلغاء ثم إرسال حوالة جديدة.",
                raw.message_key, is_alert=True)
            return
        leg = deal.sell_leg or deal.buy_leg
        if leg is None or leg.amount is None:
            await self.bus.reply_central(f"🔴 تعذّر تعديل {ref} (بلا مبلغ أصلي).", raw.message_key,
                                         is_alert=True)
            return

        current_net = leg.amount
        ratio = amendment_ratio(deal)                       # نسبة الحوالة الأصلية (§5)
        new_commission = floor_commission(amount, ratio) if ratio else None
        old_commission = leg.commission
        prior_note = " (معدّلة سابقًا)" if deal.amendments else ""   # تعديل بعد تعديل (§3)

        # WAITING/PARSED (لم تُكتب) → تعديل مباشر بالـ DB بلا MONEYADO
        if deal.status in (Status.WAITING_SECOND_LEG, Status.PARSED):
            self._apply_amendment_to_leg(deal, amount, new_commission)
            self._record_amendment(deal, raw, current_net, amount,
                                   old_commission, new_commission, reason, now)
            await self.db.deals.upsert(deal)
            await self._react_amendment(deal, raw)
            await self.bus.reply_central(
                f"✅ عُدِّلت الحوالة {ref}: {self._amt(current_net)} ← {self._amt(amount)} "
                f"(لم تُدخَل بعد){prior_note}.", raw.message_key)
            return

        # COMPLETED → قيد الفرق في MONEYADO ثم تسجيل السجلّ
        if deal.status == Status.COMPLETED:
            # 🔴 بوابة MONEYADO (§11.3): مغلق/مصغّر/نسختين → لا نكتب قيد التعديل (لا فشل صامت)؛
            #    نُخطر المُرسِل بإعادة الإرسال عند التوفّر (التعديل رسالة تحكّم لمرّة).
            if await self._moneyado_gate(now):
                await self.bus.reply_central(
                    f"🔴 MONEYADO غير جاهز الآن — تعديل {ref} لم يُنفَّذ؛ أعد الإرسال عند التوفّر.",
                    raw.message_key, is_alert=True)
                return
            # شرط دائم: قيد أصليّ بالدفتر؟ لا → 🔴 «قيد مفقود» (لا تعديل على فراغ) — نظير الإلغاء (§10).
            entries = await self.db.ledger.entries_for_deal(deal.deal_id)
            if not any(not e.is_reversal for e in entries):
                await self.db.deals.set_status(
                    deal.deal_id, Status.HELD, hold_reason="تعديل مطلوب — قيد مفقود بالسجل")
                await self.bus.notify_admin(
                    f"🔴 {ref} — طُلب تعديلها لكن القيد مفقود بالسجل — خطر، راجع يدويًا.",
                    raw.message_key, forward_key=self._deal_key(deal))
                log.error("تعديل %s: قيد مفقود بالسجل → HELD + تصعيد", ref)
                return
            note = f"تعديل {ref}: {self._amt(current_net)} ← {self._amt(amount)}"
            jobs = build_amendment_jobs(deal, current_net, amount, new_commission, note, utcnow())
            if not jobs:
                await self.bus.reply_central(
                    f"🔴 لا فرق للتعديل — الحوالة {ref} مبلغها {self._amt(amount)} أصلًا.",
                    raw.message_key, is_alert=True)
                return
            if not await self._execute_reversal_jobs(deal, jobs, now, "التعديل"):
                return  # فشل الكتابة — صُعّد داخليًا
            # نزل بMONEYADO → سجّل amendments؛ فشل التسجيل بعد الكتابة = فشل نصفي (§15-2) → تصعيد فوري
            try:
                self._apply_amendment_to_leg(deal, amount, new_commission)
                self._record_amendment(deal, raw, current_net, amount,
                                       old_commission, new_commission, reason, now)
                await self.db.deals.upsert(deal)
            except Exception as exc:  # T5 — فشل نصفي: نزل القيد لكن لم يُسجَّل → مراجعة فورية
                await self.bus.notify_admin(
                    f"🔴 تعديل {ref}: نزل القيد في MONEYADO لكن فشل تسجيل السجلّ ({exc}) — "
                    f"فشل نصفي، مراجعة فورية (§15-2).", raw.message_key,
                    forward_key=self._deal_key(deal))
                log.exception("فشل تسجيل amendment بعد كتابة MONEYADO للصفقة %s", deal.deal_id)
                return
            await self._react_amendment(deal, raw)
            await self.bus.reply_central(
                f"✅ عُدِّلت الحوالة {ref}: {self._amt(current_net)} ← {self._amt(amount)} "
                f"وسُجّل الفرق في MONEYADO{prior_note}.", raw.message_key)
            return

        # حالات وسطى (MATCHING/HELD/READY/SELL_DONE/ESCALATED/TECH_FAILED) → مراجعة يدوية (§0)
        await self.bus.notify_admin(
            f"⚠️ تعديل {ref} على صفقة بحالة «{deal.status.value}» — تحتاج مراجعة يدوية (§0).",
            raw.message_key, forward_key=self._deal_key(deal))
        log.warning("تعديل على حالة وسطى %s للصفقة %s — تصعيد", deal.status, deal.deal_id)

    def _apply_amendment_to_leg(self, deal: Deal, new_net: float,
                                new_commission: Optional[float]) -> None:
        """يحدّث الصافي/العمولة الحاليّين في طرف البيع (يبقى deal.sell_leg = القيمة الحاليّة §3)."""
        leg = deal.sell_leg or deal.buy_leg
        if leg is None:
            return
        leg.amount = new_net
        if new_commission is not None:                       # يحفظ إشارة العمولة الأصلية
            sign = -1.0 if (leg.commission or 0.0) < 0 else 1.0
            leg.commission = sign * new_commission

    @staticmethod
    def _record_amendment(deal: Deal, raw: RawMessage, old_net: float, new_net: float,
                          old_commission: Optional[float], new_commission: Optional[float],
                          reason: str, now: datetime) -> None:
        deal.amendments.append({
            "amended_at": now, "amended_by_key": raw.message_key,
            "old_net": old_net, "new_net": new_net,
            "old_commission": old_commission, "new_commission": new_commission,
            "reason": reason or None,
        })

    async def _react_amendment(self, deal: Deal, raw: RawMessage) -> None:
        """تفاعلات التعديل (§7): ✅ على رسالة التعديل، ✏️ على رسائل الحوالة الأصلية."""
        await self.bus.mark_central(raw.message_key, Mark.DONE.value)
        for key in deal.source_message_keys:
            await self.bus.mark_central(key, Mark.AMENDED.value)
        await self.bus.flush_reactions()
        await self.bus.wait_for_reaction_sent([raw.message_key, *deal.source_message_keys])

    # ═════════════════════════════════════════════════════════════════════════
    # مساعدات
    # ═════════════════════════════════════════════════════════════════════════
    @staticmethod
    def _amt(x: Optional[float]) -> str:
        """تنسيق مبلغ للعرض: بلا كسر عشريّ زائد (10000.0 → «10000»)."""
        if x is None:
            return "?"
        return str(int(x)) if float(x).is_integer() else str(x)
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
