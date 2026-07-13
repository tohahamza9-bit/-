"""
اختبارات مصادقة لوحة التحكّم (§14.3 SEC-001) — طبقة معزولة عن منطق البوت.

يغطّي: تجزئة argon2، دخول ناجح/فاشل، عدم كشف وجود المستخدم، قفل التخمين، انتهاء الجلسة،
الخروج (إبطال فوري)، **رفض RBAC فعلي في الـ backend** لكل دور، وإدارة المستخدمين + التدقيق.

دوال المصادقة تأخذ `now` فتُختبَر أزمنة القفل/الانتهاء بالحقن بلا انتظار حقيقي.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from core.constants import Role


# ─────────────────────────────────────────────────────────────────────────────
# مساعدات
# ─────────────────────────────────────────────────────────────────────────────
def _settings(**over):
    from core.config import Settings
    return Settings(_env_file=None, **over)   # تجاهُل .env — إعدادات حتمية


async def _seed_user(db, username, password, role):
    from core.db import utcnow
    from core.models import UserRecord
    from dashboard.auth import hash_password
    await db.users.create(UserRecord(
        username=username, password_hash=hash_password(password),
        role=role, active=True, created_at=utcnow()))


@asynccontextmanager
async def _anon_client(db, settings=None):
    from httpx import ASGITransport, AsyncClient
    from dashboard.app import create_app
    app = create_app(db, settings or _settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@asynccontextmanager
async def _login_client(db, username, password, role, settings=None):
    """يزرع مستخدمًا ويسجّل دخوله؛ يُنتِج عميلًا يحمل كوكي الجلسة."""
    await _seed_user(db, username, password, role)
    async with _anon_client(db, settings) as ac:
        r = await ac.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200
        yield ac


# ─────────────────────────────────────────────────────────────────────────────
# 1) أدوات كلمة المرور/الرمز (بلا db/fastapi)
# ─────────────────────────────────────────────────────────────────────────────
def test_password_hash_roundtrip():
    from dashboard.auth import hash_password, verify_password
    h = hash_password("s3cret-pw")
    assert h != "s3cret-pw"                       # لا نصّ صريح
    assert verify_password(h, "s3cret-pw") is True
    assert verify_password(h, "wrong") is False
    assert verify_password("not-a-hash", "x") is False   # هاش تالف → فشل بلا استثناء


def test_token_hash_deterministic_and_unique():
    from dashboard.auth import hash_token, new_token
    t = new_token()
    assert hash_token(t) == hash_token(t)         # حتميّ
    assert new_token() != new_token()             # كلّ رمز فريد
    assert len(hash_token(t)) == 64               # sha256 hex


# ─────────────────────────────────────────────────────────────────────────────
# 2) منطق الدخول المباشر (حقن now) — قفل/انتهاء بلا انتظار
# ─────────────────────────────────────────────────────────────────────────────
async def test_login_creates_session_and_audits(db):
    from core.db import utcnow
    from dashboard import auth
    await _seed_user(db, "admin", "pw-123456", Role.MANAGER)
    now = utcnow()
    token, user = await auth.login(db, _settings(), username="admin", password="pw-123456",
                                   ip="1.2.3.4", now=now)
    assert user.username == "admin"
    sess = await db.sessions.get(auth.hash_token(token))
    assert sess is not None and sess.username == "admin" and sess.role is Role.MANAGER
    stored = await db.users.get("admin")
    assert stored.last_login_at is not None and stored.failed_attempts == 0
    assert any(e["event"] == "login_success" for e in await db.auth_events.list_recent(10))


async def test_login_invalid_and_unknown_are_indistinguishable(db):
    """كلمة خاطئة ومستخدم مجهول: كلاهما InvalidCredentials (لا كشف وجود §14.3)."""
    from core.db import utcnow
    from dashboard import auth
    await _seed_user(db, "admin", "pw-123456", Role.MANAGER)
    with pytest.raises(auth.InvalidCredentials):
        await auth.login(db, _settings(), username="admin", password="nope", ip=None, now=utcnow())
    with pytest.raises(auth.InvalidCredentials):
        await auth.login(db, _settings(), username="ghost", password="whatever", ip=None, now=utcnow())
    # لا جلسة أُنشئت في الحالتين
    assert await db.sessions.col.count_documents({}) == 0


async def test_disabled_user_cannot_login(db):
    from core.db import utcnow
    from dashboard import auth
    await _seed_user(db, "sara", "pw-123456", Role.REVIEWER)
    await db.users.set_active("sara", False)
    with pytest.raises(auth.InvalidCredentials):   # معطّل → رسالة عامة (لا كشف)
        await auth.login(db, _settings(), username="sara", password="pw-123456", ip=None, now=utcnow())


async def test_lockout_after_max_attempts_then_unlocks(db):
    from core.db import utcnow
    from dashboard import auth
    s = _settings(login_max_attempts=3, login_lockout_minutes=5)
    await _seed_user(db, "admin", "pw-123456", Role.MANAGER)
    now = utcnow()
    for _ in range(3):                              # ٣ محاولات فاشلة → قفل
        with pytest.raises(auth.InvalidCredentials):
            await auth.login(db, s, username="admin", password="wrong", ip=None, now=now)
    # مقفول الآن — حتى بكلمة صحيحة → AccountLocked
    with pytest.raises(auth.AccountLocked):
        await auth.login(db, s, username="admin", password="pw-123456", ip=None, now=now)
    # بعد انقضاء نافذة القفل → يُسمح
    later = now + timedelta(minutes=6)
    token, user = await auth.login(db, s, username="admin", password="pw-123456", ip=None, now=later)
    assert token and user.username == "admin"
    assert any(e["event"] == "locked" for e in await db.auth_events.list_recent(20))


# ─────────────────────────────────────────────────────────────────────────────
# 3) تدفّق HTTP (fastapi) — كوكي/انتهاء/خروج
# ─────────────────────────────────────────────────────────────────────────────
async def test_login_http_sets_cookie_and_me(db):
    pytest.importorskip("fastapi")
    await _seed_user(db, "admin", "pw-123456", Role.MANAGER)
    async with _anon_client(db) as ac:
        r = await ac.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
        assert r.status_code == 401                            # خطأ عام
        r = await ac.post("/api/auth/login", json={"username": "admin", "password": "pw-123456"})
        assert r.status_code == 200 and r.json()["role"] == "manager"
        assert "moneyado_session" in r.headers.get("set-cookie", "")
        r = await ac.get("/api/auth/me")
        assert r.status_code == 200 and r.json()["username"] == "admin"


async def test_me_requires_login(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/auth/me")).status_code == 401


async def test_login_http_lockout_returns_429(db):
    pytest.importorskip("fastapi")
    s = _settings(login_max_attempts=3, login_lockout_minutes=5)
    await _seed_user(db, "admin", "pw-123456", Role.MANAGER)
    async with _anon_client(db, s) as ac:
        for _ in range(3):
            r = await ac.post("/api/auth/login", json={"username": "admin", "password": "x"})
            assert r.status_code == 401
        r = await ac.post("/api/auth/login", json={"username": "admin", "password": "pw-123456"})
        assert r.status_code == 429
        assert "retry-after" in {k.lower() for k in r.headers}


async def test_expired_session_rejected(db):
    pytest.importorskip("fastapi")
    from core.db import utcnow
    async with _login_client(db, "admin", "pw-123456", Role.MANAGER) as ac:
        assert (await ac.get("/api/auth/me")).status_code == 200
        # أنهِ الجلسة يدويًّا (تُفحَص في الكود لا اعتمادًا على TTL)
        await db.sessions.col.update_many({}, {"$set": {"expires_at": utcnow() - timedelta(hours=1)}})
        assert (await ac.get("/api/auth/me")).status_code == 401


async def test_logout_revokes_session(db):
    pytest.importorskip("fastapi")
    async with _login_client(db, "admin", "pw-123456", Role.MANAGER) as ac:
        assert (await ac.get("/api/auth/me")).status_code == 200
        r = await ac.post("/api/auth/logout")
        assert r.status_code == 200
        assert (await ac.get("/api/auth/me")).status_code == 401       # إبطال فوري
    assert await db.sessions.col.count_documents({}) == 0


# ─────────────────────────────────────────────────────────────────────────────
# 4) رفض RBAC فعلي في الـ backend (لا إخفاء واجهة) — استدعاء API مباشر
# ─────────────────────────────────────────────────────────────────────────────
async def test_rbac_backend_enforced_per_role(db):
    pytest.importorskip("fastapi")
    # بلا دخول: قراءة وكتابة مرفوضتان (401)
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/treasuries")).status_code == 401
        assert (await ac.post("/api/treasuries", json={"name": "خ", "code": "1"})).status_code == 401
    # مراجع: قراءة 200، كتابة 403
    async with _login_client(db, "rev", "pw-123456", Role.REVIEWER) as ac:
        assert (await ac.get("/api/treasuries")).status_code == 200
        assert (await ac.post("/api/treasuries", json={"name": "خ", "code": "1"})).status_code == 403
    # data_entry: بلا وصول للإعدادات (403 حتى للقراءة)
    async with _login_client(db, "de", "pw-123456", Role.DATA_ENTRY) as ac:
        assert (await ac.get("/api/treasuries")).status_code == 403
    # مدير: كلاهما مسموح
    async with _login_client(db, "admin", "pw-123456", Role.MANAGER) as ac:
        assert (await ac.get("/api/treasuries")).status_code == 200
        assert (await ac.post("/api/treasuries", json={"name": "خ", "code": "1"})).status_code == 201


# ─────────────────────────────────────────────────────────────────────────────
# 5) إدارة المستخدمين (manager فقط) + تدقيق
# ─────────────────────────────────────────────────────────────────────────────
async def test_user_management_manager_only_and_no_hash_leak(db):
    pytest.importorskip("fastapi")
    async with _login_client(db, "rev", "pw-123456", Role.REVIEWER) as ac:
        r = await ac.post("/api/users", json={"username": "x", "password": "pw-123456", "role": "reviewer"})
        assert r.status_code == 403                            # غير مدير → ممنوع
    async with _login_client(db, "admin", "pw-123456", Role.MANAGER) as ac:
        r = await ac.post("/api/users", json={"username": "sara", "password": "pw-123456", "role": "reviewer"})
        assert r.status_code == 201 and r.json()["role"] == "reviewer"
        assert "password_hash" not in r.json()                 # لا تسريب هاش
        r = await ac.post("/api/users", json={"username": "sara", "password": "pw-123456", "role": "reviewer"})
        assert r.status_code == 409                            # مكرّر
        r = await ac.get("/api/users")
        assert r.status_code == 200
        assert all("password_hash" not in u for u in r.json())


async def test_disable_user_revokes_and_blocks_relogin(db):
    pytest.importorskip("fastapi")
    await _seed_user(db, "sara", "pw-123456", Role.REVIEWER)
    async with _anon_client(db) as sara:
        assert (await sara.post("/api/auth/login",
                                json={"username": "sara", "password": "pw-123456"})).status_code == 200
        assert (await sara.get("/api/auth/me")).status_code == 200
        async with _login_client(db, "admin", "pw-123456", Role.MANAGER) as mgr:
            assert (await mgr.post("/api/users/sara/disable")).status_code == 200
        assert (await sara.get("/api/auth/me")).status_code == 401     # جلستها أُبطلت فورًا
    async with _anon_client(db) as sara2:                        # ولا تستطيع الدخول ثانية
        assert (await sara2.post("/api/auth/login",
                                 json={"username": "sara", "password": "pw-123456"})).status_code == 401


async def test_password_change_revokes_sessions(db):
    pytest.importorskip("fastapi")
    await _seed_user(db, "sara", "pw-123456", Role.REVIEWER)
    async with _anon_client(db) as sara:
        await sara.post("/api/auth/login", json={"username": "sara", "password": "pw-123456"})
        assert (await sara.get("/api/auth/me")).status_code == 200
        async with _login_client(db, "admin", "pw-123456", Role.MANAGER) as mgr:
            r = await mgr.post("/api/users/sara/password", json={"password": "new-pw-123456"})
            assert r.status_code == 200
        assert (await sara.get("/api/auth/me")).status_code == 401     # الجلسة القديمة أُبطلت


async def test_manager_cannot_disable_or_demote_self_last_manager(db):
    pytest.importorskip("fastapi")
    async with _login_client(db, "admin", "pw-123456", Role.MANAGER) as ac:
        assert (await ac.post("/api/users/admin/disable")).status_code == 400        # لا تعطيل للذات
        r = await ac.post("/api/users/admin/role", json={"role": "reviewer"})
        assert r.status_code == 400                                                  # لا تنزيل لآخر مدير


async def test_enable_reactivates_and_role_change_takes_effect(db):
    pytest.importorskip("fastapi")
    async with _login_client(db, "admin", "pw-123456", Role.MANAGER) as ac:
        await ac.post("/api/users", json={"username": "sara", "password": "pw-123456", "role": "data_entry"})
        # ترقية sara إلى reviewer
        assert (await ac.post("/api/users/sara/role", json={"role": "reviewer"})).status_code == 200
        assert (await db.users.get("sara")).role is Role.REVIEWER
        # تعطيل ثم تفعيل
        assert (await ac.post("/api/users/sara/disable")).status_code == 200
        assert (await db.users.get("sara")).active is False
        assert (await ac.post("/api/users/sara/enable")).status_code == 200
        assert (await db.users.get("sara")).active is True


async def test_auth_events_manager_only(db):
    pytest.importorskip("fastapi")
    async with _login_client(db, "admin", "pw-123456", Role.MANAGER) as ac:
        r = await ac.get("/api/auth/events")
        assert r.status_code == 200
        assert any(e["event"] == "login_success" for e in r.json())
    async with _login_client(db, "rev", "pw-123456", Role.REVIEWER) as ac:
        assert (await ac.get("/api/auth/events")).status_code == 403
