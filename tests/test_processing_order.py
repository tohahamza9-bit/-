"""
جدولة المعالجة بترتيب وصول الرسالة الأولى (§7.3) — لا بوقت الاكتمال.

المشكلة: زوج (A + ثانيته) قد يكتمل متأخّرًا في نفس الدفعة، فلو عولج لحظة اكتماله لتجاوز
رسالةً مستقلّة (SI) وصلت قبل رسالته الأولى. الإصلاح: تُجمَع الصفقات الجاهزة أثناء المرور،
ثم تُعالَج (تُكتب) **مرتّبةً بـ first_received_at** = ختم وصول رسالتها الأولى، بعد فرز الدفعة.

المعيار الحاسم: A التي وصلت أولاها **قبل** SI تُكتب قبلها ولو اكتملت **بعدها** (ثانيتها لاحقة).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.constants import Status
from core.models import BotControl, RawMessage, WriteResult
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
S1 = "sender1@s.whatsapp.net"
S2 = "sender2@s.whatsapp.net"

# زوج A: ترويسة (رقم إشاري + هاتف + مبلغ) ثم ثانية (كود + اسم + سعر + خزينة) → صفقة مفردة تُكتب
A_HEADER = "A7351\nفودافون\n01025642842\n3950 مصري"
A_SECOND = "1500 احمد علي 5.90\nبلس"
# حوالة SI مكتملة برسالة واحدة (رقم إشاري SI2301، خزينة «بلاس فون») → تُكتب مباشرة
SI_COMPLETE = (
    "رقم العملية: SI2301\nرقم المستلم: 01093232832\n"
    "اسم الزبون: مروان الشاوش كود 1284\nالقيمة قبل الخصم: 3540 ج.م\n"
    "القيمة بعد الخصم 1%: 3505 ج.م\nالسعر: 5.9\nنوع التحويل: فودافون كاش\n"
    "الخزينة: بلاس فون"
)


class _RecordingWriter:
    """يسجّل ترتيب الكتابة الفعليّ عبر الرقم الإشاري لكل طرف."""
    name = "rec"

    def __init__(self):
        self.order: list[str] = []

    async def write(self, job, *, commit):
        self.order.append(job.leg.reference_number)
        return WriteResult(ok=True)


class _StubVerifier:
    enabled = False

    async def verify_transaction(self, *a, **k):
        return (False, None)

    async def find_last_pending(self, *a, **k):
        return None


def _pipeline(db, writer):
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    return Pipeline(db, bus, writer, _StubVerifier(),
                    customer_room_jids=[], treasury_room_jids=[])


def _raw(key, text, sender, at):
    return RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=sender,
                      text=text, received_at=at)


async def _enable_storage(db):
    await db.control.set(BotControl(storage_enabled=True, state="running"), "test")


# ═════════════════════════════════════════════════════════════════════════════
# SI وصلت قبل ترويسة الزوج → تُكتب SI أولًا (رغم اكتمال الزوج في نفس الدورة)
# ═════════════════════════════════════════════════════════════════════════════
async def test_si_before_pair_writes_first(db):
    await _enable_storage(db)
    writer = _RecordingWriter()
    pipe = _pipeline(db, writer)
    # SI في T+5، ثم ترويسة الزوج T+6 وثانيته T+8 (تكتمل متأخّرة في نفس الدفعة)
    await pipe.capture(_raw("si", SI_COMPLETE, S2, NOW + timedelta(seconds=5)))
    await pipe.capture(_raw("hdr", A_HEADER, S1, NOW + timedelta(seconds=6)))
    await pipe.capture(_raw("sec", A_SECOND, S1, NOW + timedelta(seconds=8)))

    await pipe.process_inbox(NOW + timedelta(seconds=10))

    # SI (وصلت T+5) قبل A7351 (وصلت أولاها T+6) — الترتيب بوصول الأولى لا بالاكتمال
    assert writer.order == ["SI2301", "A7351"]


# ═════════════════════════════════════════════════════════════════════════════
# المعيار الحاسم: ترويسة الزوج وصلت قبل SI لكنها تكتمل **بعدها** → تُكتب الزوج أولًا
# (الترتيب بوصول الرسالة الأولى، لا بوقت الاكتمال)
# ═════════════════════════════════════════════════════════════════════════════
async def test_pair_first_message_before_si_writes_first_despite_late_completion(db):
    await _enable_storage(db)
    writer = _RecordingWriter()
    pipe = _pipeline(db, writer)
    # ترويسة الزوج T+5 (تنتظر ثانيتها)، ثم SI T+6 (مكتملة فورًا)، ثم ثانية الزوج T+7
    await pipe.capture(_raw("hdr", A_HEADER, S1, NOW + timedelta(seconds=5)))
    await pipe.capture(_raw("si", SI_COMPLETE, S2, NOW + timedelta(seconds=6)))
    await pipe.capture(_raw("sec", A_SECOND, S1, NOW + timedelta(seconds=7)))

    await pipe.process_inbox(NOW + timedelta(seconds=10))

    # A7351 (وصلت أولاها T+5) قبل SI2301 (T+6) — رغم أنّ الزوج اكتمل عند T+7 بعد SI
    assert writer.order == ["A7351", "SI2301"]


# ═════════════════════════════════════════════════════════════════════════════
# ثبات: first_received_at لا يُستبدَل بختم الرسالة الثانية عند الدمج
# ═════════════════════════════════════════════════════════════════════════════
async def test_first_received_at_preserved_across_merge(db):
    writer = _RecordingWriter()
    pipe = _pipeline(db, writer)
    await pipe.capture(_raw("hdr", A_HEADER, S1, NOW + timedelta(seconds=5)))
    await pipe.capture(_raw("sec", A_SECOND, S1, NOW + timedelta(seconds=7)))
    await pipe.process_inbox(NOW + timedelta(seconds=10))

    d = await db.deals.find_by_source_key("hdr")
    assert d is not None
    # الختم = وصول الترويسة (T+5)، لا وصول الثانية (T+7)
    assert d.first_received_at.replace(tzinfo=timezone.utc) == NOW + timedelta(seconds=5)
