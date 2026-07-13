"""
اختبارات لوحة التحكّم (§13). اختبارات منطق المستودعات تعمل دائمًا عبر fixture `db`.
اختبارات HTTP تُتخطّى بوضوح عند غياب fastapi (pytest.importorskip).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

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
# المصادقة الآن بجلسة خادم + كوكي (§14.3): العميل يسجّل الدخول فيحمل الكوكي تلقائيًا.
# ─────────────────────────────────────────────────────────────────────────────
def _settings(**over):
    from core.config import Settings
    return Settings(_env_file=None, **over)   # تجاهُل .env — إعدادات حتمية للاختبار


async def _seed_user(db, username="admin", password="pw-123456", role=None):
    from core.constants import Role
    from core.db import utcnow
    from core.models import UserRecord
    from dashboard.auth import hash_password
    await db.users.create(UserRecord(
        username=username, password_hash=hash_password(password),
        role=role or Role.MANAGER, active=True, created_at=utcnow()))


@asynccontextmanager
async def _anon_client(db, settings=None):
    """عميل غير مسجّل (بلا جلسة)."""
    from httpx import ASGITransport, AsyncClient
    from dashboard.app import create_app
    app = create_app(db, settings or _settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@asynccontextmanager
async def _client(db, role=None, *, username="admin", password="pw-123456", settings=None):
    """عميل مسجّل الدخول بدور محدّد (افتراضي: manager) — يزرع الحساب ويحمل كوكي الجلسة."""
    from core.constants import Role
    await _seed_user(db, username=username, password=password, role=role or Role.MANAGER)
    async with _anon_client(db, settings) as ac:
        r = await ac.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200
        yield ac


async def test_write_requires_login_and_manager(db):
    """§14.3: الكتابة ترفض بلا دخول (401)، ترفض للمراجع (403)، وتقبل للمدير (201)."""
    pytest.importorskip("fastapi")
    from core.constants import Role
    # بلا دخول → 401
    async with _anon_client(db) as ac:
        r = await ac.post("/api/treasuries", json={"name": "خ1", "code": "10"})
        assert r.status_code == 401
    # مراجع → 403 (قراءة فقط)
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.post("/api/treasuries", json={"name": "خ1", "code": "10"})
        assert r.status_code == 403
    # مدير → 201
    async with _client(db) as ac:
        r = await ac.post("/api/treasuries", json={"name": "خ1", "code": "10"})
        assert r.status_code == 201
        r = await ac.get("/api/treasuries")
        assert r.status_code == 200
        assert any(t["name"] == "خ1" for t in r.json())


async def test_reads_require_login_and_role(db):
    """§14.3: القراءة تتطلّب دخولًا (401 للمجهول)، متاحة للمدير/المراجع، ممنوعة على data_entry (403)."""
    pytest.importorskip("fastapi")
    from core.constants import Role
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/treasuries")).status_code == 401
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        assert (await ac.get("/api/treasuries")).status_code == 200
    async with _client(db, Role.DATA_ENTRY, username="de") as ac:
        assert (await ac.get("/api/treasuries")).status_code == 403


async def test_enable_treasury_reactivates(db):
    """POST /treasuries/{name}/enable يعيد التفعيل (active=true) — عكس الإيقاف (§13)."""
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        r = await ac.post("/api/treasuries/بلاس فون/disable")
        assert r.status_code == 200 and r.json()["active"] is False
        assert "بلاس فون" not in {t.name for t in await db.treasuries.all_active()}
        # التفعيل يعيدها
        r = await ac.post("/api/treasuries/بلاس فون/enable")
        assert r.status_code == 200 and r.json()["active"] is True
        assert "بلاس فون" in {t.name for t in await db.treasuries.all_active()}


async def test_enable_treasury_requires_auth_and_404(db):
    """التفعيل مَحروس (401 بلا دخول) و 404 لخزينة غير موجودة (للمدير)."""
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        r = await ac.post("/api/treasuries/بلاس فون/enable")   # بلا دخول
        assert r.status_code == 401
    async with _client(db) as ac:
        r = await ac.post("/api/treasuries/لا-توجد/enable")
        assert r.status_code == 404


async def test_control_toggle_endpoint(db):
    """نقطة toggle تبدّل الحالة وتُرجعها (§13)."""
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        r = await ac.get("/api/control")
        assert r.status_code == 200 and r.json()["state"] == "stopped"
        r = await ac.post("/api/control/toggle")
        assert r.status_code == 200
        assert r.json()["storage_enabled"] is True
        assert r.json()["state"] == "running"


async def test_auto_trust_toggle_endpoint(db):
    """نقطة auto_trust تبدّل «وضع التلقائي» وتُرجعه (الافتراضي مُعطّل)."""
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        r = await ac.get("/api/control")
        assert r.json()["auto_trust"] is False                       # الافتراضي
        r = await ac.post("/api/control/auto_trust")
        assert r.status_code == 200 and r.json()["auto_trust"] is True
        # لا يمسّ التخزين (مستقلّ عن Kill Switch)
        assert r.json()["storage_enabled"] is False
        r = await ac.post("/api/control/auto_trust")
        assert r.json()["auto_trust"] is False                       # تبديل ثانٍ يعيده
    # يتطلّب المصادقة (§14.3): بلا دخول → 401
    async with _anon_client(db) as ac:
        r = await ac.post("/api/control/auto_trust")
        assert r.status_code == 401


async def test_treasuries_expose_currency_for_country_column(db):
    """عمود «البلد» في اللوحة يُشتقّ من currency الخزينة (db.treasuries مصدر الحقيقة، عرض فقط).

    نتحقّق أن /api/treasuries يُصدّر العملة الصحيحة التي يترجمها الـUI لعلَم:
    EGP→🇪🇬 مصر، TND→🇹🇳 تونس، None→— (بلا حقل country جديد ولا PATCH).
    """
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        r = await ac.get("/api/treasuries")
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
    async with _client(db) as ac:
        r = await ac.patch("/api/rooms/p1@g.us", json={"type": "customer"})
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
    async with _client(db) as ac:
        r = await ac.patch("/api/rooms/p2@g.us", json={"active": False})
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
    async with _client(db) as ac:
        # ربط بخزينة «بلاس فون» (code=74 في SEED)
        r = await ac.patch("/api/rooms/p3@g.us",
                           json={"type": "treasury", "treasury_code": "74"})
        assert r.status_code == 200
        assert r.json()["type"] == "treasury"
        assert r.json()["treasury_code"] == "74"
        # التبديل إلى «زبون» يُصفّر الكود اليتيم
        r = await ac.patch("/api/rooms/p3@g.us", json={"type": "customer"})
        assert r.status_code == 200
        assert r.json()["treasury_code"] is None
    assert (await db.rooms.get("p3@g.us")).treasury_code is None


async def test_rooms_patch_requires_manager(db):
    """§14.3: PATCH يرفض بلا دخول (401)، يرفض للمراجع (403)، ويقبل للمدير (200)."""
    pytest.importorskip("fastapi")
    from core.constants import Role, RoomType
    from core.models import Room
    await db.rooms.upsert(Room(jid="p4@g.us", type=RoomType.UNCLASSIFIED, name="غرفة"))
    async with _anon_client(db) as ac:
        r = await ac.patch("/api/rooms/p4@g.us", json={"type": "ignore"})
        assert r.status_code == 401
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.patch("/api/rooms/p4@g.us", json={"type": "ignore"})
        assert r.status_code == 403
    async with _client(db) as ac:
        r = await ac.patch("/api/rooms/p4@g.us", json={"type": "ignore"})
        assert r.status_code == 200


async def test_rooms_patch_unknown_jid_404(db):
    """PATCH لغرفة غير مكتشَفة → 404 (لا إنشاء يدوي بلا اكتشاف)."""
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        r = await ac.patch("/api/rooms/nope@g.us", json={"type": "customer"})
        assert r.status_code == 404


async def test_rooms_endpoint_exposes_treasury_code(db):
    """GET /api/rooms يُصدّر حقل treasury_code (للربط في صفحة /rooms)."""
    pytest.importorskip("fastapi")
    from core.constants import RoomType
    from core.models import Room
    await db.rooms.upsert(Room(jid="p5@g.us", type=RoomType.TREASURY,
                               name="خزينة", treasury_code="74"))
    async with _client(db) as ac:
        r = await ac.get("/api/rooms")
        assert r.status_code == 200
        row = next(x for x in r.json() if x["jid"] == "p5@g.us")
        assert row["treasury_code"] == "74"


async def test_rooms_page_served(db):
    """صفحة /rooms تُقدَّم (200) بلا مصادقة — البوّابة تتمّ في الواجهة/الـ API."""
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        r = await ac.get("/rooms")
        assert r.status_code == 200
        assert "إدارة الغرف" in r.text


async def test_login_page_served(db):
    """صفحة /login تُقدَّم (200) بلا مصادقة."""
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        r = await ac.get("/login")
        assert r.status_code == 200
        assert "تسجيل الدخول" in r.text


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
    async with _client(db) as ac:
        r = await ac.get("/api/rooms")
        assert r.status_code == 200
        rows = r.json()
        assert [x["type"] for x in rows] == [
            "central", "admin", "customer", "treasury", "unclassified",
        ]
        assert {"name", "jid", "type", "active"} <= set(rows[0])


# ─────────────────────────────────────────────────────────────────────────────
# الكلمات المجهولة (§4.5) — GET القائمة + POST الإسناد كـ alias
# ─────────────────────────────────────────────────────────────────────────────
async def test_unknown_terms_list_and_assign(db):
    pytest.importorskip("fastapi")
    await db.unknown_terms.record("فودافون تجريبي", "treasury")   # خزينة 74 (بلاس فون) مزروعة
    async with _client(db) as ac:
        r = await ac.get("/api/unknown-terms")
        assert r.status_code == 200
        assert any(x["term"] == "فودافون تجريبي" for x in r.json())
        r = await ac.post("/api/unknown-terms/فودافون تجريبي/assign",
                          json={"type": "treasury", "target_code": "74"})
        assert r.status_code == 200 and "فودافون تجريبي" in r.json()["aliases"]
    doc = await db.treasuries.col.find_one({"code": "74"})
    assert "فودافون تجريبي" in doc["aliases"]                      # صار alias
    assert not any(x["term"] == "فودافون تجريبي"                   # وحُذف من المجهولات
                   for x in await db.unknown_terms.list_recent(20))


async def test_assign_unknown_term_requires_auth(db):
    pytest.importorskip("fastapi")
    await db.unknown_terms.record("خزينه ما", "treasury")
    async with _anon_client(db) as ac:
        r = await ac.post("/api/unknown-terms/خزينه ما/assign",
                          json={"type": "treasury", "target_code": "74"})   # بلا دخول
        assert r.status_code == 401
