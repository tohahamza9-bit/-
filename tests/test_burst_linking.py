"""
ربط التكملات تحت **دفعة عميقة** (14 رسالة في دقيقة واحدة) — شروط حادثة 2026-07-20.

كلّ اختبارات الربط القائمة تستعمل رسالتين أو ثلاثًا، والعطل لا يظهر إلّا بعمق الدفعة:
حين تتراكم المعلّقات يهبط كلّ ردٍّ عديم‑مرجع على أقدمهنّ أيًّا كان محتواه، فتنزاح الروابط
بواحد (عائلة X850‑X853). هذا الملفّ يثبّت الشروط الحقيقية بدل الحالات المصغّرة.
"""
from __future__ import annotations

import contextlib
import io
from datetime import datetime, timedelta, timezone

import pytest

from core.bus import Bus
from core.models import RawMessage, WriteResult
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 20, 17, 19, 0, tzinfo=timezone.utc)
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


def _pipe(db, ai_client=None):
    return Pipeline(db, Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN), _W(), _V(),
                    customer_room_jids=[], treasury_room_jids=[], ai_client=ai_client)


async def _run(pipe, msgs, *, gap=1.5, tail_ticks=(90, 130, 200)):
    """يحاكي التشغيل الحيّ: التقاطٌ ثمّ نبضةُ عاملٍ بعده مباشرةً (العامل يدور كلّ ٢ث).

    🔴 التشابك ليس تفصيلًا تجميليًّا: `_reference_reuse_action` يستثني الصفقات في
    WAITING_SECOND_LEG، فلا يُكتشَف «مرجعٌ مُعاد استخدامه» إلّا بعد أن تُغادر الأُولى
    الانتظار. حصادٌ دفعيّ (كلّ الالتقاط ثمّ نبضة) يُبقي الجميع منتظِرين فلا يقع الكشف
    أبدًا — وهو ما جعل حزمة اختبارٍ سابقة تُخالف الإنتاج وتُخفي أثر الإصلاح."""
    with contextlib.redirect_stderr(io.StringIO()):
        for i, (txt, key) in enumerate(msgs):
            at = NOW + timedelta(seconds=i * gap)
            await pipe.capture(RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=S1,
                                          text=txt, received_at=at))
            await pipe.process_inbox(at + timedelta(seconds=0.5))
        for t in tail_ticks:                    # ما بقي معلّقًا يتخطّى نافذة الاستقرار (60ث)
            await pipe.process_inbox(NOW + timedelta(seconds=t))


async def _pairs(db) -> dict[str, set[str]]:
    """{المرجع: مجموعة مفاتيح الرسائل المرتبطة به} — عبر كلّ صفقات المرجع."""
    out: dict[str, set[str]] = {}
    async for d in db.deals.col.find({}):
        ref = ((d.get("sell_leg") or {}).get("reference_number")
               or (d.get("buy_leg") or {}).get("reference_number"))
        if ref:
            out.setdefault(ref, set()).update(d.get("source_message_keys") or [])
    return out


# ═════════════════════════════════════════════════════════════════════════════
# التيّارات الطبيعية (٢-٣ رسائل) — يجب ألّا تنكسر
# ═════════════════════════════════════════════════════════════════════════════
async def test_normal_pair_still_links(db):
    """زوجٌ عاديّ: حوالة ثمّ تكملتها عديمة المرجع → يُربطان."""
    await _run(_pipe(db), [
        ("X2001\nفودافون\n01091270337\n1170ج.م", "n1"),
        ("1144 محمد ريشي 6.92\nبلاس", "n2"),
    ])
    assert {"n1", "n2"} <= (await _pairs(db))["X2001"]


async def test_three_sequential_pairs_link_in_order(db):
    """ثلاثة أزواج متتابعة — كلٌّ لصاحبه (التيّار الطبيعيّ للشركة)."""
    await _run(_pipe(db), [
        ("X2010\nفودافون\n01000000001\n1000ج.م", "p1"),
        ("111 زبون اول 6.01\nبلاس", "p2"),
        ("X2011\nفودافون\n01000000002\n2000ج.م", "p3"),
        ("222 زبون ثان 6.02\nبلاس", "p4"),
        ("X2012\nفودافون\n01000000003\n3000ج.م", "p5"),
        ("333 زبون ثالث 6.03\nبلاس", "p6"),
    ])
    pairs = await _pairs(db)
    assert {"p1", "p2"} <= pairs["X2010"], pairs
    assert {"p3", "p4"} <= pairs["X2011"], pairs
    assert {"p5", "p6"} <= pairs["X2012"], pairs


