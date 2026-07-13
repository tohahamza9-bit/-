"""
إصلاح ١ (§7.3): استحواذ ذرّي على الردود المعلّقة + تأجيل الرسالة الثانية ذات المرجع عند غياب أُولاها.

يعالج أعطال إنتاج حقيقية بعد commit FIFO:
- A9078: تسوية خصم (رسالة ثانية بمرجع) وصلت/عولجت قبل رسالة الهوية بنفس المرجع → كانت تصير صفقة
  headless تبتلع هويةً أجنبية («603 بن ناصر») وتُكتب بكود خاطئ. الآن تُؤجَّل رداً معلّقاً بمرجعها
  فتُربَط بأُولاها ويُحتسَب الخصم.
- الاستحواذ الذرّي يمنع أخذ صفقتين لنفس الرد تحت الضغط.
- الرد المعلّق ذو المرجع الذي تنتهي مهلته دون وصول أُولاه → تصعيد ⚠️ لا هدرزة صامتة.
"""
from __future__ import annotations

import contextlib
import io
from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.constants import OperationType, PENDING_REPLY_MAX_SECONDS, TreasuryType
from core.models import ParsedLeg, RawMessage, TreasuryRef, WriteResult

from core.pipeline import Pipeline

NOW = datetime(2026, 7, 12, 19, 41, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
S1 = "sender1@lid"
S2 = "sender2@lid"


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


async def _deals_by_ref(db):
    out: dict[str, list] = {}
    async for d in db.deals.col.find({}):
        r = (d.get("sell_leg") or {}).get("reference_number")
        if r:
            out.setdefault(r, []).append(d)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# A9078 — تسوية الخصم تُعالَج قبل الهوية بنفس المرجع → تُربَط، لا صفقة headless
# ═════════════════════════════════════════════════════════════════════════════
async def test_a9078_settlement_before_identity_merges_by_ref(db):
    """رسالة التسوية (A9078، خزينة+مبلغ بعد الخصم، بلا كود) تصل **قبل** رسالة الهوية (A9078، كود 612)
    + طُعم أجنبي «603 بن ناصر». يجب: صفقة A9078 واحدة بكود 612 والخصم محتسَب، والطُّعم لا يُبتلَع."""
    pipe = _pipeline(db)
    await _run_burst(pipe, [
        # (١) تسوية A9078 أولًا (نفس ثانية الوصول في الإنتاج، رُتّبت قبل الهوية)
        ("A9078\n01205807643\nالقيمة : 2.797 ج م\nفودافون كاش\nبلاس فون", "a78b", S1),
        # (٢) الطُّعم الأجنبي (الرسالة الثانية الحقيقية لـA9080) — كان يُبتلَع في headless
        ("603 بن ناصر 5.92\nبلاس فون", "frag603", S1),
        # (٣) هوية A9078 تصل بعد التسوية
        ("A9078\n01205807643\nالقيمة : 2,825 ج م\nفودافون كاش\n612 شركة الرائد 5.98", "a78a", S1),
    ])
    by = await _deals_by_ref(db)
    assert "A9078" in by and len(by["A9078"]) == 1, f"A9078 انفصلت إلى {len(by.get('A9078', []))} صفقة"
    sl = by["A9078"][0]["sell_leg"]
    assert sl["customer_code"] == "612", f"كُتبت بكود خاطئ {sl['customer_code']} (تلوّث fragment)"
    assert sl["customer_code"] != "603"
    assert sl["amount"] == 2825.0 and sl["amount_after_discount"] == 2797.0
    assert sl["commission"] == 2797.0 - 2825.0     # خصم سالب صحيح (بعد − قبل)
    assert (sl.get("treasury") or {}).get("name") == "بلاس فون"


async def test_a9078_identity_before_settlement_still_merges(db):
    """الاتجاه الطبيعي (هوية أولًا ثم تسوية بنفس المرجع) يبقى سليمًا — لا انحدار."""
    pipe = _pipeline(db)
    await _run_burst(pipe, [
        ("A9078\n01205807643\nالقيمة : 2,825 ج م\nفودافون كاش\n612 شركة الرائد 5.98", "a78a", S1),
        ("A9078\n01205807643\nالقيمة : 2.797 ج م\nفودافون كاش\nبلاس فون", "a78b", S1),
    ])
    by = await _deals_by_ref(db)
    assert len(by["A9078"]) == 1
    sl = by["A9078"][0]["sell_leg"]
    assert sl["customer_code"] == "612" and sl["amount_after_discount"] == 2797.0
    assert sl["commission"] == 2797.0 - 2825.0


# ═════════════════════════════════════════════════════════════════════════════
# الاستحواذ الذرّي — لا يأخذ ردٌّ واحد صفقتين
# ═════════════════════════════════════════════════════════════════════════════
async def test_atomic_claim_prevents_double_take(db):
    """claim_fifo_for_sender ذرّيّ: أوّل استدعاء يفوز، الثاني يُرجِع None (محجوز)."""
    leg = ParsedLeg(operation=OperationType.SELL, customer_code="501", customer_name="زبون",
                    price_raw="5.9", sender_jid=S1)
    await db.pending_replies.add(message_key="pk1", chat_jid=CENTRAL, leg=leg, received_at=NOW)
    first = await db.pending_replies.claim_fifo_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5),
                                                           PENDING_REPLY_MAX_SECONDS)
    second = await db.pending_replies.claim_fifo_for_sender(CENTRAL, S1, NOW + timedelta(seconds=5),
                                                            PENDING_REPLY_MAX_SECONDS)
    assert first is not None and first["message_key"] == "pk1"
    assert second is None, "رد محجوز أُخِذ مرّتين (سباق)"


