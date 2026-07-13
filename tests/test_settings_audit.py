"""
اختبارات لوحة V2 م٦ — السجل التقنيّ (تدقيق تغييرات الإعدادات) + مركز الإعدادات.

يتحقّق أن كل طلب غير-GET يمرّ بحارس المدير يُسجَّل (من/ماذا/متى)، وأن قراءات المدير (GET)
لا تُسجَّل، وأن السجل يظهر في /auth/events، وأن صفحة /settings تُقدَّم.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from core.constants import Role
from core.db import utcnow
from core.models import UserRecord


def _settings(**over):
    from core.config import Settings
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


def _changes(events):
    return [e for e in events if e["event"] == "setting_change"]


async def test_mutations_are_audited(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        await ac.post("/api/control/toggle")
        await ac.post("/api/treasuries", json={"name": "خ ت", "code": "930"})
        events = (await ac.get("/api/auth/events?limit=100")).json()
    ch = _changes(events)
    details = [e["detail"] for e in ch]
    assert any(d == "POST /api/control/toggle" for d in details)
    assert any(d == "POST /api/treasuries" for d in details)
    assert all(e["username"] == "admin" for e in ch)          # «من» صحيح


async def test_manager_reads_not_audited(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        await ac.get("/api/users")             # GET محروس بالمدير — لا يُسجَّل
        await ac.get("/api/treasuries")
        events = (await ac.get("/api/auth/events?limit=100")).json()
    # لا setting_change من قراءات GET
    assert not any(e["detail"] and e["detail"].startswith("GET ") for e in _changes(events))


async def test_detection_change_audited(db):
    pytest.importorskip("fastapi")
    from core.models import DetectionConfig
    async with _client(db) as ac:
        await ac.put("/api/settings/detection", json=DetectionConfig().model_dump(mode="json"))
        events = (await ac.get("/api/auth/events?limit=100")).json()
    assert any(e["detail"] == "PUT /api/settings/detection" for e in _changes(events))


async def test_user_mgmt_audited(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        await ac.post("/api/users", json={"username": "sara", "password": "pw-123456", "role": "reviewer"})
        await ac.post("/api/users/sara/disable")
        events = (await ac.get("/api/auth/events?limit=100")).json()
    details = [e["detail"] for e in _changes(events)]
    assert any(d == "POST /api/users" for d in details)
    assert any(d == "POST /api/users/sara/disable" for d in details)


async def test_login_event_has_no_detail(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        events = (await ac.get("/api/auth/events?limit=50")).json()
    login = [e for e in events if e["event"] == "login_success"]
    assert login and login[0]["detail"] is None


async def test_settings_page_served(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        r = await ac.get("/settings")
        assert r.status_code == 200 and "مركز الإعدادات" in r.text
