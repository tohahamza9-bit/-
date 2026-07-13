"""
طبقة MongoDB — مصدر الحقيقة (§2). مستودعات مُصنّفة (repositories) تستخدمها كل الوحدات.

مبادئ:
- الرسالة الخام تُخزَّن فورًا قبل أي معالجة (§7.1 بند 1).
- الدفتر (ledger) هو مرجع منع التكرار (§9) — append-only، لا تُحذف قيود (§10).
- الحالة الحقيقية في MongoDB؛ تفاعلات واتساب مرآة فقط (§8.3).

الاستخدام:
    db = Database(settings.mongo_uri, settings.mongo_db)
    await db.connect()
    await db.ensure_indexes()
    await db.raw.insert(raw_msg)

كل مستودع يكشف مجموعته عبر `.col` لأي استعلام إضافي تحتاجه الوحدات (extensible).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase

from .constants import RoomType, Status
from .logging_setup import get_logger
from .models import (
    BotControl,
    Deal,
    EmployeeRecord,
    LedgerEntry,
    OutgoingMessage,
    ParsedLeg,
    RawMessage,
    Room,
    SupplierRecord,
    TreasuryRecord,
    WriteJob,
)

log = get_logger(__name__)


def utcnow() -> datetime:
    """وقت UTC مُدرك للمنطقة — يُستخدم لكل الأختام الزمنية."""
    return datetime.now(timezone.utc)


def _naive_utc(dt: datetime) -> datetime:
    """توحيد للمقارنة: القاعدة (mongomock/motor) قد تُرجع أوقاتًا بلا منطقة — نقارن الجميع UTC-naive."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ─────────────────────────────────────────────────────────────────────────────
# مستودع أساس — تسلسل/إلغاء تسلسل pydantic ↔ Mongo
# ─────────────────────────────────────────────────────────────────────────────
class _Repo:
    def __init__(self, mdb: AsyncIOMotorDatabase, name: str, id_field: str):
        self.col = mdb[name]
        self._id_field = id_field

    @staticmethod
    def _dump(model: Any) -> dict:
        return model.model_dump(mode="python")


# ─────────────────────────────────────────────────────────────────────────────
# 1) الرسائل الخام (§7.1)
# ─────────────────────────────────────────────────────────────────────────────
class RawMessageRepo(_Repo):
    async def insert(self, msg: RawMessage) -> None:
        """التقاط فوري — upsert على message_key (لا ازدواج لو أعاد Baileys الإرسال §12)."""
        await self.col.update_one(
            {"message_key": msg.message_key}, {"$setOnInsert": self._dump(msg)}, upsert=True
        )

    async def get(self, message_key: str) -> Optional[RawMessage]:
        doc = await self.col.find_one({"message_key": message_key})
        return RawMessage(**doc) if doc else None

    async def update_edit(self, message_key: str, text: str, edited_at: datetime) -> None:
        """«الحرف» — الموظف يعدّل الرسالة خلال المهلة (§7.2)."""
        await self.col.update_one(
            {"message_key": message_key}, {"$set": {"text": text, "edited_at": edited_at}}
        )

    async def mark_processed(self, message_key: str) -> None:
        await self.col.update_one({"message_key": message_key}, {"$set": {"processed": True}})

    async def last_processed_before(
        self, chat_jid: str, before: datetime
    ) -> Optional[RawMessage]:
        """آخر رسالة **معالَجة** في الغرفة وصلت قبل `before` — لقاعدة تجاور الرسالة الثانية (§7.3):
        الثانية تُربَط فقط إن كانت السابقة مباشرةً من نفس الصفقة."""
        doc = await self.col.find_one(
            {"chat_jid": chat_jid, "processed": True, "received_at": {"$lt": before}},
            sort=[("received_at", -1), ("_id", -1)],
        )
        return RawMessage(**doc) if doc else None

    async def unprocessed(self, limit: int = 200) -> list[RawMessage]:
        """الطابور: غير المعالَجة مرتّبة بختم الوصول (§7.1 بند 3). مفتاح ثانويّ `_id` يكسر التعادل
        عند تساوي `received_at` (دقّة الثانية) → ترتيب **حتميّ** فلا تُسجَّل رسالة قبل أختها عشوائيًّا."""
        cur = self.col.find({"processed": False}).sort([("received_at", 1), ("_id", 1)]).limit(limit)
        return [RawMessage(**d) async for d in cur]


