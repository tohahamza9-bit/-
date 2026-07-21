"""
(R1) المرجع الصريح مُلزِم مطلقًا — لا FIFO ولا ذكاء يتجاوزه.

حادثتا إنتاج 2026-07-20 (دفعة 17:21، ستّة مراجع خلال 20 ثانية):

1. **X1567 ابتُلعت.** رسالتها الأولى — حوالةٌ كاملةٌ تحمل مرجعها في سطرها الأوّل — فشل تفكيكها
   فصُنّفت noise، فرُبطت بأقدم صفقة معلّقة (X1566) عبر link_orphan_completion الذي لا يقرأ أيّ
   مرجع. لوّثتها بـ«270 العطوي»، ولم تُنشَأ لـX1567 صفقةٌ قطّ — حوالةٌ ضائعة بلا أثر.
2. **تكملة X1571 سُرقت.** جاءت بلا مرجع فعلًا، فأعطتها FIFO العمياء لأقدم معلّقة (X1568) بلا
   أيّ فحص محتوى. بقيت X1571 بلا مورّد وصُعِّدت.

المرجع كان أمام النظام في الحالة الأولى ولم يُقرأ قطّ، لأنّ كلّ قارئ للمرجع كان يقرأه من
**ناتج التفكيك** لا من النصّ الخام.
"""
from __future__ import annotations

import contextlib
import io
import logging
from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.models import DetectionConfig, RawMessage, WriteResult
from core.pipeline import Pipeline

from .test_ai_first import _FakeAi

NOW = datetime(2026, 7, 20, 17, 21, 0, tzinfo=timezone.utc)
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


def _pipeline(db, ai_client=None):
    return Pipeline(db, Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN), _W(), _V(),
                    customer_room_jids=[], treasury_room_jids=[], ai_client=ai_client)


async def _run_burst(pipe, msgs, *, base=NOW, gap=0.3, now_offset=6):
    """دفعةٌ متقاربة (نفس الدقيقة) ثم دورتا معالجة — شكل حادثة الإنتاج بالضبط.

    الدورة الثانية ضرورية: بوّابة الاستقرار تُؤجِّل **آخر** رسالة في الدفعة (تنتظر تكملتها 3ث)،
    فتبقى بلا معالجة في دورة واحدة — والتكملة اليتيمة هي آخر رسالة في هذه الاختبارات."""
    for i, (text, key, sender) in enumerate(msgs):
        await pipe.capture(RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=sender,
                                      text=text, received_at=base + timedelta(seconds=i * gap)))
    with contextlib.redirect_stderr(io.StringIO()):
        await pipe.process_inbox(base + timedelta(seconds=now_offset))
        await pipe.process_inbox(base + timedelta(seconds=now_offset + 10))


async def _deals_by_ref(db):
    out: dict[str, list[dict]] = {}
    async for d in db.deals.col.find({}):
        r = (d.get("sell_leg") or {}).get("reference_number")
        if r:
            out.setdefault(r, []).append(d)
    return out


async def _all_sell_legs(db):
    return [(d.get("sell_leg") or {}) async for d in db.deals.col.find({})]


# رسائل حادثة X1566/X1567 حرفيًّا من واتساب (2026-07-20 5:19 م)
_X1566 = ("X1566\nفدفوان كاش\n01033761670\n915ج.م\n755 مهند بندلسي6.08", "m1566", S1)
_X1567 = ("X1567\nارجو تحويل 5800 جني فودافون 01025146087\n270 العطوي6.08", "m1567", S1)