async def test_ref_bearing_continuation_binds_in_stream(db):
    """تكملةٌ بمرجع صريح وسط تيّار → لصاحبتها لا لأقدم معلّقة."""
    await _run(_pipe(db), [
        ("X2020\nفودافون\n01000000001\n1000ج.م", "r1"),
        ("X2021\nفودافون\n01000000002\n2000ج.م", "r2"),
        ("X2021\nبلاس فون", "r3"),
    ])
    pairs = await _pairs(db)
    assert "r3" in pairs["X2021"], pairs
    assert "r3" not in pairs.get("X2020", set()), "الأقدم خطفت تكملةً بمرجع غيرها"


# ═════════════════════════════════════════════════════════════════════════════
# الدفعة الحقيقية — 2026-07-20 17:19‑17:21 حرفيًّا من واتساب
# ═════════════════════════════════════════════════════════════════════════════
BURST: list[tuple[str, str]] = [
    ("X1565\n01002740484\n940 ج م \nانستاباي\nبدون خصم", "k1"),
    ("562 بوجناح 5.94\nالبراق5.98", "k2"),
    ("X1566\nفدفوان كاش\n01033761670\n915ج.م\n755 مهند بندلسي6.08", "k3"),
    ("X1566\nفدفوان كاش\n01033761670\n906.م\nة\nطه6.08", "k4"),
    ("X1567\nارجو تحويل 5800 جني فودافون 01025146087\n270 العطوي6.08", "k5"),
    ("X1567\nارجو تحويل5.742 جني فودافون 01025146087\nطه6.08", "k6"),
    ("X1568\nفدافون كاش\n01099161496\n2925 جنيه مصري\nصافي", "k7"),
    ("1277 شركة القت 6،02\nطه6.08", "k8"),
    ("X1569\n13.586 ج\nفودافون بدون خصم \n01023098506\nاحمد بدوي", "k9"),
    ("633 حريز 6.02\nطه6.08", "k10"),
    ("X1570\n58700 جنيه مصري \nفودافون\nمصر القاهره\n+20 155 366 7043\n33 قدرابو 6.08", "k11"),
    ("X1570\n58.113جنيه مصري \nفودافون\nمصر القاهره\n+20 155 366 7043\nطه6.08", "k12"),
    ("X1571\n01019903834\nمبروك احمد شكري \nالقاهرة \nمبلغ 59000 ج م", "k13"),
    ("مطلب الفيتوري5.98\nمومن عريبي6.02", "k14"),
]

# صاحب كلّ تكملةٍ عديمة المرجع، كما يقرؤها الإنسان من ترتيب الإرسال.
_TRUE_OWNER = {"k2": "X1565", "k8": "X1568", "k10": "X1569", "k14": "X1571"}


async def test_burst_ref_bearing_messages_are_never_swallowed(db):
    """كلّ رسالةٍ تحمل مرجعًا تبقى في صفقة مرجعها — لا تُبتلَع في صفقةٍ أجنبية.

    هذا ما أصلحه 266be4b فعلًا (إلزام المرجع)، ويجب أن يبقى أخضر."""
    await _run(_pipe(db), BURST)
    pairs = await _pairs(db)
    for key, ref in (("k3", "X1566"), ("k4", "X1566"), ("k5", "X1567"),
                     ("k6", "X1567"), ("k7", "X1568"), ("k9", "X1569"),
                     ("k11", "X1570"), ("k12", "X1570"), ("k13", "X1571")):
        owners = [r for r, keys in pairs.items() if key in keys]
        assert owners == [ref], f"{key} يجب أن تبقى في {ref}، ووُجدت في {owners}"