# ─────────────────────────────────────────────────────────────────────────────
# 1-ب) الغرف (§2.2) — تصنيف يُدار في MongoDB: اكتشاف تلقائي + تصنيف من Dashboard.
# 🔴 المركزية/المسؤول (حدود الكتابة) تبقى من env — لا تُقرأ من هنا للكتابة أبدًا (Option A).
# ─────────────────────────────────────────────────────────────────────────────
class RoomRepo(_Repo):
    async def upsert(self, room: Room, *, updated_by: str = "system") -> None:
        """إضافة/تعديل غرفة (تصنيف صريح). يفعّل فورًا (hot-reload عبر المطابق)."""
        room.updated_at = utcnow()
        room.updated_by = updated_by
        payload = self._dump(room)
        if payload.get("discovered_at") is None:
            payload["discovered_at"] = utcnow()
        await self.col.update_one({"jid": room.jid}, {"$set": payload}, upsert=True)

    async def discover(self, jid: str, name: Optional[str] = None) -> None:
        """اكتشاف metadata فقط (§شرط 3): يسجّل jid/الاسم كـ unclassified دون لمس تصنيف موجود.

        لا يُخزَّن نص أي رسالة من غرفة غير مصنّفة — هذا metadata بحت.
        """
        if not jid:
            return
        update: dict = {
            "$setOnInsert": {
                "jid": jid,
                "type": RoomType.UNCLASSIFIED.value,
                "active": True,
                "discovered_at": utcnow(),
                "updated_by": "discovery",
            }
        }
        if name:
            update["$set"] = {"name": name, "updated_at": utcnow()}
        await self.col.update_one({"jid": jid}, update, upsert=True)

    async def seed_if_missing(self, jid: str, room_type: str, name: Optional[str] = None) -> None:
        """بذر غرفة من env أول تشغيل (§شرط 4) — لا يلمس تصنيفًا موجودًا (setOnInsert)."""
        if not jid:
            return
        await self.col.update_one(
            {"jid": jid},
            {"$setOnInsert": {
                "jid": jid, "type": room_type, "name": name, "active": True,
                "discovered_at": utcnow(), "updated_by": "seed",
            }},
            upsert=True,
        )

    async def jids_of_type(self, room_type: str) -> list[str]:
        """قائمة JID المصنّفة النشطة لنوع معيّن (زبون/خزينة) — للمطابقة الصامتة (§8)."""
        cur = self.col.find({"type": room_type, "active": True})
        return [d["jid"] async for d in cur if d.get("jid")]

    async def jid_by_treasury_code(self, treasury_code: str) -> Optional[str]:
        """JID غرفة الخزينة النشطة المرتبطة بكود خزينة معيّن (§8.1) — للبحث المحصور في غرفة
        خزينة الصفقة تحديدًا، لا كل الخزائن. None إن لا غرفة مرتبطة (خزينة بلا غرفة)."""
        if not treasury_code:
            return None
        doc = await self.col.find_one(
            {"type": RoomType.TREASURY.value, "active": True, "treasury_code": treasury_code}
        )
        return doc.get("jid") if doc else None

    async def all(self) -> list[Room]:
        cur = self.col.find({}).sort("discovered_at", 1)
        out: list[Room] = []
        async for d in cur:
            d.pop("_id", None)
            out.append(Room(**d))
        return out

    async def get(self, jid: str) -> Optional[Room]:
        doc = await self.col.find_one({"jid": jid})
        if not doc:
            return None
        doc.pop("_id", None)
        return Room(**doc)


# ─────────────────────────────────────────────────────────────────────────────
# 2) الصفقات (§5 §7.3)
# ─────────────────────────────────────────────────────────────────────────────
class DealRepo(_Repo):
    async def upsert(self, deal: Deal) -> None:
        deal.updated_at = utcnow()
        await self.col.update_one(
            {"deal_id": deal.deal_id}, {"$set": self._dump(deal)}, upsert=True
        )

    async def get(self, deal_id: str) -> Optional[Deal]:
        doc = await self.col.find_one({"deal_id": deal_id})
        return Deal(**doc) if doc else None

    async def find_by_grouping_key(self, key: str) -> Optional[Deal]:
        """التجميع التلقائي (§7.3): رقم إشاري + هاتف خلال المهلة."""
        doc = await self.col.find_one(
            {"grouping_key": key, "status": Status.WAITING_SECOND_LEG.value}
        )
        return Deal(**doc) if doc else None

    async def find_by_source_key(self, message_key: str) -> Optional[Deal]:
        """التجميع اليدوي/Reply (§7.3 §10): الربط بمفتاح الرسالة الأصلية."""
        doc = await self.col.find_one({"source_message_keys": message_key})
        return Deal(**doc) if doc else None

    async def waiting_in_room(self, chat_jid: str) -> list[Deal]:
        """الصفقات المنتظِرة طرفًا ثانيًا في غرفة معيّنة (§7.3 ربط رد الخزينة/المورد بنفس الغرفة)."""
        cur = self.col.find(
            {"status": Status.WAITING_SECOND_LEG.value, "chat_jid": chat_jid}
        ).sort("created_at", 1)
        return [Deal(**d) async for d in cur]

    async def by_status(self, *statuses: Status) -> list[Deal]:
        vals = [s.value for s in statuses]
        cur = self.col.find({"status": {"$in": vals}}).sort("created_at", 1)
        return [Deal(**d) async for d in cur]

    async def set_status(self, deal_id: str, status: Status, **fields: Any) -> None:
        payload = {"status": status.value, "updated_at": utcnow(), **fields}
        await self.col.update_one({"deal_id": deal_id}, {"$set": payload})

    async def begin_cancelling(self, deal_id: str) -> bool:
        """انتقال ذرّي COMPLETED → CANCELLING (ميزة الإلغاء): يمنع الإلغاء المزدوج المتزامن —
        الفلتر يشترط الحالة الحالية completed، فينجح واحد فقط. يُرجع True إن نجح الانتقال."""
        res = await self.col.update_one(
            {"deal_id": deal_id, "status": Status.COMPLETED.value},
            {"$set": {"status": Status.CANCELLING.value, "updated_at": utcnow()}},
        )
        return res.modified_count == 1

    async def completed_since(self, since: datetime) -> list[Deal]:
        """الصفقات المكتملة (COMPLETED) التي تحدّثت منذ `since` — للتدقيق الدوري (§ Reconciliation).
        قراءة فقط، خارج المسار الحيّ. الترشيح الزمنيّ في بايثون (توحيد naive/aware كبقية المستودعات)."""
        out: list[Deal] = []
        lo = _naive_utc(since)
        cur = self.col.find({"status": Status.COMPLETED.value}).sort("updated_at", 1)
        async for d in cur:
            ts = d.get("updated_at")
            if ts is not None and _naive_utc(ts) >= lo:
                out.append(Deal(**d))
        return out


