"""
طبقة حلّ الخزينة من قروب الخزينة (نسخة مخففة، الأولوية ألّا تقف حوالة).

القاعدة: خزينةٌ لم تُحلّ من النص → بحثٌ في رسائل قروبات الخزائن ضمن النافذة عن رسالة تطابق
**هاتف + قيمة** (الإجمالي أو ×0.99 صافي، بهامش تقريب). المرجع تعزيزٌ لا شرط. الأقرب زمنيًّا يُؤخَذ.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.constants import OperationType, RoomType, Status, TreasuryType
from core.models import Deal, ParsedLeg, Room, TreasuryRecord

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
TROOM = "troom82@g.us"
PHONE = "01012345678"


class _RecBus:
    def __init__(self):
        self.central_jid = CENTRAL
        self.admin_jid = ADMIN
        self.admin_msgs = []

    async def notify_admin(self, text, reply_to_key=None, forward_key=None):
        self.admin_msgs.append(text)

    async def reply_central(self, text, reply_to_key=None, *, is_alert=False):
        pass


def _pipe(db, bus=None, room_match_enabled=True):
    from core.pipeline import Pipeline

    class _W:
        name = "w"
        async def write(self, job, *, commit): ...
    p = Pipeline(db, bus or _RecBus(), _W(), None, customer_room_jids=[], treasury_room_jids=[])
    p._room_match_enabled = room_match_enabled
    p._room_match_window = 50.0
    return p


async def _seed(db, *, room_msgs):
    """خزينة code 82 + قروبها + رسائل القروب، وحوالة بلا خزينة (هاتف+قيمة)."""
    await db.treasuries.upsert(TreasuryRecord(name="قروب اوس", code="82", type=TreasuryType.SELL_ONLY))
    await db.rooms.upsert(Room(jid=TROOM, type=RoomType.TREASURY, treasury_code="82",
                               name="قروب اوس", active=True))
    for i, (text, offset) in enumerate(room_msgs):
        await db.raw.col.insert_one({
            "message_key": f"rm{i}", "chat_jid": TROOM, "text": text,
            "received_at": (NOW + timedelta(seconds=offset)).replace(tzinfo=None), "processed": False})


def _deal(amount=1000.0, phone=PHONE, ref="X1301"):
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code="149",
                    customer_name="زبون", amount=amount, phone=phone, price_normalized="6.0",
                    treasury=None, sender_jid="emp@lid")
    return Deal(deal_id="d1", status=Status.PARSED, sell_leg=leg, created_at=NOW, updated_at=NOW,
                first_received_at=NOW, chat_jid=CENTRAL, source_message_keys=["d1-a"])


async def _treas(db):
    return await db.treasuries.all_active()


async def test_room_match_gross_value(db):
    """هاتف + قيمة إجمالية مطابقة في القروب → تُحلّ الخزينة (code 82) + تنبيه «حُلّت من قروب»."""
    await _seed(db, room_msgs=[(f"X1301\n{PHONE}\nالقيمة 1000 دت\n149 زبون", 5)])
    bus = _RecBus()
    pipe = _pipe(db, bus)
    d = _deal(amount=1000.0)
    assert await pipe._room_match_treasury(d, NOW, await _treas(db), []) is True
    assert d.sell_leg.treasury is not None and d.sell_leg.treasury.code == "82"
    assert any("حُلّت الخزينة من قروب" in m for m in bus.admin_msgs)
    assert any(dv.get("method") == "room_match" for dv in d.sell_leg.deviation_log)


async def test_room_match_discounted_value(db):
    """قيمة القروب صافية بعد خصم 1% (990 = 1000×0.99) → تُقبل وتُحلّ."""
    await _seed(db, room_msgs=[(f"X1301\n{PHONE}\nالقيمة 990 دت\n149 زبون", 3)])
    pipe = _pipe(db)
    d = _deal(amount=1000.0)
    assert await pipe._room_match_treasury(d, NOW, await _treas(db), []) is True
    assert d.sell_leg.treasury.code == "82"


async def test_room_match_without_reference(db):
    """رسالة القروب بلا رقم إشاري → المطابقة بالهاتف+القيمة تكفي (المرجع تعزيز لا شرط)."""
    await _seed(db, room_msgs=[(f"{PHONE}\nالقيمة 1000 دت\n149 زبون", 4)])
    pipe = _pipe(db)
    d = _deal(amount=1000.0)
    assert await pipe._room_match_treasury(d, NOW, await _treas(db), []) is True


async def test_room_match_message_earlier_by_seconds(db):
    """رسالة القروب أسبق من ختم الوصول بثوانٍ (ضمن النافذة) → تُمسَك."""
    await _seed(db, room_msgs=[(f"X1301\n{PHONE}\nالقيمة 1000 دت\n149 زبون", -8)])
    pipe = _pipe(db)
    d = _deal(amount=1000.0)
    assert await pipe._room_match_treasury(d, NOW, await _treas(db), []) is True


async def test_room_match_disabled_switch(db):
    """المفتاح مطفأ → الطبقة معطّلة (لا حلّ من القروب)."""
    await _seed(db, room_msgs=[(f"X1301\n{PHONE}\nالقيمة 1000 دت\n149 زبون", 5)])
    pipe = _pipe(db, room_match_enabled=False)
    d = _deal(amount=1000.0)
    assert await pipe._room_match_treasury(d, NOW, await _treas(db), []) is False
    assert d.sell_leg.treasury is None


async def test_room_match_no_match_outside_window(db):
    """رسالة القروب خارج النافذة (بعد 200s) → لا مطابقة (تُترك للتصعيد الحالي)."""
    await _seed(db, room_msgs=[(f"X1301\n{PHONE}\nالقيمة 1000 دت\n149 زبون", 200)])
    pipe = _pipe(db)
    d = _deal(amount=1000.0)
    assert await pipe._room_match_treasury(d, NOW, await _treas(db), []) is False


async def test_room_match_two_groups_closest_and_note(db):
    """ظهر في قروبين ضمن النافذة → يُؤخَذ الأقرب زمنيًّا + ملاحظة «قروبات» للمراجعة (لا يوقف الحوالة)."""
    await db.treasuries.upsert(TreasuryRecord(name="قروب اوس", code="82", type=TreasuryType.SELL_ONLY))
    await db.treasuries.upsert(TreasuryRecord(name="قروب باسل", code="83", type=TreasuryType.SELL_ONLY))
    await db.rooms.upsert(Room(jid=TROOM, type=RoomType.TREASURY, treasury_code="82", name="قروب اوس", active=True))
    await db.rooms.upsert(Room(jid="troom83@g.us", type=RoomType.TREASURY, treasury_code="83", name="قروب باسل", active=True))
    # القروب 83 أقرب زمنيًّا (Δ2s) من 82 (Δ20s)
    await db.raw.col.insert_one({"message_key": "m82", "chat_jid": TROOM,
        "text": f"X1301\n{PHONE}\nالقيمة 1000 دت", "received_at": (NOW + timedelta(seconds=20)).replace(tzinfo=None), "processed": False})
    await db.raw.col.insert_one({"message_key": "m83", "chat_jid": "troom83@g.us",
        "text": f"X1301\n{PHONE}\nالقيمة 1000 دت", "received_at": (NOW + timedelta(seconds=2)).replace(tzinfo=None), "processed": False})
    bus = _RecBus()
    pipe = _pipe(db, bus)
    d = _deal(amount=1000.0)
    assert await pipe._room_match_treasury(d, NOW, await _treas(db), []) is True
    assert d.sell_leg.treasury.code == "83"                          # الأقرب زمنيًّا
    assert any("قروبات" in m for m in bus.admin_msgs)               # ملاحظة التعدّد
