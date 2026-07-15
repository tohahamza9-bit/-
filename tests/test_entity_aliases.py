"""
اختبارات نظام الكيانات الموحّد — الخطوة ٣ (core/entity_aliases.py + EntityAliasRepo).
تحليل نقيّ (حيّ→مدينة، غموض→None، ثقة، إيقاف) + مستودع + CRUD RBAC + استقلال عن إملاءات الخزائن.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest

from core.constants import EntityType, TreasuryType
from core.entity_aliases import canonical_for, resolve_entity
from core.models import EntityAlias, TreasuryRecord


def _a(alias, canonical, etype=EntityType.DISTRICT, confidence="high", active=True):
    return EntityAlias(alias=alias, entity_type=etype, canonical_name=canonical,
                       confidence=confidence, active=active)


# ── التحليل النقيّ (§4) ───────────────────────────────────────────────────────
def test_resolve_district_to_city():
    aliases = [_a("باردو", "العاصمة"), _a("مدنين", "جربة")]
    assert resolve_entity("باردو", aliases).canonical_name == "العاصمة"
    assert canonical_for("مدنين", aliases) == "جربة"


def test_resolve_no_match_returns_none():
    assert resolve_entity("لا-يوجد", [_a("باردو", "العاصمة")]) is None
    assert canonical_for("", [_a("باردو", "العاصمة")]) is None


def test_resolve_ambiguous_across_canonicals_returns_none():
    """نفس النصّ يطابق اسمين معياريّين مختلفين ⇒ None (لا تخمين §4)."""
    aliases = [
        _a("طه", "طه خزينة", EntityType.TREASURY),
        _a("طه", "طه مورد", EntityType.SUPPLIER),
    ]
    assert resolve_entity("طه", aliases) is None                 # غموض بلا نوع
    # لكن حصر النوع يحسمه
    assert resolve_entity("طه", aliases, EntityType.TREASURY).canonical_name == "طه خزينة"
    assert resolve_entity("طه", aliases, "supplier").canonical_name == "طه مورد"


def test_resolve_prefers_higher_confidence():
    """تكرار نفس (alias→canonical) بثقات مختلفة ⇒ أعلى ثقة."""
    aliases = [_a("أريانة", "العاصمة", confidence="low"), _a("أريانة", "العاصمة", confidence="high")]
    assert resolve_entity("أريانة", aliases).confidence == "high"


def test_resolve_ignores_inactive():
    assert resolve_entity("منوبة", [_a("منوبة", "العاصمة", active=False)]) is None


def test_resolve_normalizes_whitespace_and_case():
    aliases = [_a("Bardo", "العاصمة", EntityType.CITY)]
    assert resolve_entity("  bardo ", aliases, EntityType.CITY).canonical_name == "العاصمة"


# ── المستودع (§4، §13) ───────────────────────────────────────────────────────
async def test_repo_upsert_and_all_active(db):
    await db.entity_aliases.upsert(_a("باردو", "العاصمة"))
    actives = await db.entity_aliases.all_active()
    match = resolve_entity("باردو", actives)                     # القراءة الحيّة → التحليل
    assert match is not None and match.canonical_name == "العاصمة"


async def test_repo_disable_enable_no_delete(db):
    await db.entity_aliases.upsert(_a("باردو", "العاصمة"))
    assert await db.entity_aliases.set_active("باردو", "district", False) is True
    assert resolve_entity("باردو", await db.entity_aliases.all_active()) is None  # اختفى من النشط
    assert await db.entity_aliases.col.find_one({"alias": "باردو"}) is not None   # لم يُحذف
    assert await db.entity_aliases.set_active("باردو", "district", True) is True
    assert resolve_entity("باردو", await db.entity_aliases.all_active()) is not None
    assert await db.entity_aliases.set_active("لا", "district", False) is False   # غير موجود


async def test_seed_if_missing_idempotent(db):
    from core.constants import SEED_ENTITY_ALIASES
    await db.entity_aliases.seed_if_missing(SEED_ENTITY_ALIASES)
    n1 = await db.entity_aliases.col.count_documents({})
    assert n1 >= len(SEED_ENTITY_ALIASES)
    assert canonical_for("باردو", await db.entity_aliases.all_active()) == "العاصمة"
    await db.entity_aliases.seed_if_missing(SEED_ENTITY_ALIASES)   # ثانيةً
    assert await db.entity_aliases.col.count_documents({}) == n1, "idempotent — لا تكرار"


async def test_independent_from_treasury_aliases(db):
    """الكيانات في مجموعة مستقلّة — لا تلمس إملاءات الخزائن (§4)."""
    await db.treasuries.upsert(TreasuryRecord(name="بلاس فون", code="74",
                                              type=TreasuryType.SELL_ONLY, aliases=["بلاس"]))
    await db.entity_aliases.upsert(_a("بلاس", "قناة ما", EntityType.SERVICE))
    tre = await db.treasuries.col.find_one({"name": "بلاس فون"})
    assert tre["aliases"] == ["بلاس"], "إملاءات الخزينة لم تُمَسّ"
    assert await db.entity_aliases.col.count_documents({}) == 1
    assert await db.treasuries.col.count_documents({"aliases": "قناة ما"}) == 0


# ── CRUD عبر HTTP (§14.3 RBAC) ───────────────────────────────────────────────
def _settings(**over):
    from core.config import Settings
    return Settings(_env_file=None, admin_room_jid="admin@g.us", **over)


@asynccontextmanager
async def _anon_client(db):
    from httpx import ASGITransport, AsyncClient
    from dashboard.app import create_app
    app = create_app(db, _settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@asynccontextmanager
async def _client(db, role=None, *, username="admin"):
    from core.constants import Role
    from core.db import utcnow
    from core.models import UserRecord
    from dashboard.auth import hash_password
    await db.users.create(UserRecord(
        username=username, password_hash=hash_password("pw-123456"),
        role=role or Role.MANAGER, active=True, created_at=utcnow()))
    async with _anon_client(db) as ac:
        r = await ac.post("/api/auth/login", json={"username": username, "password": "pw-123456"})
        assert r.status_code == 200
        yield ac


async def test_entity_alias_crud_and_rbac(db):
    pytest.importorskip("fastapi")
    from core.constants import Role
    body = {"alias": "باردو", "entity_type": "district", "canonical_name": "العاصمة"}
    # مجهول → 401
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/entity-aliases")).status_code == 401
    # مراجع: قراءة نعم، كتابة لا
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        assert (await ac.get("/api/entity-aliases")).status_code == 200
        assert (await ac.post("/api/entity-aliases", json=body)).status_code == 403
    # مدير: إضافة + قراءة + إيقاف/تفعيل + 404
    async with _client(db) as ac:
        r = await ac.post("/api/entity-aliases", json=body)
        assert r.status_code == 201 and r.json()["canonical_name"] == "العاصمة"
        rows = (await ac.get("/api/entity-aliases")).json()
        assert any(x["alias"] == "باردو" for x in rows)
        key = {"alias": "باردو", "entity_type": "district"}
        assert (await ac.post("/api/entity-aliases/disable", json=key)).json()["active"] is False
        assert (await ac.post("/api/entity-aliases/enable", json=key)).json()["active"] is True
        miss = {"alias": "لا", "entity_type": "city"}
        assert (await ac.post("/api/entity-aliases/disable", json=miss)).status_code == 404
    # تنبيه المالك أُدرِج (نمط الخزائن/الموردين)
    alerts = [m for m in await db.outgoing.next_unsent(50) if m.get("is_alert")]
    assert any("باردو" in (m.get("text") or "") for m in alerts), "تنبيه للمالك عند التغيير"
