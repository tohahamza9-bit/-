"""
اختبارات لوحة V2 م٥ — الاتصال + صحة MONEYADO (قراءة فقط).

يغطّي: تصنيف صحة MONEYADO النقيّ، سلوك النقاط عند تعذّر Node (رشيق، لا 500)،
وRBAC (QR/صفحة connect للمدير فقط؛ الحالة/الصحة للمدير+المراجع).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from core.constants import Role
from core.db import utcnow
from core.models import UserRecord


def _settings(**over):
    from core.config import Settings
    # منفذ جسر غير مستخدَم → توسيط Node يفشل بسرعة (نختبر التعامل الرشيق)
    over.setdefault("whatsapp_bridge_url", "http://127.0.0.1:59999")
    return Settings(_env_file=None, **over)


async def _seed_user(db, username, password, role):
    from dashboard.auth import hash_password
    await db.users.create(UserRecord(username=username, password_hash=hash_password(password),
                                     role=role, active=True, created_at=utcnow()))


@asynccontextmanager
async def _anon_client(db, settings=None):
    from httpx import ASGITransport, AsyncClient
    from dashboard.app import create_app
    app = create_app(db, settings or _settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@asynccontextmanager
async def _client(db, role=Role.MANAGER, *, username="admin", password="pw-123456", settings=None):
    await _seed_user(db, username, password, role)
    async with _anon_client(db, settings) as ac:
        r = await ac.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200
        yield ac


# ── تصنيف صحة MONEYADO (نقيّ، بلا نظام) ─────────────────────────────────────
def test_moneyado_classify_states():
    from core.writers.moneyado.health import _classify
    assert _classify([], None)["state"] == "not_running"
    assert _classify([111], None)["state"] == "unknown_visibility"      # لا pywinauto
    assert _classify([111], [])["state"] == "not_visible"               # حيّ لكن مخفي
    v = _classify([111, 222], [222])
    assert v["state"] == "visible" and v["pid"] == 222 and v["running"] is True


def test_moneyado_health_returns_state():
    from core.writers.moneyado.health import moneyado_health
    h = moneyado_health()
    assert h["state"] in {"not_running", "not_visible", "visible", "unknown_visibility", "unavailable"}


# ── نقاط المدير/المراجع + التعامل الرشيق عند تعذّر Node ─────────────────────
async def test_connection_status_graceful(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/connection")).status_code == 401     # لا دخول
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.get("/api/connection")                             # Node متوقّفة
        assert r.status_code == 200 and r.json()["available"] is False   # رشيق لا 500
    async with _client(db, Role.DATA_ENTRY, username="de") as ac:
        assert (await ac.get("/api/connection")).status_code == 403


async def test_moneyado_health_endpoint_rbac(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/moneyado/health")).status_code == 401
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.get("/api/moneyado/health")
        assert r.status_code == 200 and "state" in r.json()
    async with _client(db, Role.DATA_ENTRY, username="de") as ac:
        assert (await ac.get("/api/moneyado/health")).status_code == 403


async def test_qr_manager_only(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/connection/qr")).status_code == 401
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        assert (await ac.get("/api/connection/qr")).status_code == 403   # QR حسّاس → المدير فقط
    async with _client(db, Role.MANAGER) as ac:
        r = await ac.get("/api/connection/qr")
        assert r.status_code == 200 and r.json()["available"] is False    # Node متوقّفة → رشيق


async def test_connect_page_served(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        r = await ac.get("/connect")
        assert r.status_code == 200 and "الاتصال" in r.text
