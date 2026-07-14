"""
ميزة التعديل عبر Reply «تعديل X» (§10 نسخة نهائية) — اختبارات معزولة.

الموظف يردّ «تعديل 9000» على رسالة الحوالة الأصلية (X = المبلغ الجديد). المسار معزول:
يعترضه process_inbox قبل _ingest، ويُنفَّذ بعد فرز الدفعة بترتيب received_at، ويقرأ الصفقة من DB
لحظة تنفيذه (§3) — فتعديل/إلغاء متتاليان يقرآن الحالة المحدّثة بالدور.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.amendment import amendment_ratio, detect_amendment, floor_commission
from core.bus import Bus
from core.constants import Mark, OperationType, Status, TreasuryType
from core.models import BotControl, Deal, ParsedLeg, RawMessage, TreasuryRef, WriteResult
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
EMP = "20100@s.whatsapp.net"
_TREAS = TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY)


class _RecWriter:
    name = "rec"

    def __init__(self):
        self.jobs: list = []

    async def write(self, job, *, commit):
        self.jobs.append(job)
        return WriteResult(ok=True)


class _StubVerifier:
    # مُفعّل ويؤكّد: الطبقة ٣ للتحقّق قبل الإلغاء (م: SI2891) تتطلّب تأكيد SQL؛ صفقات هذه
    # الاختبارات مُدرَجة COMPLETED مباشرةً (لا كتابة أماميّة)، فتفعيل التحقّق آمن هنا.
    enabled = True

    async def verify_transaction(self, *a, **k):
        return (True, "MREF")

    async def find_last_pending(self, *a, **k):
        return None


def _pipeline(db, writer=None):
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    return Pipeline(db, bus, writer or _RecWriter(), _StubVerifier(),
                    customer_room_jids=[], treasury_room_jids=[])


async def _enable_storage(db):
    await db.control.set(BotControl(storage_enabled=True, state="running"), "test")


def _sell_leg(ref="A100", code="1208", amount=10000.0, price="5.0", commission=None, src="orig"):
    return ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code=code,
                     customer_name="فداء", amount=amount, price_raw=price, price_normalized=price,
                     treasury=_TREAS, commission=commission, sender_jid=EMP, source_message_key=src)


async def _seed_completed(db, *, deal_id="d1", sell=None, buy=None, created=None, status=Status.COMPLETED):
    deal = Deal(
        deal_id=deal_id, status=status, created_at=created or NOW, updated_at=created or NOW,
        chat_jid=CENTRAL, sell_leg=sell if sell is not None else _sell_leg(),
        buy_leg=buy, is_two_legged=buy is not None, source_message_keys=["orig"],
    )
    await db.deals.upsert(deal)
    # قيد أصليّ (طبقة ٢ للتحقّق قبل الإلغاء) لصفقة COMPLETED مُدرَجة — محاكاة كتابة فعليّة
    if status == Status.COMPLETED:
        import uuid as _uuid

        from core.constants import Currency
        from core.models import LedgerEntry
        slg = deal.sell_leg
        await db.ledger.append(LedgerEntry(
            entry_id=str(_uuid.uuid4()), deal_id=deal_id, message_key=f"led-{deal_id}",
            reference_number=slg.reference_number, operation=OperationType.SELL, is_reversal=False,
            amount=slg.amount or 0.0, currency=slg.currency or Currency.EGP,
            customer_code=slg.customer_code, status=Status.COMPLETED, sql_verified=True,
            created_at=created or NOW))
    return deal


def _amend_raw(text="تعديل 9000", reply="orig", at=None, key="am1"):
    return RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=EMP,
                      text=text, reply_to_key=reply, received_at=at or NOW + timedelta(seconds=10))


async def _reactions(db):
    return [(o["reply_to_key"], o["reaction"])
            for o in await db.outgoing.next_unsent(200) if o.get("reaction")]


async def _texts(db):
    return [o.get("text") or "" for o in await db.outgoing.next_unsent(200) if not o.get("reaction")]


# ═════════════════════════════════════════════════════════════════════════════
# وحدات نقيّة
# ═════════════════════════════════════════════════════════════════════════════
def test_detect_amendment():
    assert detect_amendment("تعديل 9000") == {"amount": 9000.0, "reason": ""}
    assert detect_amendment("تعديل 7070 خطأ مبلغ")["amount"] == 7070.0
    assert detect_amendment("تعديل")["amount"] is None      # بلا رقم
    assert detect_amendment("1208 فداء 5.84") is None       # حوالة عادية
    assert detect_amendment("") is None


def test_amendment_ratio_and_floor():
    deal = Deal(deal_id="x", status=Status.COMPLETED, created_at=NOW, updated_at=NOW,
                sell_leg=_sell_leg(amount=10000.0, commission=100.0))
    assert amendment_ratio(deal) == 0.01                    # 100/10000
    assert floor_commission(7070.0, 0.01) == 70.0           # FLOOR(70.7)
    no_disc = Deal(deal_id="y", status=Status.COMPLETED, created_at=NOW, updated_at=NOW,
                   sell_leg=_sell_leg(commission=None))
    assert amendment_ratio(no_disc) == 0.0


async def test_amend_completed_no_ledger_holds_red(db):
    """تعديل على COMPLETED **بلا قيد بالدفتر** → 🔴 «قيد مفقود» + HELD (لا تعديل على فراغ)."""
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    # COMPLETED مُدرَجة يدويًّا بلا أي قيد دفتر (عكس _seed_completed)
    await db.deals.upsert(Deal(
        deal_id="d1", status=Status.COMPLETED, created_at=NOW, updated_at=NOW,
        chat_jid=CENTRAL, sell_leg=_sell_leg(), source_message_keys=["orig"]))
    await pipe._handle_amendment(_amend_raw("تعديل 9000"), 9000.0, "", NOW + timedelta(seconds=10))
    d = await db.deals.get("d1")
    assert d.status == Status.HELD, "بلا قيد → HELD"
    assert not writer.jobs, "لا كتابة تعديل بلا قيد"
    assert any("🔴" in t and "قيد مفقود" in t for t in await _texts(db))


# ═════════════════════════════════════════════════════════════════════════════
# تعديل عادي (بلا خصم) → شراء عكسي بالفرق + ✏️ + ✅
# ═════════════════════════════════════════════════════════════════════════════
async def test_amend_no_discount_reverses_difference(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db)                                # 10000 بلا خصم

    await pipe._handle_amendment(_amend_raw("تعديل 9000"), 9000.0, "", NOW + timedelta(seconds=10))

    assert len(writer.jobs) == 1
    job = writer.jobs[0]
    assert job.operation == OperationType.BUY and job.is_reversal is True
    assert job.leg.amount == 1000.0                          # الفرق 10000−9000
    assert job.leg.treasury.code == "74"
    assert job.leg.recipient_name == "تعديل A100: 10000 ← 9000"

    d = await db.deals.get("d1")
    assert d.sell_leg.amount == 9000.0                       # الصافي الحاليّ محدّث
    assert len(d.amendments) == 1 and d.amendments[0]["new_net"] == 9000.0
    reacts = await _reactions(db)
    assert ("am1", Mark.DONE.value) in reacts                # ✅ على رسالة التعديل
    assert ("orig", Mark.AMENDED.value) in reacts            # ✏️ على الأصلية


# ═════════════════════════════════════════════════════════════════════════════
# تعديل على حوالة فيها خصم → العمولة بنسبة الحوالة الفعلية + FLOOR (§4)
# ═════════════════════════════════════════════════════════════════════════════
async def test_amend_discount_floor_commission(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    # مثال المستخدم بواقع الإنتاج: أصلي 10100، خصم مُخزَّن **سالبًا** (−101، نسبة 1%) → تعديل 7070
    await _seed_completed(db, sell=_sell_leg(amount=10100.0, commission=-101.0))

    await pipe._handle_amendment(_amend_raw("تعديل 7070"), 7070.0, "", NOW + timedelta(seconds=10))

    job = writer.jobs[0]
    assert job.operation == OperationType.BUY
    # الكمية = فرق GROSS = 10100−7070 = 3030 (leg.amount = GROSS)؛ MONEYADO يطرح العمولة → NET-diff=2999
    assert job.leg.amount == 3030.0
    # 🔴 خانة العمولة بشاشة الشراء = فرق العمولة **موجب دائمًا** (101 − FLOOR(7070×1%)=70) = 31
    assert job.leg.commission == 31.0
    d = await db.deals.get("d1")
    # الصافي بعد التعديل = الجديد الكامل؛ العمولة تبقى بإشارة البيع السالبة (−70)
    assert d.sell_leg.amount == 7070.0 and d.sell_leg.commission == -70.0
    assert d.amendments[0]["old_commission"] == -101.0 and d.amendments[0]["new_commission"] == 70.0


# ═════════════════════════════════════════════════════════════════════════════
# رفض: بلا رقم / بلا Reply / طرفين / مُلغاة / > 96س / لم تُوجد
# ═════════════════════════════════════════════════════════════════════════════
async def test_amend_without_number_rejected(db):
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db)
    await pipe._handle_amendment(_amend_raw("تعديل"), None, "", NOW + timedelta(seconds=10))
    assert writer.jobs == [] and any("يحتاج مبلغ" in t for t in await _texts(db))


async def test_amend_without_reply_rejected(db):
    pipe = _pipeline(db)
    await pipe.capture(RawMessage(message_key="a0", chat_jid=CENTRAL, sender_jid=EMP,
                                  text="تعديل 9000", received_at=NOW))
    await pipe.process_inbox(NOW + timedelta(seconds=2))
    assert any("Reply" in t for t in await _texts(db))


async def test_amend_two_leg_rejected(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    buy = ParsedLeg(operation=OperationType.BUY, reference_number="A100", customer_code="760",
                    amount=9900.0, treasury=_TREAS, source_message_key="orig")
    await _seed_completed(db, sell=_sell_leg(), buy=buy)
    await pipe._handle_amendment(_amend_raw("تعديل 9000"), 9000.0, "", NOW + timedelta(seconds=10))
    assert writer.jobs == [] and any("بيع+شراء" in t for t in await _texts(db))


async def test_amend_cancelled_rejected(db):
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db, status=Status.CANCELLED)
    await pipe._handle_amendment(_amend_raw("تعديل 9000"), 9000.0, "", NOW + timedelta(seconds=10))
    assert writer.jobs == [] and any("مُلغاة" in t for t in await _texts(db))


async def test_amend_beyond_window_rejected(db):
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db, created=NOW - timedelta(hours=97))
    await pipe._handle_amendment(_amend_raw("تعديل 9000"), 9000.0, "", NOW + timedelta(seconds=10))
    assert writer.jobs == [] and any("مدة التعديل" in t for t in await _texts(db))


async def test_amend_deal_not_found(db):
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await pipe._handle_amendment(_amend_raw(reply="ghost"), 9000.0, "", NOW + timedelta(seconds=10))
    assert writer.jobs == [] and any("لم يُعثر" in t for t in await _texts(db))


# ═════════════════════════════════════════════════════════════════════════════
# تعديل على WAITING/PARSED → تعديل مباشر بالـ DB بلا MONEYADO
# ═════════════════════════════════════════════════════════════════════════════
async def test_amend_waiting_direct_no_moneyado(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db, status=Status.WAITING_SECOND_LEG)
    await pipe._handle_amendment(_amend_raw("تعديل 8000"), 8000.0, "", NOW + timedelta(seconds=10))
    assert writer.jobs == []                                 # بلا كتابة MONEYADO
    d = await db.deals.get("d1")
    assert d.sell_leg.amount == 8000.0 and len(d.amendments) == 1


# ═════════════════════════════════════════════════════════════════════════════
# §3: تعديلان متتاليان → الثاني يقرأ الصافي المحدّث ويحسب فرقه منه
# ═════════════════════════════════════════════════════════════════════════════
async def test_two_amendments_sequential_read_updated(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db)                                # 10000

    await pipe._handle_amendment(_amend_raw("تعديل 7000", key="a1"), 7000.0, "", NOW + timedelta(seconds=10))
    await pipe._handle_amendment(_amend_raw("تعديل 5000", key="a2"), 5000.0, "", NOW + timedelta(seconds=20))

    assert [j.leg.amount for j in writer.jobs] == [3000.0, 2000.0]   # 10000−7000 ثم 7000−5000
    d = await db.deals.get("d1")
    assert d.sell_leg.amount == 5000.0 and len(d.amendments) == 2
    assert any("معدّلة سابقًا" in t for t in await _texts(db))       # تُذكر بالرد الثاني


# ═════════════════════════════════════════════════════════════════════════════
# §3: تعديل ثم إلغاء (نفس الدفعة) → الإلغاء يعكس الصافي بعد التعديل — عبر process_inbox
# ═════════════════════════════════════════════════════════════════════════════
async def test_amend_then_cancel_same_batch_reverses_updated_net(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db)                                # 10000
    await pipe.capture(_amend_raw("تعديل 7000", at=NOW + timedelta(seconds=10), key="am"))
    await pipe.capture(RawMessage(message_key="cn", chat_jid=CENTRAL, sender_jid=EMP,
                                  text="إلغاء", reply_to_key="orig", received_at=NOW + timedelta(seconds=11)))
    await pipe.process_inbox(NOW + timedelta(seconds=15))

    # التعديل أولًا (فرق 3000) ثم الإلغاء يعكس الصافي المحدّث (7000)
    assert [j.leg.amount for j in writer.jobs] == [3000.0, 7000.0]
    d = await db.deals.get("d1")
    assert d.status == Status.CANCELLED
    # ملاحظة الإلغاء تذكر التعديل السابق
    cancel_note = writer.jobs[1].leg.recipient_name
    assert "شامل تعديل سابق" in cancel_note and "7000" in cancel_note


# ═════════════════════════════════════════════════════════════════════════════
# إلغاء حوالة فيها تعديل سابق (بخصم) → يعكس الصافي/العمولة الفعليين لا الأصليين
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_after_amendment_reverses_current(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db, sell=_sell_leg(amount=10000.0, commission=100.0))

    await pipe._handle_amendment(_amend_raw("تعديل 7070", key="am"), 7070.0, "", NOW + timedelta(seconds=10))
    from core.models import RawMessage as RM
    cancel = RM(message_key="cn", chat_jid=CENTRAL, sender_jid=EMP, text="إلغاء",
                reply_to_key="orig", received_at=NOW + timedelta(seconds=20))
    await pipe._handle_cancellation(cancel, NOW + timedelta(seconds=20))

    # قيد التعديل = فرق GROSS (10000−7070=2930)؛ ثم الإلغاء يعكس GROSS الحاليّ = leg.amount = 7070
    assert writer.jobs[0].leg.amount == 2930.0
    assert writer.jobs[1].leg.amount == 7070.0 and writer.jobs[1].leg.commission == 70.0
    assert (await db.deals.get("d1")).status == Status.CANCELLED
