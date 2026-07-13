"""
مصادقة لوحة التحكّم (§14.3 SEC-001) — طبقة معزولة تمامًا عن منطق البوت.

القرارات (محسومة مع صاحب العمل):
- **جلسة خادم** (رمز مبهم) في MongoDB، تُسلَّم عبر كوكي httpOnly + SameSite=Strict.
  الإبطال الفوري عند الخروج/التعطيل = حذف سجل الجلسة (لا يمكن مع JWT بلا قائمة حظر).
- الرمز يُخزَّن **هاشًا SHA-256** في DB؛ الكوكي وحده يحمل الرمز الخام (تسريب DB لا يكشف جلسة حيّة).
- كلمة المرور بـ **argon2id** (argon2-cffi) — لا نصّ صريح أبدًا.
- قفل تخمين: N محاولات فاشلة → قفل M دقائق (على سجل المستخدم).
- رسالة خطأ **عامة موحّدة** لمستخدم مجهول/كلمة خاطئة — لا كشف وجود المستخدم.

هذه الوحدة لا تلمس parser/pipeline/queue/writer — قوائم Mongo فقط (users/sessions/auth_events).
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, Request, Response, status

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from core.config import Settings
from core.constants import Role
from core.db import Database, utcnow
from core.logging_setup import get_logger
from core.models import SessionRecord, UserRecord

log = get_logger(__name__)

# اسم كوكي الجلسة (نفس الأصل، فلا نحتاج credentials عابرة للأصول).
COOKIE_NAME = "moneyado_session"

_hasher = PasswordHasher()
# هاش وهميّ ثابت لمعادلة زمن التحقّق عند غياب المستخدم (منع كشف الوجود عبر التوقيت §14.3).
_DUMMY_HASH = _hasher.hash("timing-equalizer-not-a-real-secret")


# ─────────────────────────────────────────────────────────────────────────────
# أدوات كلمة المرور والرموز
# ─────────────────────────────────────────────────────────────────────────────
def hash_password(password: str) -> str:
    """تجزئة argon2id لكلمة المرور (تتضمّن salt عشوائيًا داخليًا)."""
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """تحقّق ثابت النتيجة — أي خطأ في التحقّق يُعامَل كفشل (لا استثناء يتسرّب)."""
    try:
        _hasher.verify(password_hash, password)
        return True
    except VerifyMismatchError:
        return False
    except Exception:  # هاش تالف/غير صالح → غير مُتحقَّق (T5: يُرجَع فشلًا لا يُبتلع صمتًا)
        return False


def new_token() -> str:
    """رمز جلسة مبهم عالي الإنتروبيا (URL-safe)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 للرمز — المخزَّن في DB (الرمز الخام يبقى في الكوكي فقط)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _naive(dt: datetime) -> datetime:
    """توحيد UTC-naive للمقارنة (mongomock/motor قد يُرجعان أوقاتًا بلا منطقة)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def client_ip(request: Request) -> Optional[str]:
    """عنوان IP للعميل — يحترم X-Forwarded-For عند وجود وسيط (وإلا اتصال مباشر LAN)."""
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


# ─────────────────────────────────────────────────────────────────────────────
# استثناءات تسجيل الدخول — تُترجَم في نقطة النهاية (رسالة عامة/429)
# ─────────────────────────────────────────────────────────────────────────────
class InvalidCredentials(Exception):
    """مستخدم مجهول أو كلمة مرور خاطئة أو حساب معطّل — رسالة عامة موحّدة."""


class AccountLocked(Exception):
    """الحساب مقفول مؤقتًا بعد محاولات فاشلة متكرّرة."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("الحساب مقفول مؤقتًا")
        self.retry_after_seconds = retry_after_seconds