# ─────────────────────────────────────────────────────────────────────────────
# 3) الدفتر — منع التكرار (§9) — Append-only (§10)
# ─────────────────────────────────────────────────────────────────────────────
class LedgerRepo(_Repo):
    async def append(self, entry: LedgerEntry) -> None:
        """قيد إلحاقي فقط — لا تعديل ولا حذف (حفظ الأثر المحاسبي §10)."""
        await self.col.insert_one(self._dump(entry))

    async def was_downloaded(self, message_key: str) -> bool:
        """الحارس (§9): هل نزّل البوت هذه الرسالة فعلًا؟ المرجع = الدفتر + message key."""
        doc = await self.col.find_one(
            {"message_key": message_key, "is_reversal": False}
        )
        return doc is not None

    async def entries_for_deal(self, deal_id: str) -> list[LedgerEntry]:
        cur = self.col.find({"deal_id": deal_id}).sort("created_at", 1)
        return [LedgerEntry(**d) async for d in cur]

    async def by_reference(self, reference_number: str) -> list[LedgerEntry]:
        cur = self.col.find({"reference_number": reference_number}).sort("created_at", 1)
        return [LedgerEntry(**d) async for d in cur]

    async def mark_sql_verified(self, entry_id: str, moneyado_ref: str) -> None:
        await self.col.update_one(
            {"entry_id": entry_id},
            {"$set": {"sql_verified": True, "moneyado_ref": moneyado_ref, "status": Status.COMPLETED.value}},
        )


# ─────────────────────────────────────────────────────────────────────────────
# 4) Outbox — أوامر الكتابة (§2 §7.3 الترتيب: بيع أولًا)
# ─────────────────────────────────────────────────────────────────────────────
class OutboxRepo(_Repo):
    async def enqueue(self, job: WriteJob) -> None:
        await self.col.update_one(
            {"job_id": job.job_id}, {"$setOnInsert": self._dump(job)}, upsert=True
        )

    async def next_pending(self) -> Optional[WriteJob]:
        """أوامر الكتابة بالترتيب الصارم: deal ثم order_index (بيع=0 قبل شراء=1)."""
        doc = await self.col.find_one(
            {"attempts": {"$lt": _ATTEMPTS_GUARD}},
            sort=[("created_at", 1), ("order_index", 1)],
        )
        return WriteJob(**doc) if doc else None

    async def increment_attempt(self, job_id: str) -> None:
        await self.col.update_one({"job_id": job_id}, {"$inc": {"attempts": 1}})

    async def remove(self, job_id: str) -> None:
        await self.col.delete_one({"job_id": job_id})


_ATTEMPTS_GUARD = 999  # يُستبدل بمنطق max_attempts في الأنبوب؛ الحقل موجود للاستعلام


# ─────────────────────────────────────────────────────────────────────────────
# 5) الصادر — طابور رسائل واتساب (يقرأه الجسر). القاعدة الصارمة §2.2 تُطبَّق في bus.py
# ─────────────────────────────────────────────────────────────────────────────
class OutgoingRepo(_Repo):
    async def enqueue(self, msg: OutgoingMessage) -> None:
        payload = self._dump(msg)
        payload["created_at"] = utcnow()
        payload["sent"] = False
        await self.col.insert_one(payload)

    async def next_unsent(self, limit: int = 20) -> list[dict]:
        cur = self.col.find({"sent": False}).sort("created_at", 1).limit(limit)
        return [d async for d in cur]

    async def mark_sent(self, oid: Any) -> None:
        await self.col.update_one({"_id": oid}, {"$set": {"sent": True, "sent_at": utcnow()}})

    async def pending_reactions(self, message_keys: list[str]) -> int:
        """عدد التفاعلات (reaction) على هذه المفاتيح التي لم تُرسَل بعد (sent=False) — لضمان
        تأكيد ظهور ✅/🔴 قبل معالجة الصفقة التالية (§8.3، wait_for_reaction_sent)."""
        if not message_keys:
            return 0
        return await self.col.count_documents({
            "reply_to_key": {"$in": message_keys},
            "reaction": {"$nin": [None, ""]},
            "sent": False,
        })


