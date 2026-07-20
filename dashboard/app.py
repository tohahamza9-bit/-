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

import asyncio
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from core.config import Settings, get_settings
from core.constants import Currency, EntityType, Role, RoomType, TreasuryType
from core.db import Database, utcnow
from core.logging_setup import get_logger
from core.models import (
    BotControl,
    DetectionConfig,
    EmployeeRecord,
    EntityAlias,
    FxRatesConfig,
    OutgoingMessage,
    PaymentChannelRecord,
    Room,
    SupplierRecord,
    TreasuryRecord,
    UserRecord,
)

from . import auth, queue_admin, stats, transfers

log = get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
# صفحات HTML: no-cache (يُعاد التحقّق عبر ETag دائمًا) — كي يرى المدير تعديلات الواجهة فور النشر
#   بلا Ctrl+F5 (م: بطاقة «حلّ الخزينة من القروب» لم تظهر بسبب نسخة المتصفّح المخبّأة).
_HTML_NOCACHE = {"Cache-Control": "no-cache"}

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


class ChannelIn(BaseModel):
    """قناة دفع مُدارة (لوحة V2 م٣) — اسم + إملاءات بديلة + كود MONEYADO."""
    name: str = Field(..., min_length=1)
    code: Optional[str] = None
    aliases: list[str] = Field(default_factory=list)
    active: bool = True


class EmployeeIn(BaseModel):
    whatsapp_number: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    active: bool = True


class UnknownTermAssignIn(BaseModel):
    """إسناد كلمة مجهولة كـ alias لخزينة/مورد محدَّد بالكود (§4.5 §5.4)."""
    type: str = Field(..., pattern="^(treasury|supplier)$")
    target_code: str = Field(..., min_length=1)


class AliasPushIn(BaseModel):
    """إملاء بديل واحد يُلحَق (push) لمكدّس aliases الخزينة."""
    alias: str = Field(..., min_length=1)


class CancellationModeIn(BaseModel):
    """وضع قرار الإلغاء/التعديل على COMPLETED (§10)."""
    mode: str = Field(..., pattern="^(immediate|sql_required|manual)$")


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


class RoomsImportIn(BaseModel):
    """استيراد غرف بلصق قائمة JIDs — كلٌّ يُسجَّل «غير مصنّفة» ينتظر التصنيف (§2.2)."""
    jids: list[str] = Field(default_factory=list)


class FxTestParseIn(BaseModel):
    """اختبار محلّل الأسعار (زرّ «اختبار» — تحليل تجريبيّ بلا تخزين). FX_RATES_SPEC §12."""
    text: str = Field(..., min_length=1)
    currency: str = Field(..., pattern="^(EGP|TND)$")
    template: Optional[str] = None                # غياب ⇒ قالب الإعداد المحفوظ (أو fallback إن فرغ)


class EntityAliasIn(BaseModel):
    """كيان موحّد (FX_RATES_SPEC §4) — إضافة/تعديل (upsert على alias+entity_type)."""
    alias: str = Field(..., min_length=1)
    entity_type: EntityType
    canonical_name: str = Field(..., min_length=1)
    confidence: str = Field("high", pattern="^(high|medium|low)$")
    active: bool = True


