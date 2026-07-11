"""
اختبارات «حوالة A الناقصة» (§7.3، قرار المستخدم):

في حوالة A، الرسالة الأولى تحمل: رقم إشاري + مبلغ + هاتف + وسيلة **فقط**؛ والرسالة الثانية
تحمل: كود الزبون + الاسم + سعر البيع + الخزينة. فكل حوالة A ناقصة تنتظر الرسالة الثانية:

  - وصلت           → تكتمل وتُدخَل (مغطّى في test_second_message_fixes).
  - لم تصل خلال 90s → تنبيه خفيف في المركزية «⚠️ … يُرجى إكمال البيانات» وتبقى منتظِرة.
  - لم يردّ أحد 15د  → تصعيد لغرفة المسؤول (ESCALATED).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.constants import OperationType, Status
from core.models import ParsedLeg, RawMessage, WriteResult
from core.parsing import parse_message
from core.pipeline import Pipeline
from core.queue.service import QueueService, is_incomplete_first_message

NOW = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
EMP = "20100@s.whatsapp.net"

# الرسالة الأولى لحوالة A: رقم إشاري + وسيلة + هاتف + مبلغ فقط (بلا كود/اسم/سعر/خزينة)
HEADER_ONLY = "A7351\nفودافون\n01025642842\n3950 مصري"
# الرسالة الثانية: كود + اسم + سعر + خزينة
SECOND_MESSAGE = "1500 احمد علي 5.90\nبلس"

# سيناريو الإنتاج الحقيقي (بلاغ A11–A16): الرسالة الأولى تحلّ خزينة «صافي» لكن الهوية غائبة،
# والرسالة الثانية تحمل الكود/الاسم/السعر + الخزينة الحقيقية (أبو يوسف).
FIRST_A13 = "A13\n\nفودافون\n010972989\n60.0000 مصري\nصافي"
SECOND_A13 = "570 ايهاب 5.70 \nابو يوسف"


async def _treas(db):
    return await db.treasuries.all_active()


async def _header_leg(db, key="hdr"):
    leg = parse_message(HEADER_ONLY, await _treas(db), []).leg
    leg.source_message_key = key
    return leg


# ── إعداد الأنبوب (نفس نمط test_second_message_fixes) ─────────────────────────
class _FakeWriter:
    name = "fake"

    def __init__(self):
        self.calls: list = []

    async def write(self, job, *, commit):
        self.calls.append((job.operation.value, commit))
        return WriteResult(ok=True)


class _StubVerifier:
    enabled = False

    async def verify_transaction(self, *a, **k):
        return (False, None)

    async def find_last_pending(self, *a, **k):
        return None


def _pipeline(db, writer=None, rooms=False):
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    return Pipeline(
        db, bus, writer or _FakeWriter(), _StubVerifier(),
        customer_room_jids=[] if not rooms else ["c@g.us"],
        treasury_room_jids=[] if not rooms else ["t@g.us"],
    )


def _raw(key, text, at):
    return RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=EMP, text=text, received_at=at)


# ═════════════════════════════════════════════════════════════════════════════
# كاشف «الرسالة الأولى الناقصة» (وحدة نقيّة)
# ═════════════════════════════════════════════════════════════════════════════
def test_is_incomplete_first_message_positive():
    # رقم إشاري + مبلغ + هاتف + وسيلة، بلا كود/سعر/خزينة → ناقصة
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="A13",
                    amount=3950.0, phone="0100", payment_method="فودافون")
    assert is_incomplete_first_message(leg) is True


def test_is_incomplete_first_message_negative_has_code():
    # كود حاضر → مرساة هوية كافية، ليست ناقصة
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="A13", amount=3950.0,
                    customer_code="53", price_normalized="5.90")
    assert is_incomplete_first_message(leg) is False


def test_is_incomplete_first_message_negative_name_only():
    # اسم مقروء بلا كود → مرساة كافية (trust_gate يمرّرها) → ليست ناقصة
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="A13", amount=3950.0,
                    customer_name="ايهاب ابو حميد")
    assert is_incomplete_first_message(leg) is False


async def test_is_incomplete_first_message_negative_parsed_header(db):
    # ترويسة حقيقية تُفكَّك: بلا كود/سعر/خزينة → ناقصة
    assert is_incomplete_first_message(await _header_leg(db)) is True


def test_is_incomplete_first_message_none():
    assert is_incomplete_first_message(None) is False


# ═════════════════════════════════════════════════════════════════════════════
# لا تُنهى كطرف واحد: sweep_waiting يتخطّى الحوالة الناقصة
# ═════════════════════════════════════════════════════════════════════════════
async def test_incomplete_a_not_finalized_by_sweep_waiting(db):
    svc = QueueService(db)
    await svc.try_group(await _header_leg(db), NOW, chat_jid=CENTRAL)
    # حتى بعد 90s: لا إدخال مفرد (تُدار عبر sweep_incomplete_a)
    assert await svc.sweep_waiting(NOW + timedelta(seconds=91)) == []
    d = await db.deals.find_by_source_key("hdr")
    assert d.status == Status.WAITING_SECOND_LEG


# ═════════════════════════════════════════════════════════════════════════════
# 90s → تنبيه خفيف (تبقى منتظِرة)، مرّة واحدة فقط
# ═════════════════════════════════════════════════════════════════════════════
async def test_incomplete_a_warns_after_ninety_seconds(db):
    svc = QueueService(db)
    await svc.try_group(await _header_leg(db), NOW, chat_jid=CENTRAL)

    # قبل 90s — لا تنبيه
    assert await svc.sweep_incomplete_a(NOW + timedelta(seconds=60)) == ([], [])

    # بعد 90s — تنبيه واحد، وتبقى WAITING
    to_warn, to_escalate = await svc.sweep_incomplete_a(NOW + timedelta(seconds=91))
    assert [d.deal_id for d in to_warn] and to_escalate == []
    d = await db.deals.find_by_source_key("hdr")
    assert d.status == Status.WAITING_SECOND_LEG
    assert d.incomplete_warned is True


async def test_incomplete_a_warns_only_once(db):
    svc = QueueService(db)
    await svc.try_group(await _header_leg(db), NOW, chat_jid=CENTRAL)
    first, _ = await svc.sweep_incomplete_a(NOW + timedelta(seconds=91))
    assert len(first) == 1
    # نبضة تالية قبل الـ15د — لا تكرار للتنبيه
    again, esc = await svc.sweep_incomplete_a(NOW + timedelta(seconds=200))
    assert again == [] and esc == []


# ═════════════════════════════════════════════════════════════════════════════
# 15 دقيقة → تصعيد لغرفة المسؤول
# ═════════════════════════════════════════════════════════════════════════════
async def test_incomplete_a_escalates_after_fifteen_minutes(db):
    svc = QueueService(db)
    await svc.try_group(await _header_leg(db), NOW, chat_jid=CENTRAL)
    to_warn, to_escalate = await svc.sweep_incomplete_a(NOW + timedelta(minutes=15, seconds=1))
    assert to_warn == [] and [d.deal_id for d in to_escalate]
    d = await db.deals.find_by_source_key("hdr")
    assert d.status == Status.ESCALATED
    # خرجت من WAITING → لا يتكرّر التصعيد في نبضة تالية
    assert await svc.sweep_incomplete_a(NOW + timedelta(minutes=20)) == ([], [])


# ═════════════════════════════════════════════════════════════════════════════
# وصول الرسالة الثانية قبل 90s → تكتمل، فلا تنبيه ولا تصعيد
# ═════════════════════════════════════════════════════════════════════════════
async def test_incomplete_a_completed_before_ninety_seconds(db):
    svc = QueueService(db)
    d1 = await svc.try_group(await _header_leg(db), NOW, chat_jid=CENTRAL)
    assert d1.status == Status.WAITING_SECOND_LEG

    frag = parse_message(SECOND_MESSAGE, await _treas(db), []).leg
    d2 = await svc.absorb_fragment(frag, CENTRAL, "second", NOW + timedelta(seconds=30))
    assert d2 is not None and d2.status == Status.PARSED
    assert d2.sell_leg.treasury is not None            # اكتملت الخزينة

    # لم تعد WAITING → لا تنبيه/تصعيد
    assert await svc.sweep_incomplete_a(NOW + timedelta(seconds=91)) == ([], [])


# ═════════════════════════════════════════════════════════════════════════════
# تكامل الأنبوب: تنبيه المركزية @90s، تصعيد المسؤول @15د
# ═════════════════════════════════════════════════════════════════════════════
async def test_pipeline_warns_central_at_ninety_seconds(db):
    pipe = _pipeline(db, rooms=False)
    await pipe.capture(_raw("hdr", HEADER_ONLY, NOW))
    await pipe.process_inbox(NOW)                       # الترويسة مستقرّة فورًا (رقم إشاري + مبلغ)

    d = await db.deals.find_by_source_key("hdr")
    assert d is not None and d.status == Status.WAITING_SECOND_LEG

    await pipe.tick(NOW + timedelta(seconds=91))
    d = await db.deals.find_by_source_key("hdr")
    assert d.status == Status.WAITING_SECOND_LEG        # تبقى منتظِرة
    assert d.incomplete_warned is True

    outs = await db.outgoing.next_unsent(100)
    central_warns = [o for o in outs
                     if o["chat_jid"] == CENTRAL and "إكمال البيانات" in (o.get("text") or "")]
    assert central_warns, "لم يصل التنبيه الخفيف للمركزية"
    assert not [o for o in outs if o["chat_jid"] == ADMIN]  # لا تصعيد بعد
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)


async def test_pipeline_escalates_admin_at_fifteen_minutes(db):
    pipe = _pipeline(db, rooms=False)
    await pipe.capture(_raw("hdr", HEADER_ONLY, NOW))
    await pipe.process_inbox(NOW)

    await pipe.tick(NOW + timedelta(seconds=91))        # تنبيه أولًا
    await pipe.tick(NOW + timedelta(minutes=15, seconds=1))  # ثم تصعيد

    d = await db.deals.find_by_source_key("hdr")
    assert d.status == Status.ESCALATED

    outs = await db.outgoing.next_unsent(100)
    admin_alerts = [o for o in outs
                    if o["chat_jid"] == ADMIN and "15 دقيقة" in (o.get("text") or "")]
    assert admin_alerts, "لم يصل تصعيد لغرفة المسؤول"
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)


async def test_pipeline_no_warn_when_completed_in_time(db):
    pipe = _pipeline(db, rooms=False)
    await pipe.capture(_raw("hdr", HEADER_ONLY, NOW))
    await pipe.capture(_raw("second", SECOND_MESSAGE, NOW + timedelta(seconds=30)))
    await pipe.process_inbox(NOW + timedelta(seconds=40))

    d = await db.deals.find_by_source_key("hdr")
    assert d is not None and d.status != Status.WAITING_SECOND_LEG   # اكتملت

    await pipe.tick(NOW + timedelta(seconds=95))
    outs = await db.outgoing.next_unsent(100)
    assert not [o for o in outs if "إكمال البيانات" in (o.get("text") or "")]


# ═════════════════════════════════════════════════════════════════════════════
# الحجب الترتيبيّ لكل غرفة (§7.2): رأس غير مستقرّ يحجب ما بعده في نفس الغرفة فقط
# ═════════════════════════════════════════════════════════════════════════════
async def test_room_ordering_blocks_when_head_unstable(db):
    """رسالة «حرف» غير مستقرّة في رأس الغرفة تحجب الحوالة الكاملة التالية في **نفس الغرفة** فلا
    تُسجَّل قبل سابقتها؛ وغرفة أخرى تُكمَّل بلا تأثّر؛ وبعد استقرار الرأس تُصرَّف بالترتيب."""
    pipe = _pipeline(db, rooms=False)
    OTHER = "other@g.us"
    # نفس الغرفة (CENTRAL): «حرف» غير مستقرّة (رأس، تنتظر حتى 90s) ثم حوالة كاملة بعدها بثانية
    await pipe.capture(_raw("harf", "حرف", NOW))
    await pipe.capture(_raw("full", HEADER_ONLY, NOW + timedelta(seconds=1)))
    # غرفة أخرى: رسالة مستقرّة — يجب أن تُعالَج (تُعلَّم) رغم حجب CENTRAL (الاستثناء: غرفة مختلفة)
    await pipe.capture(RawMessage(message_key="other", chat_jid=OTHER, sender_jid=EMP,
                                  text=HEADER_ONLY, received_at=NOW + timedelta(seconds=1)))

    await pipe.process_inbox(NOW + timedelta(seconds=30))   # «حرف» لم تستقرّ بعد (تحتاج 90s)

    # الرأس غير المستقرّ يُؤجَّل، والكاملة **خلفه في نفس الغرفة** محجوبة → لم تُعلَّم ولم تُسجَّل صفقة
    assert (await db.raw.get("harf")).processed is False
    assert (await db.raw.get("full")).processed is False
    assert await db.deals.find_by_source_key("full") is None
    # غرفة أخرى: عُولجت (عُلِّمت) رغم حجب CENTRAL — الحجب لكل غرفة على حدة
    assert (await db.raw.get("other")).processed is True

    # بعد STABILIZE_MAX: «حرف» تستقرّ (هدرزة) فتُصرَّف أولًا ثم الكاملة → الترتيب محفوظ
    await pipe.process_inbox(NOW + timedelta(seconds=95))
    assert (await db.raw.get("harf")).processed is True
    assert (await db.raw.get("full")).processed is True
    assert await db.deals.find_by_source_key("full") is not None   # سُجّلت الآن بعد سابقتها


# ═════════════════════════════════════════════════════════════════════════════
# انحدار الإنتاج (A11–A16): خزينة محلولة في الرسالة الأولى + هوية غائبة
# كان يذهب فورًا إلى trust_gate → «كود ناقص»؛ الإصلاح: ينتظر الرسالة الثانية.
# ═════════════════════════════════════════════════════════════════════════════
async def test_first_message_treasury_note_not_resolved(db):
    # 🔴 الرسالة الأولى لا تُحلّ خزينة: «صافي» ملاحظة لا خزينة (الخزينة من الرسالة الثانية)
    leg = parse_message(FIRST_A13, await _treas(db), []).leg
    assert leg.treasury is None
    assert leg.notes is not None and "صافي" in leg.notes
    assert leg.customer_code is None and not (leg.customer_name or "")  # الهوية غائبة
    assert is_incomplete_first_message(leg) is True


async def test_first_message_waits_not_parsed(db):
    # الرسالة الأولى (رقم+مبلغ+هاتف، خزينة None، هوية غائبة) → تنتظر، لا PARSED فوري
    svc = QueueService(db)
    leg = parse_message(FIRST_A13, await _treas(db), []).leg
    leg.source_message_key = "a13"
    deal = await svc.try_group(leg, NOW, chat_jid=CENTRAL)
    assert deal.status == Status.WAITING_SECOND_LEG      # كان PARSED قبل الإصلاح
    assert deal.waiting_deadline == NOW + timedelta(seconds=90)


async def test_second_message_brings_real_treasury_and_identity(db):
    # 🔴 جوهر الإصلاح: خزينة الرسالة الأولى None (صافي ملاحظة)، فخزينة الرسالة الثانية
    # الحقيقية «أبو يوسف» (77) تملأ الفراغ + الهوية → صفقة قابلة للتنزيل (لا «كود ناقص»).
    svc = QueueService(db)
    first = parse_message(FIRST_A13, await _treas(db), []).leg
    first.source_message_key = "a13"
    d1 = await svc.try_group(first, NOW, chat_jid=CENTRAL)
    assert d1.status == Status.WAITING_SECOND_LEG

    frag = parse_message(SECOND_A13, await _treas(db), []).leg
    d2 = await svc.absorb_fragment(frag, CENTRAL, "a13b", NOW + timedelta(seconds=20))
    assert d2 is not None and d2.deal_id == d1.deal_id
    assert d2.status == Status.PARSED
    assert d2.sell_leg.customer_code == "570"            # الهوية اكتملت من الرسالة الثانية
    assert d2.sell_leg.customer_name == "ايهاب"
    assert d2.sell_leg.treasury is not None and d2.sell_leg.treasury.code == "77"  # أبو يوسف
    assert "a13b" in d2.source_message_keys


async def test_pipeline_a13_no_immediate_hold(db):
    # لا رد «كود ناقص» فوري: الصفقة تنتظر بدل الذهاب إلى trust_gate
    pipe = _pipeline(db, rooms=False)
    await pipe.capture(_raw("a13", FIRST_A13, NOW))
    await pipe.process_inbox(NOW)
    await pipe.tick(NOW + timedelta(seconds=5))          # فورًا بعد الوصول

    d = await db.deals.find_by_source_key("a13")
    assert d is not None and d.status == Status.WAITING_SECOND_LEG
    outs = await db.outgoing.next_unsent(100)
    assert not [o for o in outs if "كود ناقص" in (o.get("text") or "")]


async def test_pipeline_a13_completes_on_second_message(db):
    pipe = _pipeline(db, rooms=False)
    await pipe.capture(_raw("a13", FIRST_A13, NOW))
    await pipe.capture(_raw("a13b", SECOND_A13, NOW + timedelta(seconds=20)))
    await pipe.process_inbox(NOW + timedelta(seconds=30))

    d = await db.deals.find_by_source_key("a13")
    assert d is not None and d.status != Status.WAITING_SECOND_LEG   # اكتملت الهوية
    assert d.sell_leg.customer_code == "570"
    outs = await db.outgoing.next_unsent(100)
    assert not [o for o in outs if "كود ناقص" in (o.get("text") or "")]
