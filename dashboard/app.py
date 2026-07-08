"""
لوحة التحكّم (Dashboard — §13). تطبيق FastAPI مكتفٍ ذاتيًا يمكن تركيبه لاحقًا
في التطبيق الرئيسي عبر get_router(db) أو تشغيله مستقلًا عبر create_app(db).

المبادئ الحاكمة:
- §13 Kill Switch: الافتراضي عند التشغيل «إيقاف» (storage_enabled=False). مؤشّر حالة running/stopped/error.
- §13 إيقاف بلا حذف للخزائن/الموردين؛ التغييرات تُفعَّل فورًا (تُكتب في MongoDB — مصدر الحقيقة §2).
- §2.2 اللوحة لا ترسل أي رسالة واتساب إطلاقًا؛ تدير القوائم في MongoDB فقط.
- §14.3 نقاط الكتابة محميّة برمز X-Internal-Token (SEC-002). لا أسرار في الكود — الرمز من settings.internal_token (env).
- T5: لا silent catches — كل رفض/خطأ يُسجَّل عبر get_logger.

TODO (SEC-001 JWT §14.3): استبدال/تعزيز حماية X-Internal-Token بمصادقة JWT للمستخدمين
(operator login → JWT قصير الأجل)، وربطها بـ require_internal_token عبر Depends واحد
موحّد. النقطة المخصّصة للربط: الدالة require_internal_token أدناه — تُضاف طبقة JWT هنا.
"""
from __future__ import annotations

import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.config import Settings, get_settings
from core.constants import Currency, RoomType, TreasuryType
from core.db import Database
from core.logging_setup import get_logger
from core.models import BotControl, EmployeeRecord, Room, SupplierRecord, TreasuryRecord

log = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"

# ترتيب عرض الغرف في اللوحة حسب النوع (§13 عرض): مركزية → مسؤول → زبون → خزينة → مورد → غير مصنّفة → متجاهَلة
_ROOM_TYPE_ORDER = {
    RoomType.CENTRAL.value: 0,
    RoomType.ADMIN.value: 1,
    RoomType.CUSTOMER.value: 2,
    RoomType.TREASURY.value: 3,
    RoomType.SUPPLIER.value: 4,
    RoomType.UNCLASSIFIED.value: 5,
    RoomType.IGNORE.value: 6,
}


# ─────────────────────────────────────────────────────────────────────────────
# نماذج الإدخال (تحقّق الطلبات) — منفصلة عن عقود التخزين في core.models
# ─────────────────────────────────────────────────────────────────────────────
class TreasuryIn(BaseModel):
    name: str = Field(..., min_length=1)
    code: Optional[str] = None
    type: TreasuryType = TreasuryType.SELL_ONLY   # بيع فقط / بيع وشراء
    currency: Optional[Currency] = None
    aliases: list[str] = Field(default_factory=list)
    active: bool = True


class SupplierIn(BaseModel):
    name: str = Field(..., min_length=1)
    code: Optional[str] = None                     # كود MONEYADO
    aliases: list[str] = Field(default_factory=list)  # الإملاءات البديلة
    active: bool = True


class EmployeeIn(BaseModel):
    whatsapp_number: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    active: bool = True


class RoomClassifyIn(BaseModel):
    """تصنيف غرفة مكتشَفة. 🔴 لا يغيّر حدود الكتابة (المركزية/المسؤول من env فقط)."""
    type: RoomType
    active: bool = True


class RoomPatchIn(BaseModel):
    """تعديل جزئي لغرفة من صفحة الغرفة (auto-save). كل الحقول اختيارية — يُطبَّق المُرسَل فقط.

    🔴 التصنيف يحكم القراءة/الالتقاط فقط؛ حدود الكتابة (المركزية/المسؤول) تبقى من env (Option A).
    """
    type: Optional[RoomType] = None
    active: Optional[bool] = None
    treasury_code: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# الأمان — X-Internal-Token (SEC-002 §14.3). fail-closed: يرفض إن غاب الرمز إعدادًا.