async def test_burst_no_transfer_is_lost(db):
    """كلّ مرجعٍ في الدفعة له صفقة — لا حوالة تختفي (كانت X1567 تضيع)."""
    pairs = await _pairs(db)
    await _run(_pipe(db), BURST)
    pairs = await _pairs(db)
    for ref in ("X1565", "X1566", "X1567", "X1568", "X1569", "X1570", "X1571"):
        assert ref in pairs, f"{ref} بلا صفقة إطلاقًا"


@pytest.mark.xfail(strict=True, reason=(
    "عطلٌ مفتوح (2026-07-20): التكملات عديمة المرجع تنزاح بواحد تحت الدفعة العميقة. "
    "الصفقات المولودة من إعادة استخدام مرجعٍ لرسالةٍ ثانية (k4/k6) تجلس في الطابور "
    "شَرَكًا فتكسر تقابل ١:١ الذي تفترضه FIFO. مرشِّح المحتوى (_fragment_targets) "
    "يُضيّق المرشّحين ولا يحسم، ولا إشارة حتميّة تميّز الصاحب الحقيقيّ — يلزم إمّا "
    "تحكيم الذكاء (مفعّل في الإنتاج) أو استبعاد صفقات إعادة الاستخدام من الترشّح."))
async def test_burst_refless_continuations_reach_true_owner(db):
    """كلّ تكملةٍ عديمة المرجع تصل صاحبها الحقيقيّ — الهدف النهائيّ."""
    await _run(_pipe(db), BURST)
    pairs = await _pairs(db)
    wrong = {
        key: [r for r, keys in pairs.items() if key in keys]
        for key, ref in _TRUE_OWNER.items()
        if [r for r, keys in pairs.items() if key in keys] != [ref]
    }
    assert not wrong, f"تكملات وصلت أصحابًا خطأً: {wrong}"


# ═════════════════════════════════════════════════════════════════════════════
# آليّة الاستبعاد — تُختبَر على مستوى الوحدة لأنّ شرطها لا يتحقّق عبر الأنبوب
# ═════════════════════════════════════════════════════════════════════════════
def _deal(deal_id, ref, offset, *, reuse=False, code=None):
    from core.constants import OperationType, Status
    from core.models import Deal, ParsedLeg
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code=code,
                    amount=5000.0, sender_jid=S1, source_message_key=f"{deal_id}-a")
    at = NOW + timedelta(seconds=offset)
    return Deal(deal_id=deal_id, status=Status.WAITING_SECOND_LEG, sell_leg=leg, created_at=at,
                updated_at=at, chat_jid=CENTRAL, first_received_at=at,
                source_message_keys=[f"{deal_id}-a"], born_from_ref_reuse=reuse)


def _frag_k8():
    """«1277 شركة القت 6،02 / طه6.08» — تكملة X1568، عديمة المرجع."""
    from core.constants import OperationType
    from core.models import ParsedLeg
    return ParsedLeg(operation=OperationType.SELL, customer_code="1277",
                     customer_name="شركة القت", price_raw="6.02", price_normalized="6.02")


async def test_reuse_born_deal_excluded_from_refless_candidates(db):
    """الصفقة المولودة من إعادة استخدام مرجع تُستبعَد رغم أنّها **الأقدم**."""
    from core.queue.service import QueueService
    q = QueueService(db)
    await db.deals.upsert(_deal("decoy", "X1566", 0, reuse=True))
    await db.deals.upsert(_deal("real", "X1568", 1))
    cands = await q.pending_candidates_for_sender(
        CENTRAL, S1, NOW + timedelta(seconds=5), frag=_frag_k8())
    assert [c.deal_id for c in cands] == ["real"], "الشَّرَك لم يُستبعَد"


async def test_explicit_ref_still_reaches_reuse_born_deal(db):
    """الاستبعاد للتكملات عديمة المرجع وحدها — المرجع الصريح يبلغها دائمًا."""
    from core.queue.service import QueueService
    q = QueueService(db)
    await db.deals.upsert(_deal("decoy", "X1566", 0, reuse=True))
    await db.deals.upsert(_deal("real", "X1568", 1))
    cands = await q.pending_candidates_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5), "X1566")
    assert [c.deal_id for c in cands] == ["decoy"]
