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
# العلامة (🟡/✅/🔴) تُوضَع على **كل** رسائل الصفقة (الأولى + الثانية) §8.3
# ═════════════════════════════════════════════════════════════════════════════
async def test_mark_reacts_on_all_message_keys(db):
    from core.constants import Mark
    from core.matching.service import MatchingService
    from core.models import Deal
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    mt = MatchingService(db, bus, [], [])
    deal = Deal(deal_id="x", status=Status.PARSED, created_at=NOW, updated_at=NOW,
                chat_jid=CENTRAL, source_message_keys=["k1", "k2"],
                sell_leg=ParsedLeg(operation=OperationType.SELL))
    await mt.apply_mark(deal, Mark.DONE)
    outs = await db.outgoing.next_unsent(100)
    keys = sorted(o["reply_to_key"] for o in outs if o.get("reaction") == Mark.DONE.value)
    assert keys == ["k1", "k2"]                       # ✅ على الرسالتين


async def test_yellow_mark_reacts_on_first_message_only(db):
    # 🟡 (MATCHED) على الرسالة الأولى فقط (لا الثانية) — بخلاف ✅/🔴
    from core.constants import Mark
    from core.matching.service import MatchingService
    from core.models import Deal
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    mt = MatchingService(db, bus, [], [])
    deal = Deal(deal_id="y", status=Status.MATCHED, created_at=NOW, updated_at=NOW,
                chat_jid=CENTRAL, source_message_keys=["k1", "k2"],
                sell_leg=ParsedLeg(operation=OperationType.SELL, source_message_key="k1"))
    await mt.apply_mark(deal, Mark.MATCHED)
    outs = await db.outgoing.next_unsent(100)
    keys = [o["reply_to_key"] for o in outs if o.get("reaction") == Mark.MATCHED.value]
    assert keys == ["k1"]                             # 🟡 على الأولى فقط


# ═════════════════════════════════════════════════════════════════════════════
# رد خزينة بلا ref يُفضّل الصفقة المعلّقة من **نفس المُرسِل** (sender_jid) §7.3
# ═════════════════════════════════════════════════════════════════════════════
async def test_bare_treasury_prefers_same_sender(db):
    from core.constants import Currency
    from core.models import Deal
    from core.parsing.parser import parse_completion_fragment
    svc = QueueService(db)
    mine = ParsedLeg(operation=OperationType.SELL, reference_number="A1", amount=2000.0,
                     currency=Currency.TND, customer_code="526", sender_jid="mohaymen@s")
    other = ParsedLeg(operation=OperationType.SELL, reference_number="A2", amount=3000.0,
                      currency=Currency.TND, customer_code="603", sender_jid="other@s")
    await db.deals.upsert(Deal(deal_id="dOther", status=Status.WAITING_SECOND_LEG,   # أحدث
        created_at=NOW - timedelta(seconds=5), updated_at=NOW, chat_jid=CENTRAL, sell_leg=other))
    await db.deals.upsert(Deal(deal_id="dMine", status=Status.WAITING_SECOND_LEG,
        created_at=NOW - timedelta(seconds=30), updated_at=NOW, chat_jid=CENTRAL, sell_leg=mine))
    frag = parse_completion_fragment("وليد", await _treas(db), [])
    frag.sender_jid = "mohaymen@s"
    d = await svc.absorb_fragment(frag, CENTRAL, "w", NOW)
    assert d is not None and d.deal_id == "dMine"     # نفس المُرسِل، لا الأحدث


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


# ═════════════════════════════════════════════════════════════════════════════
# ربط الرسالة الثانية: ref صريح، لا ربط بمُصعَّدة، والتباس → لا تخمين (حادثة A8667)
# ═════════════════════════════════════════════════════════════════════════════
async def test_fragment_links_by_explicit_ref_not_newest(db):
    """رسالة ثانية بـref صريح (A100) تُربَط بصاحبة الـref لا بالأحدث (منع تقاطع الدفعات §0)."""
    from core.constants import Currency
    from core.models import Deal
    svc = QueueService(db)
    old = ParsedLeg(operation=OperationType.SELL, reference_number="A100", amount=1000.0,
                    currency=Currency.EGP, customer_code="111")     # treasury None → ينتظر إكمالًا
    new = ParsedLeg(operation=OperationType.SELL, reference_number="A200", amount=2000.0,
                    currency=Currency.EGP, customer_code="222")
    await db.deals.upsert(Deal(deal_id="dOld", status=Status.WAITING_SECOND_LEG,
        created_at=NOW - timedelta(seconds=60), updated_at=NOW, chat_jid=CENTRAL, sell_leg=old))
    await db.deals.upsert(Deal(deal_id="dNew", status=Status.WAITING_SECOND_LEG,       # الأحدث
        created_at=NOW - timedelta(seconds=10), updated_at=NOW, chat_jid=CENTRAL, sell_leg=new))
    frag = ParsedLeg(operation=OperationType.SELL, reference_number="A100", currency=Currency.EGP)
    deal = await svc._find_recent_waiting(CENTRAL, NOW, frag)
    assert deal is not None and deal.deal_id == "dOld"      # بالـref لا بالأحدث dNew


