"""
اختبارات لوحة التحكّم (§13). اختبارات منطق المستودعات تعمل دائمًا عبر fixture `db`.
اختبارات HTTP تُتخطّى بوضوح عند غياب fastapi (pytest.importorskip).
"""
from __future__ import annotations

import pytest

from core.constants import TreasuryType
from core.models import EmployeeRecord, TreasuryRecord


# ─────────────────────────────────────────────────────────────────────────────
# 1) منطق المستودعات (يعمل بلا fastapi)
# ─────────────────────────────────────────────────────────────────────────────
async def test_add_treasury_then_read(db):
    """إضافة خزينة عبر الطبقة ثم قراءتها (§13 تُفعَّل فورًا)."""
    rec = TreasuryRecord(name="خزينة اختبار", code="99", type=TreasuryType.SELL_AND_BUY,
                         aliases=["اختبار"])
    await db.treasuries.upsert(rec)
    actives = await db.treasuries.all_active()
    names = {t.name: t for t in actives}
    assert "خزينة اختبار" in names
    assert names["خزينة اختبار"].code == "99"
    assert names["خزينة اختبار"].type == TreasuryType.SELL_AND_BUY


async def test_disable_treasury_no_delete(db):
    """الإيقاف = active=false بلا حذف (§13): تختفي من all_active وتبقى في المجموعة."""
    await db.treasuries.col.update_one({"name": "بلاس فون"}, {"$set": {"active": False}})
    active_names = {t.name for t in await db.treasuries.all_active()}
    assert "بلاس فون" not in active_names
    # ما زالت موجودة (لم تُحذف)
    assert await db.treasuries.col.find_one({"name": "بلاس فون"}) is not None


async def test_dedupe_treasuries_by_code(db):
    """تنظيف db.treasuries: خزينة واحدة لكل كود؛ السجلات بلا كود (شرعية) لا تُمسّ."""
    # حقن مكرّرين بنفس الكود «51» (كما يظهر في الإنتاج: وليد 51 مرّتين)
    dup = {"name": "وليد تونس العاصمة", "code": "51", "currency": "TND",
           "type": "sell_only", "aliases": [], "active": True}
    await db.treasuries.col.insert_one(dict(dup))
    await db.treasuries.col.insert_one(dict(dup))
    assert await db.treasuries.col.count_documents({"code": "51"}) >= 2
    none_before = await db.treasuries.col.count_documents({"code": None})
    assert none_before >= 1                                  # خزائن معلّقة الأكواد (هادم/خصم/صافي…)

    removed = await db.treasuries.dedupe_by_code()
    assert removed >= 1
    assert await db.treasuries.col.count_documents({"code": "51"}) == 1   # واحدة فقط بقيت
    # كل الأكواد صارت فريدة
    codes = [d["code"] async for d in db.treasuries.col.find({}) if d.get("code")]
    assert len(codes) == len(set(codes))
    # السجلات بلا كود لم تُلمَس (متعدّدة وشرعية)
    assert await db.treasuries.col.count_documents({"code": None}) == none_before
    # idempotent — تشغيل ثانٍ لا يحذف شيئًا
    assert await db.treasuries.dedupe_by_code() == 0


async def test_dedupe_keeps_active_over_inactive(db):
    """قاعدة الإبقاء: لا يُعطَّل كود بحذف نسخته النشطة — يُبقى النشط ولو كان أحدث."""
    # الأقدم موقوف، والأحدث نشط — بنفس الكود
    await db.treasuries.col.insert_one(
        {"name": "أبو يوسف جديد", "code": "77", "currency": "EGP",
         "type": "sell_only", "aliases": [], "active": False})
    await db.treasuries.col.insert_one(
        {"name": "أبو يوسف جديد", "code": "77", "currency": "EGP",
         "type": "sell_only", "aliases": [], "active": True})
    removed = await db.treasuries.dedupe_by_code()
    assert removed >= 1
    survivors = [d async for d in db.treasuries.col.find({"code": "77"})]
    assert len(survivors) == 1
    assert survivors[0]["active"] is True          # النشط بقي، لا الموقوف الأقدم


async def test_control_default_stopped(db):
    """الافتراضي عند التشغيل: إيقاف (storage_enabled=False، state=stopped) — §13."""
    ctrl = await db.control.get()
    assert ctrl.storage_enabled is False
    assert ctrl.state == "stopped"


async def test_toggle_kill_switch_persists(db):
    """toggle يبدّل storage_enabled ويحفظ الحالة (running/stopped)."""
    ctrl = await db.control.get()
    ctrl.storage_enabled = not ctrl.storage_enabled          # → True
    ctrl.state = "running" if ctrl.storage_enabled else "stopped"
    await db.control.set(ctrl, updated_by="test")
    again = await db.control.get()
    assert again.storage_enabled is True
    assert again.state == "running"
    # تبديل عكسي
    again.storage_enabled = False
    again.state = "stopped"
    await db.control.set(again, updated_by="test")
    assert (await db.control.get()).storage_enabled is False