class EntityAliasKeyIn(BaseModel):
    """مفتاح كيان (alias, entity_type) — للإيقاف/التفعيل."""
    alias: str = Field(..., min_length=1)
    entity_type: EntityType


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

    async def require_manager_audited(request: Request,
                                      user: UserRecord = Depends(require_manager)) -> UserRecord:
        """حارس المدير + **سجل تقنيّ (م٦)**: يدوّن كل طلب غير-GET يمرّ عبره (من/ماذا/متى).

        يلتقط كل تحوّلات الإعدادات تلقائيًا (كلها محروسة بالمدير) — الأثر الدائم المكمّل للتنبيه
        اللحظيّ. best-effort: لا يوقف العملية إن فشل التسجيل (T5).
        """
        if request.method != "GET":
            try:
                await db.auth_events.log(
                    username=user.username, event="setting_change",
                    ip=auth.client_ip(request), now=utcnow(),
                    detail=f"{request.method} {request.url.path}")
            except Exception as exc:
                log.warning("تعذّر تسجيل تدقيق الإعداد (متابعة): %s", exc)
        return user

    manager = Depends(require_manager_audited)
    reader = Depends(require_read)

    async def _active_managers() -> int:
        """عدد المديرين النشطين — لمنع تعطيل/تنزيل آخر مدير (تفادي القفل الكامل)."""
        return await db.users.col.count_documents({"role": Role.MANAGER.value, "active": True})

    # ── أدوات م٣: حارس تكرار الكود + تنبيه المالك (best-effort عبر طابور outgoing) ──
    async def _check_code_conflict(repo, code: Optional[str], name: str) -> None:
        """يمنع (409) كودًا مستخدَمًا على سجلّ **مختلف الاسم** — لا يمسّ التكرار القائم."""
        if not code:
            return
        other = await repo.col.find_one({"code": code, "name": {"$ne": name}})
        if other is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"الكود {code} مستخدَم مسبقًا لـ«{other.get('name')}»")

    async def _notify_owner(text: str) -> None:
        """best-effort: يُدرِج تنبيهًا في طابور outgoing (is_alert، وجهة المسؤول). لا يوقف الحفظ.

        نفس مسار ⚠️/🔴 (لا HTTP مباشر لخدمة Node). يُتخطّى بلا فشل إن لم تُهيّأ غرفة المسؤول.
        """
        admin = (settings.admin_room_jid or "").strip()
        if not admin:
            log.info("تنبيه المالك متخطّى: admin_room_jid غير مُهيّأة")
            return
        try:
            await db.outgoing.enqueue(OutgoingMessage(chat_jid=admin, text=text, is_alert=True))
        except Exception as exc:  # best-effort — T5: يُسجَّل ولا يوقف حفظ التغيير
            log.warning("تعذّر إدراج تنبيه المالك (متابعة): %s", exc)

    def _owner_text(entity: str, action: str, name: str, code: Optional[str] = None) -> str:
        c = f" (كود {code})" if code else ""
        return f"🔧 {action} {entity}: «{name}»{c} — من لوحة التحكّم"

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
                           actor: UserRecord = Depends(require_manager_audited)) -> dict:
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
                              actor: UserRecord = Depends(require_manager_audited)) -> dict:
        """تغيير كلمة مرور مستخدم + إبطال جلساته (إلزام دخول جديد)."""
        if not await db.users.update_password(username, auth.hash_password(body.password)):
            raise HTTPException(status_code=404, detail=f"مستخدم غير موجود: {username}")
        revoked = await db.sessions.delete_for_user(username)
        log.info("تغيير كلمة مرور مستخدم لوحة: %s (أُبطلت %d جلسة)", username, revoked)
        return {"username": username, "password_changed": True}

    @router.post("/users/{username}/role")
    async def change_role(username: str, body: RoleIn,
                          actor: UserRecord = Depends(require_manager_audited)) -> dict:
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

    @router.post("/control/cancellation_mode", dependencies=[manager])
    async def set_cancellation_mode(body: CancellationModeIn) -> dict:
        """وضع قرار الإلغاء/التعديل على COMPLETED (§10): immediate/sql_required/manual.
        SQL يبقى للتدقيق الدوري بلا تأثير على القرار الفوريّ إلا في وضع sql_required."""
        ctrl = await db.control.get()
        ctrl.cancellation_mode = body.mode
        await db.control.set(ctrl, updated_by="dashboard")
        log.info("وضع الإلغاء: cancellation_mode=%s", ctrl.cancellation_mode)
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
        await _check_code_conflict(db.treasuries, rec.code, rec.name)   # م٣: منع كود مكرّر
        await db.treasuries.upsert(rec)
        log.info("خزينة محدّثة: %s (code=%s type=%s)", rec.name, rec.code, rec.type)
        await _notify_owner(_owner_text("خزينة", "إضافة/تعديل", rec.name, rec.code))
        return rec.model_dump(mode="json")

    @router.put("/treasuries/{name}", dependencies=[manager])
    @router.post("/treasuries/{name}", dependencies=[manager])
    async def edit_treasury(name: str, body: TreasuryIn) -> dict:
        """تعديل خزينة موجودة بالاسم."""
        data = body.model_dump()
        data["name"] = name  # الاسم من المسار هو المفتاح
        rec = TreasuryRecord(**data)
        await _check_code_conflict(db.treasuries, rec.code, rec.name)   # م٣: منع كود مكرّر
        await db.treasuries.upsert(rec)
        log.info("تعديل خزينة: %s", name)
        await _notify_owner(_owner_text("خزينة", "تعديل", rec.name, rec.code))
        return rec.model_dump(mode="json")

    @router.post("/treasuries/{name}/aliases", dependencies=[manager])
    async def push_treasury_alias(name: str, body: AliasPushIn) -> dict:
        """يُلحِق إملاءً بديلاً واحدًا (push) لمكدّس aliases الخزينة — بلا تكرار، حيّ فورًا (§4.5).

        بديلٌ سريع للتعديل inline الكامل: يُضيف إملاءً واحدًا دون إعادة إرسال كل الحقول."""
        alias = body.alias.strip()
        if not alias:
            raise HTTPException(status_code=400, detail="إملاء فارغ")
        if not await db.treasuries.add_alias(name, alias):
            raise HTTPException(status_code=404, detail=f"خزينة غير موجودة: {name}")
        doc = await db.treasuries.col.find_one({"name": name})
        log.info("push إملاء «%s» للخزينة %s", alias, name)
        await _notify_owner(_owner_text("خزينة", f"إضافة إملاء «{alias}»", name))
        return {"name": name, "aliases": list(doc.get("aliases") or [])}

    @router.post("/treasuries/{name}/disable", dependencies=[manager])
    async def disable_treasury(name: str) -> dict:
        """إيقاف خزينة (active=false) — بلا حذف (§13)."""
        res = await db.treasuries.col.update_one({"name": name}, {"$set": {"active": False}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"خزينة غير موجودة: {name}")
        log.info("إيقاف خزينة (بلا حذف): %s", name)
        await _notify_owner(_owner_text("خزينة", "إيقاف", name))
        return {"name": name, "active": False}

    @router.post("/treasuries/{name}/enable", dependencies=[manager])
    async def enable_treasury(name: str) -> dict:
        """تفعيل خزينة موقوفة (active=true) — عكس الإيقاف (§13)."""
        res = await db.treasuries.col.update_one({"name": name}, {"$set": {"active": True}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"خزينة غير موجودة: {name}")
        log.info("تفعيل خزينة: %s", name)
        await _notify_owner(_owner_text("خزينة", "تفعيل", name))
        return {"name": name, "active": True}

    # ── إدارة الموردين (§5.4 §13) — مثل الخزائن ───────────────────────────────
    @router.get("/suppliers", dependencies=[reader])
    async def list_suppliers() -> list[dict]:
        return [_clean(d) async for d in db.suppliers.col.find({})]

    @router.post("/suppliers", dependencies=[manager], status_code=status.HTTP_201_CREATED)
    async def add_supplier(body: SupplierIn) -> dict:
        """إضافة/تعديل مورد (اسم + إملاءات بديلة + كود MONEYADO)."""
        rec = SupplierRecord(**body.model_dump())
        await _check_code_conflict(db.suppliers, rec.code, rec.name)    # م٣: منع كود مكرّر
        await db.suppliers.upsert(rec)
        log.info("مورد محدّث: %s (code=%s)", rec.name, rec.code)
        await _notify_owner(_owner_text("مورد", "إضافة/تعديل", rec.name, rec.code))
        return rec.model_dump(mode="json")

    @router.put("/suppliers/{name}", dependencies=[manager])
    @router.post("/suppliers/{name}", dependencies=[manager])
    async def edit_supplier(name: str, body: SupplierIn) -> dict:
        data = body.model_dump()
        data["name"] = name
        rec = SupplierRecord(**data)
        await _check_code_conflict(db.suppliers, rec.code, rec.name)    # م٣: منع كود مكرّر
        await db.suppliers.upsert(rec)
        log.info("تعديل مورد: %s", name)
        await _notify_owner(_owner_text("مورد", "تعديل", rec.name, rec.code))
        return rec.model_dump(mode="json")

    @router.post("/suppliers/{name}/disable", dependencies=[manager])
    async def disable_supplier(name: str) -> dict:
        """إيقاف مورد (active=false) — بلا حذف (§13)."""
        res = await db.suppliers.col.update_one({"name": name}, {"$set": {"active": False}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"مورد غير موجود: {name}")
        log.info("إيقاف مورد (بلا حذف): %s", name)
        await _notify_owner(_owner_text("مورد", "إيقاف", name))
        return {"name": name, "active": False}

    @router.post("/suppliers/{name}/enable", dependencies=[manager])
    async def enable_supplier(name: str) -> dict:
        """تفعيل مورد موقوف (active=true) — تناظر مع الخزائن (§13)."""
        res = await db.suppliers.col.update_one({"name": name}, {"$set": {"active": True}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"مورد غير موجود: {name}")
        log.info("تفعيل مورد: %s", name)
        await _notify_owner(_owner_text("مورد", "تفعيل", name))
        return {"name": name, "active": True}

    @router.post("/suppliers/{name}/aliases", dependencies=[manager])
    async def push_supplier_alias(name: str, body: AliasPushIn) -> dict:
        """يُلحِق إملاءً بديلاً واحدًا (push) لمكدّس aliases المورد — بلا تكرار، حيّ فورًا (§5.4).

        تعميم زر «＋» من الخزائن (025d3ef): بديلٌ سريع للتعديل inline الكامل."""
        alias = body.alias.strip()
        if not alias:
            raise HTTPException(status_code=400, detail="إملاء فارغ")
        if not await db.suppliers.add_alias(name, alias):
            raise HTTPException(status_code=404, detail=f"مورد غير موجود: {name}")
        doc = await db.suppliers.col.find_one({"name": name})
        log.info("push إملاء «%s» للمورد %s", alias, name)
        await _notify_owner(_owner_text("مورد", f"إضافة إملاء «{alias}»", name))
        return {"name": name, "aliases": list(doc.get("aliases") or [])}

    # ── إدارة قنوات الدفع (لوحة V2 م٣) — إدارة قائمة فقط؛ لا ربط بالكتابة بعد ──────
    @router.get("/payment-channels", dependencies=[reader])
    async def list_channels() -> list[dict]:
        return [_clean(d) async for d in db.payment_channels.col.find({})]

    @router.post("/payment-channels", dependencies=[manager], status_code=status.HTTP_201_CREATED)
    async def add_channel(body: ChannelIn) -> dict:
        """إضافة/تعديل قناة دفع (upsert على الاسم) + إملاءات بديلة لتصحيح الإملاء."""
        rec = PaymentChannelRecord(**body.model_dump())
        await _check_code_conflict(db.payment_channels, rec.code, rec.name)
        await db.payment_channels.upsert(rec)
        log.info("قناة دفع محدّثة: %s (code=%s)", rec.name, rec.code)
        await _notify_owner(_owner_text("قناة دفع", "إضافة/تعديل", rec.name, rec.code))
        return rec.model_dump(mode="json")

    @router.put("/payment-channels/{name}", dependencies=[manager])
    @router.post("/payment-channels/{name}", dependencies=[manager])
    async def edit_channel(name: str, body: ChannelIn) -> dict:
        """تعديل قناة دفع موجودة بالاسم (اسم/كود/إملاءات) — نفس نمط الخزائن/الموردين."""
        data = body.model_dump()
        data["name"] = name                              # الاسم من المسار هو المفتاح
        rec = PaymentChannelRecord(**data)
        await _check_code_conflict(db.payment_channels, rec.code, rec.name)   # م٣: منع كود مكرّر
        await db.payment_channels.upsert(rec)
        log.info("تعديل قناة دفع: %s", name)
        await _notify_owner(_owner_text("قناة دفع", "تعديل", rec.name, rec.code))
        return rec.model_dump(mode="json")

    @router.post("/payment-channels/{name}/disable", dependencies=[manager])
    async def disable_channel(name: str) -> dict:
        res = await db.payment_channels.col.update_one({"name": name}, {"$set": {"active": False}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"قناة غير موجودة: {name}")
        log.info("إيقاف قناة دفع (بلا حذف): %s", name)
        await _notify_owner(_owner_text("قناة دفع", "إيقاف", name))
        return {"name": name, "active": False}

    @router.post("/payment-channels/{name}/enable", dependencies=[manager])
    async def enable_channel(name: str) -> dict:
        res = await db.payment_channels.col.update_one({"name": name}, {"$set": {"active": True}})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail=f"قناة غير موجودة: {name}")
        log.info("تفعيل قناة دفع: %s", name)
        await _notify_owner(_owner_text("قناة دفع", "تفعيل", name))
        return {"name": name, "active": True}

    @router.post("/payment-channels/{name}/aliases", dependencies=[manager])
    async def push_channel_alias(name: str, body: AliasPushIn) -> dict:
        """يُلحِق إملاءً بديلاً واحدًا (push) لمكدّس aliases القناة — بلا تكرار (تعميم زر «＋» م٣)."""
        alias = body.alias.strip()
        if not alias:
            raise HTTPException(status_code=400, detail="إملاء فارغ")
        if not await db.payment_channels.add_alias(name, alias):
            raise HTTPException(status_code=404, detail=f"قناة غير موجودة: {name}")
        doc = await db.payment_channels.col.find_one({"name": name})
        log.info("push إملاء «%s» للقناة %s", alias, name)
        await _notify_owner(_owner_text("قناة دفع", f"إضافة إملاء «{alias}»", name))
        return {"name": name, "aliases": list(doc.get("aliases") or [])}

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
        await _notify_owner(_owner_text("موظف", "إضافة/تعديل", rec.name, rec.whatsapp_number))
        return rec.model_dump(mode="json")

    @router.put("/employees/{number}", dependencies=[manager])
    @router.post("/employees/{number}", dependencies=[manager])
    async def edit_employee(number: str, body: EmployeeIn) -> dict:
        """تعديل **اسم** موظف معتمد فقط — الرقم مفتاح ثابت هنا (تغيير الرقم = حذف ثم إضافة)."""
        target = await db.employees.col.find_one({"whatsapp_number": number})
        if target is None:
            raise HTTPException(status_code=404, detail=f"موظف غير موجود: {number}")
        rec = EmployeeRecord(whatsapp_number=number, name=body.name,
                             active=bool(target.get("active", True)))   # نُبقي حالة التفعيل
        await db.employees.upsert(rec)
        log.info("تعديل اسم موظف معتمد: %s (%s)", rec.name, number)
        await _notify_owner(_owner_text("موظف", "تعديل", rec.name, number))
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

    # ── استيراد الغرف (بديل كتابة JID يدويًّا) — تُسجَّل «غير مصنّفة» تنتظر التصنيف ──────
    # 🔴 discover = metadata فقط (setOnInsert): لا يمسّ تصنيفًا/اسمًا موجودًا، لا نصّ رسالة،
    #    ولا حدود الكتابة (المركزية/المسؤول من env). صفر مساس بالمطابقة/الالتقاط.
    @router.post("/rooms/import", dependencies=[manager])
    async def import_rooms(body: RoomsImportIn) -> dict:
        """استيراد غرف بلصق قائمة JIDs — كلٌّ يُسجَّل «غير مصنّفة» (discover) إن لم يوجد سابقًا.

        لا يمسّ تصنيف/اسم غرفة موجودة (setOnInsert). يُرجِع عدّاد المُضاف/الموجود."""
        existing = set(await db.rooms.col.distinct("jid"))
        seen: set[str] = set()
        added = 0
        for raw in body.jids:
            jid = (raw or "").strip()
            if not jid or jid in seen:
                continue
            seen.add(jid)
            if jid not in existing:
                await db.rooms.discover(jid)
                added += 1
        log.info("استيراد غرف (لصق JIDs): مُضاف=%d، موجود=%d", added, len(seen) - added)
        return {"added": added, "existing": len(seen) - added, "total": len(seen)}

    @router.post("/rooms/import-from-messages", dependencies=[manager])
    async def import_rooms_from_messages() -> dict:
        """استيراد كل الغرف التي وصلت منها رسالة (chat_jid المميّزة في raw_messages) — تُسجَّل
        «غير مصنّفة» إن لم توجد (discover). قراءة raw_messages فقط؛ لا يمسّ الالتقاط/المعالجة."""
        existing = set(await db.rooms.col.distinct("jid"))
        jids = await db.raw.distinct_chat_jids()
        added = 0
        for jid in jids:
            if jid and jid not in existing:
                await db.rooms.discover(jid)
                added += 1
        log.info("استيراد الغرف من الرسائل: فُحص=%d، مُضاف=%d", len(jids), added)
        return {"added": added, "scanned": len(jids), "total_rooms": len(existing) + added}

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

    @router.get("/stats", dependencies=[reader])
    async def get_stats() -> dict:
        """إحصاءات صحة اللوحة (م٤) — حجم اليوم/الأسبوع + اتجاه ٧ أيام + عدّاد الانتباه. قراءة فقط."""
        return await stats.compute_stats(db, utcnow())

    # ── الاتصال + صحة MONEYADO (م٥) — قراءة فقط ───────────────────────────────
    # توسيط قراءة حالة/QR من خدمة Node (127.0.0.1 + X-Internal-Token). معلومة واردة فقط؛
    # 🔴 لا يُرسل الداشبورد أي رسالة عبر Node (عزلة الإرسال تبقى: التنبيهات عبر طابور outgoing).
    async def _bridge_get(path: str) -> dict:
        import httpx
        url = f"{settings.whatsapp_bridge_url.rstrip('/')}{path}"
        headers = {"X-Internal-Token": settings.internal_token} if settings.internal_token else {}
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(url, headers=headers)
            r.raise_for_status()
            return r.json()

    @router.get("/connection", dependencies=[reader])
    async def connection_status() -> dict:
        """حالة اتصال واتساب (من خدمة Node). تعامل رشيق عند تعذّر الوصول (لا 500)."""
        try:
            data = await _bridge_get("/status")
            return {"available": True, **data}
        except Exception as exc:  # T5 — Node متوقّفة/غير مهيّأة → متاح=false بلا كسر
            log.info("تعذّر قراءة حالة اتصال Node: %s", exc)
            return {"available": False, "connected": False,
                    "detail": "تعذّر الاتصال بخدمة واتساب"}

    @router.get("/connection/qr", dependencies=[manager])
    async def connection_qr() -> dict:
        """سلسلة QR للربط (المدير فقط — حسّاسة: مسحها يربط جلسة واتساب)."""
        try:
            data = await _bridge_get("/qr")
            return {"available": True, "qr": data.get("qr"),
                    "connected": data.get("connected", False)}
        except Exception as exc:  # T5
            log.info("تعذّر قراءة QR من Node: %s", exc)
            return {"available": False, "qr": None, "detail": "تعذّر الاتصال بخدمة واتساب"}

    @router.get("/moneyado/health", dependencies=[reader])
    async def moneyado_health_endpoint() -> dict:
        """صحة نافذة MONEYADO (قراءة فقط) — يميّز غير مشغّل/غير مرئي/مرئي (§11.3)."""
        from core.writers.moneyado.health import moneyado_health
        return moneyado_health()

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

    # ══ الميزة ١: تشغيل النظام (pm2) — عرض للقارئ، تنفيذ للمدير فقط ══════════════
    @router.get("/system/processes", dependencies=[reader])
    async def system_processes() -> dict:
        """حالة حيّة لمكوّني البوت (الكيرنل/الجسر) من pm2: online/stopped + عدّاد ↺.

        pm2 غير متاح → available=False بقائمة فارغة (لا 500) — نمط /connection نفسه.
        """
        from core import process_control as pc
        procs = await asyncio.to_thread(pc.list_processes)
        return {"available": bool(procs), "processes": procs}

    @router.get("/system/bridge-control", dependencies=[manager])
    async def system_bridge_control() -> dict:
        """بيانات نداء الجسر لتشغيل الكيرنل وهو **مطفأ** — للمدير فقط.

        🔴 لماذا يُسلَّم التوكن للمتصفّح: الكيرنل هو مَن يخدم هذه اللوحة، فإن مات لم يبقَ
           مَن يوسّط الطلب. الصفحة تحتفظ بالتوكن من لحظة تحميلها (والكيرنل حيّ) فيظلّ زرّ
           «تشغيل الكيرنل» عاملًا بعد موته — يناديه المتصفّح مباشرةً على الجسر.
           الحدّ: التوكن يصل **المدير المُصادَق وحده**، والجسر يستمع على 127.0.0.1 فقط،
           والنقطة تقبل فعلًا واحدًا مغلقًا (start-kernel) بوسائط ثابتة في الكود.
        """
        # إعدادات **التطبيق** لا العامّة: create_app قد يُحقَن بإعدادات مختلفة.
        return {
            "url": f"{settings.whatsapp_bridge_url.rstrip('/')}/pm2",
            "token": settings.bridge_control_token,
            "enabled": bool(settings.bridge_control_token),
        }

    @router.post("/system/processes/{target}/{action}", dependencies=[manager])
    async def system_process_action(target: str, action: str,
                                    request: Request,
                                    actor: UserRecord = Depends(require_manager_audited)) -> dict:
        """ينفّذ pm2 على مكوّن — **قائمة مغلقة**: target ∈ {moneyado-kernel, moneyado-wa, all}
        و action ∈ {start, stop, restart}. أي قيمة أخرى → 400 قبل أي تنفيذ (لا shell injection).

        الأوامر التي تمسّ الكيرنل تُجدوَل منفصلةً مؤجَّلة (SELF_KILL_DELAY) فيعود هذا الردّ أولًا
        ثم يموت الخادم — وإلا لانقطع الاتصال قبل أن يعرف المتصفّح النتيجة.
        """
        from core import process_control as pc
        if action not in pc.ACTIONS or target not in pc.TARGETS:
            raise HTTPException(status_code=400, detail="أمر غير مسموح")
        # سجلّ تقنيّ صريح (أدقّ من سطر require_manager_audited العامّ: مَن/ماذا/على أيّ مكوّن)
        try:
            await db.auth_events.log(
                username=actor.username, event="process_control",
                ip=auth.client_ip(request), now=utcnow(),
                detail=f"pm2 {action} {target}")
        except Exception as exc:                                  # T5 — لا يوقف العملية
            log.warning("تعذّر تسجيل تدقيق التحكّم بالعمليات (متابعة): %s", exc)
        result = await asyncio.to_thread(pc.run_action, action, target)
        log.info("تحكّم تشغيل: %s طلب «pm2 %s %s» → %s", actor.username, action, target, result)
        return {"target": target, "action": action, **result}

    # ══ الميزة ٢: إدارة الطابور — عرض للقارئ، استبعاد/إرجاع للمدير فقط ═══════════
    @router.get("/queue", dependencies=[reader])
    async def queue_list() -> dict:
        """المنتظرات (parsed/ready/matched/sell_done) + المستبعدات يدويًّا — للعرض والفرز."""
        return await queue_admin.list_queue(db)

    @router.post("/queue/exclude", dependencies=[manager])
    async def queue_exclude(body: queue_admin.QueueExcludeIn, request: Request,
                            actor: UserRecord = Depends(require_manager_audited)) -> dict:
        """استبعاد من التنزيل = manual_completed بسبب **إلزاميّ**. لا حذف من القاعدة إطلاقًا.

        الصفقة ذات قيود الدفتر تُوسَم needs_review (البوت كتب نصفها — أثر §11.4 لا يُمحى).
        """
        try:
            await db.auth_events.log(
                username=actor.username, event="queue_exclude", ip=auth.client_ip(request),
                now=utcnow(), detail=f"{len(body.deal_ids)} صفقة — {body.reason[:120]}")
        except Exception as exc:
            log.warning("تعذّر تسجيل تدقيق الاستبعاد (متابعة): %s", exc)
        return await queue_admin.exclude(db, body, actor.username)

    @router.post("/queue/restore", dependencies=[manager])
    async def queue_restore(body: queue_admin.QueueRestoreIn, request: Request,
                            actor: UserRecord = Depends(require_manager_audited)) -> dict:
        """إرجاع مستبعدة للطابور (ضغطة خاطئة) — **بشرط صفر قيود دفتر** لها (§9).

        ذات القيود تُرفَض بالاسم في `rejected` مع السبب: إرجاعها يعيد تنزيلها فيزدوج القيد.
        """
        try:
            await db.auth_events.log(
                username=actor.username, event="queue_restore", ip=auth.client_ip(request),
                now=utcnow(), detail=f"{len(body.deal_ids)} صفقة")
        except Exception as exc:
            log.warning("تعذّر تسجيل تدقيق الإرجاع (متابعة): %s", exc)
        return await queue_admin.restore(db, body, actor.username)

    # ── إعداد نظام الأسعار (FX_RATES_SPEC §12) — عرض للقارئ، تعديل للمدير فقط ─────
    @router.get("/settings/fx-rates", dependencies=[reader])
    async def get_fx_rates() -> dict:
        """إعداد الأسعار الحاليّ (قيم §12 القابلة للتعديل). fx_rates_enabled=False افتراضيًّا."""
        return (await db.fx_config.get()).model_dump(mode="json")

    @router.put("/settings/fx-rates", dependencies=[manager])
    async def put_fx_rates(body: FxRatesConfig) -> dict:
        """تحديث إعداد الأسعار (المدير يرسل الإعداد كاملًا). سباكة إعداد فقط — لا منطق تسعير بعد."""
        await db.fx_config.set(body)
        log.info("تحديث إعداد نظام الأسعار (FX §12) عبر اللوحة")
        return body.model_dump(mode="json")

    @router.post("/settings/fx-rates/test-parse", dependencies=[manager])
    async def test_parse_fx(body: FxTestParseIn) -> dict:
        """تحليل تجريبيّ لرسالة أسعار — **بلا تخزين**. يغذّي زرّ «اختبار» (واجهته في الخطوة ٥).

        يستخدم القالب المُرسَل إن وُجد، وإلا قالب الإعداد المحفوظ للعملة (أو fallback إن فرغ)."""
        from core.fx_rates import parse_price_message
        cfg = await db.fx_config.get()
        if body.template is not None:
            template = body.template
        else:
            template = cfg.egp_price_template if body.currency == "EGP" else cfg.tnd_price_template
        rates = parse_price_message(body.text, template, body.currency)
        return {"currency": body.currency, "rates": rates}

    # ── الكيانات الموحّدة (FX_RATES_SPEC §4) — عرض للقارئ، تعديل للمدير. مستقلّة عن إملاءات
    #    الخزائن/الموردين تمامًا (لا تلمسها). نمط الخزائن/الموردين + تنبيه المالك + audit.
    @router.get("/entity-aliases", dependencies=[reader])
    async def list_entity_aliases() -> list[dict]:
        return [_clean(d) async for d in db.entity_aliases.col.find({})]

    @router.post("/entity-aliases", dependencies=[manager], status_code=status.HTTP_201_CREATED)
    async def add_entity_alias(body: EntityAliasIn) -> dict:
        """إضافة/تعديل كيان (upsert على alias+entity_type). لا يمسّ إملاءات الخزائن/الموردين."""
        rec = EntityAlias(**body.model_dump())
        await db.entity_aliases.upsert(rec)
        log.info("كيان محدّث: «%s» (%s) → %s", rec.alias, rec.entity_type.value, rec.canonical_name)
        await _notify_owner(_owner_text("كيان", "إضافة/تعديل", rec.alias, rec.entity_type.value))
        return rec.model_dump(mode="json")

    @router.post("/entity-aliases/disable", dependencies=[manager])
    async def disable_entity_alias(body: EntityAliasKeyIn) -> dict:
        """إيقاف كيان (active=false) — بلا حذف (§13)."""
        if not await db.entity_aliases.set_active(body.alias, body.entity_type.value, False):
            raise HTTPException(status_code=404, detail=f"كيان غير موجود: {body.alias}")
        await _notify_owner(_owner_text("كيان", "إيقاف", body.alias, body.entity_type.value))
        return {"alias": body.alias, "entity_type": body.entity_type.value, "active": False}

    @router.post("/entity-aliases/enable", dependencies=[manager])
    async def enable_entity_alias(body: EntityAliasKeyIn) -> dict:
        """تفعيل كيان موقوف (active=true) — تناظر مع الخزائن (§13)."""
        if not await db.entity_aliases.set_active(body.alias, body.entity_type.value, True):
            raise HTTPException(status_code=404, detail=f"كيان غير موجود: {body.alias}")
        await _notify_owner(_owner_text("كيان", "تفعيل", body.alias, body.entity_type.value))
        return {"alias": body.alias, "entity_type": body.entity_type.value, "active": True}

    # ── تاريخ الأسعار (FX §15) — قراءة فقط لعرض اللقطات في اللوحة ──────────────
    @router.get("/fx-rates/history", dependencies=[reader])
    async def fx_rates_history(currency: Optional[str] = None, limit: int = 50) -> list[dict]:
        """أحدث لقطات الأسعار (الأحدث أولًا). تصفية اختيارية بالعملة (EGP/TND)."""
        cur = currency if currency in ("EGP", "TND") else None
        snaps = await db.fx_rates.history(cur, min(max(limit, 1), 200))
        return [s.model_dump(mode="json") for s in snaps]

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
        return FileResponse(str(STATIC_DIR / "index.html"), headers=_HTML_NOCACHE)

    @app.get("/login")
    async def login_page() -> FileResponse:
        """صفحة تسجيل الدخول (§14.3) — تُقدَّم بلا مصادقة؛ البوّابة تتمّ في الواجهة/الـ API."""
        return FileResponse(str(STATIC_DIR / "login.html"), headers=_HTML_NOCACHE)

    @app.get("/rooms")
    async def rooms_page() -> FileResponse:
        """صفحة إدارة الغرف البسيطة (§2.2) — تصنيف بالأسماء فقط، بلا إدخال JID يدوي."""
        return FileResponse(str(STATIC_DIR / "rooms.html"), headers=_HTML_NOCACHE)

    @app.get("/attention")
    async def attention_page() -> FileResponse:
        """قائمة الانتباه (لوحة V2 م١) — البوّابة في الواجهة/الـ API."""
        return FileResponse(str(STATIC_DIR / "attention.html"), headers=_HTML_NOCACHE)

    @app.get("/transfers")
    async def transfers_page() -> FileResponse:
        """سجل الحوالات (لوحة V2 م١)."""
        return FileResponse(str(STATIC_DIR / "transfers.html"), headers=_HTML_NOCACHE)

    @app.get("/queue")
    async def queue_page() -> FileResponse:
        """إدارة الطابور (الميزة ٢) — للمدير فقط (الحارس في الـAPI والواجهة)."""
        return FileResponse(str(STATIC_DIR / "queue.html"), headers=_HTML_NOCACHE)

    @app.get("/connect")
    async def connect_page() -> FileResponse:
        """صفحة الاتصال + QR + صحة MONEYADO (لوحة V2 م٥) — البوّابة في الواجهة/الـ API."""
        return FileResponse(str(STATIC_DIR / "connect.html"), headers=_HTML_NOCACHE)

    @app.get("/fx-rates")
    async def fx_rates_page() -> FileResponse:
        """صفحة إدارة الأسعار (FX §15) — المصادقة/الصلاحيات تُفرَض في الـAPI."""
        return FileResponse(str(STATIC_DIR / "fx_rates.html"), headers=_HTML_NOCACHE)

    @app.get("/entity-aliases")
    async def entity_aliases_page() -> FileResponse:
        """صفحة إدارة الكيانات الموحّدة (§4)."""
        return FileResponse(str(STATIC_DIR / "entity_aliases.html"), headers=_HTML_NOCACHE)

    @app.get("/settings")
    async def settings_page() -> FileResponse:
        """مركز الإعدادات (لوحة V2 م٦): مستخدمون + Kill Switch + موظفون + كشف + سجل تقنيّ."""
        return FileResponse(str(STATIC_DIR / "settings.html"), headers=_HTML_NOCACHE)

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
