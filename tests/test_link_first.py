"""
البند 1 — الربط أولاً بلا شرط حل + البند 5 — الرسائل الإلزامية.

المبدأ: أيّ رسالة **بشكل تكملة** من مُرسِلٍ عنده صفقة معلّقة (WAITING_SECOND_LEG) تُربَط بأقدمها —
قبل وبغضّ النظر عن نجاح حلّ خزينتها/موردها. ما انحلّ يُطبَّق؛ ما بقي ناقصًا يُصعَّد برسالة إلزامية
مربوطة بمرجع الصفقة، لا رسالة يتيمة تسقط بإيموجي صامت. FIFO لتعدّد صفقات المُرسِل (درس X850).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.constants import OperationType, Status, TreasuryType
from core.models import Deal, ParsedLeg, SupplierRef, TreasuryRef
from core.queue.service import QueueService

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
S1 = "sender1@lid"
S2 = "sender2@lid"
_TR = TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY)
_SUP = SupplierRef(code="1280", name="البراق")


def _waiting_deal(deal_id, sender, created, *, treasury=None, code="100", ref="X1", chat=CENTRAL):
    """صفقة WAITING_SECOND_LEG (تنتظر تكملة) لمُرسِل معيّن."""
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code=code,
                    customer_name="زبون", amount=5000.0, price_raw="6.0", price_normalized="6.0",
                    treasury=treasury, sender_jid=sender, source_message_key=f"{deal_id}-a",
                    expects_pair=True)
    return Deal(deal_id=deal_id, status=Status.WAITING_SECOND_LEG, sell_leg=leg,
                created_at=created, updated_at=created, chat_jid=chat, first_received_at=created,
                source_message_keys=[f"{deal_id}-a"])


def _frag(*, price=None, treasury=None, supplier=None, is_supplier=False,
          code=None, name=None, unresolved_tr=None):
    return ParsedLeg(operation=OperationType.SELL, price_raw=price, price_normalized=price,
                     treasury=treasury, supplier=supplier, is_supplier_counterpart=is_supplier,
                     customer_code=code, customer_name=name, unresolved_treasury=unresolved_tr)


# ── الأشكال الخمسة الموثّقة للرسالة الثانية — حالتان لكلّ (محلولة / اسم غير محلول) ──────────
FORMS = {
    "1_سعر+خزينة": (_frag(price="6.0", treasury=_TR), _frag(price="6.0", unresolved_tr="بلاسون")),
    "3_سعر فقط":   (_frag(price="6.0"),                _frag(price="6.0", unresolved_tr="خزنة؟")),
    "4_خزينة فقط": (_frag(treasury=_TR),               _frag(unresolved_tr="بلاسون")),
    "5_مورّد+خزينة": (_frag(is_supplier=True, supplier=_SUP, treasury=_TR, price="5.95"),
                      _frag(is_supplier=True, supplier=None, price="5.95")),
}


@pytest.mark.parametrize("form", list(FORMS))
async def test_form_resolved_links_and_applies(db, form):
    """كل شكل — الحالة المحلولة: تُربَط بالصفقة المعلّقة ويُطبَّق ما انحلّ (خزينة/مورد)."""
    q = QueueService(db)
    deal = _waiting_deal("d1", S1, NOW - timedelta(seconds=10), treasury=None)
    await db.deals.upsert(deal)
    resolved, _ = FORMS[form]
    merged = await q.link_orphan_completion(resolved, CENTRAL, S1, "msg-b", NOW)
    assert merged is not None, f"{form}: يجب أن تُربَط"
    assert merged.status == Status.PARSED
    assert "msg-b" in merged.source_message_keys
    # إن حمل الشكلُ خزينةً محلولة → طُبِّقت على الطرف الناقص
    if resolved.treasury is not None:
        assert (merged.sell_leg.treasury or merged.buy_leg.treasury) is not None
    if resolved.is_supplier_counterpart and resolved.supplier is not None:
        assert merged.buy_leg is not None and merged.is_two_legged


@pytest.mark.parametrize("form", list(FORMS))
async def test_form_unresolved_still_links(db, form):
    """كل شكل — حالة الاسم غير المحلول: تُربَط رغم فشل الحل (لا سقوط صامت)، وتبقى ناقصةً للتصعيد."""
    q = QueueService(db)
    deal = _waiting_deal("d1", S1, NOW - timedelta(seconds=10), treasury=None)
    await db.deals.upsert(deal)
    _, unresolved = FORMS[form]
    merged = await q.link_orphan_completion(unresolved, CENTRAL, S1, "msg-b", NOW)
    assert merged is not None, f"{form}: تُربَط حتى مع فشل الحل"
    assert "msg-b" in merged.source_message_keys
    # لم يُحلّ اسم الخزينة → الطرف ما زال بلا خزينة (يُصعَّد لاحقًا برسالة إلزامية)
    if unresolved.treasury is None and not unresolved.is_supplier_counterpart:
        assert merged.sell_leg.treasury is None


async def test_chitchat_not_linked(db):
    """دردشة (بلا سعر/خزينة/مورد/كود) لا تُربَط بصفقة معلّقة — نربط التكملات لا «شكرًا»."""
    q = QueueService(db)
    await db.deals.upsert(_waiting_deal("d1", S1, NOW - timedelta(seconds=10)))
    chit = _frag()  # فارغ تمامًا
    assert QueueService.is_completion_shaped(chit) is False
    assert await q.link_orphan_completion(chit, CENTRAL, S1, "msg-b", NOW) is None


async def test_no_pending_returns_none(db):
    """لا صفقة معلّقة للمُرسِل → None (تُترك للمسار العادي has_ref/صامت)."""
    q = QueueService(db)
    assert await q.link_orphan_completion(_frag(treasury=_TR), CENTRAL, S1, "msg-b", NOW) is None


async def test_oldest_pending_fifo_and_sender_scoping(db):
    """FIFO: مرسِل عنده صفقتان معلّقتان → الأقدم أولًا؛ وتُقيَّد بنفس المُرسِل والنافذة (درس X850)."""
    q = QueueService(db)
    older = _waiting_deal("old", S1, NOW - timedelta(seconds=30), ref="X10")
    newer = _waiting_deal("new", S1, NOW - timedelta(seconds=10), ref="X11")
    other = _waiting_deal("oth", S2, NOW - timedelta(seconds=5), ref="X12")
    for d in (newer, older, other):
        await db.deals.upsert(d)
    picked = await q.oldest_pending_for_sender(CENTRAL, S1, NOW)
    assert picked is not None and picked.deal_id == "old"     # الأقدم لنفس المُرسِل
    # مُرسِل آخر → صفقته هو
    assert (await q.oldest_pending_for_sender(CENTRAL, S2, NOW)).deal_id == "oth"
    # خارج النافذة (قديمة جدًّا) → None
    from core.constants import SECOND_MESSAGE_LINK_SECONDS
    far = NOW + timedelta(seconds=SECOND_MESSAGE_LINK_SECONDS + 60)
    assert await q.oldest_pending_for_sender(CENTRAL, S1, far) is None


async def test_fifo_two_completions_link_in_order(db):
    """صفقتان معلّقتان + تكملتان → كلٌّ تُربَط بالأقدم المتبقّي (تعثّر/سقوط وحدة لا يزيح الباقي)."""
    q = QueueService(db)
    older = _waiting_deal("old", S1, NOW - timedelta(seconds=30), treasury=None, ref="X10")
    newer = _waiting_deal("new", S1, NOW - timedelta(seconds=10), treasury=None, ref="X11")
    await db.deals.upsert(newer); await db.deals.upsert(older)
    m1 = await q.link_orphan_completion(_frag(treasury=_TR), CENTRAL, S1, "b1", NOW)
    assert m1.deal_id == "old"                                # الأقدم أولًا (خرج من الانتظار)
    m2 = await q.link_orphan_completion(_frag(treasury=_TR), CENTRAL, S1, "b2", NOW)
    assert m2.deal_id == "new"                                # التالية بالترتيب


# ── البند 5 — الرسائل الإلزامية والخنق ────────────────────────────────────────────────
class _RecBus:
    def __init__(self):
        self.central_jid = CENTRAL
        self.admin_jid = "admin@g.us"
        self.msgs: list[tuple[str, bool]] = []

    async def reply_central(self, text, reply_to_key, *, is_alert=False):
        self.msgs.append((text, is_alert))


def _pipe_with_bus(db, bus):
    from core.pipeline import Pipeline

    class _W:
        name = "w"
        async def write(self, job, *, commit): ...
    return Pipeline(db, bus, _W(), None, customer_room_jids=[], treasury_room_jids=[])


async def test_mandatory_alert_throttle_full_twice_then_brief(db):
    """(البند 5) رسالة إلزامية لكل ⚠️: كامل مرّتين ثم مختصرة — **ممنوع صفر**، وكلّها is_alert=True."""
    bus = _RecBus()
    pipe = _pipe_with_bus(db, bus)
    deal = _waiting_deal("d1", S1, NOW, ref="X99")
    for _ in range(4):
        await pipe._mandatory_alert(deal, "ناقص: الخزينة", "k")
    assert len(bus.msgs) == 4, "أربع رسائل (ممنوع صفر)"
    assert all(is_alert for _, is_alert in bus.msgs), "كلّها is_alert=True"
    assert bus.msgs[0][0].count("المطلوب") == 1 and bus.msgs[1][0].count("المطلوب") == 1  # كامل مرّتين
    assert "تكرار 3" in bus.msgs[2][0] and "تكرار 4" in bus.msgs[3][0]                     # مختصرة
    # مرجع مختلف يبدأ من جديد (كامل)
    await pipe._mandatory_alert(_waiting_deal("d2", S1, NOW, ref="X88"), "ناقص: السعر", "k2")
    assert "المطلوب" in bus.msgs[4][0]


# ── حلّ المورّد بعد إزالة التكرار (القرار 1) ────────────────────────────────────────────
def test_supplier_resolves_ignoring_sharika_prefix_after_dedup():
    """(القرار 1) «شركة البراق» → المورّد «البراق» عبر المرحلة 4 (كلمات، بلا fuzzy) حين سجلّ **وحيد**؛
    السجلّان المكرّران يُحدثان التباسًا → None (ولذلك يُنظَّف التكرار)."""
    from core.models import SupplierRecord
    from core.parsing.resolve import resolve_supplier
    single = [SupplierRecord(name="البراق", code="1280", aliases=["البراق"], active=True)]
    assert resolve_supplier("شركة البراق", single).name == "البراق"      # بادئة «شركة» مُتجاهَلة
    assert resolve_supplier("البراق", single).name == "البراق"
    dup = [SupplierRecord(name="البراق ", code="1280 ", aliases=["البراق"], active=True),
           SupplierRecord(name="براق", code="1280", aliases=["نور"], active=True)]
    assert resolve_supplier("شركة البراق", dup) is None                  # التباس التكرار → None
