"""
اختبارات إحصاءات لوحة المدير (لوحة V2 م٤) — قراءة فقط.

يتحقّق من حساب اليوم/الأسبوع/الاتجاه/الانتباه، ونسبة النجاح = مكتملة ÷ (مكتملة + فشل تقنيّ)،
ومجاميع المبالغ، وRBAC على /stats.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from core.constants import Role, Status
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


async def _seed(db, deal_id, status, created_at, *, amount=1000, currency="EGP"):
    await db.deals.col.insert_one({
        "deal_id": deal_id, "status": status.value, "created_at": created_at,
        "updated_at": created_at, "first_received_at": created_at, "is_two_legged": False,
        "sell_leg": {"operation": "sell", "amount": amount, "currency": currency,
                     "phone": "01", "reference_number": deal_id}, "amendments": []})


async def test_compute_stats_today_week_success(db):
    from dashboard import stats
    now = utcnow()
    # اليوم: ٣ مكتملة + ١ فشل تقنيّ + ١ معلّقة (انتباه)
    for i in range(3):
        await _seed(db, f"c{i}", Status.COMPLETED, now, amount=1000)
    await _seed(db, "tech", Status.TECH_FAILED, now, amount=1000)
    await _seed(db, "held", Status.HELD, now, amount=1000)
    # قبل ٣ أيام: مكتملة (ضمن الأسبوع لا اليوم)
    await _seed(db, "old", Status.COMPLETED, now - timedelta(days=3), amount=500)

    s = await stats.compute_stats(db, now)
    assert s["today"]["total"] == 5
    assert s["today"]["completed"] == 3
    assert s["today"]["attention"] == 2                 # HELD + TECH_FAILED (كلاهما ضمن الانتباه)
    assert s["today"]["amount"]["EGP"] == 5000          # ٥ حوالات اليوم × 1000
    assert s["week"]["completed"] == 4                  # ٣ اليوم + ١ قبل ٣ أيام
    assert s["week"]["tech_failed"] == 1
    assert s["week"]["success_rate"] == 0.8             # 4 / (4 + 1)
    assert s["attention_now"] == 2                       # held + tech (old مكتملة)
    assert len(s["trend"]) == 7
    assert s["trend"][-1]["total"] == 5 and s["trend"][-1]["completed"] == 3


async def test_success_rate_none_when_no_terminal(db):
    from dashboard import stats
    now = utcnow()
    await _seed(db, "h1", Status.HELD, now)              # لا مكتملة ولا فشل تقنيّ
    s = await stats.compute_stats(db, now)
    assert s["week"]["success_rate"] is None


async def test_stats_endpoint_rbac(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/stats")).status_code == 401
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.get("/api/stats")
        assert r.status_code == 200 and "today" in r.json() and "trend" in r.json()
    async with _client(db, Role.DATA_ENTRY, username="de") as ac:
        assert (await ac.get("/api/stats")).status_code == 403