# ─────────────────────────────────────────────────────────────────────────────
# منطق الدخول/الخروج (يأخذ now للاختبار الحتميّ للقفل/الانتهاء)
# ─────────────────────────────────────────────────────────────────────────────
async def login(db: Database, settings: Settings, *, username: str, password: str,
                ip: Optional[str], now: datetime) -> tuple[str, UserRecord]:
    """يتحقّق من الاعتماد وينشئ جلسة. يُرجع (الرمز الخام، المستخدم). يرفع:
    - AccountLocked إن كان مقفولًا ضمن نافذة القفل.
    - InvalidCredentials لأي فشل آخر (مجهول/خاطئ/معطّل) — بلا كشف وجود.
    """
    username = (username or "").strip()
    user = await db.users.get(username)

    # قفل التخمين (لمستخدم معروف فقط) — يُفحَص قبل أي تحقّق.
    if user and user.locked_until and _naive(user.locked_until) > _naive(now):
        await db.auth_events.log(username=username, event="locked", ip=ip, now=now)
        remaining = int((_naive(user.locked_until) - _naive(now)).total_seconds())
        raise AccountLocked(max(remaining, 1))

    # معادلة الزمن: حتى مع غياب المستخدم نُجري تحقّقًا وهميًّا (منع كشف الوجود بالتوقيت).
    if user is None or not user.active:
        verify_password(_DUMMY_HASH, password)
        await db.auth_events.log(username=username, event="login_fail", ip=ip, now=now)
        raise InvalidCredentials()

    if not verify_password(user.password_hash, password):
        attempts = await db.users.bump_failed(username)
        await db.auth_events.log(username=username, event="login_fail", ip=ip, now=now)
        if attempts >= settings.login_max_attempts:
            until = now + timedelta(minutes=settings.login_lockout_minutes)
            await db.users.set_locked_until(username, until)
            await db.auth_events.log(username=username, event="locked", ip=ip, now=now)
            log.warning("قفل حساب اللوحة «%s» بعد %d محاولة فاشلة", username, attempts)
        raise InvalidCredentials()

    # نجاح — أنشئ جلسة وسجّل.
    token = new_token()
    ttl = timedelta(hours=settings.session_ttl_hours)
    sess = SessionRecord(
        token_hash=hash_token(token), username=user.username, role=user.role,
        created_at=now, expires_at=now + ttl, last_seen_at=now, ip=ip,
    )
    await db.sessions.create(sess)
    await db.users.touch_login(user.username, now)
    await db.auth_events.log(username=user.username, event="login_success", ip=ip, now=now)
    log.info("دخول لوحة ناجح: %s (role=%s)", user.username, user.role.value)
    return token, user


async def logout(db: Database, token: Optional[str], *, ip: Optional[str] = None,
                 now: Optional[datetime] = None) -> None:
    """إبطال فوري لجلسة الكوكي الحالية (حذف السجل)."""
    if not token:
        return
    sess = await db.sessions.get(hash_token(token))
    await db.sessions.delete(hash_token(token))
    if sess is not None:
        await db.auth_events.log(username=sess.username, event="logout", ip=ip,
                                 now=now or utcnow())


# ─────────────────────────────────────────────────────────────────────────────
# ملفّات تعريف الكوكي
# ─────────────────────────────────────────────────────────────────────────────
def set_session_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        key=COOKIE_NAME, value=token, httponly=True, samesite="strict",
        secure=settings.session_cookie_secure, path="/",
        max_age=settings.session_ttl_hours * 3600,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/", samesite="strict")


# ─────────────────────────────────────────────────────────────────────────────
# اعتماديات FastAPI — تحلّ محلّ require_internal_token (§14.3 نقطة الربط)
# تُبنى مرّة في get_router(db, settings) وتُستعمل عبر Depends على كل مسار.
# ─────────────────────────────────────────────────────────────────────────────
def make_current_user(db: Database, settings: Settings):
    """يُنشئ اعتمادية تُرجع UserRecord للجلسة الصالحة أو ترفع 401."""

    async def current_user(request: Request) -> UserRecord:
        token = request.cookies.get(COOKIE_NAME)
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="غير مسجّل الدخول")
        sess = await db.sessions.get(hash_token(token))
        now = utcnow()
        if sess is None or _naive(sess.expires_at) < _naive(now):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="الجلسة منتهية أو غير صالحة")
        user = await db.users.get(sess.username)
        if user is None or not user.active:
            # عُطِّل الحساب بعد الدخول → إبطال فوري لكل جلساته.
            await db.sessions.delete_for_user(sess.username)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="الحساب معطّل")
        return user

    return current_user


def make_require_roles(current_user_dep, *roles: Role):
    """يُنشئ اعتمادية تتطلّب دخولًا + دورًا ضمن `roles`، وإلا 403 (رفض فعلي بالـ backend)."""
    allowed = set(roles)

    async def require(user: UserRecord = Depends(current_user_dep)) -> UserRecord:
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="صلاحية غير كافية لهذا الإجراء")
        return user

    return require