# ─────────────────────────────────────────────────────────────────────────────
# 6) القوائم المُدارة من Dashboard (§13)
# ─────────────────────────────────────────────────────────────────────────────
class TreasuryListRepo(_Repo):
    async def upsert(self, rec: TreasuryRecord) -> None:
        await self.col.update_one({"name": rec.name}, {"$set": self._dump(rec)}, upsert=True)

    async def all_active(self) -> list[TreasuryRecord]:
        cur = self.col.find({"active": True})
        return [TreasuryRecord(**d) async for d in cur]

    async def seed_if_empty(self, seed: list[dict]) -> None:
        if await self.col.count_documents({}) == 0:
            for s in seed:
                doc = {"active": True, "aliases": [], **s}  # active افتراضيًا (§13 إيقاف بلا حذف)
                await self.col.update_one({"name": doc["name"]}, {"$setOnInsert": doc}, upsert=True)
            log.info("تمّت تهيئة الخزائن الافتراضية (%d)", len(seed))

    async def dedupe_by_code(self) -> int:
        """يزيل الخزائن المكرّرة بنفس الكود (يبقي سجلًّا واحدًا لكل code).

        قاعدة الإبقاء (مهمّة محاسبيًا §4): يُفضَّل السجلّ **النشط** إن وُجد، وإلا الأقدم
        بالإدراج — كي لا يُعطَّل كود بحذف نسخته النشطة والإبقاء على موقوفة.
        السجلات بلا كود (code=None/فارغ) شرعية ومتعدّدة (خزائن معلّقة الأكواد) — لا تُلمَس.
        يُرجع عدد السجلات المحذوفة. آمن للتشغيل المتكرّر (idempotent).
        """
        groups: dict[str, list[dict]] = {}
        async for d in self.col.find({}).sort("_id", 1):
            code = d.get("code")
            if not code:                    # None/"" = غير معلّق بكود → لا يُدمج
                continue
            groups.setdefault(code, []).append(d)
        removed = 0
        for docs in groups.values():
            if len(docs) < 2:
                continue
            keeper = next((x for x in docs if x.get("active")), docs[0])
            for d in docs:
                if d["_id"] == keeper["_id"]:
                    continue
                await self.col.delete_one({"_id": d["_id"]})
                removed += 1
        if removed:
            log.info("إزالة %d خزينة مكرّرة (نفس الكود) — أُبقي النشط/الأقدم لكل كود", removed)
        return removed


class SupplierListRepo(_Repo):
    async def upsert(self, rec: SupplierRecord) -> None:
        await self.col.update_one({"name": rec.name}, {"$set": self._dump(rec)}, upsert=True)

    async def seed_if_missing(self, seed: list[dict]) -> None:
        """يُدرِج الموردين الافتراضيين الناقصين فقط ($setOnInsert) — لا يمسّ الموجود/تعديلات
        Dashboard، فيعمل على قاعدة مأهولة (بخلاف seed_if_empty §5.4)."""
        for s in seed:
            doc = {"active": True, "aliases": [], **s}
            await self.col.update_one({"name": doc["name"]}, {"$setOnInsert": doc}, upsert=True)

    async def all_active(self) -> list[SupplierRecord]:
        cur = self.col.find({"active": True})
        return [SupplierRecord(**d) async for d in cur]


class EmployeeListRepo(_Repo):
    async def upsert(self, rec: EmployeeRecord) -> None:
        await self.col.update_one(
            {"whatsapp_number": rec.whatsapp_number}, {"$set": self._dump(rec)}, upsert=True
        )

    async def all_active(self) -> list[EmployeeRecord]:
        cur = self.col.find({"active": True})
        return [EmployeeRecord(**d) async for d in cur]

    async def is_authorized(self, whatsapp_number: str) -> bool:
        """«تم»/الإلغاء تُقبل من المعتمدين فقط (§8.3 §10)."""
        doc = await self.col.find_one({"whatsapp_number": whatsapp_number, "active": True})
        return doc is not None


class UnknownTermRepo(_Repo):
    """كلمات خزينة/مورد تعذّر حلّها (§4.5 §5.4) — للمراجعة والإسناد اليدويّ (بلا تخمين §0)."""

    async def record(self, term: str, context: str) -> None:
        """يسجّل كلمة مجهولة أو يزيد عدّادها. المفتاح = (term, context). يُطبَّع الفراغ ويُتجاهَل الفارغ."""
        term = (term or "").strip()
        if not term or context not in ("treasury", "supplier"):
            return
        await self.col.update_one(
            {"term": term, "context": context},
            {"$inc": {"count": 1}, "$set": {"last_seen": utcnow()},
             "$setOnInsert": {"term": term, "context": context}},
            upsert=True,
        )

    async def list_recent(self, limit: int = 20) -> list[dict]:
        """آخر الكلمات المجهولة (الأحدث ظهورًا أولًا)."""
        cur = self.col.find({}).sort("last_seen", -1).limit(limit)
        out: list[dict] = []
        async for d in cur:
            d.pop("_id", None)
            out.append(d)
        return out

    async def remove(self, term: str, context: str) -> int:
        """يحذف كلمة مجهولة بعد إسنادها (يُرجع عدد المحذوف)."""
        res = await self.col.delete_one({"term": term, "context": context})
        return res.deleted_count


