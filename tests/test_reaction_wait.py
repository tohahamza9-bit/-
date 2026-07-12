"""
ضمان إرسال التفاعلات قبل الصفقة التالية (§8.3) — wait_for_reaction_sent.

بعد كتابة ✅/🔴 في الطابور ننتظر تأكيد إرسالها فعلًا (sent=True) قبل متابعة الصفقة التالية،
أو حتى المهلة (5ث) ثم تحذير ومتابعة (لا يوقف البوت). بلا جسر مُهيَّأ → لا انتظار (لا مُرسِل).
"""
from __future__ import annotations

from core.bus import Bus
from core.models import OutgoingMessage

CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
BRIDGE = "http://bridge.local"


def _bus(db, *, bridge: bool):
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN,
              bridge_url=BRIDGE if bridge else "")
    if bridge:
        # نُبقي _bridge_url مضبوطًا (كي يعمل الانتظار) لكن نُعطّل POST الفعليّ للجسر الوهميّ
        # (نختبر منطق الاستطلاع/الانتظار لا HTTP الجسر) — فلا مهام httpx خلفية تتسرّب.
        async def _noop() -> None:
            return None
        bus._flush_bridge = _noop
    return bus


async def _enqueue_reaction(db, key, emoji="✅"):
    await db.outgoing.enqueue(
        OutgoingMessage(chat_jid=CENTRAL, text="", reply_to_key=key, reaction=emoji))


# ═════════════════════════════════════════════════════════════════════════════
# pending_reactions — عدّ التفاعلات غير المُرسَلة لهذه المفاتيح
# ═════════════════════════════════════════════════════════════════════════════
async def test_pending_reactions_counts_only_unsent_reactions(db):
    await _enqueue_reaction(db, "k1")
    await _enqueue_reaction(db, "k2")
    # نصّ (بلا reaction) على نفس المفتاح — لا يُحتسَب
    await db.outgoing.enqueue(OutgoingMessage(chat_jid=CENTRAL, text="تنبيه", reply_to_key="k1"))
    assert await db.outgoing.pending_reactions(["k1", "k2"]) == 2

    # علّم k1 مُرسَلًا → يبقى واحد (k2)
    doc = await db.outgoing.col.find_one({"reply_to_key": "k1", "reaction": {"$ne": None}})
    await db.outgoing.mark_sent(doc["_id"])
    assert await db.outgoing.pending_reactions(["k1", "k2"]) == 1
    assert await db.outgoing.pending_reactions([]) == 0
    assert await db.outgoing.pending_reactions(["other"]) == 0


# ═════════════════════════════════════════════════════════════════════════════
# بلا جسر → لا انتظار (يرجع فورًا حتى مع تفاعلات غير مُرسَلة)
# ═════════════════════════════════════════════════════════════════════════════
async def test_wait_no_bridge_returns_immediately(db):
    bus = _bus(db, bridge=False)
    await _enqueue_reaction(db, "k1")                 # غير مُرسَل
    # مهلة كبيرة لكن بلا جسر → يرجع فورًا بلا انتظار (لو انتظر لتعطّل الاختبار)
    await bus.wait_for_reaction_sent(["k1"], timeout=999.0)


# ═════════════════════════════════════════════════════════════════════════════
# مع جسر: كل التفاعلات مُرسَلة → يرجع فورًا (بلا استطلاع/نوم)
# ═════════════════════════════════════════════════════════════════════════════
async def test_wait_returns_when_all_sent(db):
    bus = _bus(db, bridge=True)
    await _enqueue_reaction(db, "k1")
    doc = await db.outgoing.col.find_one({"reply_to_key": "k1"})
    await db.outgoing.mark_sent(doc["_id"])           # أُرسِل فعلًا
    await bus.wait_for_reaction_sent(["k1"], timeout=5.0)   # pending=0 → رجوع فوريّ
    assert await db.outgoing.pending_reactions(["k1"]) == 0


async def test_wait_no_reactions_returns_immediately(db):
    bus = _bus(db, bridge=True)
    await bus.wait_for_reaction_sent(["nokey"], timeout=5.0)   # لا تفاعلات أصلًا → رجوع فوريّ


# ═════════════════════════════════════════════════════════════════════════════
# مع جسر: تفاعل لا يُرسَل أبدًا → مهلة قصيرة ثم تحذير ومتابعة (لا توقّف)
# ═════════════════════════════════════════════════════════════════════════════
async def test_wait_times_out_and_continues(db, caplog):
    import logging
    bus = _bus(db, bridge=True)
    await _enqueue_reaction(db, "k1")                 # يبقى غير مُرسَل
    with caplog.at_level(logging.WARNING):
        await bus.wait_for_reaction_sent(["k1"], timeout=0.5)   # ~0.5ث ثم يتابع
    assert any("مهلة انتظار إرسال التفاعلات" in r.message for r in caplog.records)
    # لم يوقف البوت: التفاعل ما زال معلّقًا لكن الدالة عادت
    assert await db.outgoing.pending_reactions(["k1"]) == 1


# ═════════════════════════════════════════════════════════════════════════════
# التكامل: apply_mark ينتظر التفاعل (مع جسر) حتى يُعلَّم مُرسَلًا أثناء الانتظار
# ═════════════════════════════════════════════════════════════════════════════
async def test_apply_mark_waits_until_reaction_sent(db):
    import asyncio

    from core.constants import Mark, Status
    from core.matching.service import MatchingService
    from core.models import Deal, ParsedLeg
    from core.constants import OperationType
    from core.db import utcnow

    bus = _bus(db, bridge=True)
    matcher = MatchingService(db, bus, [], [])
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="A1",
                    amount=1000.0, source_message_key="m1")
    deal = Deal(deal_id="d1", status=Status.COMPLETED, created_at=utcnow(),
                updated_at=utcnow(), sell_leg=leg, source_message_keys=["m1"])

    # «مُرسِل» خلفيّ يعلّم تفاعل m1 مُرسَلًا بعد جزء من الثانية (يحاكي Baileys)
    async def _fake_sender():
        await asyncio.sleep(0.2)
        d = await db.outgoing.col.find_one({"reply_to_key": "m1", "reaction": {"$ne": None}})
        if d:
            await db.outgoing.mark_sent(d["_id"])

    task = asyncio.create_task(_fake_sender())
    await matcher.apply_mark(deal, Mark.DONE)          # يكتب ✅ ثم ينتظر تأكيد الإرسال
    await task

    # عاد بعد أن صار التفاعل مُرسَلًا (لا معلّقات)
    assert await db.outgoing.pending_reactions(["m1"]) == 0
    doc = await db.outgoing.col.find_one({"reply_to_key": "m1", "reaction": {"$ne": None}})
    assert doc["sent"] is True