# ═════════════════════════════════════════════════════════════════════════════
# (أ) تكملة بمرجع صريح → تُربَط بصفقتها، لا بأقدم معلّقة
# ═════════════════════════════════════════════════════════════════════════════
async def test_ref_message_is_not_swallowed_by_older_pending(db):
    """X1567 لا تُلصَق بـX1566 المعلّقة قبلها — المرجع مختلف فالربط مرفوض.

    الفحص على **ابتلاع المفتاح** لا على تلوّث الحقول: حين تكون للصفقة المضيفة هويةٌ سلفًا لا
    يدهسها _apply_fragment، فتبدو نظيفةً بينما رسالةُ X1567 ابتُلعت فيها وضاعت. المفتاح لا يكذب."""
    await _run_burst(_pipeline(db), [_X1566, _X1567])
    async for d in db.deals.col.find({}):
        ref = (d.get("sell_leg") or {}).get("reference_number")
        if ref != "X1567":
            assert "m1567" not in (d.get("source_message_keys") or []), (
                f"رسالة X1567 ابتُلعت في صفقة {ref}")


async def test_ref_continuation_binds_own_deal_over_older_pending(db):
    """تكملةٌ بمرجعها الصريح تتخطّى صفقةً معلّقةً **أقدم** وتربط صاحبتها."""
    await _run_burst(_pipeline(db), [
        ("X1560\n01000000000\n3000 جنيه مصري\nفودافون", "m1560", S1),   # الأقدم — طُعم FIFO
        ("X1561\n01011111111\n7000 جنيه مصري\nفودافون", "m1561", S1),
        ("X1561\nبلاس فون", "m1561b", S1),                              # تكملة بمرجع صريح
    ])
    by = await _deals_by_ref(db)
    t1560 = (by["X1560"][0]["sell_leg"] or {}).get("treasury") or {}
    assert t1560.get("name") != "بلاس فون", "الأقدم (X1560) خطفت تكملة X1561"


# ═════════════════════════════════════════════════════════════════════════════
# (ب) مرجع بلا صفقة مطابقة → لا يبتلعه أحد، ولا يختفي
# ═════════════════════════════════════════════════════════════════════════════
async def test_ref_without_matching_deal_is_never_absorbed(db):
    """«رفض ربط» لا «أقدم معلّقة»: بيانات X1567 لا تظهر في أيّ صفقة أخرى."""
    await _run_burst(_pipeline(db), [_X1566, _X1567])
    legs = await _all_sell_legs(db)
    owners = [lg for lg in legs
              if lg.get("customer_code") == "270" or "العطوي" in (lg.get("customer_name") or "")]
    for lg in owners:
        assert lg.get("reference_number") == "X1567", (
            f"بيانات X1567 هبطت على صفقة {lg.get('reference_number')}")


async def test_unlinkable_ref_message_is_escalated_not_lost(db):
    """X1567 يجب أن تُصعَّد لا أن تختفي — «لا خسارة صامتة» (§0)."""
    await _run_burst(_pipeline(db), [_X1566, _X1567])
    by = await _deals_by_ref(db)
    if "X1567" in by:
        return                                  # نجت بصفقتها الخاصّة — مقبول أيضًا
    texts = [(o.get("text") or "") async for o in db.outgoing.col.find({})]
    assert any("X1567" in t for t in texts), "X1567 لم تُنشئ صفقةً ولم تُصعَّد — ضاعت صامتةً"


# ═════════════════════════════════════════════════════════════════════════════
# (ج) بلا مرجع + مرشّحان → الذكاء يرجّح بالمحتوى؛ FIFO حين يكون مطفأً
# ═════════════════════════════════════════════════════════════════════════════
# يُختبَر على مستوى الوحدة: بلوغ الفرع E عبر الأنبوب يتطلّب تركيبةً نادرة (الفرع B يلتقط
# التكملة ذات السطرين، وبوّابة الاستقرار تحجب ذات السطر الواحد 60 ثانية). التحكيم نفسه هو
# موضع العطل، فيُختبَر مباشرةً — كما يفعل tests/test_link_first.py.
def _pending(deal_id, created_offset, ref):
    from core.constants import OperationType, Status
    from core.models import Deal, ParsedLeg
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=ref, amount=5000.0,
                    sender_jid=S1, source_message_key=f"{deal_id}-a", expects_pair=True)
    at = NOW + timedelta(seconds=created_offset)
    return Deal(deal_id=deal_id, status=Status.WAITING_SECOND_LEG, sell_leg=leg,
                created_at=at, updated_at=at, chat_jid=CENTRAL, first_received_at=at,
                source_message_keys=[f"{deal_id}-a"])