async def test_employee_is_authorized(db):
    """«تم» تُقبل من الموظف المعتمد فقط (§8.3)."""
    await db.employees.upsert(EmployeeRecord(whatsapp_number="201000000000", name="محمد"))
    assert await db.employees.is_authorized("201000000000") is True
    assert await db.employees.is_authorized("999") is False
    # الإيقاف يُبطل الاعتماد
    await db.employees.col.update_one(
        {"whatsapp_number": "201000000000"}, {"$set": {"active": False}}
    )
    assert await db.employees.is_authorized("201000000000") is False


# ─────────────────────────────────────────────────────────────────────────────
# 2) اختبارات HTTP (تتطلّب fastapi — تُتخطّى بوضوح عند غيابه)
# ─────────────────────────────────────────────────────────────────────────────
def _settings(token: str):
    from core.config import Settings
    return Settings(internal_token=token)


async def _client(db, token: str):
    from httpx import ASGITransport, AsyncClient

    from dashboard.app import create_app

    app = create_app(db, _settings(token))
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_write_requires_internal_token(db):
    """SEC-002: نقاط الكتابة ترفض بلا X-Internal-Token (401) وتقبل مع الرمز الصحيح."""
    pytest.importorskip("fastapi")
    async with await _client(db, "s3cret") as ac:
        # بلا رمز → 401
        r = await ac.post("/api/treasuries", json={"name": "خ1", "code": "10"})
        assert r.status_code == 401
        # رمز خاطئ → 401
        r = await ac.post("/api/treasuries", json={"name": "خ1", "code": "10"},
                          headers={"X-Internal-Token": "wrong"})
        assert r.status_code == 401
        # رمز صحيح → 201
        r = await ac.post("/api/treasuries", json={"name": "خ1", "code": "10"},
                          headers={"X-Internal-Token": "s3cret"})
        assert r.status_code == 201
        # القراءة مفتوحة بلا رمز
        r = await ac.get("/api/treasuries")
        assert r.status_code == 200
        assert any(t["name"] == "خ1" for t in r.json())


async def test_control_toggle_endpoint(db):
    """نقطة toggle تبدّل الحالة وتُرجعها (§13)."""
    pytest.importorskip("fastapi")
    async with await _client(db, "s3cret") as ac:
        r = await ac.get("/api/control")
        assert r.status_code == 200 and r.json()["state"] == "stopped"
        r = await ac.post("/api/control/toggle", headers={"X-Internal-Token": "s3cret"})
        assert r.status_code == 200
        assert r.json()["storage_enabled"] is True
        assert r.json()["state"] == "running"


async def test_treasuries_expose_currency_for_country_column(db):
    """عمود «البلد» في اللوحة يُشتقّ من currency الخزينة (db.treasuries مصدر الحقيقة، عرض فقط).

    نتحقّق أن /api/treasuries يُصدّر العملة الصحيحة التي يترجمها الـUI لعلَم:
    EGP→🇪🇬 مصر، TND→🇹🇳 تونس، None→— (بلا حقل country جديد ولا PATCH).
    """
    pytest.importorskip("fastapi")
    async with await _client(db, "s3cret") as ac:
        r = await ac.get("/api/treasuries")           # القراءة مفتوحة بلا رمز
        assert r.status_code == 200
        by_name = {t["name"]: t for t in r.json()}
        assert by_name["بلاس فون"]["currency"] == "EGP"            # 🇪🇬 مصر
        assert by_name["وليد تونس العاصمة"]["currency"] == "TND"   # 🇹🇳 تونس
        assert by_name["خصم 1%"]["currency"] == "EGP"              # 🇪🇬 مصر (كودها مستكمَل 72)
        assert by_name["صافي"]["currency"] is None                 # — غير محدّد


async def test_rooms_patch_type_only_preserves_name(db):
    """PATCH يعدّل النوع فقط ويحافظ على الاسم/الاكتشاف (auto-save §2.2)."""
    pytest.importorskip("fastapi")
    from core.constants import RoomType
    from core.models import Room
    await db.rooms.upsert(Room(jid="p1@g.us", type=RoomType.UNCLASSIFIED, name="غرفة أ"))
    async with await _client(db, "s3cret") as ac:
        r = await ac.patch("/api/rooms/p1@g.us", json={"type": "customer"},
                           headers={"X-Internal-Token": "s3cret"})
        assert r.status_code == 200
        assert r.json()["type"] == "customer"
        assert r.json()["name"] == "غرفة أ"          # الاسم لم يُمسّ
    stored = await db.rooms.get("p1@g.us")
    assert stored.type == RoomType.CUSTOMER
    assert stored.name == "غرفة أ"


async def test_rooms_patch_active_only(db):
    """PATCH يعدّل active فقط (إيقاف بلا حذف §13) دون تغيير النوع."""
    pytest.importorskip("fastapi")
    from core.constants import RoomType
    from core.models import Room
    await db.rooms.upsert(Room(jid="p2@g.us", type=RoomType.CUSTOMER, name="غرفة ب"))
    async with await _client(db, "s3cret") as ac:
        r = await ac.patch("/api/rooms/p2@g.us", json={"active": False},
                           headers={"X-Internal-Token": "s3cret"})
        assert r.status_code == 200
        assert r.json()["active"] is False
        assert r.json()["type"] == "customer"        # النوع ثابت
    stored = await db.rooms.get("p2@g.us")
    assert stored.active is False


