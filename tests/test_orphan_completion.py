# -*- coding: utf-8 -*-
"""حفظ التكملة الكاملة اليتيمة (بلاغ X1918).

الجذر: في دفعةٍ واحدة الثانية تصل تكملةٌ كاملةٌ («كود اسم سعر») **قبل** ميلاد صفقتها
(صفقتها مؤجَّلة للذكاء AI-first ~5ث). كان مسار التكملة الكاملة يُسقِطها صامتةً حين
`cands=[]` — بخلاف الجزء المجرّد («بلس») الذي يحفظه `absorb_fragment`. الإصلاح: تُحفَظ
ردًّا معلّقًا فيسحبها `_pull_pending_reply` لحظة ميلاد الصفقة (نفس النبضة، بلا انتظار).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.constants import PENDING_REPLY_MAX_SECONDS, Status
from core.models import RawMessage, WriteResult
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
EMP = "20100@s.whatsapp.net"


class _FakeWriter:
    def __init__(self):
        self.calls = []

    async def write(self, job, *, commit):
        self.calls.append((job.operation.value, commit))
        return WriteResult(ok=True)


class _StubVerifier:
    enabled = False

    async def verify_transaction(self, *a, **k):
        return (False, None)

    async def find_last_pending(self, *a, **k):
        return None


def _pipeline(db):
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    return Pipeline(db, bus, _FakeWriter(), _StubVerifier(),
                    customer_room_jids=[], treasury_room_jids=[]), bus


def _raw(key, text, at):
    return RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=EMP,
                      text=text, received_at=at, reply_to_key=None)


# «553 شركة الزهراء 5.92 / البراق 5.95» — تكملة كاملة بهوية زبون، بلا مرجع صريح.
COMPLETION = "553 شركة الزهراء 5.92\nطه 5.93"
# حوالة X بلا هوية زبون → تنتظر طرفًا ثانيًا.
TRANSFER = "X1918\n01028767002\n5850 ج م\nفودافون كاش"


async def _find_deal(db, ref):
    async for d in db.deals.col.find({}):
        leg = d.get("sell_leg") or {}
        if leg.get("reference_number") == ref:
            return d
    return None


# نبضة بعد نافذة الاستقرار (STABILIZE_MIN=60ث) كي تُعالَج الرسائل فعليًّا.
STABLE = timedelta(seconds=120)


# ═══════════════════════════════════════════════════════════════════════════
# (١) التكملة تصل قبل صفقتها (نفس الدفعة) → تُحفَظ → تُسحَب عند ميلاد الصفقة
# ═══════════════════════════════════════════════════════════════════════════
async def test_orphan_completion_saved_then_pulled_on_deal_birth(db):
    pipe, bus = _pipeline(db)
    # الدفعة كما في X1918: التكملة أوّلًا (received_at أقدم)، ثم حوالتها بعد ثوانٍ قليلة.
    await pipe.capture(_raw("comp1", COMPLETION, NOW))
    await pipe.capture(_raw("x1918", TRANSFER, NOW + timedelta(seconds=3)))
    # نبضةٌ واحدة تعالج الاثنتين بترتيب الوصول: التكملة تُحفَظ ثم تُسحَب عند ميلاد الصفقة.
    await pipe.process_inbox(NOW + STABLE)

    d = await _find_deal(db, "X1918")
    assert d is not None
    leg = d["sell_leg"]
    assert leg.get("customer_code") == "553", "لم تُسحَب التكملة عند ميلاد الصفقة (عطل X1918)"
    assert leg.get("customer_name") and "الزهراء" in leg["customer_name"]
    # استُهلكت التكملة (لم تعد معلّقة قابلة للسحب)
    pr = await db.pending_replies.col.find_one({"message_key": "comp1"})
    assert pr is None or pr.get("consumed") is True


# ═══════════════════════════════════════════════════════════════════════════
# (٢) التكملة وحدها (بلا حوالتها) → تُحفَظ ردًّا معلّقًا (لا تُسقَط)
# ═══════════════════════════════════════════════════════════════════════════
async def test_orphan_completion_saved_when_alone(db):
    pipe, bus = _pipeline(db)
    await pipe.capture(_raw("comp2", COMPLETION, NOW))
    await pipe.process_inbox(NOW + STABLE)
    saved = await db.pending_replies.col.find_one({"message_key": "comp2"})
    assert saved is not None, "التكملة اليتيمة أُسقِطت بدل حفظها (عطل X1918)"
    assert saved["leg"].get("customer_code") == "553"


# ═══════════════════════════════════════════════════════════════════════════
# (٣) تكملة بلا مرشّح بعد 90 ثانية → تُصعَّد (لا تُسقَط صامتةً)
# ═══════════════════════════════════════════════════════════════════════════
async def test_orphan_completion_escalates_after_ttl(db):
    pipe, bus = _pipeline(db)
    T = NOW + STABLE
    await pipe.capture(_raw("comp3", COMPLETION, NOW))
    await pipe.process_inbox(T)                       # تُحفَظ ردًّا معلّقًا (received_at=T)
    assert await db.pending_replies.col.find_one({"message_key": "comp3"}) is not None

    # لا تصل صفقتها — بعد المهلة، النبضة تُصعّدها
    await pipe.tick(T + timedelta(seconds=PENDING_REPLY_MAX_SECONDS + 5))
    outs = await db.outgoing.next_unsent(100)
    esc = [o for o in outs if o["chat_jid"] == CENTRAL and "الزهراء" in (o.get("text") or "")]
    assert esc, "التكملة اليتيمة انتهت مهلتها بلا تصعيد — سقوط صامت"


# ═══════════════════════════════════════════════════════════════════════════
# (٤) عكسيّ: جزء مجرّد («بلس») بلا هوية يبقى هدرزةً صامتة عند انتهاء المهلة
# ═══════════════════════════════════════════════════════════════════════════
async def test_bare_fragment_still_silent_after_ttl(db):
    pipe, bus = _pipeline(db)
    T = NOW + STABLE
    await pipe.capture(_raw("bare1", "بلس", NOW))
    await pipe.process_inbox(T)
    await pipe.tick(T + timedelta(seconds=PENDING_REPLY_MAX_SECONDS + 5))
    outs = await db.outgoing.next_unsent(100)
    assert not [o for o in outs if "رسالته الأولى" in (o.get("text") or "")
                or "رسالتها الأولى" in (o.get("text") or "")], \
        "جزء مجرّد بلا هوية صُعِّد — يجب أن يبقى صامتًا"


# ═══════════════════════════════════════════════════════════════════════════
# (٥) الالتباس (>1 مرشّح) لا يُحفَظ يتيمًا — يبقى للتحكيم/التعليق
# ═══════════════════════════════════════════════════════════════════════════
async def test_orphan_save_only_when_no_candidates(db):
    pipe, bus = _pipeline(db)
    # حوالتان معلّقتان من نفس المُرسِل بلا هوية → مرشّحان لأي تكملة عديمة المرجع
    await pipe.capture(_raw("t1", "X2001\n01000000001\n1000 ج م\nفودافون كاش", NOW))
    await pipe.capture(_raw("t2", "X2002\n01000000002\n2000 ج م\nفودافون كاش", NOW + timedelta(seconds=1)))
    await pipe.capture(_raw("comp4", COMPLETION, NOW + timedelta(seconds=90)))
    await pipe.process_inbox(NOW + STABLE)
    assert await db.pending_replies.col.find_one({"message_key": "comp4"}) is None, \
        "التكملة حُفِظت يتيمةً رغم وجود مرشّحين — يجب أن تمرّ للتحكيم"
