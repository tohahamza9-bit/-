"""
اختبارات كشف الاحتيال (لوحة V2 م٢) — القواعد الأربع + الإعداد + الاندماج + RBAC.

كلّه قراءة فقط: يتحقّق أن الكشف يُصدر إشارات صحيحة دون كتابة على deals/pipeline.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from core.constants import Role, Status
from core.db import utcnow
from core.models import DetectionConfig, UserRecord


# ── مساعدات ──────────────────────────────────────────────────────────────────
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


def _leg(**over):
    leg = {"operation": "sell", "amount": 1600, "amount_after_discount": 1584, "commission": -16,
           "currency": "EGP", "phone": "0100", "reference_number": "A1", "customer_name": "زبون",
           "treasury": {"code": "74", "name": "بلاس فون", "type": "sell_only", "currency": "EGP"}}
    leg.update(over)
    return leg


async def _seed(db, deal_id, *, status=Status.COMPLETED, created_at=None, **legover):
    now = created_at or utcnow()
    await db.deals.col.insert_one({
        "deal_id": deal_id, "status": status.value, "created_at": now, "updated_at": now,
        "first_received_at": now, "is_two_legged": False, "sell_leg": _leg(**legover),
        "source_message_keys": [], "amendments": []})


def _rules(sigs):
    return sorted({s["rule"] for s in sigs})


# ── 1) تجزئة الحوالات ────────────────────────────────────────────────────────
async def test_structuring_same_phone(db):
    from dashboard import detect
    cfg = DetectionConfig(structuring_count_threshold=3, structuring_sum_threshold=1e9)
    base = utcnow()
    for i in range(3):
        await _seed(db, f"s{i}", phone="0111", reference_number=f"R{i}",
                    created_at=base + timedelta(minutes=i))
    sigs = await detect.detect_signals(db, utcnow(), cfg)
    st = [s for s in sigs if s["rule"] == "structuring"]
    assert len(st) == 1 and st[0]["deal_id"] == "s2"           # يُعلَّم الأحدث
    assert set(st[0]["related_deal_ids"]) == {"s0", "s1"}


# ── 2) إعادة استخدام مرجع بهوية مختلفة ──────────────────────────────────────
async def test_ref_reuse_different_identity(db):
    from dashboard import detect
    cfg = DetectionConfig(structuring_sum_threshold=1e9, structuring_count_threshold=99)
    base = utcnow()
    await _seed(db, "r0", reference_number="A223", phone="0111", customer_name="أحمد",
                created_at=base)
    await _seed(db, "r1", reference_number="A223", phone="0999", customer_name="خالد",
                created_at=base + timedelta(minutes=5))
    sigs = await detect.detect_signals(db, utcnow(), cfg)
    rr = [s for s in sigs if s["rule"] == "ref_reuse"]
    assert len(rr) == 1 and rr[0]["deal_id"] == "r1"
    assert rr[0]["related_deal_ids"] == ["r0"]


async def test_ref_reuse_same_identity_not_flagged(db):
    from dashboard import detect
    cfg = DetectionConfig(structuring_sum_threshold=1e9, structuring_count_threshold=99)
    base = utcnow()
    await _seed(db, "q0", reference_number="A300", phone="0111", customer_name="أحمد", created_at=base)
    await _seed(db, "q1", reference_number="A300", phone="0111", customer_name="أحمد",
                created_at=base + timedelta(minutes=1))
    sigs = await detect.detect_signals(db, utcnow(), cfg)
    assert not any(s["rule"] == "ref_reuse" for s in sigs)      # نفس الهوية → لا إشارة


# ── 3) انحراف نسبة الخصم ─────────────────────────────────────────────────────
async def test_discount_deviation_outlier(db):
    from dashboard import detect
    cfg = DetectionConfig(discount_min_samples=3, discount_deviation_tolerance=0.3,
                          structuring_sum_threshold=1e9, structuring_count_threshold=99)
    base = utcnow()
    # أربع حوالات نسبتها ~0.01 + شاذّة 0.5 (نفس الخزينة 74)
    for i in range(4):
        await _seed(db, f"n{i}", amount=1000, amount_after_discount=990, phone=f"02{i}",
                    reference_number=f"D{i}", created_at=base + timedelta(minutes=i))
    await _seed(db, "outlier", amount=1000, amount_after_discount=500, phone="0299",
                reference_number="DX", created_at=base + timedelta(minutes=9))
    sigs = await detect.detect_signals(db, utcnow(), cfg)
    dd = [s for s in sigs if s["rule"] == "discount_deviation"]
    assert [s["deal_id"] for s in dd] == ["outlier"]


# ── 4) كيان جديد ─────────────────────────────────────────────────────────────
async def test_new_entity_first_appearance(db):
    from dashboard import detect
    cfg = DetectionConfig(new_entity_lookback_hours=48, scan_window_hours=48,
                          structuring_sum_threshold=1e9, structuring_count_threshold=99)
    base = utcnow()
    # خزينة 74 لها ظهور قديم (>48س) → ليست جديدة؛ خزينة 99 حديثة فقط → جديدة
    await _seed(db, "old74", phone="0333", reference_number="O1",
                created_at=base - timedelta(hours=100),
                treasury={"code": "74", "name": "بلاس", "type": "sell_only", "currency": "EGP"})
    await _seed(db, "rec74", phone="0334", reference_number="O2", created_at=base - timedelta(hours=1),
                treasury={"code": "74", "name": "بلاس", "type": "sell_only", "currency": "EGP"})
    await _seed(db, "rec99", phone="0335", reference_number="O3", created_at=base - timedelta(hours=1),
                treasury={"code": "99", "name": "خزينة جديدة", "type": "sell_only", "currency": "EGP"})
    sigs = await detect.detect_signals(db, utcnow(), cfg)
    ne = [s for s in sigs if s["rule"] == "new_entity"]
    assert [s["deal_id"] for s in ne] == ["rec99"]              # 74 قديمة، 99 جديدة


async def test_detection_disabled_returns_nothing(db):
    from dashboard import detect
    await _seed(db, "x0", phone="0111")
    await _seed(db, "x1", phone="0111")
    sigs = await detect.detect_signals(db, utcnow(), DetectionConfig(enabled=False))
    assert sigs == []


# ── 5) الاندماج في قائمة الانتباه + الترتيب ─────────────────────────────────
async def test_attention_merges_signals_status_first(db):
    from dashboard import transfers
    cfg = DetectionConfig(structuring_count_threshold=3, structuring_sum_threshold=1e9)
    base = utcnow()
    # حوالة معلّقة (HELD) بلا إشارة
    await _seed(db, "held1", status=Status.HELD, phone="0500", reference_number="H1", created_at=base)
    # ٣ حوالات مكتملة بنفس الهاتف → إشارة تجزئة على الأحدث (خارج الحالات النشطة)
    for i in range(3):
        await _seed(db, f"c{i}", status=Status.COMPLETED, phone="0111",
                    reference_number=f"C{i}", created_at=base + timedelta(minutes=i))
    items = await transfers.attention_items(db, utcnow(), cfg)
    ids = [it["deal_id"] for it in items]
    assert ids[0] == "held1"                                   # الحالة أوّلًا (HELD رتبة 0)
    flagged = next(it for it in items if it["deal_id"] == "c2")
    assert flagged["urgency_rank"] == 4                        # مُشار إليها خارج الحالات النشطة
    assert any(s["rule"] == "structuring" for s in flagged["signals"])
    assert ids.index("held1") < ids.index("c2")


# ── 6) HTTP: إعداد الحدود + RBAC + ظهور الإشارات ─────────────────────────────
async def test_detection_settings_rbac_and_persist(db):
    pytest.importorskip("fastapi")
    # قراءة متاحة للمراجع، تعديل للمدير فقط
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        assert (await ac.get("/api/settings/detection")).status_code == 200
        assert (await ac.put("/api/settings/detection",
                             json=DetectionConfig().model_dump(mode="json"))).status_code == 403
    async with _client(db, Role.MANAGER, username="mgr") as ac:
        cfg = DetectionConfig(structuring_count_threshold=3).model_dump(mode="json")
        r = await ac.put("/api/settings/detection", json=cfg)
        assert r.status_code == 200 and r.json()["structuring_count_threshold"] == 3
        assert (await ac.get("/api/settings/detection")).json()["structuring_count_threshold"] == 3
    # data_entry ممنوع حتى القراءة
    async with _client(db, Role.DATA_ENTRY, username="de") as ac:
        assert (await ac.get("/api/settings/detection")).status_code == 403


async def test_attention_endpoint_exposes_signals(db):
    pytest.importorskip("fastapi")
    await db.detection.set(DetectionConfig(structuring_count_threshold=3, structuring_sum_threshold=1e9))
    base = utcnow()
    for i in range(3):
        await _seed(db, f"e{i}", status=Status.HELD, phone="0111",
                    reference_number=f"E{i}", created_at=base + timedelta(minutes=i))
    async with _client(db, Role.MANAGER) as ac:
        r = await ac.get("/api/attention")
        assert r.status_code == 200
        flagged = next(it for it in r.json() if it["deal_id"] == "e2")
        assert any(s["rule"] == "structuring" for s in flagged["signals"])
