"""
اختبارات المطابقة وبوابة الثقة والعلامات والتصعيد (الوكيل A3 — §8, §11.2).

تغطّي: تطابق الأسماء المتسامح، رفض المختلف تمامًا، بوابة الثقة، مطابقة المبلغ،
مطابقة الغرفتين → 🔸، والتذكير/التصعيد بأوقات صريحة. كل مخرجات البوت عبر Bus
(القاعدة §2.2) — نتحقّق أنّ لا كتابة في وجهة ممنوعة.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.bus import Bus, OutputBlocked
from core.constants import (
    Currency,
    Mark,
    OperationType,
    REMINDER_INTERVAL_SECONDS,
    RoomType,
    Status,
    TreasuryType,
)
from core.matching import (
    MatchingService,
    amount_matches,
    names_match,
    normalize_ar,
    trust_gate,
    verify_customer_name,
)
from core.models import Deal, ParsedLeg, RawMessage, Room, SupplierRef, TreasuryRef

# ── معرّفات ثابتة للاختبار ────────────────────────────────────────────────────
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
CUST_ROOM = "cust-room@g.us"
TREAS_ROOM = "treas-room@g.us"
TREAS_CODE = "77"                       # كود خزينة الصفقة (يربط الغرفة بالخزينة §8.1)
T0 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)

# خزينة الصفقة الافتراضية — كودها يطابق treasury_code لغرفة الخزينة المرتبطة.
TREAS = TreasuryRef(code=TREAS_CODE, name="ابو يوسف", type=TreasuryType.SELL_ONLY, currency=Currency.EGP)


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
        treasury=TREAS,                 # خزينة بكود → مطابقة الخزينة محصورة بغرفتها (§8.1)
    )
    base.update(kw)
    return ParsedLeg(**base)


async def _seed_treasury_room(db, jid: str = TREAS_ROOM, code: str = TREAS_CODE) -> None:
    """يربط غرفة خزينة في db.rooms بكود خزينة (§8.1) — شرط البحث المحصور في غرفة الصفقة."""
    await db.rooms.upsert(Room(jid=jid, type=RoomType.TREASURY, treasury_code=code, active=True))


def make_deal(leg: ParsedLeg, created_at: datetime = T0, **kw) -> Deal:
    return Deal(
        deal_id=kw.pop("deal_id", "d1"),
        status=kw.pop("status", Status.MATCHING),
        sell_leg=leg,
        created_at=created_at,
        updated_at=created_at,
        source_message_keys=[leg.source_message_key],
        **kw,
    )


async def _outgoing(db) -> list[dict]:
    return await db.outgoing.next_unsent(limit=100)


# ═══════════════════════════════════════════════════════════════════════════
# 1) التطبيع + تطابق الأسماء (§8.2, §3.4)
# ═══════════════════════════════════════════════════════════════════════════
def test_normalize_ar_unifies_and_strips():
    assert normalize_ar("أحمَد") == "احمد"          # همزة + تشكيل
    assert normalize_ar("فاطمة") == "فاطمه"          # تاء مربوطة
    assert normalize_ar("مصطفى") == "مصطفي"          # ألف مقصورة
    assert normalize_ar("  مروان   الشاوش ") == "مروان الشاوش"  # مسافات


@pytest.mark.parametrize("a,b", [
    ("الشاوش", "الشتوش"),           # خطأ إملائي (§8.2)
    ("أحمد", "أحمر"),               # خطأ إملائي (§8.2)
    ("بكر", "بوبكر"),               # اختصار (§11.2)
    ("بكر الهمالي", "بوبكر الهمالي"),
    ("مروان الشاوش", "مروان الشتوش"),
    ("محمد عبدالرحيم", "محمد عبد الرحيم"),  # اختلاف تقطيع
])
def test_names_match_tolerant(a, b):
    assert names_match(a, b) is True


@pytest.mark.parametrize("a,b", [
    ("مروان الشاوش", "خالد العماري"),   # مختلف تمامًا — ليس خطأ إملاء (§8.2)
    ("احمد العكاري", "طه"),
    ("", "احمد"),                        # فارغ
    ("احمد", ""),
])
def test_names_match_rejects_different(a, b):
    assert names_match(a, b) is False


# ═══════════════════════════════════════════════════════════════════════════
# 2) مطابقة المبلغ (§8.2 — المركزية هي المرجع)
# ═══════════════════════════════════════════════════════════════════════════
def test_amount_matches():
    assert amount_matches(990, 990) is True
    assert amount_matches(990, 980) is False        # 990 مركزية vs 980 خزينة (§8.2)
    assert amount_matches(1600.0, 1600.0) is True
    assert amount_matches(None, 990) is False
    assert amount_matches(990, None) is False


# ═══════════════════════════════════════════════════════════════════════════
# 3) بوابة الثقة (§8.2)
# ═══════════════════════════════════════════════════════════════════════════
def test_trust_gate_ok_code_and_amount():
    ok, reason = trust_gate(make_leg())
    assert ok is True and reason is None


def test_trust_gate_holds_missing_amount():
    ok, reason = trust_gate(make_leg(amount=None))
    assert ok is False and reason  # سبب غير فارغ


def test_trust_gate_tolerates_name_typo():
    # الكود + المبلغ مقروءان → يمشي رغم اسم مشكوك (الكود حاسم §8.2)
    ok, _ = trust_gate(make_leg(customer_name="اسم غلط تمامًا"))
    assert ok is True


def test_trust_gate_holds_missing_code_and_name():
    ok, reason = trust_gate(make_leg(customer_code=None, customer_name="  "))
    assert ok is False and reason


def test_trust_gate_passes_missing_code_with_clear_name():
    # كود ناقص لكن اسم واضح → لا يعلّق على هذه البوابة (§8.2: يحتاج الاثنين معًا)
    ok, _ = trust_gate(make_leg(customer_code=None, customer_name="احمد العكاري"))
    assert ok is True


def test_trust_gate_holds_explicit_conflict():
    # المبلغ بعد الخصم أكبر من قبله → تعارض صريح
    ok, reason = trust_gate(make_leg(amount=1000.0, amount_after_discount=1200.0))
    assert ok is False and reason


# ═══════════════════════════════════════════════════════════════════════════
# 4) تحقّق اسم الزبون (§11.2)
# ═══════════════════════════════════════════════════════════════════════════
def test_verify_customer_name_approx_ok():
    # «بكر الهمالي» في المركزية مقابل «بوبكر الهمالي» في MONEYADO (§11.2)
    ok, _ = verify_customer_name("526", "بوبكر الهمالي", "بكر الهمالي")
    assert ok is True


def test_verify_customer_name_empty_display_holds():
    ok, _ = verify_customer_name("526", "", "بكر الهمالي")
    assert ok is False


def test_verify_customer_name_totally_different_holds():
    ok, _ = verify_customer_name("999", "سالم القذافي", "بكر الهمالي")
    assert ok is False


def test_verify_customer_name_no_expected_trusts_code():
    # لا اسم متوقّع للمقارنة → يُعتمد الكود (الكود أضمن §11.2)
    ok, _ = verify_customer_name("526", "بوبكر الهمالي", None)
    assert ok is True


# ═══════════════════════════════════════════════════════════════════════════
# 5) مطابقة الغرفتين → 🔸 (§8.1)
# ═══════════════════════════════════════════════════════════════════════════
async def _seed_room(db, chat_jid: str, key: str, text: str):
    await db.raw.insert(RawMessage(
        message_key=key, chat_jid=chat_jid, text=text, received_at=T0,
    ))


async def _seed_room_at(db, chat_jid: str, key: str, text: str, received_at: datetime):
    await db.raw.insert(RawMessage(
        message_key=key, chat_jid=chat_jid, text=text, received_at=received_at,
    ))


async def test_match_in_rooms_both_marks_matched(db):
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)                 # غرفة الخزينة مرتبطة بكود خزينة الصفقة

    # غرفة الزبون: اسم + مبلغ ؛ غرفة الخزينة: مبلغ + هاتف (§8.1)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475 استلم")
    await _seed_room(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")

    deal = make_deal(make_leg())
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))

    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is True
    assert deal.status is Status.MATCHED
    assert deal.mark is Mark.MATCHED

    out = await _outgoing(db)
    reactions = [o for o in out if o.get("reaction") == Mark.MATCHED.value]
    assert len(reactions) == 1
    assert reactions[0]["chat_jid"] == CENTRAL          # على المركزية فقط
    assert reactions[0]["reply_to_key"] == "msg-1"


async def test_match_in_rooms_one_missing_stays_matching(db):
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)                 # الغرفة مرتبطة لكن بلا رسالة الحوالة

    # غرفة الزبون فقط — رسالة الخزينة غائبة (رغم وجود الغرفة المرتبطة)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")

    deal = make_deal(make_leg())
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))

    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is False
    assert deal.status is Status.MATCHING
    assert deal.mark is None
    # لا علامة 🔸 صدرت
    out = await _outgoing(db)
    assert not [o for o in out if o.get("reaction")]


# ── ربط غرفة الخزينة بخزينة الصفقة تحديدًا (treasury_code §8.1) ────────────────
async def test_treasury_match_only_searches_linked_room(db):
    # الحوالة ظاهرة في غرفة خزينة أخرى (كود مختلف) لا غرفة خزينة الصفقة → لا يُحتسب.
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db, jid=TREAS_ROOM, code=TREAS_CODE)          # غرفة الصفقة (77) — بلا رسالة
    await _seed_treasury_room(db, jid="other-treas@g.us", code="99")        # غرفة خزينة أخرى (99)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")
    await _seed_room(db, "other-treas@g.us", "t9", "تحويل 8475 على 01115233493")  # الحوالة هنا فقط

    deal = make_deal(make_leg())                                            # خزينة الصفقة كودها 77
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is False      # لم تُبحث الغرفة الأخرى (99)
    assert deal.status is Status.MATCHING


async def test_treasury_no_linked_room_needs_manual_confirm(db):
    # لا غرفة مرتبطة بخزينة الصفقة (مثل «بلاس فون») → لا اعتماد تلقائي؛ اعتماد يدوي («تم») (§8.1).
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])          # لا نبذر أي غرفة خزينة
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")

    deal = make_deal(make_leg())
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is False      # لا غرفة → لا اعتماد تلقائي
    assert deal.treasury_no_room is True
    assert deal.status is Status.MATCHING           # معلّقة تنتظر «تم»
    # لا 🔸 صدرت (لم تكتمل المطابقة)
    assert not [o for o in await _outgoing(db) if o.get("reaction")]


async def test_treasury_no_room_alerts_once_then_escalates(db):
    # خزينة بلا غرفة: تنبيه اعتماد يدوي (مرة) في المركزية ثم تصعيد للمسؤول بعد 15 دقيقة (§8.1).
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")

    deal = make_deal(make_leg())
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))
    assert deal.treasury_no_room is True and deal.status is Status.MATCHING

    # داخل نافذة المطابقة (< 15s) → لا تنبيه بعد
    deal = await svc.escalation_tick(deal, now=T0 + timedelta(seconds=5))
    assert deal.reminders_sent == 0 and not await _outgoing(db)

    # بعد النافذة → تنبيه اعتماد يدوي واحد (Reply في المركزية على رسالة الحوالة)
    deal = await svc.escalation_tick(deal, now=T0 + timedelta(seconds=20))
    assert deal.reminders_sent == 1
    assert deal.status is Status.HELD
    out = await _outgoing(db)
    assert len(out) == 1
    assert out[0]["chat_jid"] == CENTRAL and out[0]["reply_to_key"] == "msg-1"
    assert "لا غرفة مرتبطة" in out[0]["text"] and "تم" in out[0]["text"]
    assert not out[0].get("reaction")               # تنبيه نصّي لا تفاعل

    # قبل مرور 15 دقيقة → لا تصعيد ولا تكرار للتنبيه
    t1 = T0 + timedelta(seconds=20)
    deal = await svc.escalation_tick(deal, now=t1 + timedelta(seconds=REMINDER_INTERVAL_SECONDS - 5))
    assert deal.reminders_sent == 1 and deal.status is Status.HELD
    assert len(await _outgoing(db)) == 1            # لا تنبيه ثانٍ

    # بعد 15 دقيقة بلا «تم» → تصعيد لغرفة المسؤول + إغلاق
    deal = await svc.escalation_tick(deal, now=t1 + timedelta(seconds=REMINDER_INTERVAL_SECONDS))
    assert deal.status is Status.ESCALATED
    out = await _outgoing(db)
    admin = [o for o in out if o["chat_jid"] == ADMIN]
    assert len(admin) == 1 and admin[0]["reply_to_key"] == "msg-1"
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in out)   # §2.2 لا وجهة ممنوعة


async def test_treasury_no_room_confirm_via_reply_proceeds(db):
    # «تم» من موظف معتمد (تجاوز بشري §8.1 بند 6) يضبط الغرفتين True — نتحقّق من الآلية مباشرة.
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")

    deal = make_deal(make_leg())
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))
    deal = await svc.escalation_tick(deal, now=T0 + timedelta(seconds=20))
    assert deal.status is Status.HELD                # معلّقة تنتظر «تم»
    # HELD ليست حالة نهائية تحجب «تم» — يمرّ التجاوز البشري ويُكمل الإدخال (Pipeline._handle_control).


async def test_sell_and_buy_treasury_auto_matches_without_room(db):
    # 🔴 خزينة خارجية sell_and_buy (خصم1%) بلا غرفة → اعتماد تلقائي (لا «تم» يدوي، قرار صاحب العمل)
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")
    leg = make_leg(treasury=TreasuryRef(
        code="85", name="خصم 1%", type=TreasuryType.SELL_AND_BUY, currency=Currency.EGP))
    deal = await svc.match_in_rooms(make_deal(leg), now=T0 + timedelta(seconds=12))
    assert deal.matched_treasury_room is True    # اعتماد تلقائي
    assert deal.treasury_no_room is False        # لا «تم» يدوي
    assert deal.matched_customer_room is True
    assert deal.status is Status.MATCHED


async def test_sell_and_buy_treasury_no_manual_confirm_alert(db):
    # لا تنبيه «تم» ولا تصعيد للخزينة الخارجية (بعكس sell_only بلا غرفة)
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")
    leg = make_leg(treasury=TreasuryRef(
        code="85", name="خصم 1%", type=TreasuryType.SELL_AND_BUY, currency=Currency.EGP))
    deal = await svc.match_in_rooms(make_deal(leg), now=T0 + timedelta(seconds=12))
    deal = await svc.escalation_tick(deal, now=T0 + timedelta(seconds=20))
    # MATCHED نهائية → لا تذكير/تصعيد، ولا رسالة «تم»
    assert deal.status is Status.MATCHED
    assert not [o for o in await _outgoing(db) if "تم" in (o.get("text") or "")]


async def test_treasury_linked_room_found_when_transfer_present(db):
    # غرفة خزينة الصفقة موجودة والحوالة ظهرت فيها → خزينة=True (بحث فعلي في الغرفة المرتبطة).
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")
    await _seed_room(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")

    deal = make_deal(make_leg())
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))
    assert deal.matched_treasury_room is True
    assert deal.status is Status.MATCHED


# ── غرفة المورد: صفقة طرفين بلا غرفة مورد مضافة → «تم» يدوي (نظير الخزينة §8) ──
SUP_ROOM = "sup-room@g.us"


def _make_two_legged(**kw) -> Deal:
    """صفقة طرفين: بيع (زبون + خزينة كودها مرتبط بغرفة) + شراء من مورد بنفس الرقم الإشاري."""
    sell = make_leg()
    buy = ParsedLeg(
        operation=OperationType.BUY,
        supplier=SupplierRef(code="760", name="طه"),
        amount=8391.0, currency=Currency.EGP,
        reference_number="A6779", source_message_key="msg-1",
    )
    return make_deal(sell, buy_leg=buy, is_two_legged=True, deal_id="d2l", **kw)


async def test_two_legged_no_supplier_room_needs_manual_confirm(db):
    # صفقة طرفين وزبون+خزينة موجودان لكن لا غرفة مورد مضافة → لا اعتماد تلقائي؛ «تم» يدوي (§8).
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)                                # غرفة الخزينة (code 77) موجودة
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")
    await _seed_room(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")

    deal = await svc.match_in_rooms(_make_two_legged(), now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True and deal.matched_treasury_room is True
    assert deal.matched_supplier_room is False
    assert deal.supplier_no_room is True
    assert deal.status is Status.MATCHING                        # معلّقة تنتظر «تم»
    assert not [o for o in await _outgoing(db) if o.get("reaction")]   # لا 🔸


async def test_two_legged_no_supplier_room_alerts_once_then_escalates(db):
    # بلا غرفة مورد: تنبيه اعتماد يدوي (مرة) في المركزية ثم تصعيد للمسؤول بعد 15 دقيقة (§8).
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")
    await _seed_room(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")

    deal = await svc.match_in_rooms(_make_two_legged(), now=T0 + timedelta(seconds=12))
    assert deal.supplier_no_room is True and deal.status is Status.MATCHING

    # بعد النافذة → تنبيه اعتماد يدوي واحد (Reply في المركزية على رسالة الحوالة)
    deal = await svc.escalation_tick(deal, now=T0 + timedelta(seconds=20))
    assert deal.reminders_sent == 1 and deal.status is Status.HELD
    out = await _outgoing(db)
    assert len(out) == 1
    assert out[0]["chat_jid"] == CENTRAL and out[0]["reply_to_key"] == "msg-1"
    assert "لا غرفة مورد" in out[0]["text"] and "تم" in out[0]["text"]
    assert not out[0].get("reaction")                           # تنبيه نصّي لا تفاعل

    # بعد 15 دقيقة بلا «تم» → تصعيد لغرفة المسؤول
    t1 = T0 + timedelta(seconds=20)
    deal = await svc.escalation_tick(deal, now=t1 + timedelta(seconds=REMINDER_INTERVAL_SECONDS))
    assert deal.status is Status.ESCALATED
    out = await _outgoing(db)
    admin = [o for o in out if o["chat_jid"] == ADMIN]
    assert len(admin) == 1 and admin[0]["reply_to_key"] == "msg-1"
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in out)  # §2.2 لا وجهة ممنوعة


async def test_two_legged_supplier_room_found_completes_match(db):
    # غرفة مورد مضافة وطرف الشراء ظهر فيها (رقم إشاري + مبلغ الشراء) → 🔸 مكتملة.
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    svc._rooms_cache_ttl = 0
    await _seed_treasury_room(db)
    await db.rooms.upsert(Room(jid=SUP_ROOM, type=RoomType.SUPPLIER, active=True))
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")
    await _seed_room(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493")
    await _seed_room(db, SUP_ROOM, "s1", "A6779 شراء 8391 من طه")

    deal = await svc.match_in_rooms(_make_two_legged(), now=T0 + timedelta(seconds=12))
    assert deal.supplier_no_room is False
    assert deal.matched_supplier_room is True
    assert deal.status is Status.MATCHED


# ── صيغة الخصم: مبلغ منفصل لكل غرفة (§6.3) — الزبون قبل الخصم، الخزينة بعده ─────
async def test_discount_customer_before_treasury_after(db):
    # الزبون يُطابَق بالمبلغ قبل الخصم (8550)، الخزينة بالمبلغ بعده (8465) → الغرفتان تتطابقان.
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)                                # غرفة الخزينة (code 77)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8550")   # الزبون: قبل الخصم
    await _seed_room(db, TREAS_ROOM, "t1", "احمد العكاري 8465")  # الخزينة: بعد الخصم

    leg = make_leg(amount=8550.0, amount_after_discount=8465.0)
    deal = await svc.match_in_rooms(make_deal(leg), now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is True
    assert deal.status is Status.MATCHED


async def test_discount_treasury_uses_after_not_before(db):
    # الخزينة تبحث بعد الخصم حصرًا: لو الغرفة حملت المبلغ قبل الخصم (8550) → لا تطابق.
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8550")
    await _seed_room(db, TREAS_ROOM, "t1", "احمد العكاري 8550")  # الخزينة بالمبلغ قبل الخصم

    leg = make_leg(amount=8550.0, amount_after_discount=8465.0)
    deal = await svc.match_in_rooms(make_deal(leg), now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is False      # الخزينة تبحث 8465 لا 8550
    assert deal.status is Status.MATCHING


async def test_discount_customer_uses_before_not_after(db):
    # الزبون يبحث قبل الخصم حصرًا: لو غرفة الزبون حملت المبلغ بعد الخصم (8465) → لا تطابق.
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8465")   # الزبون بالمبلغ بعد الخصم
    await _seed_room(db, TREAS_ROOM, "t1", "احمد العكاري 8465")

    leg = make_leg(amount=8550.0, amount_after_discount=8465.0)
    deal = await svc.match_in_rooms(make_deal(leg), now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is False      # الزبون يبحث 8550 لا 8465
    assert deal.matched_treasury_room is True


async def test_si_discount_treasury_matches_after_amount(db):
    # 🔴 SI بخصم: غرفة الخزينة (ابو يوسف) تنشر المبلغ **بعد** الخصم (20271) → تُطابَق به،
    #    بينما غرفة الزبون بالمبلغ قبله (20475). (رُفع استثناء SI عن amount_after_discount.)
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 20475")          # الزبون: قبل الخصم
    await _seed_room(db, TREAS_ROOM, "t1", "احمد العكاري القيمة: 20271 ج.م")  # الخزينة: بعد الخصم

    leg = make_leg(amount=20475.0, amount_after_discount=20271.0, is_si_format=True)
    deal = await svc.match_in_rooms(make_deal(leg), now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True     # الزبون طابق 20475 (قبل)
    assert deal.matched_treasury_room is True     # الخزينة طابقت 20271 (بعد)
    assert deal.status is Status.MATCHED


async def test_si_plain_treasury_uses_amount_unchanged(db):
    # SI عادية (بلا خصم، amount_after_discount=None) → الخزينة تُطابَق بالمبلغ نفسه (بلا تغيير).
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)
    await _seed_room(db, CUST_ROOM, "c1", "احمد العكاري 8475")
    await _seed_room(db, TREAS_ROOM, "t1", "احمد العكاري 8475")

    leg = make_leg(is_si_format=True)             # amount=8475 (افتراضي)، بلا amount_after_discount
    deal = await svc.match_in_rooms(make_deal(leg), now=T0 + timedelta(seconds=12))
    assert deal.matched_treasury_room is True
    assert deal.status is Status.MATCHED


# ── نافذة البحث الزمنية ±ساعة حول deal.created_at (§8.1) ──────────────────────
async def test_room_match_ignores_message_outside_window(db):
    # رسالة الزبون بعيدة (ساعتان) عن وقت الحوالة → تُتجاهل رغم تطابق الاسم/المبلغ
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)
    await _seed_room_at(db, CUST_ROOM, "cold", "احمد العكاري 8475", T0 + timedelta(hours=2))
    await _seed_room_at(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493", T0)

    deal = make_deal(make_leg(), created_at=T0)
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is False      # خارج ±ساعة → لا مطابقة
    assert deal.status is Status.MATCHING


async def test_room_match_within_window_ok(db):
    # رسالتان ضمن ±ساعة (±30 دقيقة حول created_at) → مطابقة الغرفتين → 🔸
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)
    await _seed_room_at(db, CUST_ROOM, "c1", "احمد العكاري 8475", T0 + timedelta(minutes=30))
    await _seed_room_at(db, TREAS_ROOM, "t1", "تحويل 8475 على 01115233493", T0 - timedelta(minutes=30))

    deal = make_deal(make_leg(), created_at=T0)
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is True
    assert deal.status is Status.MATCHED


# ── المبلغ عبر أسطر: الهاتف فوق المبلغ لا يُلصَق به (§8.2) ─────────────────────
def test_amount_in_text_not_glued_across_newlines():
    # رسالة غرفة مرتّبة أسطرًا: الهاتف ثم المبلغ. المبلغ 53000 يُلتقَط، لا يُلصَق بالهاتف.
    text = "فودافون\n01887777881\n53.000 مصري\nصافي"
    assert MatchingService._amount_in_text(53000.0, text) is True
    assert MatchingService._amount_in_text(6000.0, text) is False   # رقم آخر لا يُطابَق


async def test_match_multiline_room_messages(db):
    # 🔴 حالة الإنتاج (A30): رسائل غرف متعددة الأسطر (هاتف فوق المبلغ) → مطابقة الغرفتين → 🔸
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM])
    await _seed_treasury_room(db)
    await _seed_room(db, CUST_ROOM, "c1", "فودافون\n01887777881\n53.000 مصري\nصافي")
    await _seed_room(db, TREAS_ROOM, "t1", "A30\nفودافون\n01887777881\n53.000 مصري\nصافي")

    leg = make_leg(amount=53000.0, phone="01887777881", customer_name="ايهاب ابو حميد",
                   reference_number="A30")
    deal = make_deal(leg, created_at=T0)
    deal = await svc.match_in_rooms(deal, now=T0 + timedelta(seconds=12))
    assert deal.matched_customer_room is True
    assert deal.matched_treasury_room is True
    assert deal.status is Status.MATCHED
    reactions = [o for o in await _outgoing(db) if o.get("reaction") == Mark.MATCHED.value]
    assert len(reactions) == 1 and reactions[0]["chat_jid"] == CENTRAL


# ═══════════════════════════════════════════════════════════════════════════
# 6) التذكير والتصعيد بأوقات صريحة (§8.1 بند 5–7)
# ═══════════════════════════════════════════════════════════════════════════
async def test_escalation_reminders_then_admin(db):
    bus = make_bus(db)
    svc = MatchingService(db, bus, customer_room_jids=[CUST_ROOM], treasury_room_jids=[TREAS_ROOM])
    deal = make_deal(make_leg(), status=Status.MATCHING)

    # داخل نافذة المطابقة (10–15s) → لا تذكير بعد
    deal = await svc.escalation_tick(deal, now=T0 + timedelta(seconds=5))
    assert deal.reminders_sent == 0
    assert not await _outgoing(db)

    # بعد انقضاء النافذة → تذكير أول «غير موجودة» (Reply في المركزية)
    deal = await svc.escalation_tick(deal, now=T0 + timedelta(seconds=20))
    assert deal.reminders_sent == 1
    assert deal.status is Status.HELD
    out = await _outgoing(db)
    assert len(out) == 1 and out[0]["chat_jid"] == CENTRAL and out[0]["reply_to_key"] == "msg-1"

    # قبل مرور 15 دقيقة → لا تذكير ثانٍ
    t2 = T0 + timedelta(seconds=20)
    deal = await svc.escalation_tick(deal, now=t2 + timedelta(seconds=REMINDER_INTERVAL_SECONDS - 5))
    assert deal.reminders_sent == 1

    # بعد 15 دقيقة → تذكير ثانٍ
    deal = await svc.escalation_tick(deal, now=t2 + timedelta(seconds=REMINDER_INTERVAL_SECONDS))
    assert deal.reminders_sent == 2
    t3 = t2 + timedelta(seconds=REMINDER_INTERVAL_SECONDS)

    # بعد 15 دقيقة أخرى بلا رد → غرفة المسؤول + إغلاق في الدفتر (ESCALATED)
    deal = await svc.escalation_tick(deal, now=t3 + timedelta(seconds=REMINDER_INTERVAL_SECONDS))
    assert deal.status is Status.ESCALATED

    out = await _outgoing(db)
    central = [o for o in out if o["chat_jid"] == CENTRAL]
    admin = [o for o in out if o["chat_jid"] == ADMIN]
    assert len(central) == 2          # تذكيران
    assert len(admin) == 1            # تصعيد واحد
    # لا كتابة في أي وجهة ممنوعة (§2.2)
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in out)


async def test_escalation_ignores_terminal_deal(db):
    bus = make_bus(db)
    svc = MatchingService(db, bus)
    deal = make_deal(make_leg(), status=Status.MATCHED)
    deal = await svc.escalation_tick(deal, now=T0 + timedelta(hours=2))
    assert deal.reminders_sent == 0
    assert not await _outgoing(db)


# ═══════════════════════════════════════════════════════════════════════════
# 7) وضع العلامات (§8.3)
# ═══════════════════════════════════════════════════════════════════════════
async def test_apply_mark_warn_is_reply_with_reason(db):
    bus = make_bus(db)
    svc = MatchingService(db, bus)
    deal = make_deal(make_leg(), hold_reason="كود ناقص واسم ملتبس")
    await svc.apply_mark(deal, Mark.WARN)
    out = await _outgoing(db)
    assert len(out) == 1
    assert out[0]["chat_jid"] == CENTRAL
    assert out[0]["reply_to_key"] == "msg-1"
    assert not out[0].get("reaction")            # ⚠️ = Reply لا تفاعل
    assert "كود ناقص" in out[0]["text"]


async def test_apply_mark_done_is_silent_reaction(db):
    bus = make_bus(db)
    svc = MatchingService(db, bus)
    deal = make_deal(make_leg())
    await svc.apply_mark(deal, Mark.DONE)
    out = await _outgoing(db)
    assert len(out) == 1
    assert out[0]["reaction"] == Mark.DONE.value
    assert out[0]["chat_jid"] == CENTRAL


# ═══════════════════════════════════════════════════════════════════════════
# 8) القاعدة §2.2 — لا كتابة في غرف الزبائن/الخزائن مطلقًا
# ═══════════════════════════════════════════════════════════════════════════
async def test_bus_blocks_room_output(db):
    bus = make_bus(db)
    with pytest.raises(OutputBlocked):
        await bus.reply(CUST_ROOM, "ممنوع", reply_to_key="x")
    with pytest.raises(OutputBlocked):
        await bus.react(TREAS_ROOM, "x", "🔸")