# ─────────────────────────────────────────────────────────────────────────────
# 7) التحكّم — Kill Switch (§13). الافتراضي عند التشغيل: إيقاف.
# ─────────────────────────────────────────────────────────────────────────────
class ControlRepo(_Repo):
    _KEY = "singleton"

    async def get(self) -> BotControl:
        doc = await self.col.find_one({"_key": self._KEY})
        if not doc:
            ctrl = BotControl()  # الافتراضي: storage_enabled=False (§13)
            await self.set(ctrl, updated_by="system")
            return ctrl
        doc.pop("_key", None)
        doc.pop("_id", None)
        return BotControl(**doc)

    async def set(self, ctrl: BotControl, updated_by: str) -> None:
        ctrl.updated_at = utcnow()
        ctrl.updated_by = updated_by
        payload = self._dump(ctrl)
        payload["_key"] = self._KEY
        await self.col.update_one({"_key": self._KEY}, {"$set": payload}, upsert=True)


# ─────────────────────────────────────────────────────────────────────────────
# 8) طابور الفشل (dead-letter §11.3) — نوافذ طارئة تحتاج مراجعة بشرية
# ─────────────────────────────────────────────────────────────────────────────
class DeadLetterRepo(_Repo):
    async def add(self, deal_id: str, reason: str, screenshot_path: Optional[str] = None,
                  details: Optional[dict] = None) -> None:
        await self.col.insert_one({
            "deal_id": deal_id, "reason": reason, "screenshot_path": screenshot_path,
            "details": details or {}, "created_at": utcnow(), "resolved": False,
        })