async def test_claim_by_reference_atomic_and_release(db):
    """claim_by_reference يحجز ذرّيًّا؛ release يُعيد الرد متاحًا لصفقة أصحّ."""
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="A9300",
                    treasury=TreasuryRef(code="10", name="بلاس فون", type=TreasuryType.SELL_ONLY), amount=2797.0, sender_jid=S1)
    await db.pending_replies.add(message_key="pk2", chat_jid=CENTRAL, leg=leg, received_at=NOW)
    got = await db.pending_replies.claim_by_reference(CENTRAL, "A9300", NOW + timedelta(seconds=5),
                                                      PENDING_REPLY_MAX_SECONDS)
    assert got is not None and got["message_key"] == "pk2"
    # محجوز الآن → لا يُؤخَذ ثانية
    assert await db.pending_replies.claim_by_reference(
        CENTRAL, "A9300", NOW + timedelta(seconds=5), PENDING_REPLY_MAX_SECONDS) is None
    # إطلاق سراح → يعود متاحًا
    await db.pending_replies.release("pk2")
    again = await db.pending_replies.claim_by_reference(CENTRAL, "A9300", NOW + timedelta(seconds=5),
                                                        PENDING_REPLY_MAX_SECONDS)
    assert again is not None and again["message_key"] == "pk2"


# ═════════════════════════════════════════════════════════════════════════════
# تصعيد الرد المعلّق ذي المرجع عند انتهاء المهلة — لا هدرزة صامتة
# ═════════════════════════════════════════════════════════════════════════════
async def test_refd_pending_expiry_escalates(db):
    """رد معلّق **بمرجع** تجاوز المهلة دون وصول أُولاه → sweep_expired يُرجِعه، وtick يُرسل ⚠️ للمركزية."""
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="A9400",
                    treasury=TreasuryRef(code="10", name="بلاس فون", type=TreasuryType.SELL_ONLY), amount=2797.0, sender_jid=S1)
    old = NOW - timedelta(seconds=PENDING_REPLY_MAX_SECONDS + 60)
    await db.pending_replies.add(message_key="pkexp", chat_jid=CENTRAL, leg=leg, received_at=old)
    with contextlib.redirect_stderr(io.StringIO()):
        await pipe_tick(db, NOW)
    outs = await db.outgoing.next_unsent(200)
    alerts = [o for o in outs if o["chat_jid"] == CENTRAL and "A9400" in (o.get("text") or "")]
    assert alerts, "لم يُصعَّد الرد المعلّق ذو المرجع المنتهي (هدرزة صامتة)"
    assert alerts[0].get("is_alert") is True


async def test_norefd_pending_expiry_silent(db):
    """رد معلّق **عديم المرجع** («بلس» شاردة) منتهٍ → يُسقَط صامتًا بلا تصعيد (سلوك قائم)."""
    leg = ParsedLeg(operation=OperationType.SELL, treasury=TreasuryRef(code="10", name="بلاس فون", type=TreasuryType.SELL_ONLY),
                    sender_jid=S1)
    old = NOW - timedelta(seconds=PENDING_REPLY_MAX_SECONDS + 60)
    await db.pending_replies.add(message_key="pkbare", chat_jid=CENTRAL, leg=leg, received_at=old)
    expired = await db.pending_replies.sweep_expired(NOW, PENDING_REPLY_MAX_SECONDS)
    assert expired == [], "رد عديم المرجع لا يُصعَّد"


async def pipe_tick(db, now):
    pipe = _pipeline(db)
    await pipe.tick(now)
