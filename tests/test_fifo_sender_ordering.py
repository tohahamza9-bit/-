"""
FIFO صارم بخانة المُرسِل (§7.3) — الحجب بمفتاح المُرسِل لا الغرفة.

المبدأ: كل الحوالات في غرفة المركزية، فحجب الغرفة كاملةً عند أوّل رسالة غير مستقرّة يوقف
الطابور بأكمله (A8975 غير المستقرّة تحجب A8976 وSI). الحلّ: الحجب لكل مُرسِل على حدة:

  - مُرسِلان مختلفان مستقلّان تمامًا: رسالة أحدهما غير المستقرّة لا تؤجّل الآخر.
  - حوالة SI مستقلّة (برسالة واحدة §4.5): تنزل فور استقرارها ولو كان المُرسِل نفسه محجوبًا.
  - القيد الوحيد: نفس المُرسِل — رسائله التالية تنتظر حتى تستقرّ/تُغلَق رسالته السابقة.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.constants import Status
from core.models import RawMessage, WriteResult
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
S1 = "sender1@s.whatsapp.net"
S2 = "sender2@s.whatsapp.net"

# «حرف» قصير غير مستقرّ (ينتظر 90s لالتقاط Edit) — يحجب مُرسِله فقط
PLACEHOLDER = "زد"
# ترويسة A مكتملة واضحة (رقم إشاري + هاتف + مبلغ) → مستقرّة فورًا، تنتظر طرفًا ثانيًا
A8976 = "A8976\nفودافون\n01025642842\n3950 مصري"
A9001 = "A9001\nفودافون\n01025642843\n4100 مصري"
# حوالة SI مكتملة (حقول مُعنونة صريحة) → مستقرّة فورًا، مستقلّة برسالة واحدة (خزينة «بلاس فون»)
SI_COMPLETE = (
    "رقم العملية: SI7788\nرقم المستلم: 01093232832\n"
    "اسم الزبون: مروان الشاوش كود 1284\nالقيمة قبل الخصم: 3540 ج.م\n"
    "القيمة بعد الخصم 1%: 3505 ج.م\nالسعر: 5.9\nنوع التحويل: فودافون كاش\n"
    "الخزينة: بلاس فون"
)


class _FakeWriter:
    name = "fake"

    async def write(self, job, *, commit):
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
                    customer_room_jids=[], treasury_room_jids=[])


def _raw(key, text, sender, at):
    return RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=sender,
                      text=text, received_at=at)


# ═════════════════════════════════════════════════════════════════════════════
# مُرسِلان مختلفان مستقلّان: A غير مستقرّة من S1 لا تحجب A من S2
# ═════════════════════════════════════════════════════════════════════════════
async def test_different_senders_do_not_block_each_other(db):
    pipe = _pipeline(db)
    # S1: «حرف» غير مستقرّ (يبقى ينتظر) — سبقه في الطابور
    await pipe.capture(_raw("p1", PLACEHOLDER, S1, NOW))
    # S2: ترويسة مكتملة بعد 5ث (>3ث فلا يُحسَب زوجًا متلاحقًا يُثبّت «الحرف»)
    await pipe.capture(_raw("a2", A8976, S2, NOW + timedelta(seconds=5)))

    await pipe.process_inbox(NOW + timedelta(seconds=5))

    # حوالة S2 عُولجت (لم تُحجَب خلف «حرف» S1 غير المستقرّ)
    d2 = await db.deals.find_by_source_key("a2")
    assert d2 is not None and d2.status == Status.WAITING_SECOND_LEG
    # «حرف» S1 أُجِّل (لم يستقرّ) — لم يُعالَج بعد
    assert await db.deals.find_by_source_key("p1") is None
    # الرسالة الخام ما زالت غير معالَجة (بانتظار الاستقرار)
    remaining = [r.message_key for r in await db.raw.unprocessed()]
    assert "p1" in remaining and "a2" not in remaining


# ═════════════════════════════════════════════════════════════════════════════
# SI مستقلّة: تنزل فور استقرارها حتى لو كان المُرسِل نفسه محجوبًا برسالة سابقة غير مستقرّة
# ═════════════════════════════════════════════════════════════════════════════
async def test_independent_si_bypasses_same_sender_block(db):
    pipe = _pipeline(db)
    # نفس المُرسِل: «حرف» غير مستقرّ يحجبه، ثم SI مكتملة بعده
    await pipe.capture(_raw("p1", PLACEHOLDER, S1, NOW))
    await pipe.capture(_raw("si1", SI_COMPLETE, S1, NOW + timedelta(seconds=5)))

    await pipe.process_inbox(NOW + timedelta(seconds=5))

    # SI عُولجت هذه الدورة (مستقلّة — لا تخضع لحجب خانة المُرسِل): أُنشئت الصفقة وتجاوزت الحجب،
    # ولم تبقَ منتظِرة (تُعالَج جاهزةً في فرز الدفعة — status != WAITING).
    d = await db.deals.find_by_source_key("si1")
    assert d is not None and d.sell_leg.is_si_format is True
    assert d.status != Status.WAITING_SECOND_LEG
    # «الحرف» بقي مؤجَّلًا (لم يُنشئ صفقة — حُجِب مُرسِله بسبب رسالته غير المستقرّة)
    assert await db.deals.find_by_source_key("p1") is None


# ═════════════════════════════════════════════════════════════════════════════
# القيد الوحيد: نفس المُرسِل — رسالته التالية (ليست SI/إكمالًا) تنتظر حتى تستقرّ السابقة
# ═════════════════════════════════════════════════════════════════════════════
async def test_same_sender_next_transfer_waits_for_prior_unstable(db):
    pipe = _pipeline(db)
    await pipe.capture(_raw("p1", PLACEHOLDER, S1, NOW))
    await pipe.capture(_raw("a1", A9001, S1, NOW + timedelta(seconds=5)))

    await pipe.process_inbox(NOW + timedelta(seconds=5))

    # A9001 من نفس المُرسِل المحجوب → أُجِّلت (حفظ ترتيبه التامّ)، لم تُعالَج بعد
    assert await db.deals.find_by_source_key("a1") is None
    remaining = [r.message_key for r in await db.raw.unprocessed()]
    assert "p1" in remaining and "a1" in remaining


# ═════════════════════════════════════════════════════════════════════════════
# الدورة التالية: بعد استقرار «الحرف» (90s) يُعالَج ثم تُعالَج رسالة نفس المُرسِل التالية
# ═════════════════════════════════════════════════════════════════════════════
async def test_same_sender_order_resolves_next_cycle(db):
    pipe = _pipeline(db)
    await pipe.capture(_raw("p1", PLACEHOLDER, S1, NOW))
    await pipe.capture(_raw("a1", A9001, S1, NOW + timedelta(seconds=5)))

    # الدورة الأولى: «الحرف» غير مستقرّ → A9001 مؤجَّلة
    await pipe.process_inbox(NOW + timedelta(seconds=5))
    assert await db.deals.find_by_source_key("a1") is None

    # بعد 95s: «الحرف» استقرّ (هدرزة تُهمَل) → لم يعد يحجب، فتُعالَج A9001
    await pipe.process_inbox(NOW + timedelta(seconds=95))
    d = await db.deals.find_by_source_key("a1")
    assert d is not None and d.status == Status.WAITING_SECOND_LEG
    remaining = [r.message_key for r in await db.raw.unprocessed()]
    assert remaining == []          # الطابور فرغ
