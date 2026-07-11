"""
MatchingService — مطابقة الغرف + التذكير/التصعيد + وضع العلامات (§8.1, §8.3).

التسلسل (§8.1):
  الحوالة تصل → بحث في غرفة الزبون + غرفة الخزينة (اسم + مبلغ + هاتف كمميّز)
  خلال نافذة 10–15s → وجدها في الاثنتين → 🔸 (MATCHED).
  لم يجدها → تعليق → تذكير أول → تذكير ثانٍ بعد 15 دقيقة → غرفة المسؤول + إغلاق.

مصدر الحقيقة = MongoDB (db.deals)؛ التفاعل مرآة فقط (§8.3). كل مخرجات البوت
تمرّ من Bus حصرًا — لا كتابة في غرف الزبائن/الخزائن أبدًا (§2.2).
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from typing import Optional

from ..constants import (
    Mark,
    REMINDER_INTERVAL_SECONDS,
    ROOM_MATCH_MAX_SECONDS,
    ROOM_MATCH_WINDOW_SECONDS,
    RoomType,
    Status,
    TreasuryType,
)
from ..db import Database
from ..bus import Bus
from ..logging_setup import get_logger
from ..models import Deal, ParsedLeg
from .fuzzy import names_match, normalize_ar, normalize_digits

log = get_logger(__name__)


def _as_naive_utc(dt: datetime) -> datetime:
    """توحيد للمقارنة الزمنية: القاعدة قد تُرجع أوقاتًا بلا منطقة (mongomock) أو بمنطقة (motor)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt

# الحالات النهائية التي لا يلمسها التذكير/التصعيد بعدها (§8.1 بند 7)
_TERMINAL = {
    Status.MATCHED,
    Status.READY,
    Status.SELL_DONE,
    Status.COMPLETED,
    Status.ESCALATED,
    Status.CANCELLED,
    Status.TECH_FAILED,
    Status.IGNORED,
}