async def test_fragment_no_link_to_escalated_deal(db):
    """رسالة ثانية لا تُربَط بصفقة مُصعَّدة (ESCALATED) — لا يُعاد إحياؤها بإدخال خاطئ (§0)."""
    from core.constants import Currency
    from core.models import Deal
    svc = QueueService(db)
    esc = ParsedLeg(operation=OperationType.SELL, reference_number="A300", amount=5860.0,
                    currency=Currency.EGP)                   # treasury None + هوية ناقصة
    await db.deals.upsert(Deal(deal_id="dEsc", status=Status.ESCALATED,
        created_at=NOW - timedelta(seconds=30), updated_at=NOW, chat_jid=CENTRAL, sell_leg=esc))
    frag = ParsedLeg(operation=OperationType.SELL, currency=Currency.EGP)
    assert await svc._find_recent_waiting(CENTRAL, NOW, frag) is None   # لا ربط بمُصعَّدة


async def test_fragment_ambiguous_multiple_pending_no_guess(db):
    """تعدّد معلّقات مطابقة (نفس العملة، بلا مُرسِل مميّز، بلا ref) → التباس: لا تخمين (None)."""
    from core.constants import Currency
    from core.models import Deal
    svc = QueueService(db)
    a = ParsedLeg(operation=OperationType.SELL, reference_number="A400", amount=1000.0, currency=Currency.EGP)
    b = ParsedLeg(operation=OperationType.SELL, reference_number="A401", amount=2000.0, currency=Currency.EGP)
    await db.deals.upsert(Deal(deal_id="dA", status=Status.WAITING_SECOND_LEG,
        created_at=NOW - timedelta(seconds=40), updated_at=NOW, chat_jid=CENTRAL, sell_leg=a))
    await db.deals.upsert(Deal(deal_id="dB", status=Status.WAITING_SECOND_LEG,
        created_at=NOW - timedelta(seconds=10), updated_at=NOW, chat_jid=CENTRAL, sell_leg=b))
    frag = ParsedLeg(operation=OperationType.SELL, currency=Currency.EGP)   # بلا ref، بلا مُرسِل
    cands = await svc.fragment_link_candidates(CENTRAL, NOW, frag)
    assert len(cands) == 2                                  # التباس: مرشّحان
    assert await svc._find_recent_waiting(CENTRAL, NOW, frag) is None   # لا تخمين


# ═════════════════════════════════════════════════════════════════════════════
# التقاط الخزينة المجهولة (§4.5): خزينة SI معنونة غير معروفة → db.unknown_terms
# ═════════════════════════════════════════════════════════════════════════════
async def test_si_unknown_treasury_recorded_in_unknown_terms(db):
    """خزينة SI معنونة لا تُحلّ → تُلتقط في db.unknown_terms (بلا تخمين، للإسناد اليدويّ)."""
    pipe = _pipeline(db)
    text = ("رقم العملية: SI9002\nرقم المستلم: 01000000000\n"
            "اسم الزبون: زبون تجريبي كود 1500\nالقيمة: 1000 ج.م\nالسعر: 5.9\n"
            "الخزينة: خزينه مجهوله تماما")
    raw = RawMessage(message_key="u1", chat_jid=CENTRAL, sender_jid=EMP, text=text, received_at=NOW)
    await pipe._ingest(raw, NOW)
    rows = await db.unknown_terms.list_recent(20)
    assert any(r["context"] == "treasury" and "مجهول" in r["term"] for r in rows)


# ═════════════════════════════════════════════════════════════════════════════
# العملة من السعر (§3.6 §4.1): رسالة ثانية بلا خزينة → السعر يحدّد العملة فالصفقة المناسبة
# ═════════════════════════════════════════════════════════════════════════════
def test_currency_from_price():
    from core.constants import Currency
    assert QueueService._currency_from_price("35") == Currency.TND      # 0.35
    assert QueueService._currency_from_price("0.35") == Currency.TND
    assert QueueService._currency_from_price("35.75") == Currency.TND
    assert QueueService._currency_from_price("5.90") == Currency.EGP
    assert QueueService._currency_from_price("5،84") == Currency.EGP    # فاصلة عربية
    assert QueueService._currency_from_price(None) is None