async def _two_pending(db):
    """X1568 (الأقدم) ثمّ X1571 — تركيبة حادثة سرقة التكملة."""
    from core.queue.service import QueueService
    await db.deals.upsert(_pending("d1568", 0, "X1568"))
    await db.deals.upsert(_pending("d1571", 1, "X1571"))
    return QueueService(db)


async def test_no_ref_candidates_stay_fifo_ordered(db):
    """بلا مرجع ⇒ القائمة كاملةٌ مرتّبةً FIFO — سلوك الاحتياط القائم لم يُمسّ."""
    q = await _two_pending(db)
    cands = await q.pending_candidates_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5))
    assert [c.deal_id for c in cands] == ["d1568", "d1571"]


async def test_explicit_ref_filters_candidates_to_one(db):
    """(R1) مرجعٌ صريح ⇒ صاحبته وحدها، ولو كانت الأحدث."""
    q = await _two_pending(db)
    cands = await q.pending_candidates_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5),
                                                  "X1571")
    assert [c.deal_id for c in cands] == ["d1571"]


async def test_unknown_ref_refuses_to_link(db):
    """(R1) مرجعٌ بلا صفقة مطابقة ⇒ [] = **رفض ربط**، لا أقدمَ معلّقة — حادثة X1567."""
    q = await _two_pending(db)
    cands = await q.pending_candidates_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5),
                                                  "X1567")
    assert cands == [], "مرجعٌ غريب ما زال يسقط على أقدم معلّقة"
    assert await q.oldest_pending_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5),
                                             "X1567") is None


async def test_no_ref_two_candidates_ai_arbitrates_by_content(db):
    """(R2) بلا مرجع + مرشّحان ⇒ الذكاء يرجّح بالمحتوى، فتصل التكملة لـX1571 لا لأقدم معلّقة."""
    q = await _two_pending(db)
    cands = await q.pending_candidates_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5))
    await db.detection.set(DetectionConfig(ai_enabled=True, ai_first_enabled=True,
                                           ai_model="fake/model"))
    ai = _FakeAi({"link_index": 1, "link_confidence": 0.97})          # 1 = X1571
    pipe = _pipeline(db, ai_client=ai)
    raw = RawMessage(message_key="m1571b", chat_jid=CENTRAL, sender_jid=S1,
                     text="مطلب الفيتوري5.98\nمومن عريبي6.02", received_at=NOW)
    chosen = await pipe._choose_link_target(raw, cands, [], [], NOW + timedelta(seconds=5))
    assert chosen is not None and chosen.deal_id == "d1571", "الذكاء رجّح X1571 ولم تصلها"
    assert ai.calls, "الذكاء لم يُستدعَ أصلًا عند تعدّد المرشّحين"


async def test_no_ref_two_candidates_fifo_when_ai_off(db):
    """الاحتياط القائم محفوظ: الذكاء مطفأ ⇒ FIFO (الأقدم) كما كان بالضبط."""
    q = await _two_pending(db)
    cands = await q.pending_candidates_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5))
    raw = RawMessage(message_key="m1571b", chat_jid=CENTRAL, sender_jid=S1,
                     text="مطلب الفيتوري5.98", received_at=NOW)
    chosen = await _pipeline(db)._choose_link_target(raw, cands, [], [], NOW + timedelta(seconds=5))
    assert chosen is not None and chosen.deal_id == "d1568"


