"""
اختبارات تصنيف الغرف في MongoDB (§2.2) — اكتشاف تلقائي + تصنيف بلا إعادة تشغيل.

يغطّي:
- RoomRepo.discover: اكتشاف metadata فقط، idempotent، لا يلمس تصنيفًا موجودًا.
- RoomRepo.seed_if_missing / jids_of_type: البذر لا يطمس تصنيفًا؛ القائمة تشمل النشط المصنّف فقط.
- MatchingService: يقرأ تصنيف الزبون/الخزينة من DB (hot-reload) مع احتياط seed من env.
- _seed_rooms_from_env: يبذر غرف env أول تشغيل.
- 🔴 حدود الكتابة (bus._guard) لا تتأثر بأي تصنيف في DB — تبقى من env (Option A).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.bus import Bus, OutputBlocked
from core.constants import Currency, OperationType, RoomType, Status, TreasuryType
from core.matching import MatchingService
from core.models import Deal, ParsedLeg, RawMessage, Room, SupplierRef, TreasuryRef

CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
CUST_ROOM = "cust-room@g.us"
TREAS_ROOM = "treas-room@g.us"
NEW_ROOM = "brand-new@g.us"
T0 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)


def make_bus(db) -> Bus:
    return Bus(db, allowed_jids={CENTRAL, ADMIN}, central_jid=CENTRAL, admin_jid=ADMIN)


def make_leg(**kw) -> ParsedLeg:
    base = dict(
        operation=OperationType.SELL,
        customer_code="53",
        customer_name="احمد العكاري",
        amount=8475.0,
        currency=Currency.EGP,
        phone="01115233493",
        reference_number="A6779",
        source_message_key="msg-1",
    )
    base.update(kw)
    return ParsedLeg(**base)


def make_deal(leg: ParsedLeg) -> Deal:
    return Deal(
        deal_id="deal-1", status=Status.MATCHING, sell_leg=leg,
        source_message_keys=[leg.source_message_key], created_at=T0, updated_at=T0,
    )


async def _seed_room_msg(db, chat_jid: str, key: str, text: str):
    await db.raw.insert(RawMessage(message_key=key, chat_jid=chat_jid, text=text, received_at=T0))


# ── RoomRepo.discover — metadata فقط، idempotent، لا يطمس التصنيف ──────────────
async def test_discover_creates_unclassified(db):
    await db.rooms.discover(NEW_ROOM, name="غرفة زبائن جديدة")
    room = await db.rooms.get(NEW_ROOM)
    assert room is not None
    assert room.type is RoomType.UNCLASSIFIED
    assert room.name == "غرفة زبائن جديدة"
    assert room.active is True
    assert room.discovered_at is not None


async def test_discover_is_idempotent_and_preserves_classification(db):
    # اكتشاف أولي
    await db.rooms.discover(NEW_ROOM, name="اسم أوّلي")
    # المشغّل صنّفها زبونًا
    await db.rooms.upsert(Room(jid=NEW_ROOM, type=RoomType.CUSTOMER, name="اسم أوّلي"), updated_by="dashboard")
    # اكتشاف لاحق (رسالة أخرى) — يجب ألا يعيدها unclassified
    await db.rooms.discover(NEW_ROOM, name="اسم محدّث")
    room = await db.rooms.get(NEW_ROOM)
    assert room.type is RoomType.CUSTOMER          # التصنيف محفوظ (setOnInsert)
    assert room.name == "اسم محدّث"                # الاسم يُحدَّث (metadata)


async def test_discover_no_name_leaves_name_null(db):
    await db.rooms.discover(NEW_ROOM)  # بلا اسم بعد (groups.upsert لم يصل)
    room = await db.rooms.get(NEW_ROOM)
    assert room.name is None
    assert room.type is RoomType.UNCLASSIFIED


# ── seed_if_missing / jids_of_type ────────────────────────────────────────────
async def test_seed_if_missing_does_not_overwrite(db):
    await db.rooms.upsert(Room(jid=CUST_ROOM, type=RoomType.TREASURY), updated_by="dashboard")
    await db.rooms.seed_if_missing(CUST_ROOM, RoomType.CUSTOMER.value)  # يجب ألا يغيّر التصنيف
    room = await db.rooms.get(CUST_ROOM)
    assert room.type is RoomType.TREASURY


async def test_jids_of_type_active_only(db):
    await db.rooms.upsert(Room(jid=CUST_ROOM, type=RoomType.CUSTOMER, active=True))
    await db.rooms.upsert(Room(jid="c2@g.us", type=RoomType.CUSTOMER, active=False))  # موقوفة
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY, active=True))
    customers = await db.rooms.jids_of_type(RoomType.CUSTOMER.value)
    assert customers == [CUST_ROOM]  # النشط المصنّف فقط
    treasuries = await db.rooms.jids_of_type(RoomType.TREASURY.value)
    assert treasuries == [TREAS_ROOM]


# ── المطابق يقرأ التصنيف من DB (hot-reload §شرط 5) ────────────────────────────
async def test_matcher_uses_db_classification(db):
    bus = make_bus(db)
    # لا seed في المُنشئ — التصنيف كلّه من DB
    svc = MatchingService(db, bus)
    svc._rooms_cache_ttl = 0  # قراءة حيّة دائمًا (يحاكي انتهاء cache القصير)

    await db.rooms.upsert(Room(jid=CUST_ROOM, type=RoomType.CUSTOMER))
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY))
    await _seed_room_msg(db, CUST_ROOM, "c1", "احمد العكاري 8475 استلم")
    await _seed_room_msg(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")

    deal = await svc.match_in_rooms(make_deal(make_leg()), now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is True
    assert deal.status is Status.MATCHED


async def test_matcher_hot_reload_new_room_without_restart(db):
    bus = make_bus(db)
    svc = MatchingService(db, bus)
    svc._rooms_cache_ttl = 0

    await _seed_room_msg(db, CUST_ROOM, "c1", "احمد العكاري 8475 استلم")
    await _seed_room_msg(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")

    # قبل التصنيف: لا غرف مصنّفة → لا مطابقة
    deal = await svc.match_in_rooms(make_deal(make_leg()), now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is False

    # المشغّل يصنّف الغرفتين أثناء التشغيل (نفس نسخة الخدمة) → تُلتقط بلا إعادة إنشاء
    await db.rooms.upsert(Room(jid=CUST_ROOM, type=RoomType.CUSTOMER))
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY))
    deal2 = await svc.match_in_rooms(make_deal(make_leg()), now=T0 + timedelta(seconds=12))
    assert deal2.matched_customer_room is True
    assert deal2.matched_treasury_room is True


async def test_matcher_seed_fallback_when_db_empty(db):
    # لا تصنيف في DB → يعود لقوائم env seed الممرّرة للمُنشئ (§شرط 4)
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM], treasury_room_jids=[TREAS_ROOM])
    svc._rooms_cache_ttl = 0
    await _seed_room_msg(db, CUST_ROOM, "c1", "احمد العكاري 8475 استلم")
    await _seed_room_msg(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")
    deal = await svc.match_in_rooms(make_deal(make_leg()), now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is True


# ── بذر env (§شرط 4) ─────────────────────────────────────────────────────────
async def test_seed_rooms_from_env(db):
    from types import SimpleNamespace

    from core.app import _seed_rooms_from_env

    settings = SimpleNamespace(
        central_room_jid=CENTRAL, admin_room_jid=ADMIN,
        customer_rooms=[CUST_ROOM], treasury_rooms=[TREAS_ROOM],
    )
    await _seed_rooms_from_env(db, settings)
    assert (await db.rooms.get(CENTRAL)).type is RoomType.CENTRAL
    assert (await db.rooms.get(ADMIN)).type is RoomType.ADMIN
    assert (await db.rooms.get(CUST_ROOM)).type is RoomType.CUSTOMER
    assert (await db.rooms.get(TREAS_ROOM)).type is RoomType.TREASURY


async def test_seed_rooms_from_env_preserves_manual_classification(db):
    from types import SimpleNamespace

    from core.app import _seed_rooms_from_env

    # المشغّل سبق وصنّف غرفة env كـ ignore — البذر يجب ألا يطمسها
    await db.rooms.upsert(Room(jid=CUST_ROOM, type=RoomType.IGNORE), updated_by="dashboard")
    settings = SimpleNamespace(
        central_room_jid=CENTRAL, admin_room_jid=ADMIN,
        customer_rooms=[CUST_ROOM], treasury_rooms=[],
    )
    await _seed_rooms_from_env(db, settings)
    assert (await db.rooms.get(CUST_ROOM)).type is RoomType.IGNORE


# ── غرفة المورد: المطابقة الثلاثية لطرف الشراء (buy_leg) بالرقم الإشاري + المبلغ ──
SUP_ROOM = "sup-room@g.us"


def _two_legged_deal() -> Deal:
    """صفقة طرفين: بيع (زبون 53 + خزينة كود 77) + شراء من مورد بنفس الرقم الإشاري."""
    sell = make_leg(
        treasury=TreasuryRef(code="77", name="أبو يوسف", type=TreasuryType.SELL_ONLY),
        source_message_key="sell-1",
    )
    buy = ParsedLeg(
        operation=OperationType.BUY,
        supplier=SupplierRef(code="760", name="طه"),
        amount=8391.0,
        currency=Currency.EGP,
        reference_number="A6779",           # نفس الرقم الإشاري للطرفين
        source_message_key="buy-1",
    )
    return Deal(
        deal_id="deal-2l", status=Status.MATCHING, sell_leg=sell, buy_leg=buy,
        is_two_legged=True, source_message_keys=["sell-1", "buy-1"],
        created_at=T0, updated_at=T0,
    )


async def _seed_three_rooms(db):
    await db.rooms.upsert(Room(jid=CUST_ROOM, type=RoomType.CUSTOMER))
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY, treasury_code="77"))
    await db.rooms.upsert(Room(jid=SUP_ROOM, type=RoomType.SUPPLIER))
    await _seed_room_msg(db, CUST_ROOM, "c1", "احمد العكاري 8475 استلم")
    await _seed_room_msg(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")


async def test_two_legged_matches_only_with_supplier_room(db):
    """صفقة طرفين: لا تكتمل 🔸 إلا بظهور طرف الشراء في غرفة المورد (رقم إشاري + مبلغ)."""
    bus = make_bus(db)
    svc = MatchingService(db, bus)
    svc._rooms_cache_ttl = 0
    await _seed_three_rooms(db)

    # زبون + خزينة موجودان، لكن لا رسالة في غرفة المورد بعد → لا اكتمال
    deal = await svc.match_in_rooms(_two_legged_deal(), now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is True
    assert deal.matched_supplier_room is False
    assert deal.status is Status.MATCHING

    # ظهر طرف الشراء في غرفة المورد بالرقم الإشاري + مبلغ الشراء → 🔸
    await _seed_room_msg(db, SUP_ROOM, "s1", "A6779 شراء 8391 من طه")
    deal2 = await svc.match_in_rooms(_two_legged_deal(), now=T0 + timedelta(seconds=12))
    assert deal2.matched_supplier_room is True
    assert deal2.status is Status.MATCHED


async def test_single_legged_ignores_supplier_room(db):
    """صفقة طرف واحد (لا buy_leg): المطابقة الثلاثية غير منطبقة → مورد=True لا يحجب."""
    bus = make_bus(db)
    svc = MatchingService(db, bus)
    svc._rooms_cache_ttl = 0
    await db.rooms.upsert(Room(jid=CUST_ROOM, type=RoomType.CUSTOMER))
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY))
    await _seed_room_msg(db, CUST_ROOM, "c1", "احمد العكاري 8475 استلم")
    await _seed_room_msg(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")

    deal = await svc.match_in_rooms(make_deal(make_leg()), now=T0 + timedelta(seconds=12))
    assert deal.matched_supplier_room is True   # غير منطبقة → لا تحجب
    assert deal.status is Status.MATCHED


# ── 🔴 حدود الكتابة لا تتأثر بتصنيف DB (Option A) ────────────────────────────
async def test_db_classification_does_not_grant_write(db):
    """تصنيف غرفة كـ central في DB لا يمنحها صلاحية الكتابة — الحارس من env فقط."""
    bus = make_bus(db)  # allowed = {CENTRAL, ADMIN} من env
    # نصنّف غرفة زبون كـ central في DB (سيناريو خطأ/تلاعب)
    await db.rooms.upsert(Room(jid=CUST_ROOM, type=RoomType.CENTRAL), updated_by="dashboard")
    # bus._guard ما زال يرفض الكتابة إليها (لا يقرأ DB)
    with pytest.raises(OutputBlocked):
        await bus.reply(CUST_ROOM, "نص", reply_to_key=None)