# ─────────────────────────────────────────────────────────────────────────────
# 9) الردود المعلّقة (§7.3) — رد خزينة/مورد وصل قبل حوالته → يُحفظ ريثما تصل الأولى
# ─────────────────────────────────────────────────────────────────────────────
class PendingReplyRepo(_Repo):
    """رد خزينة/مورد («بلس»/«صافي» وحدها) وصل بلا حوالة معلّقة تطابقه — يُحتجَز 90s (§7.3)."""

    async def add(self, *, message_key: str, chat_jid: str, leg: ParsedLeg,
                  received_at: datetime) -> None:
        await self.col.update_one(
            {"message_key": message_key},
            {"$setOnInsert": {
                "message_key": message_key, "chat_jid": chat_jid,
                "leg": leg.model_dump(mode="python"),
                "received_at": received_at, "consumed": False,
            }},
            upsert=True,
        )

    async def find_recent(self, chat_jid: str, now: datetime,
                          within_seconds: int) -> Optional[dict]:
        """أحدث رد معلّق غير مُستهلَك في نفس الغرفة خلال النافذة (الأقرب زمنيًا أولًا)."""
        horizon = _naive_utc(now) - timedelta(seconds=within_seconds)
        cur = self.col.find({"chat_jid": chat_jid, "consumed": False}).sort("received_at", -1)
        async for d in cur:
            if _naive_utc(d["received_at"]) >= horizon:
                d.pop("_id", None)
                return d
        return None

    async def find_by_reference(self, chat_jid: str, ref: str, now: datetime,
                                within_seconds: int) -> Optional[dict]:
        """الطبقة ١ (§7.3): **أقدم** ردّ معلّق غير مُستهلَك يحمل نفس الرقم الإشاريّ (حسم بالمرجع، بلا
        قرب). يمنع سحب ردّ لمرجع مختلف."""
        if not ref:
            return None
        nref = re.sub(r"\s+", "", ref).upper()
        horizon = _naive_utc(now) - timedelta(seconds=within_seconds)
        cur = self.col.find({"chat_jid": chat_jid, "consumed": False}).sort("received_at", 1)
        async for d in cur:
            if _naive_utc(d["received_at"]) < horizon:
                continue
            pref = re.sub(r"\s+", "", (d.get("leg") or {}).get("reference_number") or "").upper()
            if pref and pref == nref:
                d.pop("_id", None)
                return d
        return None

    async def find_fifo_for_sender(self, chat_jid: str, sender_jid: Optional[str], now: datetime,
                                   within_seconds: int) -> Optional[dict]:
        """الطبقة ٢ (FIFO §7.3): **أقدم** ردّ معلّق **عديم المرجع** لنفس المُرسِل (received_at تصاعديًّا)
        — أول فتح أول قفل. الردّ ذو المرجع يُسحَب بالطبقة ١ حصرًا فلا يُلتقط هنا."""
        horizon = _naive_utc(now) - timedelta(seconds=within_seconds)
        cur = self.col.find({"chat_jid": chat_jid, "consumed": False}).sort("received_at", 1)
        async for d in cur:
            if _naive_utc(d["received_at"]) < horizon:
                continue
            leg = d.get("leg") or {}
            if (leg.get("reference_number") or "").strip():
                continue                                   # ذو مرجع → للطبقة ١ لا FIFO
            psender = leg.get("sender_jid")
            if sender_jid and psender and psender != sender_jid:
                continue                                   # مُرسِل مختلف → تخطَّ
            d.pop("_id", None)
            return d
        return None

    async def _try_claim(self, doc_id: Any) -> bool:
        """حجز ذرّي لوثيقة بعينها: consumed=false → true بشرط أنها لم تُحجَز بعد (findOneAndUpdate
        عبر update_one المشروط). يُرجع True إن فاز هذا الاستدعاء بالحجز، False إن سبقه غيره (سباق)."""
        res = await self.col.update_one(
            {"_id": doc_id, "consumed": False}, {"$set": {"consumed": True}}
        )
        return res.modified_count == 1

    async def claim_by_reference(self, chat_jid: str, ref: str, now: datetime,
                                 within_seconds: int) -> Optional[dict]:
        """الطبقة ١ (استحواذ ذرّي §7.3): يختار **الأقدم** غير المحجوز بنفس المرجع ويحجزه بعملية
        ذرّية واحدة (consumed=true) — يمنع أخذ صفقتين لنفس الرد تحت الضغط. يُرجع الوثيقة المحجوزة أو
        None (لا مطابق/سبقه غيره). المطابقة/الحجز في نفس المسح؛ عند خسارة السباق يجرّب التالي."""
        if not ref:
            return None
        nref = re.sub(r"\s+", "", ref).upper()
        horizon = _naive_utc(now) - timedelta(seconds=within_seconds)
        cur = self.col.find({"chat_jid": chat_jid, "consumed": False}).sort("received_at", 1)
        async for d in cur:
            if _naive_utc(d["received_at"]) < horizon:
                continue
            pref = re.sub(r"\s+", "", (d.get("leg") or {}).get("reference_number") or "").upper()
            if pref and pref == nref and await self._try_claim(d["_id"]):
                d.pop("_id", None)
                d["consumed"] = True
                return d
        return None

    async def claim_fifo_for_sender(self, chat_jid: str, sender_jid: Optional[str], now: datetime,
                                    within_seconds: int) -> Optional[dict]:
        """الطبقة ٢ (استحواذ ذرّي FIFO §7.3): **أقدم** ردّ عديم‑مرجع لنفس المُرسِل، يُحجَز ذرّيًّا في
        نفس المسح — أول فتح أول قفل بلا سباق. يُرجع الوثيقة المحجوزة أو None."""
        horizon = _naive_utc(now) - timedelta(seconds=within_seconds)
        cur = self.col.find({"chat_jid": chat_jid, "consumed": False}).sort("received_at", 1)
        async for d in cur:
            if _naive_utc(d["received_at"]) < horizon:
                continue
            leg = d.get("leg") or {}
            if (leg.get("reference_number") or "").strip():
                continue                                   # ذو مرجع → للطبقة ١ لا FIFO
            psender = leg.get("sender_jid")
            if sender_jid and psender and psender != sender_jid:
                continue                                   # مُرسِل مختلف → تخطَّ
            if await self._try_claim(d["_id"]):
                d.pop("_id", None)
                d["consumed"] = True
                return d
        return None

    async def release(self, message_key: str) -> None:
        """إطلاق سراح حجزٍ سابق (consumed=false): يُستدعى إن فشل التحقّق بعد الحجز الذرّي (هدف/عملة
        غير متوافقة) فتبقى متاحةً لصفقة أصحّ بدل ضياعها."""
        await self.col.update_one({"message_key": message_key}, {"$set": {"consumed": False}})

    async def consume(self, message_key: str) -> None:
        await self.col.update_one({"message_key": message_key}, {"$set": {"consumed": True}})

    async def sweep_expired(self, now: datetime, ttl_seconds: int) -> list[dict]:
        """الردود المعلّقة التي تجاوزت المهلة بلا حوالة → تُسقَط. **الردود ذات المرجع** المنتهية تُرجَع
        للتصعيد (⚠️): رسالة ثانية بمرجع صريح لم تصل أُولاها = فشل ربط يستوجب تنبيهًا لا هدرزةً صامتة
        (Fix 1ب). الردود عديمة‑المرجع («بلس» شاردة) تبقى هدرزةً صامتة. يُرجع قائمة المنتهية ذات المرجع."""
        horizon = _naive_utc(now) - timedelta(seconds=ttl_seconds)
        expired_refd: list[dict] = []
        async for d in self.col.find({"consumed": False}):
            if _naive_utc(d["received_at"]) < horizon:
                await self.col.update_one({"_id": d["_id"]}, {"$set": {"consumed": True}})
                ref = ((d.get("leg") or {}).get("reference_number") or "").strip()
                if ref:
                    log.warning("رد معلّق بمرجع %s (%s) تجاوز %ss بلا وصول رسالته الأولى → تصعيد ⚠️",
                                ref, d.get("message_key"), ttl_seconds)
                    expired_refd.append({"reference_number": ref, "message_key": d.get("message_key"),
                                         "chat_jid": d.get("chat_jid")})
                else:
                    log.info("رد معلّق %s تجاوز %ss بلا حوالة → أُسقِط كهدرزة",
                             d.get("message_key"), ttl_seconds)
        return expired_refd