# ═════════════════════════════════════════════════════════════════════════════
# (د/هـ) حراسة الانحدار + الرصد
# ═════════════════════════════════════════════════════════════════════════════
async def test_own_ref_repeated_still_merges(db):
    """R1 لا يكسر الشكل السليم: المرجع مكرَّرٌ في الرسالتين ⇒ يطابق نفسه ⇒ يُدمَج (X1570)."""
    await _run_burst(_pipeline(db), [
        ("X1570\n01000000000\n5800 جنيه مصري\nفودافون", "m70", S1),
        ("X1570\nبلاس فون", "m70b", S1),
    ])
    by = await _deals_by_ref(db)
    assert len(by.get("X1570", [])) == 1, "المرجع المكرَّر أنشأ صفقتين بدل الدمج"
    tre = (by["X1570"][0]["sell_leg"] or {}).get("treasury") or {}
    assert tre.get("name") == "بلاس فون", "التكملة ذات المرجع نفسه لم تُدمَج"


async def test_ref_mismatch_is_logged_with_reason(db, caplog):
    """(R3) رفضُ الربط لاختلاف المرجع يُسجَّل بسببه ومرشّحيه — فجوة الرصد مغلقة.

    يُستدعى المنتقي مباشرةً: بعد إضافة «جني» لـ_EGP_TOKENS صارت X1567 تُفكَّك حوالةً مستقلّةً
    فتُنشئ صفقتها عبر المسار العاديّ ولا تبلغ مسار اليتيمة أصلًا — وهو الأفضل. فيبقى فحص
    عقد التسجيل على المنتقي نفسه، حتميًّا، بلا اعتمادٍ على أيّ طريقٍ يسلكه الأنبوب."""
    q = await _two_pending(db)
    with caplog.at_level(logging.INFO):
        cands = await q.pending_candidates_for_sender(
            CENTRAL, S1, NOW + timedelta(seconds=5), "X1567", message_key="m1567")
    assert cands == []
    links = [r.getMessage() for r in caplog.records if "[link]" in r.getMessage()]
    assert links, "لا سطر [link] واحد — قرارات الربط ما زالت غير مرصودة"
    assert any("reason=rejected-ref-mismatch" in m and "text_ref=X1567" in m for m in links), (
        f"رفض المرجع لم يُسجَّل. المسجَّل: {links}")


async def test_x1567_now_becomes_its_own_deal(db):
    """أثر «جني» على حادثة X1567: تُفكَّك حوالةً مستقلّةً بدل أن تُصنَّف noise وتُبتلَع.

    دفاعٌ في العمق: إلزام المرجع يمنع الابتلاع، وهذا يمنع بلوغَ ذلك المسار أصلًا."""
    await _run_burst(_pipeline(db), [_X1566, _X1567])
    by = await _deals_by_ref(db)
    assert "X1567" in by, "X1567 ما زالت بلا صفقة"
    assert (by["X1567"][0]["sell_leg"] or {}).get("amount") == 5800.0


# ═════════════════════════════════════════════════════════════════════════════
# (و) المسار الثاني — مطالبة الردود المعلّقة (pending_replies)
# ═════════════════════════════════════════════════════════════════════════════
def _pending_deal(deal_id, ref, sender, code=None):
    from core.constants import OperationType, Status
    from core.models import Deal, ParsedLeg
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code=code,
                    amount=5000.0, sender_jid=sender, source_message_key=f"{deal_id}-a")
    return Deal(deal_id=deal_id, status=Status.WAITING_SECOND_LEG, sell_leg=leg,
                created_at=NOW, updated_at=NOW, chat_jid=CENTRAL, first_received_at=NOW,
                source_message_keys=[f"{deal_id}-a"])


def _reply_leg(code, name, sender):
    from core.constants import OperationType
    from core.models import ParsedLeg
    return ParsedLeg(operation=OperationType.SELL, customer_code=code, customer_name=name,
                     price_raw="6.0", price_normalized="6.0", sender_jid=sender)


