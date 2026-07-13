"""
اختبارات لوحة V2 م٣ — قنوات الدفع + حارس تكرار الكود + تنبيه المالك (best-effort).

إدارة قائمة فقط (لا ربط بالكتابة). يتحقّق من CRUD القنوات، منع الكود المكرّر (409)،
تنبيه المالك عبر طابور outgoing، وسلامته عند غياب غرفة المسؤول، وRBAC.
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


# ── CRUD قنوات الدفع ─────────────────────────────────────────────────────────
async def test_channel_crud(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        r = await ac.post("/api/payment-channels",
                          json={"name": "فودافون", "code": "17", "aliases": ["فودافوان"]})
        assert r.status_code == 201 and r.json()["code"] == "17"
        r = await ac.get("/api/payment-channels")
        assert any(c["name"] == "فودافون" and "فودافوان" in c["aliases"] for c in r.json())
        # إيقاف بلا حذف ثم تفعيل
        assert (await ac.post("/api/payment-channels/فودافون/disable")).json()["active"] is False
        assert await db.payment_channels.col.find_one({"name": "فودافون"}) is not None
        assert (await ac.post("/api/payment-channels/فودافون/enable")).json()["active"] is True


# ── حارس تكرار الكود (409) ───────────────────────────────────────────────────
async def test_duplicate_code_blocked_treasury(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:  # الكود 74 مبذور لـ«بلاس فون»
        r = await ac.post("/api/treasuries", json={"name": "خزينة أخرى", "code": "74"})
        assert r.status_code == 409
        # كود جديد فريد يمرّ
        assert (await ac.post("/api/treasuries", json={"name": "خزينة ألف", "code": "901"})).status_code == 201
        # تعديل نفس الخزينة بنفس كودها لا يُعدّ تعارضًا
        assert (await ac.post("/api/treasuries", json={"name": "خزينة ألف", "code": "901"})).status_code == 201


async def test_duplicate_code_blocked_channel(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        assert (await ac.post("/api/payment-channels", json={"name": "فودافون", "code": "17"})).status_code == 201
        r = await ac.post("/api/payment-channels", json={"name": "انستا", "code": "17"})
        assert r.status_code == 409


# ── تنبيه المالك (best-effort عبر طابور outgoing) ────────────────────────────
async def test_owner_alert_enqueued_on_change(db):
    pytest.importorskip("fastapi")
    s = _settings(admin_room_jid="admin@g.us")
    async with _client(db, settings=s) as ac:
        assert (await ac.post("/api/treasuries", json={"name": "خزينة جديدة", "code": "902"})).status_code == 201
    msgs = await db.outgoing.next_unsent(10)
    assert len(msgs) == 1
    assert msgs[0]["chat_jid"] == "admin@g.us" and msgs[0]["is_alert"] is True
    assert "خزينة" in msgs[0]["text"] and "خزينة جديدة" in msgs[0]["text"]


async def test_owner_alert_best_effort_no_admin(db):
    pytest.importorskip("fastapi")
    async with _client(db, settings=_settings()) as ac:  # لا admin_room_jid
        r = await ac.post("/api/payment-channels", json={"name": "قناة", "code": "50"})
        assert r.status_code == 201                       # الحفظ ينجح رغم غياب غرفة المسؤول
    assert await db.outgoing.col.count_documents({}) == 0  # لا تنبيه، لا فشل


# ── RBAC ─────────────────────────────────────────────────────────────────────
async def test_channels_rbac(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/payment-channels")).status_code == 401
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        assert (await ac.get("/api/payment-channels")).status_code == 200
        assert (await ac.post("/api/payment-channels", json={"name": "x"})).status_code == 403
    async with _client(db, Role.DATA_ENTRY, username="de") as ac:
        assert (await ac.get("/api/payment-channels")).status_code == 403


async def test_supplier_enable_added(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        await ac.post("/api/suppliers", json={"name": "مورد ب", "code": "1290"})
        assert (await ac.post("/api/suppliers/مورد ب/disable")).json()["active"] is False
        assert (await ac.post("/api/suppliers/مورد ب/enable")).json()["active"] is True