# ─────────────────────────────────────────────────────────────────────────────
# 9.5) تقارير التدقيق الدوري (Reconciliation) — تعارض DB ↔ MONEYADO (كشف بعد الحدث)
# ─────────────────────────────────────────────────────────────────────────────
class ReconciliationRepo(_Repo):
    """تعارضات التدقيق الدوري بين ما في DB وما هو مكتوب فعليًّا في MONEYADO (قراءة SQL). سجلّ
    منفصل تمامًا عن مسار المعالجة الحيّ — كشف انحراف الكتابة بعد وقوعه، لا منعٌ وقت الكتابة."""

    async def record(self, *, deal_id: str, reference_number: Optional[str],
                     mismatches: list, detected_at: datetime) -> bool:
        """يسجّل تقرير تعارض لصفقة (setOnInsert بمفتاح deal_id فلا يتكرّر التنبيه كل ساعة). يُرجع
        True إن كان **جديدًا** (أوّل اكتشاف) — عندها فقط يُرسِل الأنبوب تنبيهًا."""
        res = await self.col.update_one(
            {"deal_id": deal_id},
            {"$setOnInsert": {
                "deal_id": deal_id, "reference_number": reference_number,
                "mismatches": mismatches, "detected_at": detected_at, "resolved": False,
            }},
            upsert=True,
        )
        return res.upserted_id is not None


# ─────────────────────────────────────────────────────────────────────────────
# 10) خانات المُرسِل (§7.3) — فهرس مشتقّ (cache) لربط الرسالة الثانية بنفس المُرسِل حتمًا
# ─────────────────────────────────────────────────────────────────────────────
class SenderSlotRepo(_Repo):
    """خانة = «مُرسِل فتح رسالةً أولى تنتظر ثانيتها في غرفة» (§7.3). ليست مصدر حقيقة (Deal هو)؛
    مجرّد فهرس يُسرّع اختيار الصفقة المعلّقة بلا تخمين. تُنظَّف بـ TTL على expires_at. فريدة
    لكل (غرفة|مُرسِل). فقدانها/انتهاؤها لا يفقد بيانات — دورة حياة الصفقة تبقى عبر sweeps."""

    @staticmethod
    def _key(chat_jid: str, sender_jid: str) -> str:
        return f"{chat_jid}|{sender_jid}"

    async def open(self, *, chat_jid: str, sender_jid: str, deal_id: str,
                   first_message_key: Optional[str], now: datetime, window_seconds: int) -> None:
        """يفتح/يستبدل خانة (غرفة|مُرسِل) — خانة واحدة مفتوحة لكل مُرسِل (الأحدث تفوز)."""
        slot_key = self._key(chat_jid, sender_jid)
        await self.col.update_one(
            {"slot_key": slot_key},
            {"$set": {
                "slot_key": slot_key, "chat_jid": chat_jid, "sender_jid": sender_jid,
                "deal_id": deal_id, "first_message_key": first_message_key,
                "opened_at": now, "expires_at": now + timedelta(seconds=window_seconds),
                "status": "open",
            }},
            upsert=True,
        )

    async def find_open(self, chat_jid: str, sender_jid: Optional[str],
                        now: datetime) -> Optional[dict]:
        """خانة مفتوحة غير منتهية لـ (غرفة|مُرسِل). العمر يُفحَص في الاستعلام صراحةً — لا اعتماد
        على توقيت TTL (يمسح بتأخّر ~دقيقة). None إن لا مُرسِل/لا خانة/انتهت."""
        if not sender_jid:
            return None
        doc = await self.col.find_one(
            {"slot_key": self._key(chat_jid, sender_jid), "status": "open"}
        )
        if not doc:
            return None
        if _naive_utc(doc["expires_at"]) < _naive_utc(now):
            return None
        doc.pop("_id", None)
        return doc

    async def fill(self, chat_jid: str, sender_jid: str) -> None:
        """استُهلكت الخانة (رُبطت ثانيتها) → تُحذف (مشتقّة، لا أثر محاسبي)."""
        await self.col.delete_one({"slot_key": self._key(chat_jid, sender_jid)})

    async def clear_all(self) -> None:
        """مسح كل الخانات — تُعاد اشتقاقها عند الإقلاع (§7.3 Recovery)."""
        await self.col.delete_many({})