async def test_pending_reply_never_crosses_senders_when_unknown(db):
    """ردٌّ معلّق مجهولُ المُرسِل لا تسحبه صفقةُ مُرسِلٍ آخر.

    كان حصر المُرسِل يُتخطّى كلّه إن جُهِل أيُّ الطرفين (`sender and psender and ...`)، فيُسلَّم
    ردُّ مُرسِلٍ إلى صفقة مُرسِلٍ أجنبيّ — تلوّثٌ صامت. الآن يلزم تطابقٌ إيجابيّ."""
    from core.queue.service import QueueService
    q = QueueService(db)
    await db.pending_replies.add(message_key="r-unknown", chat_jid=CENTRAL,
                                 leg=_reply_leg("999", "زبون أجنبيّ", None), received_at=NOW)
    deal = _pending_deal("dForeign", "X9999", "other@lid")
    await db.deals.upsert(deal)
    pulled = await q._pull_pending_reply(deal, CENTRAL, NOW + timedelta(seconds=5))
    assert pulled is False, "ردٌّ مجهول المُرسِل سُحِب إلى صفقة مُرسِلٍ أجنبيّ"
    assert deal.sell_leg.customer_code != "999"


async def test_pending_reply_same_sender_still_links(db):
    """الحصر أُغلِق على المجهول فقط — التطابق الإيجابيّ يعمل كما كان."""
    from core.queue.service import QueueService
    q = QueueService(db)
    await db.pending_replies.add(message_key="r-same", chat_jid=CENTRAL,
                                 leg=_reply_leg("111", "زبون", S1), received_at=NOW)
    deal = _pending_deal("dSame", None, S1)
    await db.deals.upsert(deal)
    assert await q._pull_pending_reply(deal, CENTRAL, NOW + timedelta(seconds=5)) is True
    assert deal.sell_leg.customer_code == "111"


# ═════════════════════════════════════════════════════════════════════════════
# (ز) «جني» بلا هاء — إملاءٌ أسقط حوالتين (X1478, X1567)
# ═════════════════════════════════════════════════════════════════════════════
async def test_geny_without_haa_is_egp_amount(db):
    """«مبلغ40.000 جني م» تُفكَّك حوالةً بمبلغ 40000 — نصّ X1478 حرفيًّا.

    كان «جني» خارج _EGP_TOKENS فيفشل استخراج المبلغ، فتُصنَّف الرسالة «بلا بنية»
    وتُصعَّد بلا صفقة (bot.log.1 @ 11:49:29)."""
    from core.parsing import parse_message
    tre, sup = await db.treasuries.all_active(), await db.suppliers.all_active()
    res = parse_message(
        "X1478\nفودافون\n01037703891\nمبلغ40.000 جني م\n825 عبد العاطي هروس 6.08", tre, sup)
    assert res.kind == "transfer", "X1478 ما زالت تسقط noise"
    assert res.leg.amount == 40000.0
    assert res.leg.phone == "01037703891"


async def test_geny_variants_all_parse(db):
    """الإملاءات المتقاربة كلّها تُفكَّك — والسليمة لم تنكسر."""
    from core.constants import Currency
    from core.parsing import parse_message
    tre, sup = await db.treasuries.all_active(), await db.suppliers.all_active()
    for token in ("جني م", "جنيه م", "جنية م", "مصري"):
        res = parse_message(f"X9\nفودافون\n01037703891\nمبلغ40.000 {token}\n825 اسم 6.08", tre, sup)
        assert res.kind == "transfer" and res.leg.amount == 40000.0, token
        assert res.leg.currency == Currency.EGP, token


async def test_geny_does_not_swallow_tnd(db):
    """الإضافة لا تُلوّث التونسي: «دت» تبقى TND."""
    from core.constants import Currency
    from core.parsing import parse_message
    tre, sup = await db.treasuries.all_active(), await db.suppliers.all_active()
    res = parse_message("X9\n0917133939\nالمبلغ 800 دت\nصفاقس", tre, sup)
    assert res.leg is not None and res.leg.currency == Currency.TND


