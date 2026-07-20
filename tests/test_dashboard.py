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


async def test_cancellation_mode_endpoint(db):
    """نقطة cancellation_mode تضبط وضع الإلغاء (immediate/sql_required/manual، الافتراضي immediate)."""
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        assert (await ac.get("/api/control")).json()["cancellation_mode"] == "immediate"  # الافتراضي
        r = await ac.post("/api/control/cancellation_mode", json={"mode": "sql_required"})
        assert r.status_code == 200 and r.json()["cancellation_mode"] == "sql_required"
        r = await ac.post("/api/control/cancellation_mode", json={"mode": "manual"})
        assert r.json()["cancellation_mode"] == "manual"
        assert r.json()["storage_enabled"] is False                    # مستقلّ عن Kill Switch
        bad = await ac.post("/api/control/cancellation_mode", json={"mode": "xxx"})
        assert bad.status_code == 422                                  # قيمة غير صالحة تُرفَض
    async with _anon_client(db) as ac:                                 # يتطلّب المصادقة
        assert (await ac.post("/api/control/cancellation_mode",
                              json={"mode": "manual"})).status_code == 401


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
        assert by_name["تونسي خارجي"]["currency"] == "TND"         # 🇹🇳 تونس
        assert "صافي" not in by_name                               # حُذفت من البذرة (2026-07-20)


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


# ─────────────────────────────────────────────────────────────────────────────
# تعديل inline للسجلات الموجودة + تنبيه المالك + RBAC (م: تعديل البيانات + قنوات + موظف)
# ─────────────────────────────────────────────────────────────────────────────
def _admin_settings():
    return _settings(admin_room_jid="admin@g.us")


async def _alerts(db):
    return [m for m in await db.outgoing.next_unsent(50) if m.get("is_alert")]


async def test_edit_treasury_updates_aliases_currency_and_alerts(db):
    pytest.importorskip("fastapi")
    await db.treasuries.upsert(TreasuryRecord(name="بلاس فون", code="74", type=TreasuryType.SELL_ONLY))
    async with _client(db, settings=_admin_settings()) as ac:
        r = await ac.post("/api/treasuries/بلاس فون",
                          json={"name": "بلاس فون", "code": "74", "type": "sell_only",
                                "currency": "EGP", "aliases": ["بلاصو", "بلاس"]})
        assert r.status_code == 200
    doc = await db.treasuries.col.find_one({"name": "بلاس فون"})
    assert doc["aliases"] == ["بلاصو", "بلاس"] and doc["currency"] == "EGP"
    assert any("بلاس فون" in (m.get("text") or "") for m in await _alerts(db)), "تنبيه is_alert للمالك"


async def test_push_treasury_alias_appends_and_dedupes(db):
    """زر «إضافة» (push): يُلحِق إملاءً واحدًا لمكدّس aliases بلا إعادة إرسال كل الحقول، بلا تكرار."""
    pytest.importorskip("fastapi")
    await db.treasuries.upsert(
        TreasuryRecord(name="بلاس فون", code="74", type=TreasuryType.SELL_ONLY, aliases=["بلاس"]))
    async with _client(db, settings=_admin_settings()) as ac:
        r = await ac.post("/api/treasuries/بلاس فون/aliases", json={"alias": "بلاصو"})
        assert r.status_code == 200 and r.json()["aliases"] == ["بلاس", "بلاصو"]
        r2 = await ac.post("/api/treasuries/بلاس فون/aliases", json={"alias": "بلاصو"})  # مكرّر
        assert r2.json()["aliases"] == ["بلاس", "بلاصو"], "لا تكرار ($addToSet)"
        r3 = await ac.post("/api/treasuries/بلاس فون/aliases", json={"alias": "  بلس  "})  # يُشذّب
        assert r3.json()["aliases"] == ["بلاس", "بلاصو", "بلس"]
    doc = await db.treasuries.col.find_one({"name": "بلاس فون"})
    assert doc["aliases"] == ["بلاس", "بلاصو", "بلس"]
    assert any("بلاس فون" in (m.get("text") or "") for m in await _alerts(db)), "تنبيه للمالك"