# ─────────────────────────────────────────────────────────────────────────────
def _make_token_guard(settings: Settings):
    async def require_internal_token(
        x_internal_token: Optional[str] = Header(default=None, alias="X-Internal-Token"),
    ) -> None:
        expected = settings.internal_token
        if not expected:
            # لا رمز مُهيّأ في env → لا تُفتَح الكتابة إطلاقًا (T5: يُسجَّل، لا يُبتلع)
            log.error("رفض كتابة: internal_token غير مُهيّأ في البيئة (SEC-002 §14.3)")
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="internal_token غير مُهيّأ — الكتابة معطّلة (SEC-002)",
            )
        # مقارنة ثابتة الزمن (secrets.compare_digest) — تفادي تسريب الفرق الزمني (SEC-002)
        if not x_internal_token or not secrets.compare_digest(x_internal_token, expected):
            log.warning("رفض كتابة غير مصرّح بها (X-Internal-Token مفقود/خاطئ)")
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="X-Internal-Token مفقود أو غير صحيح",
            )
        # TODO (SEC-001 JWT): بعد إضافة JWT، تحقّق من التوكن + صلاحية operator هنا.

    return require_internal_token


# ─────────────────────────────────────────────────────────────────────────────
# أدوات تسلسل — إخراج السجلات بلا حقول Mongo الداخلية (_id)
# ─────────────────────────────────────────────────────────────────────────────
def _clean(doc: dict) -> dict:
    doc.pop("_id", None)
    doc.pop("_key", None)
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# الراوتر — قابل للتركيب في التطبيق الرئيسي (§13)
# ─────────────────────────────────────────────────────────────────────────────
def get_router(db: Database, settings: Optional[Settings] = None) -> APIRouter:
    settings = settings or get_settings()
    router = APIRouter(prefix="/api", tags=["dashboard"])
    guard = Depends(_make_token_guard(settings))

    # ── تحكّم البوت — Kill Switch (§13) ──────────────────────────────────────
    @router.get("/control")
    async def get_control() -> dict:
        """حالة التخزين الحالية. الافتراضي عند أول تشغيل: stopped (storage_enabled=False)."""
        ctrl = await db.control.get()
        return ctrl.model_dump(mode="json")

    @router.post("/control/toggle", dependencies=[guard])
    async def toggle_control() -> dict:
        """تبديل التخزين: تفعيل = يخزّن تلقائيًا؛ إيقاف = يتوقف عند «تخزين» (§13)."""
        ctrl = await db.control.get()
        ctrl.storage_enabled = not ctrl.storage_enabled
        ctrl.state = "running" if ctrl.storage_enabled else "stopped"
        await db.control.set(ctrl, updated_by="dashboard")
        log.info("Kill Switch: storage_enabled=%s state=%s", ctrl.storage_enabled, ctrl.state)
        return ctrl.model_dump(mode="json")

    # ── إدارة الخزائن (§13) — إيقاف بلا حذف، تفعيل فوري ───────────────────────
    @router.get("/treasuries")
    async def list_treasuries() -> list[dict]:
        """كل الخزائن (نشطة وموقوفة) — الموقوفة تظهر لإعادة تفعيلها (لا حذف §13)."""
        return [_clean(d) async for d in db.treasuries.col.find({})]

    @router.post("/treasuries", dependencies=[guard], status_code=status.HTTP_201_CREATED)
    async def add_treasury(body: TreasuryIn) -> dict:
        """إضافة/تعديل خزينة (upsert على الاسم). تُفعَّل فورًا في MongoDB."""
        rec = TreasuryRecord(**body.model_dump())
        await db.treasuries.upsert(rec)
        log.info("خزينة محدّثة: %s (code=%s type=%s)", rec.name, rec.code, rec.type)
        return rec.model_dump(mode="json")

    @router.put("/treasuries/{name}", dependencies=[guard])
    @router.post("/treasuries/{name}", dependencies=[guard])
    async def edit_treasury(name: str, body: TreasuryIn) -> dict:
        """تعديل خزينة موجودة بالاسم."""
        data = body.model_dump()
        data["name"] = name  # الاسم من المسار هو المفتاح
        rec = TreasuryRecord(**data)
        await db.treasuries.upsert(rec)
        log.info("تعديل خزينة: %s", name)
        return rec.model_dump(mode="json")

    @router.post("/treasuries/{name}/disable", dependencies=[guard])
    async def disable_treasury(name: str) -> dict:
        """إيقاف خزينة (active=false) — بلا حذف (§13)."""
        res = await db.treasuries.col.update_one({"name": name}, {"$set": {"active": False}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"خزينة غير موجودة: {name}")
        log.info("إيقاف خزينة (بلا حذف): %s", name)
        return {"name": name, "active": False}

    # ── إدارة الموردين (§5.4 §13) — مثل الخزائن ───────────────────────────────
    @router.get("/suppliers")
    async def list_suppliers() -> list[dict]:
        return [_clean(d) async for d in db.suppliers.col.find({})]

    @router.post("/suppliers", dependencies=[guard], status_code=status.HTTP_201_CREATED)
    async def add_supplier(body: SupplierIn) -> dict:
        """إضافة/تعديل مورد (اسم + إملاءات بديلة + كود MONEYADO)."""
        rec = SupplierRecord(**body.model_dump())
        await db.suppliers.upsert(rec)
        log.info("مورد محدّث: %s (code=%s)", rec.name, rec.code)
        return rec.model_dump(mode="json")

    @router.put("/suppliers/{name}", dependencies=[guard])
    @router.post("/suppliers/{name}", dependencies=[guard])
    async def edit_supplier(name: str, body: SupplierIn) -> dict:
        data = body.model_dump()
        data["name"] = name
        rec = SupplierRecord(**data)
        await db.suppliers.upsert(rec)
        log.info("تعديل مورد: %s", name)
        return rec.model_dump(mode="json")

    @router.post("/suppliers/{name}/disable", dependencies=[guard])
    async def disable_supplier(name: str) -> dict:
        """إيقاف مورد (active=false) — بلا حذف (§13)."""
        res = await db.suppliers.col.update_one({"name": name}, {"$set": {"active": False}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"مورد غير موجود: {name}")
        log.info("إيقاف مورد (بلا حذف): %s", name)
        return {"name": name, "active": False}

    # ── إدارة الموظفين المعتمدين (§8.3 §13) — «تم» تُقبل من هؤلاء فقط ─────────
    @router.get("/employees")
    async def list_employees() -> list[dict]:
        return [_clean(d) async for d in db.employees.col.find({})]

    @router.post("/employees", dependencies=[guard], status_code=status.HTTP_201_CREATED)
    async def add_employee(body: EmployeeIn) -> dict:
        """إضافة موظف معتمد (رقم واتساب + اسم)."""
        rec = EmployeeRecord(**body.model_dump())
        await db.employees.upsert(rec)
        log.info("موظف معتمد محدّث: %s (%s)", rec.name, rec.whatsapp_number)
        return rec.model_dump(mode="json")

    @router.post("/employees/{number}/disable", dependencies=[guard])
    async def disable_employee(number: str) -> dict:
        """إيقاف موظف (active=false) — يُبطل قبول «تم» منه."""
        res = await db.employees.col.update_one(
            {"whatsapp_number": number}, {"$set": {"active": False}}
        )
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"موظف غير موجود: {number}")
        log.info("إيقاف موظف: %s", number)
        return {"whatsapp_number": number, "active": False}

    @router.delete("/employees/{number}", dependencies=[guard])
    async def delete_employee(number: str) -> dict:
        """حذف موظف معتمد (الحذف مسموح للموظفين — §13)."""
        res = await db.employees.col.delete_one({"whatsapp_number": number})
        if res.deleted_count == 0:
            raise HTTPException(status_code=404, detail=f"موظف غير موجود: {number}")
        log.info("حذف موظف معتمد: %s", number)
        return {"whatsapp_number": number, "deleted": True}

    # ── إدارة الغرف (§2.2) — اكتشاف تلقائي + تصنيف بلا إعادة تشغيل ─────────────
    # 🔴 التصنيف هنا يحكم القراءة/الالتقاط فقط. حدود الكتابة (المركزية/المسؤول)
    #    تبقى مثبّتة في .env (bus._guard) ولا تتأثر بأي تصنيف من اللوحة (Option A).
    @router.get("/rooms")
    async def list_rooms() -> list[dict]:
        """كل الغرف (مصنّفة + مكتشَفة unclassified) — مرتّبة حسب النوع ثم الاسم.

        الترتيب: مركزية → مسؤول → زبون → خزينة → غير مصنّفة → متجاهَلة (§13 عرض).
        يُرجع الحقول name/jid/type/active (+ حقول الاكتشاف) لعرضها في اللوحة.
        """
        rooms = [_clean(d) async for d in db.rooms.col.find({})]
        rooms.sort(key=lambda r: (
            _ROOM_TYPE_ORDER.get(r.get("type"), 99),
            (r.get("name") or r.get("jid") or ""),
        ))
        return rooms

    @router.post("/rooms/{jid}/classify", dependencies=[guard])
    @router.put("/rooms/{jid}/classify", dependencies=[guard])
    async def classify_room(jid: str, body: RoomClassifyIn) -> dict:
        """تصنيف غرفة (زبون/خزينة/تجاهل/…). يُفعَّل فورًا (hot-reload) — بلا إعادة تشغيل."""
        existing = await db.rooms.get(jid)
        name = existing.name if existing else None
        rec = Room(jid=jid, name=name, type=body.type, active=body.active,
                   discovered_at=existing.discovered_at if existing else None)
        await db.rooms.upsert(rec, updated_by="dashboard")
        log.info("تصنيف غرفة: %s → %s (active=%s)", jid, body.type.value, body.active)
        return rec.model_dump(mode="json")

    @router.patch("/rooms/{jid}", dependencies=[guard])
    async def patch_room(jid: str, body: RoomPatchIn) -> dict:
        """تعديل جزئي موحّد لغرفة (صفحة /rooms، auto-save): {type?, active?, treasury_code?}.

        يُطبَّق المُرسَل فقط ويحافظ على البقية (الاسم/الاكتشاف). يُفعَّل فورًا (hot-reload).
        إن لم يعُد النوع «خزينة» تُصفَّر treasury_code تلقائيًا (لا كود يتيم).
        """
        existing = await db.rooms.get(jid)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"غرفة غير موجودة: {jid}")
        data = existing.model_dump()
        provided = body.model_dump(exclude_unset=True)
        if "type" in provided:
            data["type"] = body.type
        if "active" in provided:
            data["active"] = body.active
        if "treasury_code" in provided:
            data["treasury_code"] = body.treasury_code
        # صيانة الاتّساق: كود الخزينة لا يُحفظ إلا لغرفة خزينة
        if RoomType(data["type"]) is not RoomType.TREASURY:
            data["treasury_code"] = None
        rec = Room(**data)
        await db.rooms.upsert(rec, updated_by="dashboard")
        log.info("تعديل غرفة (PATCH): %s → type=%s active=%s treasury_code=%s",
                 jid, rec.type.value, rec.active, rec.treasury_code)
        return rec.model_dump(mode="json")

    @router.post("/rooms/{jid}/disable", dependencies=[guard])
    async def disable_room(jid: str) -> dict:
        """إيقاف غرفة (active=false) — بلا حذف (§13). يوقف التقاطها/مطابقتها."""
        res = await db.rooms.col.update_one({"jid": jid}, {"$set": {"active": False}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"غرفة غير موجودة: {jid}")
        log.info("إيقاف غرفة (بلا حذف): %s", jid)
        return {"jid": jid, "active": False}

    return router


# ─────────────────────────────────────────────────────────────────────────────
# التطبيق المستقل — يُشغَّل عبر uvicorn dashboard.app:app (تشغيل يدوي فقط)
# ─────────────────────────────────────────────────────────────────────────────
def create_app(db: Database, settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="MONEYADO Dashboard", version="1.0")
    app.include_router(get_router(db, settings))

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/rooms")
    async def rooms_page() -> FileResponse:
        """صفحة إدارة الغرف البسيطة (§2.2) — تصنيف بالأسماء فقط، بلا إدخال JID يدوي."""
        return FileResponse(str(STATIC_DIR / "rooms.html"))

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


# تشغيل يدوي (غير معتمد في الاختبارات): يربط MongoDB الحقيقي ثم يقدّم اللوحة.
async def _bootstrap() -> FastAPI:  # pragma: no cover - تشغيل يدوي فقط
    settings = get_settings()
    db = Database(settings.mongo_uri, settings.mongo_db)
    await db.connect()
    await db.ensure_indexes()
    return create_app(db, settings)
