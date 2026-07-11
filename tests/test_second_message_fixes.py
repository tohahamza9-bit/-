"""
اختبارات إصلاحات منطق الرسالة الثانية (§7.3) — ثلاثة أخطاء:

الخطأ ١: رد الخزينة/المورد بلا رقم إشاري («بلس»/«صافي» وحدها) كان يُهمَل كهدرزة ولا
         يُربَط بالحوالة المعلّقة. الإصلاح: ربطه بالقرب الزمني (دقيقتان) + نفس الغرفة.
الخطأ ٢: رد الخزينة/المورد لو وصل قبل حوالته كان يُسقَط. الإصلاح: يُحفَظ ردًّا معلّقًا 90s.
الخطأ ٣: حوالة SI بخزينة غير محلولة كانت تنتظر 90s بلا جدوى. الإصلاح: HELD فوري.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.constants import Mark, OperationType, Status
from core.parsing import parse_message
from core.pipeline import Pipeline
from core.queue.service import QueueService, is_completion_fragment

from core.models import ParsedLeg, RawMessage, WriteResult

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
EMP = "20100@s.whatsapp.net"


async def _treas(db):
    return await db.treasuries.all_active()


# نصوص حقيقية (§المصدر ٤ و٥)
HEADER_NO_TREASURY = "A7351\nفودافون\n01025642842\n3950 مصري"   # ترويسة بلا خزينة → معلّقة
SI_UNKNOWN_TREASURY = (
    "رقم العملية: SI0999\nرقم المستلم: 01000000000\n"
    "اسم الزبون: زبون تجريبي كود 1500\nالقيمة قبل الخصم: 1000 ج.م\n"
    "القيمة بعد الخصم 1%: 990 ج.م\nالسعر: 5.9\nنوع التحويل: فودافون كاش\n"
    "الخزينة: خزينة غير معروفة"
)


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


def _raw(key, text, at, reply=None):
    return RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=EMP,
                      text=text, received_at=at, reply_to_key=reply)


# ═════════════════════════════════════════════════════════════════════════════
# كاشف الرد المكمّل (وحدة نقيّة)
# ═════════════════════════════════════════════════════════════════════════════
async def test_bare_treasury_is_completion_fragment(db):
    frag = parse_message("بلس", await _treas(db), []).leg
    assert is_completion_fragment(frag) is True


async def test_bare_saafi_is_note_not_completion_fragment(db):
    # 🔴 تصحيح (بلاغ A11–A16): «صافي» مؤشّر خصم → ملاحظة لا خزينة، فليست ردًّا مكمّلًا للخزينة.
    # الخزينة المكمّلة تأتي باسم حقيقي («بلس» أدناه)، لا بمؤشّر الخصم.
    frag = parse_message("صافي", await _treas(db), []).leg
    assert frag.treasury is None and "صافي" in (frag.notes or "")
    assert is_completion_fragment(frag) is False


async def test_strong_transfer_is_not_fragment(db):
    # حوالة كاملة (مبلغ + مرجع) ليست ردًّا مكمّلًا
    res = parse_message("A06\nفودافون\n01097298988\n69.000 مصري\nصافي", await _treas(db), [])
    assert res.kind == "transfer"
    assert is_completion_fragment(res.leg) is False


async def test_chatter_is_not_fragment(db):
    frag = parse_message("صباح الخير يا شباب", await _treas(db), []).leg
    assert is_completion_fragment(frag) is False


# ═════════════════════════════════════════════════════════════════════════════
# A8304: رسالة ثانية «/» فيها ref + هاتف + مبلغ أقل + خزينة → تُدمَج كخصم لا صفقة جديدة
# ═════════════════════════════════════════════════════════════════════════════
async def test_a8304_slash_second_merges_discount(db):
    svc = QueueService(db)
    first = parse_message("A8304\n01095231350\n5.560 مصري\n603 بن ناصر 5.77",
                          await _treas(db), []).leg
    first.source_message_key = "m1"
    d1 = await svc.try_group(first, NOW, chat_jid=CENTRAL)
    assert d1.status == Status.WAITING_SECOND_LEG

    txt2 = "A8304 / 01095231350 / 5.549 مصري / بلس"      # ref+هاتف+مبلغ أقل+خزينة، بلا زبون
    leg2 = parse_message(txt2, await _treas(db), []).leg
    d2 = await svc.try_absorb_treasury_second(leg2, _raw("m2", txt2, NOW + timedelta(seconds=20)),
                                              NOW + timedelta(seconds=20))
    assert d2 is not None and d2.deal_id == d1.deal_id            # صفقة واحدة (لا جديدة)
    assert d2.sell_leg.amount == 5560 and d2.sell_leg.amount_after_discount == 5549
    assert d2.sell_leg.commission == -11                          # الخصم مطبَّق
    assert d2.sell_leg.treasury is not None and d2.sell_leg.treasury.code == "74"


# ═════════════════════════════════════════════════════════════════════════════
# رد خزينة بلا ref يُربَط بالمعلّقة **المطابِقة للعملة** عند تعدّد المعلّقات (§4.1)
# ═════════════════════════════════════════════════════════════════════════════
async def test_bare_treasury_links_by_currency(db):
    from core.constants import Currency
    from core.models import Deal
    from core.parsing.parser import parse_completion_fragment
    svc = QueueService(db)
    tnd = ParsedLeg(operation=OperationType.SELL, reference_number="A8305", amount=2000.0,
                    currency=Currency.TND, customer_code="526", phone="02000000000")
    egp = ParsedLeg(operation=OperationType.SELL, reference_number="A8304", amount=5000.0,
                    currency=Currency.EGP, customer_code="603", phone="01000000000")
    await db.deals.upsert(Deal(deal_id="dT", status=Status.WAITING_SECOND_LEG,
        created_at=NOW - timedelta(seconds=30), updated_at=NOW, chat_jid=CENTRAL, sell_leg=tnd))
    await db.deals.upsert(Deal(deal_id="dE", status=Status.WAITING_SECOND_LEG,   # أحدث (مصرية)
        created_at=NOW - timedelta(seconds=10), updated_at=NOW, chat_jid=CENTRAL, sell_leg=egp))

    frag = parse_completion_fragment("وليد", await _treas(db), [])   # خزينة تونسية (51, TND)
    d = await svc.absorb_fragment(frag, CENTRAL, "wmk", NOW)
    assert d is not None and d.deal_id == "dT"                       # التونسية، لا الأحدث المصرية


# ═════════════════════════════════════════════════════════════════════════════
# رسالة ثانية بنفس الرقم الإشاري تحمل خزينة فقط → تُكمِّل الخزينة، لا صفقة جديدة (§7.3)
# ═════════════════════════════════════════════════════════════════════════════
async def test_treasury_only_second_completes_pending_by_ref(db):
    svc = QueueService(db)
    # الرسالة الأولى: رقم إشاري + هاتف + مبلغ + هوية، بلا خزينة → معلّقة
    first = parse_message("A8146\n01115233493\n5000 ج م\nفودافون\n1300 عبد الله 5.90",
                          await _treas(db), []).leg
    first.source_message_key = "a8146"
    d1 = await svc.try_group(first, NOW, chat_jid=CENTRAL)
    assert d1.status == Status.WAITING_SECOND_LEG and d1.sell_leg.treasury is None

    # الرسالة الثانية: نفس الرقم + خزينة فقط (بلا كود/اسم/مبلغ)
    leg2 = parse_message("A8146\nبلس", await _treas(db), []).leg
    assert leg2.reference_number == "A8146" and leg2.treasury.code == "74"
    assert leg2.customer_code is None and leg2.amount is None
    raw = _raw("a8146b", "A8146\nبلس", NOW + timedelta(seconds=20))
    d2 = await svc.try_absorb_treasury_second(leg2, raw, NOW + timedelta(seconds=20))
    assert d2 is not None and d2.deal_id == d1.deal_id            # نفس الصفقة
    assert d2.status == Status.PARSED
    assert d2.sell_leg.treasury is not None and d2.sell_leg.treasury.code == "74"  # بلاس فون
    assert "a8146b" in d2.source_message_keys
    assert await db.deals.col.count_documents({}) == 1           # لا صفقة جديدة


async def test_treasury_only_second_no_match_is_stored_as_pending(db):
    # لا صفقة معلّقة بنفس الرقم (الخزينة وصلت قبل الأولى) → تُحفَظ ردًّا معلّقًا (Fix 2)، لا هدرزة
    svc = QueueService(db)
    leg2 = parse_message("A9999\nبلس", await _treas(db), []).leg
    raw = _raw("x", "A9999\nبلس", NOW)
    assert await svc.try_absorb_treasury_second(leg2, raw, NOW) is None
    assert await db.pending_replies.col.count_documents({}) == 1   # حُفِظ ردًّا معلّقًا


async def test_treasury_second_with_amount_diff_phone_merges_discount(db):
    # 🔴 رسالة ثانية بنفس الرقم + خزينة + مبلغ بعد الخصم **بهاتف مختلف** → تُدمَج كخصم عبر المطابقة
    # بالرقم (لا ref+phone) فلا تصير صفقتين، ولا يضيع المبلغ بعد الخصم/العمولة (Aخصم §6.3).
    svc = QueueService(db)
    first = parse_message("A8156\n01029051735\nمصر\n50000\n562 بوجناح 5.96", await _treas(db), []).leg
    first.source_message_key = "m1"
    d1 = await svc.try_group(first, NOW, chat_jid=CENTRAL)
    assert d1.status == Status.WAITING_SECOND_LEG

    txt2 = "A8156\n01025946738\n49.500 ج م\nبلاس فون"   # هاتف مختلف عن الأولى
    leg2 = parse_message(txt2, await _treas(db), []).leg
    d2 = await svc.try_absorb_treasury_second(leg2, _raw("m2", txt2, NOW + timedelta(seconds=20)),
                                              NOW + timedelta(seconds=20))
    assert d2 is not None and d2.deal_id == d1.deal_id            # صفقة واحدة، لا صفقتين
    assert d2.sell_leg.amount == 50000 and d2.sell_leg.amount_after_discount == 49500
    assert d2.sell_leg.commission == -500                         # الخصم مطبَّق (بعد − قبل)
    assert d2.sell_leg.treasury is not None and d2.sell_leg.treasury.code == "74"
    assert "m2" in d2.source_message_keys


async def test_treasury_only_before_first_stored_and_linked_on_arrival(db):
    # 🔴 خارج الترتيب: الخزينة تصل قبل الأولى → تُحفَظ ردًّا معلّقًا، ثم تُربَط تلقائيًّا عند وصول الأولى.
    svc = QueueService(db)
    leg_tre = parse_message("A8154\nبلس", await _treas(db), []).leg
    raw_tre = _raw("mT", "A8154\nبلس", NOW)
    assert await svc.try_absorb_treasury_second(leg_tre, raw_tre, NOW) is None
    assert await db.pending_replies.col.count_documents({}) == 1

    # الأولى (زبون + مبلغ، بلا خزينة) خلال 90s → صفقة تسحب الرد المعلّق فتكتمل خزينتها
    first = parse_message("A8154\n01029051735\nمصر\n50000\n562 بوجناح 5.96",
                          await _treas(db), []).leg
    first.source_message_key = "mF"
    deal = await svc.try_group(first, NOW + timedelta(seconds=30), chat_jid=CENTRAL)
    assert deal.status == Status.PARSED
    assert deal.sell_leg.treasury is not None and deal.sell_leg.treasury.code == "74"  # بلاس فون
    assert deal.sell_leg.customer_code == "562" and deal.sell_leg.amount == 50000
    assert "mT" in deal.source_message_keys


# ═════════════════════════════════════════════════════════════════════════════
# الخطأ ٣ — SI بخزينة غير محلولة: PARSED فورًا (لا WAITING 90s) ثم HELD فوري
# ═════════════════════════════════════════════════════════════════════════════
async def test_si_unresolved_treasury_does_not_wait_in_queue(db):
    svc = QueueService(db)
    leg = parse_message(SI_UNKNOWN_TREASURY, await _treas(db), []).leg
    assert leg.is_si_format is True
    assert leg.treasury is None                    # خزينة غير محلولة
    leg.source_message_key = "si-x"
    deal = await svc.try_group(leg, NOW, chat_jid=CENTRAL)
    assert deal.status == Status.PARSED            # لا تنتظر طرفًا ثانيًا
    assert deal.waiting_deadline is None


async def test_non_si_unresolved_treasury_still_waits(db):
    # ضبط: حوالة A (ليست SI) بخزينة غير محلولة تبقى تنتظر (لم يتغيّر سلوكها)
    svc = QueueService(db)
    leg = parse_message("A55\n1234 احمد علي 5.9\n01000000000\n5000 مصري\nخزينةمجهولة",
                        await _treas(db), []).leg
    assert leg.is_si_format is False and leg.treasury is None
    leg.source_message_key = "a55"
    deal = await svc.try_group(leg, NOW, chat_jid=CENTRAL)
    assert deal.status == Status.WAITING_SECOND_LEG


async def test_si_unresolved_treasury_escalates_to_admin_immediately(db):
    # SI خزينتها مذكورة صراحةً دائمًا؛ فإن لم تُحلّ (نادر) → تصعيد فوري لغرفة المسؤول،
    # لا HELD صامت ولا انتظار 90s. (خزينة محلولة تُدخَل عاديًا — تُغطّيها اختبارات SI الأخرى.)
    writer = _FakeWriter()
    pipe = _pipeline(db, writer, rooms=True)          # حتى مع غرف: تصعيد فوري قبل المطابقة
    await pipe.capture(_raw("si1", SI_UNKNOWN_TREASURY, NOW))
    await pipe.process_inbox(NOW + timedelta(seconds=2))
    await pipe.tick(NOW + timedelta(seconds=5))       # فورًا (بلا انتظار 90s)

    d = await db.deals.find_by_source_key("si1")
    assert d is not None
    assert d.status == Status.ESCALATED               # صُعّدت (مش HELD صامت)
    assert writer.calls == []                         # لم تُكتب بخزينة فارغة

    # تنبيه واضح في غرفة المسؤول فقط (§2.2)
    outs = await db.outgoing.next_unsent(100)
    admin_alerts = [o for o in outs if o["chat_jid"] == ADMIN and "SI" in (o.get("text") or "")]
    assert admin_alerts, "لم يصل تنبيه تصعيد لغرفة المسؤول"
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)


# ═════════════════════════════════════════════════════════════════════════════
# الخطأ ١ — رد الخزينة يصل بعد الحوالة المعلّقة → يُربَط (قرب زمني + نفس الغرفة)
# ═════════════════════════════════════════════════════════════════════════════
async def test_late_treasury_reply_links_to_waiting_deal_in_queue(db):
    svc = QueueService(db)
    header = parse_message(HEADER_NO_TREASURY, await _treas(db), []).leg
    header.source_message_key = "hdr"
    d1 = await svc.try_group(header, NOW, chat_jid=CENTRAL)
    assert d1.status == Status.WAITING_SECOND_LEG and d1.sell_leg.treasury is None

    frag = parse_message("بلس", await _treas(db), []).leg
    d2 = await svc.absorb_fragment(frag, CENTRAL, "bls", NOW + timedelta(seconds=30))
    assert d2 is not None and d2.deal_id == d1.deal_id
    assert d2.status == Status.PARSED
    assert d2.sell_leg.treasury is not None and d2.sell_leg.treasury.name == "بلاس فون"
    assert "bls" in d2.source_message_keys


async def test_late_reply_ignored_when_out_of_room(db):
    # نفس السيناريو لكن الرد في غرفة أخرى → لا يُربَط (شرط «نفس الغرفة»)
    svc = QueueService(db)
    header = parse_message(HEADER_NO_TREASURY, await _treas(db), []).leg
    header.source_message_key = "hdr2"
    await svc.try_group(header, NOW, chat_jid=CENTRAL)
    frag = parse_message("بلس", await _treas(db), []).leg
    res = await svc.absorb_fragment(frag, "other@g.us", "bls2", NOW + timedelta(seconds=30))
    assert res is None                               # لم يجد صفقة في تلك الغرفة → حُفِظ معلّقًا


async def test_late_reply_ignored_when_too_old(db):
    # رد متأخّر أكثر من دقيقتين → لا يُربَط بالمعلّقة
    svc = QueueService(db)
    header = parse_message(HEADER_NO_TREASURY, await _treas(db), []).leg
    header.source_message_key = "hdr3"
    await svc.try_group(header, NOW, chat_jid=CENTRAL)
    frag = parse_message("بلس", await _treas(db), []).leg
    res = await svc.absorb_fragment(frag, CENTRAL, "bls3", NOW + timedelta(seconds=150))
    assert res is None                               # خارج نافذة الدقيقتين


async def test_late_treasury_reply_links_in_pipeline(db):
    pipe = _pipeline(db, rooms=False)
    await pipe.capture(_raw("hdrP", HEADER_NO_TREASURY, NOW))
    await pipe.capture(_raw("blsP", "بلس", NOW + timedelta(seconds=30)))
    await pipe.process_inbox(NOW + timedelta(seconds=40))

    d = await db.deals.find_by_source_key("hdrP")
    assert d is not None and d.status == Status.PARSED
    assert d.sell_leg.treasury is not None and d.sell_leg.treasury.name == "بلاس فون"
    assert "blsP" in d.source_message_keys


# ═════════════════════════════════════════════════════════════════════════════
# الخطأ ٢ — رد الخزينة يصل قبل حوالته → يُحفَظ ردًّا معلّقًا ثم يُربَط
# ═════════════════════════════════════════════════════════════════════════════
async def test_early_reply_stored_then_pulled_by_new_deal_in_queue(db):
    svc = QueueService(db)
    frag = parse_message("بلس", await _treas(db), []).leg
    stored = await svc.absorb_fragment(frag, CENTRAL, "early-bls", NOW)
    assert stored is None                            # لا صفقة معلّقة → حُفِظ ردًّا معلّقًا

    header = parse_message(HEADER_NO_TREASURY, await _treas(db), []).leg
    header.source_message_key = "late-hdr"
    deal = await svc.try_group(header, NOW + timedelta(seconds=20), chat_jid=CENTRAL)
    assert deal.status == Status.PARSED              # اكتملت فورًا بالرد المعلّق
    assert deal.sell_leg.treasury is not None and deal.sell_leg.treasury.name == "بلاس فون"
    assert "early-bls" in deal.source_message_keys


async def test_early_reply_expires_after_ninety_seconds(db):
    svc = QueueService(db)
    frag = parse_message("بلس", await _treas(db), []).leg
    await svc.absorb_fragment(frag, CENTRAL, "stale-bls", NOW)

    # حوالة تصل بعد أكثر من 90s → الرد المعلّق تجاوز مهلته، لا يُربَط
    header = parse_message(HEADER_NO_TREASURY, await _treas(db), []).leg
    header.source_message_key = "way-late-hdr"
    deal = await svc.try_group(header, NOW + timedelta(seconds=95), chat_jid=CENTRAL)
    assert deal.status == Status.WAITING_SECOND_LEG  # لم يُربَط الرد المنتهي
    assert deal.sell_leg.treasury is None


async def test_early_reply_stored_then_linked_in_pipeline(db):
    pipe = _pipeline(db, rooms=False)
    await pipe.capture(_raw("blsE", "بلس", NOW))                          # الرد أولًا
    await pipe.capture(_raw("hdrE", HEADER_NO_TREASURY, NOW + timedelta(seconds=20)))
    await pipe.process_inbox(NOW + timedelta(seconds=40))

    d = await db.deals.find_by_source_key("hdrE")
    assert d is not None and d.status == Status.PARSED
    assert d.sell_leg.treasury is not None and d.sell_leg.treasury.name == "بلاس فون"