# ─────────────────────────────────────────────────────────────────────────────
# الواجهة الجامعة
# ─────────────────────────────────────────────────────────────────────────────
class Database:
    def __init__(self, uri: str, db_name: str):
        self._uri = uri
        self._db_name = db_name
        self._client: Optional[AsyncIOMotorClient] = None
        self.mdb: Optional[AsyncIOMotorDatabase] = None

    async def connect(self) -> None:
        self._client = AsyncIOMotorClient(self._uri)
        self.mdb = self._client[self._db_name]
        # المستودعات
        self.raw = RawMessageRepo(self.mdb, "raw_messages", "message_key")
        self.rooms = RoomRepo(self.mdb, "rooms", "jid")
        self.deals = DealRepo(self.mdb, "deals", "deal_id")
        self.ledger = LedgerRepo(self.mdb, "ledger", "entry_id")
        self.outbox = OutboxRepo(self.mdb, "outbox", "job_id")
        self.outgoing = OutgoingRepo(self.mdb, "outgoing", "_id")
        self.treasuries = TreasuryListRepo(self.mdb, "treasuries", "name")
        self.suppliers = SupplierListRepo(self.mdb, "suppliers", "name")
        self.employees = EmployeeListRepo(self.mdb, "employees", "whatsapp_number")
        self.control = ControlRepo(self.mdb, "bot_control", "_key")
        self.dead_letter = DeadLetterRepo(self.mdb, "dead_letter", "_id")
        self.pending_replies = PendingReplyRepo(self.mdb, "pending_replies", "message_key")
        self.unknown_terms = UnknownTermRepo(self.mdb, "unknown_terms", "term")
        self.sender_slots = SenderSlotRepo(self.mdb, "sender_slots", "slot_key")
        self.reconciliation = ReconciliationRepo(self.mdb, "reconciliation_reports", "deal_id")
        log.info("اتصال MongoDB: %s / %s", self._uri, self._db_name)

    async def ensure_indexes(self) -> None:
        """فهارس حرجة للأداء والسلامة (منع التكرار §9)."""
        await self.raw.col.create_index("message_key", unique=True)
        await self.raw.col.create_index([("processed", 1), ("received_at", 1)])
        await self.rooms.col.create_index("jid", unique=True)
        await self.rooms.col.create_index([("type", 1), ("active", 1)])
        await self.deals.col.create_index("deal_id", unique=True)
        await self.deals.col.create_index("grouping_key")
        await self.deals.col.create_index("source_message_keys")
        await self.deals.col.create_index("status")
        await self.deals.col.create_index([("status", 1), ("chat_jid", 1)])
        await self.pending_replies.col.create_index("message_key", unique=True)
        await self.pending_replies.col.create_index([("chat_jid", 1), ("consumed", 1), ("received_at", -1)])
        # الحارس (§9): قيد أصلي واحد فقط لكل (message_key + operation) غير عكسي —
        # فهرس فريد جزئي يمنع الإدخال المزدوج ذرّيًا على مستوى القاعدة (حتى مع سباق).
        # القيود العكسية (is_reversal=True) مستثناة (قد تتكرّر لعدّة تعديلات §10).
        await self.ledger.col.create_index("entry_id", unique=True)
        await self.ledger.col.create_index(
            [("message_key", 1), ("operation", 1)],
            unique=True,
            partialFilterExpression={"is_reversal": False},
            name="uniq_downloaded_leg",
        )
        await self.ledger.col.create_index([("message_key", 1), ("is_reversal", 1)])
        await self.ledger.col.create_index("reference_number")
        await self.outbox.col.create_index("job_id", unique=True)
        await self.outbox.col.create_index([("created_at", 1), ("order_index", 1)])
        await self.outgoing.col.create_index([("sent", 1), ("created_at", 1)])
        # TTL: تنظيف البنود **المُرسَلة** بعد 300s (§8.3 حماية التراكم). الفهرس على `sent_at`
        # لا `created_at` عمدًا — Mongo لا يُطبّق TTL على مستند بلا قيمة تاريخ في الحقل المفهرس،
        # فالبنود غير المُرسَلة (بلا sent_at) لا تنتهي أبدًا → لا تُحذف تنبيهات المسؤول المعلّقة.
        await self.outgoing.col.create_index("sent_at", expireAfterSeconds=300, name="ttl_sent_at")
        await self.employees.col.create_index("whatsapp_number", unique=True)
        # خانات المُرسِل (§7.3): خانة واحدة مفتوحة لكل (غرفة|مُرسِل) + TTL على expires_at.
        # TTL بلا partialFilter (قيد Mongo) — لكن الخانة مشتقّة فحذفها التلقائي غير ضارّ.
        await self.sender_slots.col.create_index("slot_key", unique=True)
        await self.sender_slots.col.create_index(
            "expires_at", expireAfterSeconds=0, name="ttl_slot_expires")
        await self.unknown_terms.col.create_index([("term", 1), ("context", 1)], unique=True)
        await self.unknown_terms.col.create_index("last_seen")
        log.info("تمّت تهيئة الفهارس")

    async def close(self) -> None:
        if self._client:
            self._client.close()