class MatchingService:
    """
    خدمة المطابقة والتصعيد. تُحقَن قوائم غرف الزبائن/الخزائن (تُدار من Dashboard §13).
    القراءة من غرف الزبائن/الخزائن مسموحة (صامتة)؛ الكتابة ممنوعة تمامًا (§2.2 — يفرضها Bus).
    """

    def __init__(
        self,
        db: Database,
        bus: Bus,
        customer_room_jids: Optional[list[str]] = None,
        treasury_room_jids: Optional[list[str]] = None,
    ):
        self._db = db
        self._bus = bus
        # قوائم seed من env (§شرط 4): تُستخدم كاحتياط إن خلت مجموعة rooms في DB.
        self._customer_rooms = list(customer_room_jids or [])
        self._treasury_rooms = list(treasury_room_jids or [])
        # مصدر التصنيف الحيّ = مجموعة rooms في MongoDB (hot-reload بلا إعادة تشغيل §شرط 5).
        # cache قصير يمنع استعلام DB في كل نبضة مطابقة مع بقاء التفعيل شبه فوري.
        self._rooms_cache: dict[str, tuple[float, list[str]]] = {}
        self._rooms_cache_ttl = 5.0  # ثوانٍ — اضبطها 0 لقراءة حيّة دائمًا (اختبارات hot-reload)

    # ── تصنيف الغرف الحيّ (§شرط 5: hot-reload) ──────────────────────────────
    async def _resolve_rooms(self, room_type: RoomType, seed: list[str]) -> list[str]:
        """قائمة JID لنوع الغرفة من DB (مصدر الحقيقة) مع cache قصير؛ seed من env كاحتياط.

        أي تصنيف جديد من Dashboard/الاكتشاف يظهر خلال مهلة الـ cache بلا إعادة تشغيل.
        """
        now = time.monotonic()
        cached = self._rooms_cache.get(room_type.value)
        if cached is not None and cached[0] > now:
            return cached[1]

        jids = seed
        repo = getattr(self._db, "rooms", None)
        if repo is not None:
            try:
                db_jids = await repo.jids_of_type(room_type.value)
                # DB مصدر الحقيقة إن حوى تصنيفًا؛ وإلا نعود للـ seed (أول تشغيل قبل البذر).
                jids = db_jids if db_jids else seed
            except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونكمل بالـ seed الآمن
                log.warning("تعذّر قراءة تصنيف الغرف من DB (%s): %s — استخدام seed", room_type.value, exc)
                jids = seed

        self._rooms_cache[room_type.value] = (now + self._rooms_cache_ttl, jids)
        return jids

    # ── بحث في الغرف ────────────────────────────────────────────────────────
    async def find_room_match(
        self, deal: Deal, room_jids: list[str], *, amount: Optional[float] = None
    ) -> bool:
        """
        هل ظهرت الحوالة في إحدى الغرف المعطاة؟ المطابقة بالاسم + المبلغ،
        والهاتف كمميّز (§8.1) — الغرف لا تحمل رقم العملية.

        بيانات الغرف تصل كـ RawMessage في db.raw (chat_jid ضمن room_jids). البحث محصور
        في **نافذة ±ساعة حول وقت الحوالة** (deal.created_at) لتفادي مطابقة رسائل قديمة
        بنفس المبلغ/الهاتف (§8.1).

        `amount`: المبلغ المطلوب مطابقته في نصّ الغرفة. يُمرَّر صراحةً لأن **كل غرفة تُطابَق
        بمبلغ مختلف** في صيغة الخصم (§6.3): الزبون بالمبلغ قبل الخصم، والخزينة بالمبلغ بعده.
        None → يُستخدم `leg.amount` (السلوك الافتراضي للصيغ بلا خصم).
        """
        leg = deal.sell_leg or deal.buy_leg
        if leg is None or not room_jids:
            return False

        match_amount = leg.amount if amount is None else amount
        anchor = _as_naive_utc(deal.created_at)
        cur = self._db.raw.col.find({"chat_jid": {"$in": room_jids}})
        async for doc in cur:
            rat = doc.get("received_at")
            if rat is not None and abs((_as_naive_utc(rat) - anchor).total_seconds()) > ROOM_MATCH_WINDOW_SECONDS:
                continue  # خارج نافذة ±ساعة → رسالة قديمة/بعيدة، تجاهل
            text = doc.get("text", "") or ""
            if self._leg_in_text(leg, text, match_amount):
                log.info(
                    "مطابقة غرفة: الصفقة %s ظهرت في %s (اسم/مبلغ/هاتف، ضمن ±%ds)",
                    deal.deal_id, doc.get("chat_jid"), ROOM_MATCH_WINDOW_SECONDS,
                )
                return True
        return False

    async def _match_treasury_room(self, deal: Deal) -> bool:
        """
        مطابقة غرفة الخزينة محصورة في غرفة **خزينة الصفقة تحديدًا** (§8.1) — لا كل الخزائن.

        الربط: `db.rooms.treasury_code` = كود خزينة الصفقة → ابحث في تلك الغرفة فقط.
        🔴 لا غرفة مرتبطة بخزينة الصفقة (خزينة بلا غرفة، مثل «بلاس فون») → **لا اعتماد تلقائي**:
           يُرفع `deal.treasury_no_room` و`return False`؛ يتولّى `escalation_tick` تنبيه المركزية
           لطلب اعتماد يدوي («تم» من موظف معتمد) ثم التصعيد للمسؤول بعد 15 دقيقة (§8.1، قرار
           صاحب العمل 2026-07-08). خطأ قراءة DB (عارض) يبقى fail-open (True، لا حجب بلا يقين).
        🔴 استثناء: خزينة خارجية (sell_and_buy: خصم1%/صافي/تونسي خارجي §6) بلا غرفة واتساب →
           **اعتماد تلقائي** (treasury=True، بلا «تم» يدوي): خزائن محاسبية داخلية لا غرفة لها
           أصلًا، فلا معنى لطلب اعتماد يدوي عليها (قرار صاحب العمل).
        """
        # خزينة خارجية sell_and_buy → اعتماد تلقائي (لا غرفة، لا «تم» يدوي).
        treasury = self._deal_treasury(deal)
        if treasury is not None and getattr(treasury, "type", None) == TreasuryType.SELL_AND_BUY:
            deal.treasury_no_room = False
            log.debug("خزينة الصفقة %s خارجية (sell_and_buy) → اعتماد تلقائي بلا غرفة (§6).",
                      deal.deal_id)
            return True

        code = self._deal_treasury_code(deal)
        repo = getattr(self._db, "rooms", None)

        # لا كود خزينة أصلاً (خزينة غير محلولة/بلا كود) → لا مرساة لربط غرفة، ولا معنى لطلب
        # «تم» بلا كود. الاعتماد التلقائي هنا غير حاجب: بوابة الثقة لاحقًا تحجب الخزينة غير
        # المحلولة/بلا كود (§8.2). (يُبقي سلوك المطابقة السابق لهذه الحالة سليمًا.)
        if not code:
            deal.treasury_no_room = False
            return True

        jid: Optional[str] = None
        if repo is not None:
            try:
                jid = await repo.jid_by_treasury_code(code)
            except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونعتبرها متطابقة (لا حجب بلا يقين، عارض)
                log.warning("تعذّر جلب غرفة خزينة الكود %s (صفقة %s): %s — خزينة=True (عارض)",
                            code, deal.deal_id, exc)
                deal.treasury_no_room = False
                return True
        if not jid:
            # خزينة بكود لكن بلا غرفة مرتبطة (مثل «بلاس فون» كود 74) → لا اعتماد تلقائي؛
            # تحتاج «تم» يدوياً من موظف معتمد (§8.1، قرار صاحب العمل 2026-07-08).
            deal.treasury_no_room = True
            log.info("خزينة الصفقة %s (كود=%s) بلا غرفة مرتبطة → اعتماد يدوي («تم») مطلوب (§8.1)",
                     deal.deal_id, code)
            return False
        deal.treasury_no_room = False
        return await self.find_room_match(deal, [jid], amount=self._treasury_match_amount(deal))

    @staticmethod
    def _deal_treasury_code(deal: Deal) -> Optional[str]:
        """كود خزينة الصفقة (نفس اختيار الطرف في find_room_match: بيع أولًا ثم شراء)."""
        leg = deal.sell_leg or deal.buy_leg
        if leg and leg.treasury:
            return leg.treasury.code
        return None

    # ── مطابقة غرفة المورد (§8 — الطرف الثاني/الشراء في صفقات الطرفين) ─────────
    async def _match_supplier_room(self, deal: Deal) -> bool:
        """
        مطابقة طرف الشراء (`buy_leg`) في غرفة المورد للمطابقة الثلاثية (§8، صفقات الطرفين).

        صفقة طرف واحد (لا `buy_leg`) → المطابقة الثلاثية غير منطبقة و`return True` (لا تحجب
        اكتمال المطابقة). صفقة الطرفين → البحث في غرف الموردين المصنّفة (`RoomType.SUPPLIER`)
        عن **الرقم الإشاري + مبلغ الشراء** — نظير غرفة الخزينة، لكن المميّز هنا الرقم الإشاري
        (المورد يذكره صراحةً) بدل الاسم/الهاتف.
        🔴 لا غرفة مورد مضافة إطلاقًا → **لا اعتماد تلقائي**: يُرفع `deal.supplier_no_room`
           و`return False`؛ يتولّى `escalation_tick` طلب اعتماد يدوي («تم») ثم التصعيد بعد
           15 دقيقة — نفس منطق الخزينة بلا غرفة (§8.1).
        """
        buy = deal.buy_leg
        if not deal.is_two_legged or buy is None:
            deal.supplier_no_room = False
            return True
        supplier_rooms = await self._resolve_rooms(RoomType.SUPPLIER, [])
        if not supplier_rooms:
            deal.supplier_no_room = True
            log.info("صفقة الطرفين %s بلا غرفة مورد مضافة → اعتماد يدوي («تم») مطلوب (§8)",
                     deal.deal_id)
            return False
        deal.supplier_no_room = False
        return await self.find_room_match_by_ref(deal, buy, supplier_rooms)

    async def find_room_match_by_ref(
        self, deal: Deal, leg: ParsedLeg, room_jids: list[str]
    ) -> bool:
        """
        هل ظهر الطرف في إحدى الغرف بالمطابقة على **الرقم الإشاري + المبلغ** (غرفة المورد §8)؟

        يختلف عن `find_room_match` (اسم/هاتف): المورد ينشر الرقم الإشاري صراحةً فهو المميّز.
        البحث محصور في نافذة ±ساعة حول وقت الصفقة (`deal.created_at`) كنظيره في غرفة الخزينة.
        """
        if not leg.reference_number or not room_jids:
            return False
        anchor = _as_naive_utc(deal.created_at)
        cur = self._db.raw.col.find({"chat_jid": {"$in": room_jids}})
        async for doc in cur:
            rat = doc.get("received_at")
            if rat is not None and abs((_as_naive_utc(rat) - anchor).total_seconds()) > ROOM_MATCH_WINDOW_SECONDS:
                continue  # خارج نافذة ±ساعة → تجاهل
            text = doc.get("text", "") or ""
            if self._ref_in_text(leg.reference_number, text) and self._amount_in_text(leg.amount, text):
                log.info(
                    "مطابقة غرفة مورد: الصفقة %s (طرف شراء) ظهرت في %s (رقم إشاري %s + مبلغ، ضمن ±%ds)",
                    deal.deal_id, doc.get("chat_jid"), leg.reference_number, ROOM_MATCH_WINDOW_SECONDS,
                )
                return True
        return False

    @staticmethod
    def _ref_in_text(reference_number: Optional[str], text: str) -> bool:
        """هل يظهر الرقم الإشاري (A6xxx) في نص الغرفة؟ يتسامح مع حالة الأحرف والأرقام العربية."""
        if not reference_number:
            return False
        ref = normalize_digits(reference_number).casefold().replace(" ", "")
        if not ref:
            return False
        return ref in normalize_digits(text).casefold().replace(" ", "")

    # ── مبلغ المطابقة لكل غرفة (§6.3: قبل الخصم للزبون، بعده للخزينة) ──────────
    @staticmethod
    def _customer_match_amount(deal: Deal) -> Optional[float]:
        """غرفة الزبون تُطابَق دائمًا بالمبلغ **الأصلي (قبل الخصم)** — `leg.amount`."""
        leg = deal.sell_leg or deal.buy_leg
        return leg.amount if leg else None

    @staticmethod
    def _treasury_match_amount(deal: Deal) -> Optional[float]:
        """
        غرفة الخزينة تُطابَق بالمبلغ **بعد الخصم** في كل صيغ الخصم (§6.3): الخزينة (مثل ابو
        يوسف) تنشر المبلغ الصافي بعد الخصم لا الأصلي — يشمل SI المعنونة وAخصم من رسالتين.

        بلا خصم (`amount_after_discount = None`) → `leg.amount` كما هو (بلا تغيير للسلوك).
        """
        leg = deal.sell_leg or deal.buy_leg
        if leg is None:
            return None
        if leg.amount_after_discount is not None:
            return leg.amount_after_discount
        return leg.amount

    def _leg_in_text(self, leg: ParsedLeg, text: str, amount: Optional[float] = None) -> bool:
        """المطابقة: المبلغ يجب أن يظهر + (الاسم تقريبيًا أو الهاتف) — الهاتف مميّز.

        `amount` يُمرَّر صراحةً (قبل/بعد الخصم حسب نوع الغرفة §6.3)؛ None → `leg.amount`.
        """
        amount_ok = self._amount_in_text(leg.amount if amount is None else amount, text)
        if not amount_ok:
            return False
        name_ok = self._name_in_text(leg.customer_name, text)
        phone_ok = self._phone_in_text(leg.phone, text)
        return name_ok or phone_ok

    @staticmethod
    def _amount_in_text(amount: Optional[float], text: str) -> bool:
        if amount is None:
            return False
        norm = normalize_digits(text)
        # شيل فواصل الآلاف بين الأرقام (§3.5): 1.600 / 1,600 / 1٬600 / «1 600» → 1600.
        # 🔴 مسافة/تاب فقط (لا \n): رسائل الغرف مرتّبة أسطرًا، و\s كان يبتلع السطر الجديد
        # فيلصق الهاتف بالمبلغ («01887777881␊53.000» → رقم واحد) فيضيع المبلغ ولا يُطابَق.
        compact = re.sub(r"(?<=\d)[ \t.,،'٬](?=\d)", "", norm)
        target = str(int(round(amount)))
        for m in re.findall(r"\d+(?:\.\d+)?", compact):
            try:
                if str(int(round(float(m)))) == target:
                    return True
            except ValueError:  # T5 — لا نبتلع بصمت
                log.debug("رقم غير قابل للتحويل أثناء مطابقة المبلغ: %r", m)
        return False

    @staticmethod
    def _phone_in_text(phone: Optional[str], text: str) -> bool:
        if not phone:
            return False
        digits = re.sub(r"\D", "", normalize_digits(phone))
        if len(digits) < 6:  # قصير جدًّا ليكون مميّزًا
            return False
        text_digits = re.sub(r"\D", "", normalize_digits(text))
        return digits in text_digits

    @staticmethod
    def _name_in_text(name: Optional[str], text: str) -> bool:
        if not name:
            return False
        name_tokens = normalize_ar(name).split()
        text_norm = normalize_ar(text)
        if not name_tokens or not text_norm:
            return False
        text_tokens = text_norm.split()
        # كل كلمة من الاسم يجب أن تجد كلمة مطابقة تقريبيًا في نص الغرفة
        for nt in name_tokens:
            if not any(names_match(nt, tt) for tt in text_tokens):
                return False
        return True

    # ── مطابقة الغرفتين (§8.1 بند 1–3) ──────────────────────────────────────
    async def match_in_rooms(self, deal: Deal, now: datetime) -> Deal:
        """
        يبحث في غرفة الزبون وغرفة الخزينة. وجدها في الاثنتين → 🔸 (MATCHED) فورًا.
        لم يجدها → تبقى قيد المطابقة (MATCHING) بانتظار الظهور/إعادة المحاولة.
        """
        if deal.status in _TERMINAL:
            return deal

        # التصنيف الحيّ للزبائن من DB (§شرط 5) مع احتياط seed من env (§شرط 4). الخزينة لا تُقرأ
        # كقائمة كاملة — البحث محصور في غرفة خزينة الصفقة تحديدًا (ربط treasury_code، أدناه).
        customer_rooms = await self._resolve_rooms(RoomType.CUSTOMER, self._customer_rooms)
        log.debug("مطابقة الغرف: صفقة %s — غرف زبون=%d", deal.deal_id, len(customer_rooms))
        # غرفة الزبون: المبلغ قبل الخصم (الأصلي §6.3). غرفة الخزينة: بعد الخصم (داخل _match_treasury_room).
        deal.matched_customer_room = await self.find_room_match(
            deal, customer_rooms, amount=self._customer_match_amount(deal)
        )
        # 🔴 الخزينة: البحث محصور في غرفة خزينة الصفقة تحديدًا (ربط treasury_code)، لا كل الخزائن.
        deal.matched_treasury_room = await self._match_treasury_room(deal)
        # طرف الشراء (صفقات الطرفين): المطابقة الثلاثية في غرفة المورد (رقم إشاري + مبلغ).
        # صفقات الطرف الواحد → True (غير منطبقة، لا تحجب اكتمال المطابقة).
        deal.matched_supplier_room = await self._match_supplier_room(deal)
        log.debug("نتيجة المطابقة: صفقة %s — زبون=%s، خزينة=%s، مورد=%s",
                  deal.deal_id, deal.matched_customer_room, deal.matched_treasury_room,
                  deal.matched_supplier_room)

        if deal.matched_customer_room and deal.matched_treasury_room and deal.matched_supplier_room:
            deal.status = Status.MATCHED
            deal.mark = Mark.MATCHED
            log.info("الصفقة %s تطابقت في الغرفتين → 🔸", deal.deal_id)
            log.debug("وضع 🔸 (mark_central) للصفقة %s على مفتاح %s",
                      deal.deal_id, self._deal_key(deal))
            await self._db.deals.upsert(deal)
            await self.apply_mark(deal, Mark.MATCHED)
            return deal

        # لم تكتمل المطابقة → تبقى قيد المطابقة (التذكير/التصعيد لاحقًا)
        if deal.status != Status.MATCHING:
            deal.status = Status.MATCHING
        await self._db.deals.upsert(deal)
        log.info(
            "الصفقة %s لم تتطابق بعد (زبون=%s، خزينة=%s، مورد=%s)",
            deal.deal_id, deal.matched_customer_room, deal.matched_treasury_room,
            deal.matched_supplier_room,
        )
        return deal

    # ── التذكير والتصعيد (§8.1 بند 5–7) ─────────────────────────────────────
    async def escalation_tick(self, deal: Deal, now: datetime) -> Deal:
        """
        إدارة «غير موجودة» → تذكير أول → تذكير ثانٍ بعد 15 دقيقة → غرفة المسؤول + إغلاق.
        يعتمد deal.reminders_sent و deal.last_reminder_at و REMINDER_INTERVAL_SECONDS.
        يُستدعى دوريًا (tick) على الصفقات قيد المطابقة.
        """
        if deal.status in _TERMINAL:
            return deal

        key = self._deal_key(deal)
        elapsed = (_as_naive_utc(now) - _as_naive_utc(deal.created_at)).total_seconds()

        # لم تنقضِ نافذة المطابقة بعد (10–15s) → ما زالت فرصة الظهور
        if deal.reminders_sent == 0 and elapsed < ROOM_MATCH_MAX_SECONDS:
            return deal

        # 🔴 خزينة الصفقة بلا غرفة مرتبطة → مسار اعتماد يدوي خاص (§8.1، قرار صاحب العمل):
        #    تنبيه واحد يطلب «تم» ثم تصعيد للمسؤول بعد 15 دقيقة — لا تذكيرا «غير موجودة».
        if deal.treasury_no_room and not deal.matched_treasury_room:
            return await self._treasury_no_room_tick(deal, now, key)

        # 🔴 صفقة طرفين بلا غرفة مورد مضافة → نفس مسار الاعتماد اليدوي («تم») للخزينة بلا غرفة.
        if deal.supplier_no_room and not deal.matched_supplier_room:
            return await self._supplier_no_room_tick(deal, now, key)

        # التذكير الأول: «غير موجودة» في المركزية (§8.1 بند 5)
        if deal.reminders_sent == 0:
            await self._bus.reply_central(self._not_found_text(deal), key)
            deal.reminders_sent = 1
            deal.last_reminder_at = now
            deal.status = Status.HELD
            log.info("الصفقة %s: تذكير «غير موجودة» أول", deal.deal_id)
            await self._db.deals.upsert(deal)
            return deal

        since_last = (
            (_as_naive_utc(now) - _as_naive_utc(deal.last_reminder_at)).total_seconds()
            if deal.last_reminder_at else 1e18
        )

        # التذكير الثاني بعد 15 دقيقة (§8.1 بند 7)
        if deal.reminders_sent == 1:
            if since_last >= REMINDER_INTERVAL_SECONDS:
                await self._bus.reply_central(self._not_found_text(deal, second=True), key)
                deal.reminders_sent = 2
                deal.last_reminder_at = now
                log.info("الصفقة %s: تذكير «غير موجودة» ثانٍ", deal.deal_id)
                await self._db.deals.upsert(deal)
            return deal

        # بعد التذكيرين بلا رد → تحويل لغرفة المسؤول + إغلاق في الدفتر (§8.1 بند 7)
        if deal.reminders_sent >= 2 and since_last >= REMINDER_INTERVAL_SECONDS:
            await self._bus.notify_admin(self._escalation_text(deal), key)
            deal.status = Status.ESCALATED
            log.warning("الصفقة %s: تصعيد لغرفة المسؤول + إغلاق (§8.1)", deal.deal_id)
            await self._db.deals.upsert(deal)
        return deal

    async def _treasury_no_room_tick(self, deal: Deal, now: datetime, key: Optional[str]) -> Deal:
        """
        خزينة الصفقة بلا غرفة مرتبطة (§8.1): تنبيه واحد في المركزية يطلب اعتماداً يدوياً
        («تم» من موظف معتمد على رسالة الحوالة) → الصفقة معلّقة (HELD) → 15 دقيقة بلا «تم»
        → تصعيد لغرفة المسؤول (ESCALATED). «تم» يُعالَج في Pipeline._handle_control (تجاوز
        بشري يضبط الغرفتين True ويُكمل الإدخال)؛ بعد ESCALATED يُرفض «تم» تلقائياً → مسؤول.
        """
        # التنبيه لمرة واحدة (reminders_sent يميّز: 0 = لم يُنبَّه بعد)
        if deal.reminders_sent == 0:
            await self._bus.reply_central(self._treasury_no_room_text(deal), key)
            deal.reminders_sent = 1
            deal.last_reminder_at = now
            deal.status = Status.HELD
            deal.mark = Mark.WARN
            log.info("الصفقة %s: خزينة بلا غرفة — تنبيه اعتماد يدوي («تم») في المركزية", deal.deal_id)
            await self._db.deals.upsert(deal)
            return deal

        since_last = (
            (_as_naive_utc(now) - _as_naive_utc(deal.last_reminder_at)).total_seconds()
            if deal.last_reminder_at else 1e18
        )
        # 15 دقيقة بلا «تم» → تصعيد لغرفة المسؤول + إغلاق (§8.1 بند 7)
        if since_last >= REMINDER_INTERVAL_SECONDS:
            await self._bus.notify_admin(self._treasury_no_room_escalation_text(deal), key)
            deal.status = Status.ESCALATED
            log.warning("الصفقة %s: خزينة بلا غرفة بلا «تم» خلال 15 دقيقة — تصعيد للمسؤول (§8.1)",
                        deal.deal_id)
            await self._db.deals.upsert(deal)
        return deal

    async def _supplier_no_room_tick(self, deal: Deal, now: datetime, key: Optional[str]) -> Deal:
        """
        صفقة طرفين بلا غرفة مورد مضافة (§8): نفس منطق الخزينة بلا غرفة — تنبيه واحد في
        المركزية يطلب اعتماداً يدوياً («تم» من موظف معتمد) → معلّقة (HELD) → 15 دقيقة بلا
        «تم» → تصعيد لغرفة المسؤول (ESCALATED). «تم» يُعالَج في Pipeline._handle_control.
        """
        if deal.reminders_sent == 0:
            await self._bus.reply_central(self._supplier_no_room_text(deal), key)
            deal.reminders_sent = 1
            deal.last_reminder_at = now
            deal.status = Status.HELD
            deal.mark = Mark.WARN
            log.info("الصفقة %s: بلا غرفة مورد — تنبيه اعتماد يدوي («تم») في المركزية", deal.deal_id)
            await self._db.deals.upsert(deal)
            return deal

        since_last = (
            (_as_naive_utc(now) - _as_naive_utc(deal.last_reminder_at)).total_seconds()
            if deal.last_reminder_at else 1e18
        )
        # 15 دقيقة بلا «تم» → تصعيد لغرفة المسؤول + إغلاق (§8.1 بند 7)
        if since_last >= REMINDER_INTERVAL_SECONDS:
            await self._bus.notify_admin(self._supplier_no_room_escalation_text(deal), key)
            deal.status = Status.ESCALATED
            log.warning("الصفقة %s: بلا غرفة مورد بلا «تم» خلال 15 دقيقة — تصعيد للمسؤول (§8)",
                        deal.deal_id)
            await self._db.deals.upsert(deal)
        return deal

    # ── وضع العلامات (§8.3) ─────────────────────────────────────────────────
    async def apply_mark(self, deal: Deal, mark: Mark) -> None:
        """
        يضع العلامة على رسالة/رسائل المركزية (§8.3). التفاعلات (قرار المستخدم):
          🟡 MATCHED (مراجعة/انتظار) → **الرسالة الأولى فقط**.
          ✅ DONE (سُجِّلت) و🔴 FAILED (فشل تقني) و❌ INCOMPLETE (ناقصة 15د) → **كل** رسائل الصفقة.
          ⚠️ WARN → Reply نصّي بالسبب (ليس تفاعلًا) على الرسالة الأولى فقط.
        """
        if mark is Mark.WARN:
            key = self._deal_key(deal)
            if key is None:
                log.error("🔴 لا مفتاح رسالة للصفقة %s — تعذّر ⚠️", deal.deal_id)
                return
            reason = deal.hold_reason or "شك — تحتاج مراجعة"
            await self._bus.reply_central(f"⚠️ {reason}", key)
            return

        # 🟡 على الأولى فقط؛ ✅/🔴 على كل الرسائل (§8.3)
        if mark is Mark.MATCHED:
            single = self._deal_key(deal)
            keys = [single] if single else []
        else:                                        # DONE / FAILED
            keys = list(deal.source_message_keys)
            if not keys:
                single = self._deal_key(deal)
                keys = [single] if single else []
        if not keys:
            log.error("🔴 لا مفتاح رسالة للصفقة %s — تعذّر وضع العلامة %s", deal.deal_id, mark)
            return
        for key in keys:
            await self._bus.mark_central(key, mark.value)

    # ── مساعدات ─────────────────────────────────────────────────────────────
    @staticmethod
    def _deal_key(deal: Deal) -> Optional[str]:
        """مفتاح رسالة المركزية للحوالة (للـ Reply/التفاعل §8.3)."""
        if deal.sell_leg and deal.sell_leg.source_message_key:
            return deal.sell_leg.source_message_key
        if deal.buy_leg and deal.buy_leg.source_message_key:
            return deal.buy_leg.source_message_key
        if deal.source_message_keys:
            return deal.source_message_keys[0]
        return None

    @staticmethod
    def _ref(deal: Deal) -> str:
        leg = deal.sell_leg or deal.buy_leg
        return (leg.reference_number if leg and leg.reference_number else deal.deal_id)

    def _not_found_text(self, deal: Deal, second: bool = False) -> str:
        tag = "تذكير ثانٍ" if second else "تنبيه"
        return f"⚠️ {tag}: الحوالة {self._ref(deal)} غير موجودة في الغرف — يُرجى المراجعة."

    @staticmethod
    def _deal_treasury(deal: Deal) -> Optional[object]:
        leg = deal.sell_leg or deal.buy_leg
        return leg.treasury if leg else None

    def _treasury_no_room_text(self, deal: Deal) -> str:
        """تنبيه المركزية: خزينة بلا غرفة → اعتماد يدوي بـ«تم» على رسالة الحوالة (§8.1)."""
        t = self._deal_treasury(deal)
        tname = t.name if t else "؟"
        tcode = t.code if (t and t.code) else "؟"
        return (
            f"⚠️ {self._ref(deal)} — لا غرفة مرتبطة لخزينة {tname} (كود {tcode})\n"
            f"للاعتماد اليدوي: ردّ بـ «تم» على رسالة الحوالة"
        )

    def _treasury_no_room_escalation_text(self, deal: Deal) -> str:
        t = self._deal_treasury(deal)
        tname = t.name if t else "؟"
        return (
            f"🚨 تصعيد: الحوالة {self._ref(deal)} بخزينة «{tname}» بلا غرفة مرتبطة، "
            f"ولم يصل «تم» خلال 15 دقيقة — تحتاج اعتماداً/إدخالاً يدوياً (§8.1)."
        )

    @staticmethod
    def _deal_supplier(deal: Deal) -> Optional[object]:
        """مورد طرف الشراء (buy_leg) — لصياغة تنبيهات «لا غرفة مورد»."""
        return deal.buy_leg.supplier if deal.buy_leg else None

    def _supplier_no_room_text(self, deal: Deal) -> str:
        """تنبيه المركزية: صفقة طرفين بلا غرفة مورد → اعتماد يدوي بـ«تم» (نظير الخزينة §8)."""
        s = self._deal_supplier(deal)
        sname = s.name if s else "؟"
        scode = s.code if (s and s.code) else "؟"
        return (
            f"⚠️ {self._ref(deal)} — لا غرفة مورد مضافة (مورد {sname}، كود {scode})\n"
            f"للاعتماد اليدوي: ردّ بـ «تم» على رسالة الحوالة"
        )

    def _supplier_no_room_escalation_text(self, deal: Deal) -> str:
        s = self._deal_supplier(deal)
        sname = s.name if s else "؟"
        return (
            f"🚨 تصعيد: الحوالة {self._ref(deal)} — طرف الشراء من مورد «{sname}» بلا غرفة مورد، "
            f"ولم يصل «تم» خلال 15 دقيقة — تحتاج اعتماداً/إدخالاً يدوياً (§8)."
        )

    def _escalation_text(self, deal: Deal) -> str:
        return (
            f"🚨 تصعيد: الحوالة {self._ref(deal)} لم تظهر بعد تذكيرين — "
            f"حُوّلت للمسؤول وأُغلقت في الدفتر (§8.1). المسؤول ينزّلها يدويًا."
        )
