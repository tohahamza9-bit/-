"""
اختبارات لوحة V2 المرحلة ١ — سجل الحوالات + قائمة الانتباه (قراءة فقط).

يغطّي: البحث، تجميع الخط الزمني (خام←مُستخرَج←تعديل←دفتر←إلغاء) وترتيبه، ترتيب الانتباه
(إلحاح ثم FIFO)، «صعّد» يُدرِج في طابور outgoing (is_alert، وجهة المسؤول)، «علّم كمراجَع»
(أوّل مُراجِع يثبت + مرئيّ)، وRBAC الفعليّ في الـ backend.

لا يكتب على deals ولا يلمس pipeline — يتحقّق فقط من طبقة القراءة والإجراءين الآمنين.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta

import pytest

from core.constants import Currency, OperationType, Role, Status
from core.db import utcnow
from core.models import LedgerEntry, RawMessage, UserRecord


# ─────────────────────────────────────────────────────────────────────────────
# مساعدات
# ─────────────────────────────────────────────────────────────────────────────
def _settings(**over):
    from core.config import Settings
    return Settings(_env_file=None, **over)


async def _seed_user(db, username, password, role):
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
async def _client(db, role=Role.MANAGER, *, username="admin", password="pw-123456", settings=None):
    await _seed_user(db, username, password, role)
    async with _anon_client(db, settings) as ac:
        r = await ac.post("/api/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200
        yield ac


def _leg(**over):
    leg = {"operation": "sell", "amount": 1600, "amount_after_discount": 1584,
           "commission": -16, "currency": "EGP", "phone": "01093232832",
           "reference_number": "A6779", "customer_name": "مروان الشاوش", "customer_code": "1284",
           "treasury": {"code": "74", "name": "بلاس فون", "type": "sell_only", "currency": "EGP"}}
    leg.update(over)
    return leg


async def _seed_deal(db, deal_id, *, status=Status.HELD, created_at=None, sell_leg=None, **extra):
    now = created_at or utcnow()
    doc = {"deal_id": deal_id, "status": status.value, "created_at": now, "updated_at": now,
           "first_received_at": now, "is_two_legged": False,
           "sell_leg": sell_leg if sell_leg is not None else _leg(),
           "source_message_keys": [], "amendments": []}
    doc.update(extra)
    await db.deals.col.insert_one(doc)


# ─────────────────────────────────────────────────────────────────────────────
# 1) البحث (قراءة مباشرة عبر الوحدة)
# ─────────────────────────────────────────────────────────────────────────────
async def test_search_by_ref_phone_name(db):
    from dashboard import transfers
    await _seed_deal(db, "D1", sell_leg=_leg(reference_number="A6779", phone="0101", customer_name="مروان"))
    await _seed_deal(db, "D2", sell_leg=_leg(reference_number="A5169", phone="0102", customer_name="فداء"))
    by_ref = await transfers.search_transfers(db, "A6779")
    assert [d["deal_id"] for d in by_ref] == ["D1"]
    by_phone = await transfers.search_transfers(db, "0102")
    assert [d["deal_id"] for d in by_phone] == ["D2"]
    by_name = await transfers.search_transfers(db, "مروان")
    assert [d["deal_id"] for d in by_name] == ["D1"]
    all_recent = await transfers.search_transfers(db, "")
    assert {d["deal_id"] for d in all_recent} == {"D1", "D2"}


# ─────────────────────────────────────────────────────────────────────────────
# 2) الخط الزمني الكامل + ترتيبه
# ─────────────────────────────────────────────────────────────────────────────
async def test_timeline_full_history_ordered(db):
    from dashboard import transfers
    t0 = utcnow()
    await db.raw.insert(RawMessage(message_key="m1", chat_jid="c@g.us",
                                   text="1284 مروان 1600", received_at=t0))
    await _seed_deal(
        db, "D9", status=Status.CANCELLED, created_at=t0 + timedelta(seconds=1),
        source_message_keys=["m1"],
        amendments=[{"amended_at": t0 + timedelta(seconds=2), "amended_by_key": "m2",
                     "old_net": 1584, "new_net": 1500, "old_commission": -16,
                     "new_commission": -20, "reason": "تعديل"}],
        cancelled_at=t0 + timedelta(seconds=5), cancellation_reason="إلغاء بطلب الزبون")
    await db.ledger.append(LedgerEntry(entry_id="e1", deal_id="D9", message_key="m1",
                                       operation=OperationType.SELL, amount=1584,
                                       currency=Currency.EGP, status=Status.COMPLETED,
                                       created_at=t0 + timedelta(seconds=3)))
    # القيد العكسيّ يُكتب مع الإلغاء (قبل تثبيت حالة الإلغاء)
    await db.ledger.append(LedgerEntry(entry_id="e2", deal_id="D9", message_key="m1",
                                       operation=OperationType.BUY, is_reversal=True, amount=1500,
                                       currency=Currency.EGP, status=Status.CANCELLED,
                                       created_at=t0 + timedelta(seconds=4)))

    tl = await transfers.get_timeline(db, "D9")
    assert tl is not None
    assert [r["message_key"] for r in tl["raw_messages"]] == ["m1"]
    assert tl["extracted"]["sell"]["amount"] == 1600
    assert len(tl["amendments"]) == 1
    assert tl["amendments"][0]["old_net"] == 1584 and tl["amendments"][0]["new_net"] == 1500
    assert len(tl["ledger"]) == 2 and any(e["is_reversal"] for e in tl["ledger"])
    assert tl["cancellation"]["cancellation_reason"] == "إلغاء بطلب الزبون"
    # الترتيب الزمنيّ: خام ← مُستخرَج ← تعديل ← دفتر ← دفتر(عكسيّ) ← إلغاء
    assert [e["type"] for e in tl["timeline"]] == [
        "raw", "extracted", "amendment", "ledger", "ledger", "cancellation"]


async def test_timeline_missing_returns_none(db):
    from dashboard import transfers
    assert await transfers.get_timeline(db, "NOPE") is None


# ─────────────────────────────────────────────────────────────────────────────
# 3) ترتيب قائمة الانتباه: إلحاح ثم الأقدم أوّلًا (FIFO)
# ─────────────────────────────────────────────────────────────────────────────
async def test_attention_order_urgency_then_fifo(db):
    from dashboard import transfers
    base = utcnow()
    # HELD أقدم وأحدث، + ESCALATED + TECH_FAILED + SELL_DONE
    await _seed_deal(db, "held_new", status=Status.HELD, created_at=base + timedelta(minutes=5))
    await _seed_deal(db, "held_old", status=Status.HELD, created_at=base)
    await _seed_deal(db, "esc", status=Status.ESCALATED, created_at=base)
    await _seed_deal(db, "tech", status=Status.TECH_FAILED, created_at=base)
    await _seed_deal(db, "sell_done", status=Status.SELL_DONE, created_at=base)
    # حوالة غير محتاجة انتباه (لا تظهر)
    await _seed_deal(db, "completed", status=Status.COMPLETED, created_at=base)

    items = await transfers.attention_items(db, utcnow())
    assert [it["deal_id"] for it in items] == [
        "held_old", "held_new", "esc", "tech", "sell_done"]
    assert all(it["age_seconds"] is not None for it in items)
    assert "completed" not in {it["deal_id"] for it in items}


# ─────────────────────────────────────────────────────────────────────────────
# 4) HTTP: «صعّد» يُدرِج تنبيه المالك في طابور outgoing (is_alert، وجهة المسؤول)
# ─────────────────────────────────────────────────────────────────────────────
async def test_escalate_enqueues_owner_alert(db):
    pytest.importorskip("fastapi")
    s = _settings(admin_room_jid="admin@g.us")
    await _seed_deal(db, "D5", status=Status.HELD)
    async with _client(db, Role.MANAGER, settings=s) as ac:
        r = await ac.post("/api/attention/D5/escalate")
        assert r.status_code == 200 and r.json()["escalated"] is True
    msgs = await db.outgoing.next_unsent(10)
    assert len(msgs) == 1
    assert msgs[0]["chat_jid"] == "admin@g.us"
    assert msgs[0]["is_alert"] is True
    assert "تصعيد" in msgs[0]["text"] and "admin" in msgs[0]["text"]  # باسم المُصعِّد


async def test_escalate_missing_admin_jid_503(db):
    pytest.importorskip("fastapi")
    await _seed_deal(db, "D6", status=Status.HELD)
    async with _client(db, Role.MANAGER, settings=_settings()) as ac:  # لا admin_room_jid
        r = await ac.post("/api/attention/D6/escalate")
        assert r.status_code == 503


async def test_escalate_unknown_deal_404(db):
    pytest.importorskip("fastapi")
    async with _client(db, Role.MANAGER, settings=_settings(admin_room_jid="a@g.us")) as ac:
        assert (await ac.post("/api/attention/NOPE/escalate")).status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# 5) «علّم كمراجَع»: أوّل مُراجِع يثبت + مرئيّ للجميع
# ─────────────────────────────────────────────────────────────────────────────
async def test_review_first_reviewer_sticks_and_visible(db):
    pytest.importorskip("fastapi")
    await _seed_deal(db, "D7", status=Status.HELD)
    # المراجِع الأول
    async with _client(db, Role.REVIEWER, username="rev1") as ac:
        r = await ac.post("/api/attention/D7/review", json={"note": "تحقّقت"})
        assert r.status_code == 200 and r.json()["reviewed_by"] == "rev1"
    # مراجِع ثانٍ لا يدهس الأول (منع ازدواج)
    async with _client(db, Role.MANAGER, username="mgr") as ac:
        r = await ac.post("/api/attention/D7/review", json={})
        assert r.status_code == 200 and r.json()["reviewed_by"] == "rev1"
    # مرئيّ في قائمة الانتباه باسم المُراجِع
    from dashboard import transfers
    items = await transfers.attention_items(db, utcnow())
    d7 = next(it for it in items if it["deal_id"] == "D7")
    assert d7["review"]["reviewed_by"] == "rev1" and d7["review"]["note"] == "تحقّقت"


# ─────────────────────────────────────────────────────────────────────────────
# 6) RBAC فعليّ في الـ backend
# ─────────────────────────────────────────────────────────────────────────────
async def test_transfers_rbac_enforced(db):
    pytest.importorskip("fastapi")
    s = _settings(admin_room_jid="a@g.us")
    await _seed_deal(db, "D8", status=Status.HELD)
    # مجهول → 401 على القراءة والإجراء
    async with _anon_client(db, s) as ac:
        assert (await ac.get("/api/attention")).status_code == 401
        assert (await ac.get("/api/transfers")).status_code == 401
        assert (await ac.post("/api/attention/D8/escalate")).status_code == 401
    # مراجع → يقرأ ويصعّد ويراجع
    async with _client(db, Role.REVIEWER, username="rev", settings=s) as ac:
        assert (await ac.get("/api/attention")).status_code == 200
        assert (await ac.get("/api/transfers")).status_code == 200
        assert (await ac.post("/api/attention/D8/escalate")).status_code == 200
        assert (await ac.post("/api/attention/D8/review", json={})).status_code == 200
    # data_entry → 403 على كل شيء
    async with _client(db, Role.DATA_ENTRY, username="de", settings=s) as ac:
        assert (await ac.get("/api/attention")).status_code == 403
        assert (await ac.get("/api/transfers")).status_code == 403
        assert (await ac.post("/api/attention/D8/escalate")).status_code == 403


async def test_transfer_pages_served(db):
    pytest.importorskip("fastapi")
    async with _anon_client(db) as ac:
        assert (await ac.get("/attention")).status_code == 200
        assert (await ac.get("/transfers")).status_code == 200
        assert "قائمة الانتباه" in (await ac.get("/attention")).text
