"""
إصلاح ٢ (§7.3 §10): مفتاح الرسالة الثانية في مسار دمج الخصم عبر خانة المُرسِل (absorb_second_into).

كانت حوالة الخصم التي تكتمل عبر خانة المُرسِل تفقد مفتاح رسالتها الثانية من source_message_keys
(leg.source_message_key يبقى None قبل _merge_discount)، فيفشل إلغاؤها/تعديلها بالرد على الرسالة
الثانية بـ«لم يُعثر على الحوالة» رغم سلامتها 100% (A9030، A9062، A9079).
"""
from __future__ import annotations

import contextlib
import io
from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.models import RawMessage, WriteResult
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 12, 19, 41, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
S1 = "sender1@lid"


class _W:
    name = "w"

    async def write(self, job, *, commit):
        return WriteResult(ok=True)


class _V:
    enabled = False

    async def verify_transaction(self, *a, **k):
        return (False, None)

    async def find_last_pending(self, *a, **k):
        return None


def _pipeline(db):
    return Pipeline(db, Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN), _W(), _V(),
                    customer_room_jids=[], treasury_room_jids=[])


async def _run_burst(pipe, msgs, *, base=NOW, gap=0.3, now_offset=6):
    for i, (text, key, sender) in enumerate(msgs):
        await pipe.capture(RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=sender,
                                      text=text, received_at=base + timedelta(seconds=i * gap)))
    with contextlib.redirect_stderr(io.StringIO()):
        await pipe.process_inbox(base + timedelta(seconds=now_offset))


async def _deal_by_ref(db, ref):
    async for d in db.deals.col.find({"sell_leg.reference_number": ref}):
        return d
    return None


# ═════════════════════════════════════════════════════════════════════════════
# خصم شكل جديد يكتمل عبر absorb_second_into → مفتاحا الرسالتين مسجَّلان
# ═════════════════════════════════════════════════════════════════════════════
async def test_discount_via_sender_slot_records_both_keys(db):
    """A9500 (خصم شكل جديد): هوية (msg1) ثم تسوية (msg2) بنفس المرجع → تُدمَج خصمًا عبر خانة
    المُرسِل. يجب أن يحوي source_message_keys **مفتاحي الرسالتين**، وأن يعثر find_by_source_key
    على الصفقة بمفتاح **الرسالة الثانية** (شرط نجاح الإلغاء/التعديل بالرد عليها)."""
    pipe = _pipeline(db)
    await _run_burst(pipe, [
        ("A9500\n01127173416 فودافون 29720 جنيه مصري\n1284 مروان الشاوش 5.98", "a500a", S1),
        ("A9500\n01127173416 فودافون 29.423 جنيه مصري\nبلاس", "a500b", S1),
    ])
    d = await _deal_by_ref(db, "A9500")
    assert d is not None
    sl = d["sell_leg"]
    assert sl["customer_code"] == "1284" and sl["commission"] == -297.0   # الخصم محتسَب (بعد−قبل)
    keys = d.get("source_message_keys") or []
    assert "a500a" in keys, "مفتاح رسالة الهوية مفقود"
    assert "a500b" in keys, "مفتاح الرسالة الثانية مفقود (باغ إصلاح ٢)"

    # الرد على الرسالة الثانية (للإلغاء/التعديل) يعثر على الصفقة
    found = await db.deals.find_by_source_key("a500b")
    assert found is not None and found.deal_id == d["deal_id"], \
        "find_by_source_key بمفتاح الرسالة الثانية لم يعثر على الصفقة → سيفشل الإلغاء بـ«لم يُعثر»"


async def test_cancellation_reply_on_second_message_of_discount(db):
    """إلغاء بالرد على الرسالة الثانية لحوالة خصم مكتملة → يُعثر عليها (لا «لم يُعثر»)."""
    from core.constants import Status
    from core.models import BotControl
    await db.control.set(BotControl(storage_enabled=True, state="running"), "test")
    pipe = _pipeline(db)
    await _run_burst(pipe, [
        ("A9501\n01127173416 فودافون 29720 جنيه مصري\n1284 مروان الشاوش 5.98", "a501a", S1),
        ("A9501\n01127173416 فودافون 29.423 جنيه مصري\nبلاس", "a501b", S1),
    ])
    # ادفع الصفقة إلى COMPLETED عبر النبضة (كتابة وهمية ناجحة)
    with contextlib.redirect_stderr(io.StringIO()):
        await pipe.tick(NOW + timedelta(seconds=8))
    d = await _deal_by_ref(db, "A9501")
    # الرد على الرسالة الثانية بكلمة «الغاء»
    cancel = RawMessage(message_key="cxl", chat_jid=CENTRAL, sender_jid=S1,
                        text="الغاء", received_at=NOW + timedelta(seconds=20),
                        reply_to_key="a501b")
    await pipe.capture(cancel)
    with contextlib.redirect_stderr(io.StringIO()):
        await pipe.process_inbox(NOW + timedelta(seconds=22))
    # لا رسالة «لم يُعثر» في الصادر
    outs = await db.outgoing.next_unsent(200)
    not_found = [o for o in outs if "لم يُعثر" in (o.get("text") or "")]
    assert not not_found, "ظهرت «لم يُعثر» رغم أنّ الحوالة سليمة (باغ إصلاح ٢ لم يُحَل)"