async def test_push_treasury_alias_unknown_404(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        r = await ac.post("/api/treasuries/لا-توجد/aliases", json={"alias": "x"})
        assert r.status_code == 404


async def test_push_treasury_alias_reviewer_forbidden(db):
    pytest.importorskip("fastapi")
    from core.constants import Role
    await db.treasuries.upsert(TreasuryRecord(name="بلاس فون", code="74", type=TreasuryType.SELL_ONLY))
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.post("/api/treasuries/بلاس فون/aliases", json={"alias": "x"})
        assert r.status_code == 403, "reviewer يرى لكن لا يضيف إملاءً"


# ── تعميم زر «＋» للإملاءات: الموردون + قنوات الدفع (نفس نمط الخزائن 025d3ef) ──────
async def test_push_supplier_alias_appends_and_dedupes(db):
    """زر «＋» للمورد (push): يُلحِق إملاءً واحدًا بـ$addToSet — بلا تكرار + تشذيب + تنبيه المالك."""
    pytest.importorskip("fastapi")
    from core.models import SupplierRecord
    await db.suppliers.upsert(SupplierRecord(name="البراق", code="1280", aliases=["براق"]))
    async with _client(db, settings=_admin_settings()) as ac:
        r = await ac.post("/api/suppliers/البراق/aliases", json={"alias": "الابراق"})
        assert r.status_code == 200 and r.json()["aliases"] == ["براق", "الابراق"]
        r2 = await ac.post("/api/suppliers/البراق/aliases", json={"alias": "الابراق"})  # مكرّر
        assert r2.json()["aliases"] == ["براق", "الابراق"], "لا تكرار ($addToSet)"
        r3 = await ac.post("/api/suppliers/البراق/aliases", json={"alias": "  البرق  "})  # يُشذّب
        assert r3.json()["aliases"] == ["براق", "الابراق", "البرق"]
    doc = await db.suppliers.col.find_one({"name": "البراق"})
    assert doc["aliases"] == ["براق", "الابراق", "البرق"]
    assert any("البراق" in (m.get("text") or "") for m in await _alerts(db)), "تنبيه للمالك"


async def test_push_supplier_alias_unknown_404_and_reviewer_403(db):
    pytest.importorskip("fastapi")
    from core.constants import Role
    from core.models import SupplierRecord
    async with _client(db) as ac:
        assert (await ac.post("/api/suppliers/لا-يوجد/aliases", json={"alias": "x"})).status_code == 404
    await db.suppliers.upsert(SupplierRecord(name="البراق", code="1280"))
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.post("/api/suppliers/البراق/aliases", json={"alias": "x"})
        assert r.status_code == 403, "reviewer يرى لكن لا يضيف إملاءً"


async def test_push_channel_alias_appends_and_dedupes(db):
    """زر «＋» لقناة الدفع (push): $addToSet بلا تكرار + تشذيب + تنبيه المالك."""
    pytest.importorskip("fastapi")
    from core.models import PaymentChannelRecord
    await db.payment_channels.upsert(PaymentChannelRecord(name="فودافون", code="17", aliases=["فودا"]))
    async with _client(db, settings=_admin_settings()) as ac:
        r = await ac.post("/api/payment-channels/فودافون/aliases", json={"alias": "فودافوان"})
        assert r.status_code == 200 and r.json()["aliases"] == ["فودا", "فودافوان"]
        r2 = await ac.post("/api/payment-channels/فودافون/aliases", json={"alias": "فودافوان"})
        assert r2.json()["aliases"] == ["فودا", "فودافوان"], "لا تكرار ($addToSet)"
    doc = await db.payment_channels.col.find_one({"name": "فودافون"})
    assert doc["aliases"] == ["فودا", "فودافوان"]
    assert any("فودافون" in (m.get("text") or "") for m in await _alerts(db)), "تنبيه للمالك"


async def test_push_channel_alias_unknown_404_and_reviewer_403(db):
    pytest.importorskip("fastapi")
    from core.constants import Role
    from core.models import PaymentChannelRecord
    async with _client(db) as ac:
        assert (await ac.post("/api/payment-channels/لا-توجد/aliases", json={"alias": "x"})).status_code == 404
    await db.payment_channels.upsert(PaymentChannelRecord(name="فودافون", code="17"))
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.post("/api/payment-channels/فودافون/aliases", json={"alias": "x"})
        assert r.status_code == 403


# ── استيراد الغرف (لصق JIDs + من الرسائل) — تُسجَّل «غير مصنّفة» بلا مساس بموجود ──────
async def test_import_rooms_from_jids(db):
    """لصق قائمة JIDs → غرف «غير مصنّفة» جديدة؛ تشذيب/إسقاط الفراغ والمكرّر؛ لا يمسّ تصنيفًا موجودًا."""
    pytest.importorskip("fastapi")
    from core.constants import RoomType
    from core.models import Room
    # غرفة مصنّفة مسبقًا — يجب ألّا يتغيّر تصنيفها بالاستيراد
    await db.rooms.upsert(Room(jid="known@g.us", name="مركزية", type=RoomType.CENTRAL))
    async with _client(db) as ac:
        r = await ac.post("/api/rooms/import",
                          json={"jids": ["a@g.us", " b@g.us ", "a@g.us", "", "known@g.us"]})
        assert r.status_code == 200
        body = r.json()
        assert body["added"] == 2, "a وb جديدتان فقط (المكرّر/الفارغ/الموجود لا تُحتسب)"
    a = await db.rooms.get("a@g.us")
    assert a is not None and a.type is RoomType.UNCLASSIFIED and a.active is True
    assert (await db.rooms.get("b@g.us")) is not None
    # الغرفة المعروفة بقيت مركزية (setOnInsert لا يمسّها)
    assert (await db.rooms.get("known@g.us")).type is RoomType.CENTRAL


async def test_import_rooms_from_messages(db):
    """استيراد كل chat_jid المميّزة من raw_messages → غرف «غير مصنّفة» (بلا تكرار الموجود)."""
    pytest.importorskip("fastapi")
    from core.constants import RoomType
    from core.db import utcnow
    from core.models import RawMessage, Room
    await db.rooms.upsert(Room(jid="g1@g.us", name="زبون", type=RoomType.CUSTOMER))  # موجودة
    for i, jid in enumerate(["g1@g.us", "g2@g.us", "g2@g.us", "g3@g.us"]):
        await db.raw.insert(RawMessage(message_key=f"k{i}", chat_jid=jid, text="x", received_at=utcnow()))
    async with _client(db) as ac:
        r = await ac.post("/api/rooms/import-from-messages")
        assert r.status_code == 200
        body = r.json()
        assert body["added"] == 2, "g2 وg3 جديدتان (g1 موجودة)"
        assert body["scanned"] == 3, "ثلاث chat_jid مميّزة"
    assert (await db.rooms.get("g2@g.us")).type is RoomType.UNCLASSIFIED
    assert (await db.rooms.get("g1@g.us")).type is RoomType.CUSTOMER  # لم تُمَسّ


async def test_import_rooms_reviewer_forbidden(db):
    pytest.importorskip("fastapi")
    from core.constants import Role
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        assert (await ac.post("/api/rooms/import", json={"jids": ["x@g.us"]})).status_code == 403
        assert (await ac.post("/api/rooms/import-from-messages")).status_code == 403


async def test_edit_treasury_reviewer_forbidden(db):
    pytest.importorskip("fastapi")
    from core.constants import Role
    await db.treasuries.upsert(TreasuryRecord(name="بلاس فون", code="74", type=TreasuryType.SELL_ONLY))
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.post("/api/treasuries/بلاس فون", json={"name": "بلاس فون", "aliases": ["x"]})
        assert r.status_code == 403, "reviewer يرى لكن لا يعدّل"


async def test_edit_treasury_code_conflict_409(db):
    pytest.importorskip("fastapi")
    await db.treasuries.upsert(TreasuryRecord(name="أ", code="10", type=TreasuryType.SELL_ONLY))
    await db.treasuries.upsert(TreasuryRecord(name="ب", code="20", type=TreasuryType.SELL_ONLY))
    async with _client(db) as ac:
        r = await ac.post("/api/treasuries/ب", json={"name": "ب", "code": "10", "type": "sell_only"})
        assert r.status_code == 409, "كود مستخدَم على اسم مختلف → 409"


async def test_edit_supplier_updates_aliases_and_alerts(db):
    pytest.importorskip("fastapi")
    from core.models import SupplierRecord
    await db.suppliers.upsert(SupplierRecord(name="مورد أ", code="S1"))
    async with _client(db, settings=_admin_settings()) as ac:
        r = await ac.post("/api/suppliers/مورد أ",
                          json={"name": "مورد أ", "code": "S1", "aliases": ["م1", "م2"]})
        assert r.status_code == 200
    doc = await db.suppliers.col.find_one({"name": "مورد أ"})
    assert doc["aliases"] == ["م1", "م2"]
    assert await _alerts(db), "تنبيه المالك عند تعديل المورد"


async def test_edit_channel_updates_and_reviewer_forbidden(db):
    pytest.importorskip("fastapi")
    from core.constants import Role
    from core.models import PaymentChannelRecord
    await db.payment_channels.upsert(PaymentChannelRecord(name="فودافون كاش", code="VC"))
    async with _client(db, settings=_admin_settings()) as ac:
        r = await ac.post("/api/payment-channels/فودافون كاش",
                          json={"name": "فودافون كاش", "code": "VC", "aliases": ["فودا", "vc"]})
        assert r.status_code == 200
    doc = await db.payment_channels.col.find_one({"name": "فودافون كاش"})
    assert doc["aliases"] == ["فودا", "vc"]
    assert await _alerts(db), "تنبيه المالك عند تعديل القناة"
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.post("/api/payment-channels/فودافون كاش", json={"name": "فودافون كاش", "aliases": []})
        assert r.status_code == 403


async def test_edit_employee_name_only_preserves_number_and_alerts(db):
    pytest.importorskip("fastapi")
    await db.employees.upsert(EmployeeRecord(whatsapp_number="20100", name="اسم قديم"))
    async with _client(db, settings=_admin_settings()) as ac:
        r = await ac.post("/api/employees/20100", json={"whatsapp_number": "20100", "name": "اسم جديد"})
        assert r.status_code == 200
    doc = await db.employees.col.find_one({"whatsapp_number": "20100"})
    assert doc["name"] == "اسم جديد" and doc["whatsapp_number"] == "20100", "الاسم يتغيّر والرقم ثابت"
    assert any("اسم جديد" in (m.get("text") or "") for m in await _alerts(db))


async def test_edit_employee_reviewer_forbidden_and_404(db):
    pytest.importorskip("fastapi")
    from core.constants import Role
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.post("/api/employees/20100", json={"whatsapp_number": "20100", "name": "x"})
        assert r.status_code == 403
    async with _client(db) as ac:
        r = await ac.post("/api/employees/غير-موجود", json={"whatsapp_number": "غير-موجود", "name": "x"})
        assert r.status_code == 404


async def test_treasury_alias_edit_is_live_without_restart(db):
    """م٢: تعديل إملاء خزينة يسري فورًا على القراءة التالية (all_active) — بلا cache/إعادة تشغيل."""
    from core.parsing.resolve import resolve_treasury
    await db.treasuries.upsert(TreasuryRecord(name="بلاس فون", code="74", type=TreasuryType.SELL_ONLY))
    before = await db.treasuries.all_active()                 # نفس ما يقرؤه pipeline._ingest لكل رسالة
    assert resolve_treasury("اورنج كاش", before) is None      # الإملاء غير معرّف بعد
    await db.treasuries.upsert(TreasuryRecord(name="بلاس فون", code="74",
                                              type=TreasuryType.SELL_ONLY, aliases=["اورنج كاش"]))
    after = await db.treasuries.all_active()                  # القراءة الحيّة التالية — بلا إعادة تشغيل
    t = resolve_treasury("اورنج كاش", after)
    assert t is not None and t.name == "بلاس فون", "الإملاء الجديد يُطابَق فورًا على الرسالة التالية"


# ─────────────────────────────────────────────────────────────────────────────
# نظام الأسعار — الخطوة ١: FxRatesConfig (إعداد + مستودع + GET/PUT) — FX_RATES_SPEC §12
# ─────────────────────────────────────────────────────────────────────────────
async def test_fx_config_default_disabled(db):
    """غياب المستند ⇒ الافتراضات الموثّقة: fx_rates_enabled=False + قيم §12."""
    cfg = await db.fx_config.get()
    assert cfg.fx_rates_enabled is False, "المرحلة ١ افتراضيًّا (§2)"
    assert cfg.rate_min_egp == 5.50 and cfg.rate_max_egp == 8.00
    assert cfg.rate_min_tnd == 0.28 and cfg.rate_max_tnd == 0.45
    assert cfg.amount_tier_threshold_egp == 500000.0 and cfg.amount_tier_threshold_tnd == 200.0
    assert cfg.price_change_adaptation_deals == 10 and cfg.dynamic_deviation_lookback_deals == 50


async def test_fx_config_set_get_roundtrip(db):
    """set→get يحفظ القيم المعدّلة (upsert مستند مفرد)."""
    from core.models import FxRatesConfig
    await db.fx_config.set(FxRatesConfig(fx_rates_enabled=True, egp_room_jid="eg@g.us",
                                         tnd_room_jid="tn@g.us", rate_max_egp=7.75))
    cfg = await db.fx_config.get()
    assert cfg.fx_rates_enabled is True
    assert cfg.egp_room_jid == "eg@g.us" and cfg.tnd_room_jid == "tn@g.us"
    assert cfg.rate_max_egp == 7.75


async def test_fx_config_independent_from_detection(db):
    """fx_config وdetection مستندان مستقلّان في dashboard_config (مفتاحان مختلفان) — لا تصادم."""
    from core.models import DetectionConfig, FxRatesConfig
    await db.fx_config.set(FxRatesConfig(fx_rates_enabled=True))
    await db.detection.set(DetectionConfig(structuring_count_threshold=99))
    assert (await db.fx_config.get()).fx_rates_enabled is True           # لم يُدهَس
    assert (await db.detection.get()).structuring_count_threshold == 99  # ولا العكس
    assert await db.fx_config.col.count_documents({}) == 2, "مستندان بمفتاحين مختلفين"


async def test_fx_rates_endpoints_rbac(db):
    """GET للقارئ، PUT للمدير؛ PUT للمراجع 403؛ GET بلا دخول 401 (§14.3)."""
    pytest.importorskip("fastapi")
    from core.constants import Role
    from core.models import FxRatesConfig
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/settings/fx-rates")).status_code == 401
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        assert (await ac.get("/api/settings/fx-rates")).status_code == 200        # قراءة متاحة
        body = FxRatesConfig(fx_rates_enabled=True).model_dump(mode="json")
        assert (await ac.put("/api/settings/fx-rates", json=body)).status_code == 403  # لا يعدّل
    async with _client(db) as ac:
        r = await ac.get("/api/settings/fx-rates")
        assert r.status_code == 200 and r.json()["fx_rates_enabled"] is False
        body = FxRatesConfig(fx_rates_enabled=True, tnd_room_jid="tn@g.us").model_dump(mode="json")
        r2 = await ac.put("/api/settings/fx-rates", json=body)
        assert r2.status_code == 200 and r2.json()["fx_rates_enabled"] is True
        assert (await ac.get("/api/settings/fx-rates")).json()["tnd_room_jid"] == "tn@g.us"


# ── الخطوة ٥: تاريخ الأسعار + صفحات + deal_margin ────────────────────────────
async def test_fx_rates_history_rbac_and_returns_snapshots(db):
    pytest.importorskip("fastapi")
    from datetime import datetime
    from core.constants import Role
    from core.models import FxRateSnapshot
    await db.fx_rates.record(FxRateSnapshot(currency="EGP", rates={"vodafone": {"net": 6.1}},
                                            valid_from=datetime(2026, 7, 1, 10, 0, 0)))
    async with _anon_client(db) as ac:
        assert (await ac.get("/api/fx-rates/history")).status_code == 401
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.get("/api/fx-rates/history")                 # قراءة متاحة للمراجع
        assert r.status_code == 200 and len(r.json()) == 1
        assert r.json()[0]["currency"] == "EGP" and r.json()[0]["rates"]["vodafone"]["net"] == 6.1
    async with _client(db) as ac:
        assert (await ac.get("/api/fx-rates/history?currency=TND")).json() == []  # تصفية بالعملة


async def test_fx_and_entity_pages_served(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:                            # الصفحات تُقدَّم؛ الحماية في الـAPI
        assert (await ac.get("/fx-rates")).status_code == 200
        assert (await ac.get("/entity-aliases")).status_code == 200


async def test_deal_margin_in_timeline(db):
    pytest.importorskip("fastapi")
    from core.db import utcnow
    await db.deals.col.insert_one({"deal_id": "DMARGIN", "status": "completed",
                                   "deal_margin": 0.05, "created_at": utcnow(),
                                   "updated_at": utcnow(), "sell_leg": {"amount": 1000}})
    async with _client(db) as ac:
        r = await ac.get("/api/transfers/DMARGIN")
        assert r.status_code == 200 and r.json()["deal"]["deal_margin"] == 0.05


def test_production_app_serves_fx_and_entity_pages():
    """🔴 الإنتاج يُشغّل core.app:get_app (لا dashboard.app). لا بدّ أن مسارَي الصفحتين مسجّلان
    في core.app نفسه — تفادي تكرار خطأ «الصفحة 404 في الإنتاج رغم نجاح اختبارات dashboard.app»."""
    pytest.importorskip("fastapi")
    from core.app import create_app
    from core.config import Settings
    app = create_app(db=None, settings=Settings(_env_file=None), run_worker=False)
    paths = {r.path for r in app.routes}
    assert "/fx-rates" in paths, "core.app يجب أن يقدّم /fx-rates (الإنتاج = core.app لا dashboard.app)"
    assert "/entity-aliases" in paths, "core.app يجب أن يقدّم /entity-aliases"


# ══ تشغيل الكيرنل وهو مطفأ عبر الجسر (POST /pm2) ═══════════════════════════════
async def test_bridge_control_requires_manager(db):
    """توكن التحكّم سرٌّ ينفّذ أمر نظام — لا يُسلَّم لغير المدير ولا لغير مسجّل."""
    pytest.importorskip("fastapi")
    from core.constants import Role
    async with _anon_client(db) as ac:                      # بلا دخول → 401
        assert (await ac.get("/api/system/bridge-control")).status_code == 401
    async with _client(db, Role.REVIEWER, username="rev") as ac:   # مراجع → 403
        assert (await ac.get("/api/system/bridge-control")).status_code == 403


async def test_bridge_control_returns_url_and_token_for_manager(db):
    """المدير يحصل على وجهة الجسر + التوكن كي يبقى الزرّ عاملًا بعد موت الكيرنل."""
    pytest.importorskip("fastapi")
    st = _settings(bridge_control_token="tok-123",
                   whatsapp_bridge_url="http://localhost:3001")
    async with _client(db, settings=st) as ac:
        r = await ac.get("/api/system/bridge-control")
        assert r.status_code == 200
        body = r.json()
        assert body["url"] == "http://localhost:3001/pm2"
        assert body["token"] == "tok-123"
        assert body["enabled"] is True


async def test_bridge_control_disabled_without_token(db):
    """بلا BRIDGE_CONTROL_TOKEN → enabled=False (الواجهة تُعطّل الزرّ، fail-closed)."""
    pytest.importorskip("fastapi")
    async with _client(db, settings=_settings(bridge_control_token="")) as ac:
        body = (await ac.get("/api/system/bridge-control")).json()
        assert body["enabled"] is False