async def test_fragment_price_determines_currency_link(db):
    """رسالة ثانية بلا خزينة: السعر يحدّد العملة (35→TND, 5.90→EGP) فتُربَط بالصفقة المناسبة
    عملةً لا بالأحدث (منع تلوّث العملة عند تعدّد المعلّقات المتزامنة — حادثة A8755/A8756)."""
    from core.constants import Currency
    from core.models import Deal
    svc = QueueService(db)
    tnd = ParsedLeg(operation=OperationType.SELL, reference_number="A8755", amount=345.0, currency=Currency.TND)
    egp = ParsedLeg(operation=OperationType.SELL, reference_number="A8756", amount=30000.0, currency=Currency.EGP)
    await db.deals.upsert(Deal(deal_id="dT", status=Status.WAITING_SECOND_LEG,
        created_at=NOW - timedelta(seconds=30), updated_at=NOW, chat_jid=CENTRAL, sell_leg=tnd))
    await db.deals.upsert(Deal(deal_id="dE", status=Status.WAITING_SECOND_LEG,      # الأحدث (مصرية)
        created_at=NOW - timedelta(seconds=5), updated_at=NOW, chat_jid=CENTRAL, sell_leg=egp))
    f35 = ParsedLeg(operation=OperationType.SELL, customer_code="55", customer_name="عطيه", price_raw="35")
    picked = await svc._find_recent_waiting(CENTRAL, NOW, f35)
    assert picked is not None and picked.deal_id == "dT"               # 35 تونسي → التونسية لا الأحدث
    f590 = ParsedLeg(operation=OperationType.SELL, customer_code="55", customer_name="عطيه", price_raw="5.90")
    picked2 = await svc._find_recent_waiting(CENTRAL, NOW, f590)
    assert picked2 is not None and picked2.deal_id == "dE"            # 5.90 مصري → المصرية


async def test_fragment_tnd_price_not_contaminate_egp_only(db):
    """سعر تونسي (35) + صفقة مصرية فقط → لا يُربَط (يُستبعَد بالعملة) فلا تتلوّث المصرية (§4.1)."""
    from core.constants import Currency
    from core.models import Deal
    svc = QueueService(db)
    egp = ParsedLeg(operation=OperationType.SELL, reference_number="A8756", amount=30000.0, currency=Currency.EGP)
    await db.deals.upsert(Deal(deal_id="dE", status=Status.WAITING_SECOND_LEG,
        created_at=NOW - timedelta(seconds=5), updated_at=NOW, chat_jid=CENTRAL, sell_leg=egp))
    f35 = ParsedLeg(operation=OperationType.SELL, customer_code="55", customer_name="عطيه", price_raw="35")
    assert await svc._find_recent_waiting(CENTRAL, NOW, f35) is None


async def test_pending_reply_not_pulled_into_mismatched_currency(db):
    """رد معلّق تونسي (خزينة/سعر TND) لا يُسحَب إلى صفقة مصرية جديدة عبر _pull_pending_reply —
    منع تلوّث حادثة A8755(TND)/A8756(EGP): رد فتحي/سعر 35 كان يدخل صفقة مصرية (§4.1)."""
    from core.constants import Currency
    from core.parsing import parse_message
    from core.parsing.parser import parse_completion_fragment
    svc = QueueService(db)
    treas = await _treas(db)
    # رد خزينة تونسية «55 عليه 35 / فتحي» وصل قبل حوالته → يُحفَظ ردًّا معلّقًا (TND)
    frag = parse_completion_fragment("55 عليه 35\nفتحي", treas, [])
    assert frag.currency == Currency.TND
    assert await svc.absorb_fragment(frag, CENTRAL, "pr", NOW) is None   # لا صفقة → معلّق
    # صفقة مصرية جديدة (EGP) → لا تسحب الرد التونسي (عملة مختلفة)
    egp = parse_message("A8756\nمصر\nفودافون كاش\n01013047070\n30000 ج.م", treas, []).leg
    egp.source_message_key = "e1"
    deal = await svc.try_group(egp, NOW, chat_jid=CENTRAL)
    assert deal.status == Status.WAITING_SECOND_LEG    # ما زالت تنتظر (لم يُسحَب الرد)
    assert deal.sell_leg.treasury is None              # لا خزينة فتحي التونسية
    assert deal.sell_leg.customer_code is None         # ولا كود 55 التونسي