async def test_rooms_patch_treasury_code_then_cleared_on_type_change(db):
    """PATCH type=treasury + treasury_code يخزّن الكود؛ وتبديل النوع بعيدًا عنه يُصفّره."""
    pytest.importorskip("fastapi")
    from core.constants import RoomType
    from core.models import Room
    await db.rooms.upsert(Room(jid="p3@g.us", type=RoomType.UNCLASSIFIED, name="غرفة خزينة"))
    async with await _client(db, "s3cret") as ac:
        # ربط بخزينة «بلاس فون» (code=74 في SEED)
        r = await ac.patch("/api/rooms/p3@g.us",
                           json={"type": "treasury", "treasury_code": "74"},
                           headers={"X-Internal-Token": "s3cret"})
        assert r.status_code == 200
        assert r.json()["type"] == "treasury"
        assert r.json()["treasury_code"] == "74"
        # التبديل إلى «زبون» يُصفّر الكود اليتيم
        r = await ac.patch("/api/rooms/p3@g.us", json={"type": "customer"},
                           headers={"X-Internal-Token": "s3cret"})
        assert r.status_code == 200
        assert r.json()["treasury_code"] is None
    assert (await db.rooms.get("p3@g.us")).treasury_code is None


async def test_rooms_patch_requires_token(db):
    """SEC-002: PATCH يرفض بلا رمز (401) ويقبل مع الرمز الصحيح."""
    pytest.importorskip("fastapi")
    from core.constants import RoomType
    from core.models import Room
    await db.rooms.upsert(Room(jid="p4@g.us", type=RoomType.UNCLASSIFIED, name="غرفة"))
    async with await _client(db, "s3cret") as ac:
        r = await ac.patch("/api/rooms/p4@g.us", json={"type": "ignore"})
        assert r.status_code == 401
        r = await ac.patch("/api/rooms/p4@g.us", json={"type": "ignore"},
                           headers={"X-Internal-Token": "s3cret"})
        assert r.status_code == 200


async def test_rooms_patch_unknown_jid_404(db):
    """PATCH لغرفة غير مكتشَفة → 404 (لا إنشاء يدوي بلا اكتشاف)."""
    pytest.importorskip("fastapi")
    async with await _client(db, "s3cret") as ac:
        r = await ac.patch("/api/rooms/nope@g.us", json={"type": "customer"},
                           headers={"X-Internal-Token": "s3cret"})
        assert r.status_code == 404


async def test_rooms_endpoint_exposes_treasury_code(db):
    """GET /api/rooms يُصدّر حقل treasury_code (للربط في صفحة /rooms)."""
    pytest.importorskip("fastapi")
    from core.constants import RoomType
    from core.models import Room
    await db.rooms.upsert(Room(jid="p5@g.us", type=RoomType.TREASURY,
                               name="خزينة", treasury_code="74"))
    async with await _client(db, "s3cret") as ac:
        r = await ac.get("/api/rooms")
        assert r.status_code == 200
        row = next(x for x in r.json() if x["jid"] == "p5@g.us")
        assert row["treasury_code"] == "74"


async def test_rooms_page_served(db):
    """صفحة /rooms تُقدَّم (200) — إدارة الغرف بالأسماء بلا إدخال JID يدوي."""
    pytest.importorskip("fastapi")
    async with await _client(db, "s3cret") as ac:
        r = await ac.get("/rooms")
        assert r.status_code == 200
        assert "إدارة الغرف" in r.text


async def test_rooms_endpoint_sorted_by_type(db):
    """GET /api/rooms يرجع الغرف مرتّبة حسب النوع مع الحقول name/jid/type/active."""
    pytest.importorskip("fastapi")
    from core.constants import RoomType
    from core.models import Room
    # إدخال بترتيب مبعثر عمدًا
    await db.rooms.upsert(Room(jid="t@g.us", type=RoomType.TREASURY, name="خزينة"))
    await db.rooms.upsert(Room(jid="u@g.us", type=RoomType.UNCLASSIFIED, name="جديدة"))
    await db.rooms.upsert(Room(jid="c@g.us", type=RoomType.CENTRAL, name="المركزية"))
    await db.rooms.upsert(Room(jid="cu@g.us", type=RoomType.CUSTOMER, name="زبون"))
    await db.rooms.upsert(Room(jid="a@g.us", type=RoomType.ADMIN, name="المسؤول"))
    async with await _client(db, "s3cret") as ac:
        r = await ac.get("/api/rooms")           # القراءة مفتوحة بلا رمز
        assert r.status_code == 200
        rows = r.json()
        assert [x["type"] for x in rows] == [
            "central", "admin", "customer", "treasury", "unclassified",
        ]
        assert {"name", "jid", "type", "active"} <= set(rows[0])
