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

    async def unprocessed(self, limit: int = 200) -> list[RawMessage]:
        """الطابور: غير المعالَجة مرتّبة بختم الوصول (§7.1 بند 3)."""
        cur = self.col.find({"processed": False}).sort("received_at", 1).limit(limit)
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

    async def consume(self, message_key: str) -> None:
        await self.col.update_one({"message_key": message_key}, {"$set": {"consumed": True}})

    async def sweep_expired(self, now: datetime, ttl_seconds: int) -> int:
        """الردود المعلّقة التي تجاوزت المهلة بلا حوالة → تُسقَط (هدرزة). يُرجع العدد المُسقَط."""
        horizon = _naive_utc(now) - timedelta(seconds=ttl_seconds)
        dropped = 0
        async for d in self.col.find({"consumed": False}):
            if _naive_utc(d["received_at"]) < horizon:
                await self.col.update_one({"_id": d["_id"]}, {"$set": {"consumed": True}})
                log.info("رد معلّق %s تجاوز %ss بلا حوالة → أُسقِط كهدرزة",
                         d.get("message_key"), ttl_seconds)
                dropped += 1
        return dropped


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
        await self.employees.col.create_index("whatsapp_number", unique=True)
        log.info("تمّت تهيئة الفهارس")

    async def close(self) -> None:
        if self._client:
            self._client.close()