# ═════════════════════════════════════════════════════════════════════════════
# (ح) حارس العملة — تكملة مصريّة لا تلوّث صفقة تونسيّة والعكس (X1624، 2026-07-21)
# ═════════════════════════════════════════════════════════════════════════════
def _cur_deal(deal_id, ref, offset, currency, sender=S1, code=None):
    from core.constants import Currency, OperationType, Status
    from core.models import Deal, ParsedLeg
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code=code,
                    amount=5000.0, currency=currency, sender_jid=sender,
                    source_message_key=f"{deal_id}-a")
    at = NOW + timedelta(seconds=offset)
    return Deal(deal_id=deal_id, status=Status.WAITING_SECOND_LEG, sell_leg=leg, created_at=at,
                updated_at=at, chat_jid=CENTRAL, first_received_at=at,
                source_message_keys=[f"{deal_id}-a"])


def _completion_frag(code, name, price):
    """تكملة «كود اسم سعر» بلا مرجع — العملة تُستنتَج من السعر (6.02→EGP، 34→TND)."""
    from core.constants import OperationType
    from core.models import ParsedLeg
    return ParsedLeg(operation=OperationType.SELL, customer_code=code, customer_name=name,
                     price_raw=price, price_normalized=price, sender_jid=S1)


async def test_egyptian_completion_rejects_tunisian_deal(db):
    """X1624 حرفيًّا: صفقة تونسيّة (2500 د.ت) لا تبتلع تكملةً مصريّة (سعر 6.02).

    الأقدم تونسيّة والأحدث مصريّة؛ التكملة المصريّة تتخطّى الأقدم التونسيّة إلى المصريّة."""
    from core.constants import Currency
    from core.queue.service import QueueService
    q = QueueService(db)
    await db.deals.upsert(_cur_deal("dTND", "X1624", 0, Currency.TND))   # الأقدم — تونسيّة
    await db.deals.upsert(_cur_deal("dEGP", "X1700", 1, Currency.EGP))   # الأحدث — مصريّة
    frag = _completion_frag("131", "الساعدي عبد الوهاب", "6.02")         # سعر مصريّ
    chosen = await q.oldest_waiting_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5), frag)
    assert chosen is not None and chosen.deal_id == "dEGP", "التكملة المصريّة لوّثت الصفقة التونسيّة"


async def test_tunisian_completion_rejects_egyptian_deal(db):
    """العكس: تكملة تونسيّة (سعر 34) لا تُربَط بصفقة مصريّة."""
    from core.constants import Currency
    from core.queue.service import QueueService
    q = QueueService(db)
    await db.deals.upsert(_cur_deal("dEGP", "X1626", 0, Currency.EGP))
    await db.deals.upsert(_cur_deal("dTND", "X1624", 1, Currency.TND))
    frag = _completion_frag("1007", "مبروك دردور", "34")                # سعر تونسيّ
    chosen = await q.oldest_waiting_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5), frag)
    assert chosen is not None and chosen.deal_id == "dTND"


async def test_egyptian_completion_links_egyptian_deal(db):
    """X1626 حرفيًّا: تكملة مصريّة (8.762) تُربَط بصفقة مصريّة معلّقة — العملة لا تعيق الصحيح."""
    from core.constants import Currency
    from core.queue.service import QueueService
    q = QueueService(db)
    await db.deals.upsert(_cur_deal("dEGP", "X1626", 0, Currency.EGP))
    frag = _completion_frag("825", "عبد العاطي هروس", "6.08")
    chosen = await q.oldest_waiting_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5), frag)
    assert chosen is not None and chosen.deal_id == "dEGP"


async def test_currency_guard_in_pending_candidates(db):
    """نفس الحارس في المسار الثاني (pending_candidates_for_sender)."""
    from core.constants import Currency
    from core.queue.service import QueueService
    q = QueueService(db)
    await db.deals.upsert(_cur_deal("dTND", "X1624", 0, Currency.TND))
    await db.deals.upsert(_cur_deal("dEGP", "X1700", 1, Currency.EGP))
    frag = _completion_frag("131", "الساعدي", "6.02")                   # مصريّة
    cands = await q.pending_candidates_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5), frag=frag)
    assert [c.deal_id for c in cands] == ["dEGP"], "التونسيّة لم تُستبعَد بالعملة"
