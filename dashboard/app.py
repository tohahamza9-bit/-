"""
لوحة التحكّم (Dashboard — §13). تطبيق FastAPI مكتفٍ ذاتيًا يمكن تركيبه لاحقًا
في التطبيق الرئيسي عبر get_router(db) أو تشغيله مستقلًا عبر create_app(db).

المبادئ الحاكمة:
- §13 Kill Switch: الافتراضي عند التشغيل «إيقاف» (storage_enabled=False). مؤشّر حالة running/stopped/error.
- §13 إيقاف بلا حذف للخزائن/الموردين؛ التغييرات تُفعَّل فورًا (تُكتب في MongoDB — مصدر الحقيقة §2).
- §2.2 اللوحة لا ترسل أي رسالة واتساب إطلاقًا؛ تدير القوائم في MongoDB فقط.
- §14.3 SEC-001: مصادقة مستخدمين بأدوار (manager/reviewer/data_entry) عبر جلسات خادم + كوكي
  httpOnly. كل مسار مَحميّ: القراءة لـ manager+reviewer، الكتابة/إدارة المستخدمين لـ manager فقط.
  الرفض الفعلي في الـ backend لا الواجهة. منطق المصادقة معزول في dashboard/auth.py.
- ملاحظة: settings.internal_token لم يعُد حارس اللوحة (استُبدل بتسجيل الدخول)؛ يبقى الإعداد لأن
  Bus يستخدمه للاتصال بجسر واتساب (core/app.py) — لا نلمسه.
- T5: لا silent catches — كل رفض/خطأ يُسجَّل عبر get_logger.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.config import Settings, get_settings
from core.constants import Currency, Role, RoomType, TreasuryType
from core.db import Database, utcnow
from core.logging_setup import get_logger
from core.models import (
    BotControl,
    DetectionConfig,
    EmployeeRecord,
    OutgoingMessage,
    Room,
    SupplierRecord,
    TreasuryRecord,
    UserRecord,
)

from . import auth, transfers

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


class UnknownTermAssignIn(BaseModel):
    """إسناد كلمة مجهولة كـ alias لخزينة/مورد محدَّد بالكود (§4.5 §5.4)."""
    type: str = Field(..., pattern="^(treasury|supplier)$")
    target_code: str = Field(..., min_length=1)


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


class LoginIn(BaseModel):
    """اعتماد تسجيل الدخول (§14.3)."""
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class UserIn(BaseModel):
    """إنشاء مستخدم لوحة (manager فقط). كلمة المرور تُهاش argon2id قبل التخزين."""
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8)     # حدّ أدنى بسيط ضد كلمات هشّة
    role: Role = Role.DATA_ENTRY                 # أقلّ امتياز افتراضيًا


class PasswordIn(BaseModel):
    password: str = Field(..., min_length=8)


class RoleIn(BaseModel):
    role: Role


class ReviewIn(BaseModel):
    """تعليق «علّم كمراجَع» — ملاحظة اختيارية (لا يغيّر حالة الحوالة)."""
    note: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# أدوات تسلسل — إخراج السجلات بلا حقول Mongo الداخلية (_id) / بلا هاش كلمة المرور
# ─────────────────────────────────────────────────────────────────────────────
def _clean(doc: dict) -> dict:
    doc.pop("_id", None)
    doc.pop("_key", None)
    return doc


def _user_out(rec: UserRecord) -> dict:
    """تمثيل مستخدم آمن للإخراج — بلا password_hash/تفاصيل القفل الداخلية (§14.3)."""
    return {
        "username": rec.username,
        "role": rec.role.value,
        "active": rec.active,
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
        "last_login_at": rec.last_login_at.isoformat() if rec.last_login_at else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# الراوتر — قابل للتركيب في التطبيق الرئيسي (§13)
# ─────────────────────────────────────────────────────────────────────────────
def get_router(db: Database, settings: Optional[Settings] = None) -> APIRouter:
    settings = settings or get_settings()
    router = APIRouter(prefix="/api", tags=["dashboard"])

    # ── المصادقة والصلاحيات (§14.3 SEC-001) ──────────────────────────────────
    # current_user: جلسة صالحة أو 401. manager: كتابة/إدارة. reader: قراءة (manager+reviewer).
    # data_entry مستثنى من كل مسارات الإعدادات الحالية (ينتظر شاشة نطاقه).
    current_user = auth.make_current_user(db, settings)
    require_manager = auth.make_require_roles(current_user, Role.MANAGER)
    require_read = auth.make_require_roles(current_user, Role.MANAGER, Role.REVIEWER)
    manager = Depends(require_manager)
    reader = Depends(require_read)

    async def _active_managers() -> int:
        """عدد المديرين النشطين — لمنع تعطيل/تنزيل آخر مدير (تفادي القفل الكامل)."""
        return await db.users.col.count_documents({"role": Role.MANAGER.value, "active": True})

    # ── تسجيل الدخول/الخروج (§14.3) ───────────────────────────────────────────
    @router.post("/auth/login")
    async def auth_login(body: LoginIn, request: Request, response: Response) -> dict:
        """يتحقّق من الاعتماد، يضبط كوكي الجلسة، يُرجع {username, role}. خطأ عام (لا كشف وجود)."""
        try:
            token, user = await auth.login(
                db, settings, username=body.username, password=body.password,
                ip=auth.client_ip(request), now=utcnow(),
            )
        except auth.AccountLocked as exc:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="محاولات كثيرة — الحساب مقفول مؤقتًا، حاول لاحقًا",
                headers={"Retry-After": str(exc.retry_after_seconds)},
            )
        except auth.InvalidCredentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="اسم المستخدم أو كلمة المرور غير صحيحة",
            )
        auth.set_session_cookie(response, token, settings)
        return {"username": user.username, "role": user.role.value}

    @router.post("/auth/logout")
    async def auth_logout(request: Request, response: Response,
                          user: UserRecord = Depends(current_user)) -> dict:
        """إبطال فوري للجلسة الحالية + مسح الكوكي."""
        token = request.cookies.get(auth.COOKIE_NAME)
        await auth.logout(db, token, ip=auth.client_ip(request), now=utcnow())
        auth.clear_session_cookie(response)
        return {"ok": True}

    @router.get("/auth/me")
    async def auth_me(user: UserRecord = Depends(current_user)) -> dict:
        """هويّة المستخدم الحالي (لبوّابة الواجهة) — 401 إن لم يكن مسجّلًا."""
        return {"username": user.username, "role": user.role.value}

    @router.get("/auth/events", dependencies=[manager])
    async def auth_events(limit: int = 50) -> list[dict]:
        """سجل الدخول/الخروج الأخير (manager فقط) — تدقيق أمني (§14.3 بند ٦)."""
        return await db.auth_events.list_recent(limit)

    # ── إدارة المستخدمين (manager فقط §14.3 بند ٥) — تعطيل لا حذف ──────────────
    @router.get("/users", dependencies=[manager])
    async def list_users() -> list[dict]:
        return [_user_out(u) for u in await db.users.list_all()]

    @router.post("/users", dependencies=[manager], status_code=status.HTTP_201_CREATED)
    async def create_user(body: UserIn) -> dict:
        """إنشاء مستخدم جديد. 409 إن كان الاسم مستخدَمًا (لا يدهس موجودًا)."""
        rec = UserRecord(username=body.username, password_hash=auth.hash_password(body.password),
                         role=body.role, active=True, created_at=utcnow())
        if not await db.users.create(rec):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"اسم المستخدم مستخدَم: {body.username}")
        log.info("إنشاء مستخدم لوحة: %s (role=%s)", rec.username, rec.role.value)
        return _user_out(rec)

    @router.post("/users/{username}/disable")
    async def disable_user(username: str,
                           actor: UserRecord = Depends(require_manager)) -> dict:
        """تعطيل مستخدم (بلا حذف §13) + إبطال فوري لكل جلساته. لا تعطيل للذات/آخر مدير."""
        if username == actor.username:
            raise HTTPException(status_code=400, detail="لا يمكنك تعطيل حسابك الحالي")
        target = await db.users.get(username)
        if target is None:
            raise HTTPException(status_code=404, detail=f"مستخدم غير موجود: {username}")
        if target.role is Role.MANAGER and target.active and await _active_managers() <= 1:
            raise HTTPException(status_code=400, detail="لا يمكن تعطيل آخر مدير نشط")
        await db.users.set_active(username, False)
        revoked = await db.sessions.delete_for_user(username)
        log.info("تعطيل مستخدم لوحة: %s (أُبطلت %d جلسة)", username, revoked)
        return {"username": username, "active": False}

    @router.post("/users/{username}/enable", dependencies=[manager])
    async def enable_user(username: str) -> dict:
        """إعادة تفعيل مستخدم معطّل."""
        if not await db.users.set_active(username, True):
            raise HTTPException(status_code=404, detail=f"مستخدم غير موجود: {username}")
        log.info("تفعيل مستخدم لوحة: %s", username)
        return {"username": username, "active": True}

    @router.post("/users/{username}/password")
    async def change_password(username: str, body: PasswordIn,
                              actor: UserRecord = Depends(require_manager)) -> dict:
        """تغيير كلمة مرور مستخدم + إبطال جلساته (إلزام دخول جديد)."""
        if not await db.users.update_password(username, auth.hash_password(body.password)):
            raise HTTPException(status_code=404, detail=f"مستخدم غير موجود: {username}")
        revoked = await db.sessions.delete_for_user(username)
        log.info("تغيير كلمة مرور مستخدم لوحة: %s (أُبطلت %d جلسة)", username, revoked)
        return {"username": username, "password_changed": True}

    @router.post("/users/{username}/role")
    async def change_role(username: str, body: RoleIn,
                          actor: UserRecord = Depends(require_manager)) -> dict:
        """تغيير دور مستخدم. يُقرأ الدور حيًّا في كل طلب فيَسري فورًا. لا تنزيل لآخر مدير."""
        target = await db.users.get(username)
        if target is None:
            raise HTTPException(status_code=404, detail=f"مستخدم غير موجود: {username}")
        if (target.role is Role.MANAGER and body.role is not Role.MANAGER
                and await _active_managers() <= 1):
            raise HTTPException(status_code=400, detail="لا يمكن تنزيل دور آخر مدير نشط")
        await db.users.set_role(username, body.role)
        log.info("تغيير دور مستخدم لوحة: %s → %s", username, body.role.value)
        return {"username": username, "role": body.role.value}

    # ── تحكّم البوت — Kill Switch (§13) ──────────────────────────────────────
    @router.get("/control", dependencies=[reader])
    async def get_control() -> dict:
        """حالة التخزين الحالية. الافتراضي عند أول تشغيل: stopped (storage_enabled=False)."""
        ctrl = await db.control.get()
        return ctrl.model_dump(mode="json")

    @router.post("/control/toggle", dependencies=[manager])
    async def toggle_control() -> dict:
        """تبديل التخزين: تفعيل = يخزّن تلقائيًا؛ إيقاف = يتوقف عند «تخزين» (§13)."""
        ctrl = await db.control.get()
        ctrl.storage_enabled = not ctrl.storage_enabled
        ctrl.state = "running" if ctrl.storage_enabled else "stopped"
        await db.control.set(ctrl, updated_by="dashboard")
        log.info("Kill Switch: storage_enabled=%s state=%s", ctrl.storage_enabled, ctrl.state)
        return ctrl.model_dump(mode="json")

    @router.post("/control/auto_trust", dependencies=[manager])
    async def toggle_auto_trust() -> dict:
        """تبديل «وضع التلقائي»: تفعيل = يتخطّى مطابقة الغرف → بوابة الثقة مباشرة (§8.1)."""
        ctrl = await db.control.get()
        ctrl.auto_trust = not ctrl.auto_trust
        await db.control.set(ctrl, updated_by="dashboard")
        log.info("وضع التلقائي: auto_trust=%s", ctrl.auto_trust)
        return ctrl.model_dump(mode="json")

    # ── إدارة الخزائن (§13) — إيقاف بلا حذف، تفعيل فوري ───────────────────────
    @router.get("/treasuries", dependencies=[reader])
    async def list_treasuries() -> list[dict]:
        """كل الخزائن (نشطة وموقوفة) — الموقوفة تظهر لإعادة تفعيلها (لا حذف §13)."""
        return [_clean(d) async for d in db.treasuries.col.find({})]

    @router.post("/treasuries", dependencies=[manager], status_code=status.HTTP_201_CREATED)
    async def add_treasury(body: TreasuryIn) -> dict:
        """إضافة/تعديل خزينة (upsert على الاسم). تُفعَّل فورًا في MongoDB."""
        rec = TreasuryRecord(**body.model_dump())
        await db.treasuries.upsert(rec)
        log.info("خزينة محدّثة: %s (code=%s type=%s)", rec.name, rec.code, rec.type)
        return rec.model_dump(mode="json")

    @router.put("/treasuries/{name}", dependencies=[manager])
    @router.post("/treasuries/{name}", dependencies=[manager])
    async def edit_treasury(name: str, body: TreasuryIn) -> dict:
        """تعديل خزينة موجودة بالاسم."""
        data = body.model_dump()
        data["name"] = name  # الاسم من المسار هو المفتاح
        rec = TreasuryRecord(**data)
        await db.treasuries.upsert(rec)
        log.info("تعديل خزينة: %s", name)
        return rec.model_dump(mode="json")

    @router.post("/treasuries/{name}/disable", dependencies=[manager])
    async def disable_treasury(name: str) -> dict:
        """إيقاف خزينة (active=false) — بلا حذف (§13)."""
        res = await db.treasuries.col.update_one({"name": name}, {"$set": {"active": False}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"خزينة غير موجودة: {name}")
        log.info("إيقاف خزينة (بلا حذف): %s", name)
        return {"name": name, "active": False}

    @router.post("/treasuries/{name}/enable", dependencies=[manager])
    async def enable_treasury(name: str) -> dict:
        """تفعيل خزينة موقوفة (active=true) — عكس الإيقاف (§13)."""
        res = await db.treasuries.col.update_one({"name": name}, {"$set": {"active": True}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"خزينة غير موجودة: {name}")
        log.info("تفعيل خزينة: %s", name)
        return {"name": name, "active": True}

    # ── إدارة الموردين (§5.4 §13) — مثل الخزائن ───────────────────────────────
    @router.get("/suppliers", dependencies=[reader])
    async def list_suppliers() -> list[dict]:
        return [_clean(d) async for d in db.suppliers.col.find({})]

    @router.post("/suppliers", dependencies=[manager], status_code=status.HTTP_201_CREATED)
    async def add_supplier(body: SupplierIn) -> dict:
        """إضافة/تعديل مورد (اسم + إملاءات بديلة + كود MONEYADO)."""
        rec = SupplierRecord(**body.model_dump())
        await db.suppliers.upsert(rec)
        log.info("مورد محدّث: %s (code=%s)", rec.name, rec.code)
        return rec.model_dump(mode="json")

    @router.put("/suppliers/{name}", dependencies=[manager])
    @router.post("/suppliers/{name}", dependencies=[manager])
    async def edit_supplier(name: str, body: SupplierIn) -> dict:
        data = body.model_dump()
        data["name"] = name
        rec = SupplierRecord(**data)
        await db.suppliers.upsert(rec)
        log.info("تعديل مورد: %s", name)
        return rec.model_dump(mode="json")

    @router.post("/suppliers/{name}/disable", dependencies=[manager])
    async def disable_supplier(name: str) -> dict:
        """إيقاف مورد (active=false) — بلا حذف (§13)."""
        res = await db.suppliers.col.update_one({"name": name}, {"$set": {"active": False}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"مورد غير موجود: {name}")
        log.info("إيقاف مورد (بلا حذف): %s", name)
        return {"name": name, "active": False}

    # ── الكلمات المجهولة (§4.5 §5.4) — خزائن/موردون تعذّر حلّهم → إسناد يدويّ كـ alias ─────
    @router.get("/unknown-terms", dependencies=[reader])
    async def list_unknown_terms() -> list[dict]:
        """آخر ٢٠ كلمة (خزينة/مورد) تعذّر حلّها — للمراجعة والإسناد اليدويّ (بلا تخمين §0)."""
        return await db.unknown_terms.list_recent(20)

    @router.post("/unknown-terms/{term}/assign", dependencies=[manager])
    async def assign_unknown_term(term: str, body: UnknownTermAssignIn) -> dict:
        """يُسنِد كلمة مجهولة كـ alias للخزينة/المورد ذي `target_code`، ثم يحذفها من المجهولات."""
        repo = db.treasuries if body.type == "treasury" else db.suppliers
        doc = await repo.col.find_one({"code": body.target_code})
        if doc is None:
            raise HTTPException(
                status_code=404, detail=f"لا {body.type} بالكود {body.target_code}",
            )
        aliases = list(doc.get("aliases") or [])
        if term not in aliases:
            aliases.append(term)                     # يُطابَق لاحقًا عبر تطبيع resolve (§4.5)
        await repo.col.update_one({"code": body.target_code}, {"$set": {"aliases": aliases}})
        removed = await db.unknown_terms.remove(term, body.type)
        log.info("إسناد كلمة مجهولة «%s» كـ alias لـ %s كود %s (حُذف من المجهولات=%d)",
                 term, body.type, body.target_code, removed)
        return {"term": term, "type": body.type, "target_code": body.target_code,
                "name": doc.get("name"), "aliases": aliases}

    # ── إدارة الموظفين المعتمدين (§8.3 §13) — «تم» تُقبل من هؤلاء فقط ─────────
    @router.get("/employees", dependencies=[reader])
    async def list_employees() -> list[dict]:
        return [_clean(d) async for d in db.employees.col.find({})]

    @router.post("/employees", dependencies=[manager], status_code=status.HTTP_201_CREATED)
    async def add_employee(body: EmployeeIn) -> dict:
        """إضافة موظف معتمد (رقم واتساب + اسم)."""
        rec = EmployeeRecord(**body.model_dump())
        await db.employees.upsert(rec)
        log.info("موظف معتمد محدّث: %s (%s)", rec.name, rec.whatsapp_number)
        return rec.model_dump(mode="json")

    @router.post("/employees/{number}/disable", dependencies=[manager])
    async def disable_employee(number: str) -> dict:
        """إيقاف موظف (active=false) — يُبطل قبول «تم» منه."""
        res = await db.employees.col.update_one(
            {"whatsapp_number": number}, {"$set": {"active": False}}
        )
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"موظف غير موجود: {number}")
        log.info("إيقاف موظف: %s", number)
        return {"whatsapp_number": number, "active": False}

    @router.delete("/employees/{number}", dependencies=[manager])
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
    @router.get("/rooms", dependencies=[reader])
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

    @router.post("/rooms/{jid}/classify", dependencies=[manager])
    @router.put("/rooms/{jid}/classify", dependencies=[manager])
    async def classify_room(jid: str, body: RoomClassifyIn) -> dict:
        """تصنيف غرفة (زبون/خزينة/تجاهل/…). يُفعَّل فورًا (hot-reload) — بلا إعادة تشغيل."""
        existing = await db.rooms.get(jid)
        name = existing.name if existing else None
        rec = Room(jid=jid, name=name, type=body.type, active=body.active,
                   discovered_at=existing.discovered_at if existing else None)
        await db.rooms.upsert(rec, updated_by="dashboard")
        log.info("تصنيف غرفة: %s → %s (active=%s)", jid, body.type.value, body.active)
        return rec.model_dump(mode="json")

    @router.patch("/rooms/{jid}", dependencies=[manager])
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

    @router.post("/rooms/{jid}/disable", dependencies=[manager])
    async def disable_room(jid: str) -> dict:
        """إيقاف غرفة (active=false) — بلا حذف (§13). يوقف التقاطها/مطابقتها."""
        res = await db.rooms.col.update_one({"jid": jid}, {"$set": {"active": False}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"غرفة غير موجودة: {jid}")
        log.info("إيقاف غرفة (بلا حذف): %s", jid)
        return {"jid": jid, "active": False}

    # ── لوحة V2 (م١): سجل الحوالات + قائمة الانتباه — قراءة فقط لدورة الحياة ────
    # 🔴 لا كتابة على deals/pipeline. «صعّد» يُنتج لطابور outgoing فقط (لا مسار Node مباشر).
    @router.get("/transfers", dependencies=[reader])
    async def list_transfers(q: str = "", limit: int = 50) -> list[dict]:
        """أرشيف الحوالات — بحث فوريّ بالمرجع/الهاتف/الاسم (أو الأحدث)."""
        return await transfers.search_transfers(db, q, limit)

    @router.get("/transfers/{deal_id}", dependencies=[reader])
    async def transfer_timeline(deal_id: str) -> dict:
        """الخط الزمني الكامل لحوالة (خام ← مُستخرَج ← تعديلات ← دفتر ← إلغاء)."""
        tl = await transfers.get_timeline(db, deal_id)
        if tl is None:
            raise HTTPException(status_code=404, detail=f"حوالة غير موجودة: {deal_id}")
        return tl

    @router.get("/attention", dependencies=[reader])
    async def attention_list() -> list[dict]:
        """قائمة الانتباه — حالات محتاجة تدخّلًا + إشارات كشف الاحتيال (م٢)، مرتّبة."""
        cfg = await db.detection.get()
        return await transfers.attention_items(db, utcnow(), cfg)

    # ── إعداد كشف الاحتيال (م٢) — عرض للقارئ، تعديل للمدير فقط ─────────────────
    @router.get("/settings/detection", dependencies=[reader])
    async def get_detection() -> dict:
        """حدود قواعد التصعيد الحالية (قابلة للتعديل من المدير)."""
        return (await db.detection.get()).model_dump(mode="json")

    @router.put("/settings/detection", dependencies=[manager])
    async def put_detection(body: DetectionConfig) -> dict:
        """تحديث حدود قواعد التصعيد (المدير يرسل الإعداد كاملًا). لا يمسّ منطق الكشف نفسه."""
        await db.detection.set(body)
        log.info("تحديث حدود كشف الاحتيال (م٢) عبر اللوحة")
        return body.model_dump(mode="json")

    @router.post("/attention/{deal_id}/escalate")
    async def escalate_transfer(deal_id: str,
                                actor: UserRecord = Depends(require_read)) -> dict:
        """«صعّد»: يُدرِج تنبيه المالك في طابور outgoing (is_alert) — نفس مسار ⚠️/🔴.

        لا مسار HTTP للداشبورد إلى خدمة Node؛ الداشبورد يُنتج للطابور فقط (عزلة §2.2 سليمة).
        """
        d = await db.deals.col.find_one({"deal_id": deal_id})
        if not d:
            raise HTTPException(status_code=404, detail=f"حوالة غير موجودة: {deal_id}")
        admin_jid = (settings.admin_room_jid or "").strip()
        if not admin_jid:
            raise HTTPException(status_code=503, detail="لا غرفة مسؤول مُهيّأة (admin_room_jid)")
        d.pop("_id", None)
        text = transfers.build_escalation_text(transfers._deal_summary(d), actor.username)
        await db.outgoing.enqueue(OutgoingMessage(chat_jid=admin_jid, text=text, is_alert=True))
        log.info("تصعيد يدويّ من اللوحة: حوالة %s بواسطة %s → طابور outgoing (مسؤول)",
                 deal_id, actor.username)
        return {"deal_id": deal_id, "escalated": True, "by": actor.username}

    @router.post("/attention/{deal_id}/review")
    async def review_transfer(deal_id: str, body: ReviewIn,
                              actor: UserRecord = Depends(require_read)) -> dict:
        """«علّم كمراجَع»: تعليق مرئيّ للجميع باسم المُراجِع ووقته (لا يغيّر حالة الحوالة).

        أوّل مُراجِع يُثبَّت (منع ازدواج المراجعة + محاسبة فردية). يُرجع السجل الفعليّ.
        """
        if not await db.deals.col.find_one({"deal_id": deal_id}, {"_id": 1}):
            raise HTTPException(status_code=404, detail=f"حوالة غير موجودة: {deal_id}")
        rec = await db.reviews.mark(deal_id=deal_id, reviewed_by=actor.username,
                                    note=body.note, now=utcnow())
        return {"deal_id": deal_id, "reviewed_by": rec.reviewed_by,
                "reviewed_at": rec.reviewed_at.isoformat(), "note": rec.note}

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

    @app.get("/login")
    async def login_page() -> FileResponse:
        """صفحة تسجيل الدخول (§14.3) — تُقدَّم بلا مصادقة؛ البوّابة تتمّ في الواجهة/الـ API."""
        return FileResponse(str(STATIC_DIR / "login.html"))

    @app.get("/rooms")
    async def rooms_page() -> FileResponse:
        """صفحة إدارة الغرف البسيطة (§2.2) — تصنيف بالأسماء فقط، بلا إدخال JID يدوي."""
        return FileResponse(str(STATIC_DIR / "rooms.html"))

    @app.get("/attention")
    async def attention_page() -> FileResponse:
        """قائمة الانتباه (لوحة V2 م١) — البوّابة في الواجهة/الـ API."""
        return FileResponse(str(STATIC_DIR / "attention.html"))

    @app.get("/transfers")
    async def transfers_page() -> FileResponse:
        """سجل الحوالات (لوحة V2 م١)."""
        return FileResponse(str(STATIC_DIR / "transfers.html"))

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
