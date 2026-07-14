"""
ميزة الإلغاء عبر Reply (§10 نسخة نهائية) — اختبارات معزولة.

الموظف يعمل Reply بكلمة إلغاء على رسالة الحوالة الأصلية. المسار معزول: يعترضه process_inbox
قبل _ingest فلا يمسّ أي مسار ربط/معالجة (try_absorb_*, absorb_fragment, try_group, Sender Slot).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.cancellation import detect_cancellation, within_cancellation_window
from core.constants import Currency, Mark, OperationType, Status, TreasuryType
from core.models import (
    BotControl,
    Deal,
    ParsedLeg,
    RawMessage,
    TreasuryRef,
    WriteResult,
)
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
EMP = "20100@s.whatsapp.net"
OTHER = "20999@s.whatsapp.net"

_TREAS = TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY)


class _RecWriter:
    name = "rec"

    def __init__(self):
        self.jobs: list = []

    async def write(self, job, *, commit):
        self.jobs.append(job)
        return WriteResult(ok=True)


class _StubVerifier:
    # مُفعّل ويؤكّد: الطبقة ٣ للتحقّق قبل الإلغاء (م: SI2891) تتطلّب تأكيد SQL فعليّ
    # كي يُسمح بالعكس التلقائيّ. هذه الاختبارات تختبر آليّة العكس ⇒ حالة «مؤكَّدة».
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


def _sell_leg(ref="A100", code="1208", amount=1600.0, price="5.84",
              commission=None, sender=EMP, src="orig"):
    return ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code=code,
                     customer_name="فداء", amount=amount, price_raw=price, price_normalized=price,
                     treasury=_TREAS, commission=commission, sender_jid=sender, source_message_key=src)


async def _seed_completed(db, *, deal_id="d1", sell=None, buy=None, created=None, src_keys=None):
    deal = Deal(
        deal_id=deal_id, status=Status.COMPLETED,
        created_at=created or NOW, updated_at=created or NOW, chat_jid=CENTRAL,
        sell_leg=sell if sell is not None else _sell_leg(),
        buy_leg=buy, is_two_legged=buy is not None,
        source_message_keys=src_keys or ["orig"],
    )
    await db.deals.upsert(deal)
    # قيد أصليّ في الدفتر (طبقة ٢ للتحقّق قبل الإلغاء) — محاكاة صفقة مكتوبة فعلًا في MONEYADO
    import uuid as _uuid

    from core.models import LedgerEntry
    slg = deal.sell_leg
    await db.ledger.append(LedgerEntry(
        entry_id=str(_uuid.uuid4()), deal_id=deal_id, message_key=f"led-{deal_id}",
        reference_number=slg.reference_number, operation=OperationType.SELL, is_reversal=False,
        amount=slg.amount or 0.0, currency=slg.currency or Currency.EGP,
        customer_code=slg.customer_code, status=Status.COMPLETED, sql_verified=True,
        created_at=created or NOW))
    return deal


def _cancel_raw(reply="orig", text="إلغاء", sender=EMP, at=None, key="cancel1"):
    return RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=sender,
                      text=text, reply_to_key=reply, received_at=at or NOW + timedelta(seconds=10))


async def _reactions(db):
    return [(o["reply_to_key"], o["reaction"])
            for o in await db.outgoing.next_unsent(200) if o.get("reaction")]


async def _texts(db):
    return [(o["chat_jid"], o.get("text") or "")
            for o in await db.outgoing.next_unsent(200) if not o.get("reaction")]


# ═════════════════════════════════════════════════════════════════════════════
# وحدات نقيّة
# ═════════════════════════════════════════════════════════════════════════════
def test_detect_cancellation_keywords_and_reason():
    assert detect_cancellation("إلغاء") == ""
    assert detect_cancellation("الغاء") == ""
    assert detect_cancellation("كنسل") == ""
    assert detect_cancellation("cancel") == ""
    assert detect_cancellation("ملغاة") == ""
    assert detect_cancellation("إلغاء تحويل خاطئ") == "تحويل خاطئ"   # السبب الإضافيّ
    assert detect_cancellation("1208 فداء 5.84") is None            # حوالة عادية ليست إلغاءً
    assert detect_cancellation("") is None


def test_within_cancellation_window_libya():
    assert within_cancellation_window(NOW - timedelta(hours=95), NOW) is True
    assert within_cancellation_window(NOW - timedelta(hours=97), NOW) is False
    assert within_cancellation_window(NOW - timedelta(hours=96), NOW) is True   # ≤ 96 مسموح


# ═════════════════════════════════════════════════════════════════════════════
# بيع فقط → شراء عكسي + 🚫 + ✅
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_sell_only_reverses_buy(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db)

    await pipe._handle_cancellation(_cancel_raw(), NOW + timedelta(seconds=10))

    assert len(writer.jobs) == 1
    job = writer.jobs[0]
    assert job.operation == OperationType.BUY and job.is_reversal is True
    assert job.leg.customer_code == "1208" and job.leg.amount == 1600.0
    assert job.leg.treasury.code == "74"
    assert job.leg.recipient_name == "إلغاء A100"       # ملاحظات «إلغاء {ref}»

    d = await db.deals.get("d1")
    assert d.status == Status.CANCELLED and d.cancelled_by_key == "cancel1"
    reacts = await _reactions(db)
    assert ("cancel1", Mark.DONE.value) in reacts        # ✅ على رسالة الإلغاء
    assert ("orig", Mark.CANCELLED.value) in reacts      # 🚫 على الرسالة الأصلية


# ═════════════════════════════════════════════════════════════════════════════
# بيع + شراء → شراء (بيانات البيع) + بيع (بيانات الشراء) عكسيان
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_two_leg_reverses_both(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    buy = ParsedLeg(operation=OperationType.BUY, reference_number="A100", customer_code="760",
                    customer_name="طه", amount=1584.0, price_raw="5.86", price_normalized="5.86",
                    treasury=_TREAS, source_message_key="orig")
    await _seed_completed(db, sell=_sell_leg(), buy=buy)

    await pipe._handle_cancellation(_cancel_raw(), NOW + timedelta(seconds=10))

    ops = [(j.operation, j.order_index) for j in writer.jobs]
    assert ops == [(OperationType.BUY, 0), (OperationType.SELL, 1)]   # شراء ثم بيع
    assert writer.jobs[0].leg.customer_code == "1208"                 # عكس البيع ببيانات البيع
    assert writer.jobs[1].leg.customer_code == "760"                  # عكس الشراء ببيانات الشراء
    assert all(j.leg.recipient_name == "إلغاء A100" for j in writer.jobs)
    assert (await db.deals.get("d1")).status == Status.CANCELLED


# ═════════════════════════════════════════════════════════════════════════════
# بيع + خصم → شراء بالمبلغ قبل الخصم + الخصم موجب
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_discount_positive_commission(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    # بيع بخصم: المبلغ قبل الخصم 1600، العمولة سالبة (الخصم) -16
    sell = _sell_leg(amount=1600.0, commission=-16.0)
    sell.amount_after_discount = 1584.0
    await _seed_completed(db, sell=sell)

    await pipe._handle_cancellation(_cancel_raw(), NOW + timedelta(seconds=10))

    job = writer.jobs[0]
    assert job.operation == OperationType.BUY
    assert job.leg.amount == 1600.0            # GROSS (قبل الخصم) — MONEYADO يطرح العمولة → NET=1584
    assert job.leg.commission == 16.0          # الخصم موجب — يُطرَح فعليًّا (لا توثيقيّ)


async def test_cancel_si_discount_positive_commission(db):
    """🔴 صيغة SI (بيع فقط بخصم): الإلغاء = شراء عكسيّ بالصافي (NET) + عمولة موجبة توثيقيّة — **مطابق
    تمامًا لصيغة A** (مسار الكتابة/الإلغاء لا يفرّع على is_si_format). يوثّق أن قاعدة NET/GROSS
    تنطبق على SI وليس A فقط."""
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    # SI بخصم: قبل الخصم 5060، بعد الخصم 5010، عمولة -50، is_si_format
    sell = _sell_leg(amount=5060.0, commission=-50.0)
    sell.amount_after_discount = 5010.0
    sell.is_si_format = True
    await _seed_completed(db, sell=sell)

    await pipe._handle_cancellation(_cancel_raw(), NOW + timedelta(seconds=10))

    job = writer.jobs[0]
    assert job.operation == OperationType.BUY
    assert job.leg.amount == 5060.0            # GROSS (قبل الخصم) — كصيغة A بالضبط، NET=5010
    assert job.leg.commission == 50.0          # موجبة تُطرَح فعليًّا — كصيغة A بالضبط


# ═════════════════════════════════════════════════════════════════════════════
# > 96 ساعة (توقيت ليبيا من created_at) → 🔴 بلا كتابة
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_beyond_window_rejected(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db, created=NOW - timedelta(hours=97))

    await pipe._handle_cancellation(_cancel_raw(), NOW + timedelta(seconds=10))

    assert writer.jobs == []                                   # لم تُكتب
    assert (await db.deals.get("d1")).status == Status.COMPLETED
    assert any("مدة الإلغاء" in t for _, t in await _texts(db))


# ═════════════════════════════════════════════════════════════════════════════
# مُلغاة مسبقاً (إلغاء إلغاء) → 🔴
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_already_cancelled_rejected(db):
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    deal = await _seed_completed(db)
    deal.status = Status.CANCELLED
    await db.deals.upsert(deal)

    await pipe._handle_cancellation(_cancel_raw(), NOW + timedelta(seconds=10))

    assert writer.jobs == []
    assert any("مُلغاة مسبقًا" in t for _, t in await _texts(db))


# ═════════════════════════════════════════════════════════════════════════════
# لم تُوجد الحوالة → 🔴
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_deal_not_found(db):
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await pipe._handle_cancellation(_cancel_raw(reply="ghost"), NOW + timedelta(seconds=10))
    assert writer.jobs == []
    assert any("لم يُعثر" in t for _, t in await _texts(db))


# ═════════════════════════════════════════════════════════════════════════════
# WAITING_SECOND_LEG → إلغاء بلا MONEYADO
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_waiting_deletes_without_moneyado(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    deal = await _seed_completed(db)
    deal.status = Status.WAITING_SECOND_LEG
    await db.deals.upsert(deal)

    await pipe._handle_cancellation(_cancel_raw(), NOW + timedelta(seconds=10))

    assert writer.jobs == []                                  # لا كتابة في MONEYADO
    d = await db.deals.get("d1")
    assert d.status == Status.CANCELLED
    reacts = await _reactions(db)
    assert ("cancel1", Mark.DONE.value) in reacts and ("orig", Mark.CANCELLED.value) in reacts


# ═════════════════════════════════════════════════════════════════════════════
# إلغاء بدون Reply → 🔴 (ليس noise) — عبر process_inbox (اعتراض معزول)
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_without_reply_rejected(db):
    pipe = _pipeline(db)
    await pipe.capture(RawMessage(message_key="c0", chat_jid=CENTRAL, sender_jid=EMP,
                                  text="إلغاء", received_at=NOW))
    await pipe.process_inbox(NOW + timedelta(seconds=2))

    assert await db.deals.col.count_documents({}) == 0        # لم تُنشأ صفقة (ليست حوالة)
    assert any("Reply" in t for _, t in await _texts(db))     # إرشاد صريح


# ═════════════════════════════════════════════════════════════════════════════
# الإلغاء من شخص غير مُرسل الحوالة الأصلية → ينجح (لا فحص مُرسِل)
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_by_different_sender_succeeds(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db, sell=_sell_leg(sender=EMP))       # الحوالة من EMP

    await pipe._handle_cancellation(_cancel_raw(sender=OTHER), NOW + timedelta(seconds=10))  # الإلغاء من OTHER

    assert len(writer.jobs) == 1 and writer.jobs[0].operation == OperationType.BUY
    assert (await db.deals.get("d1")).status == Status.CANCELLED


# ═════════════════════════════════════════════════════════════════════════════
# إلغاء مزدوج متزامن → واحد ينجح والثاني 🔴 (انتقال ذرّي)
# ═════════════════════════════════════════════════════════════════════════════
async def test_concurrent_double_cancel_atomic(db):
    # الانتقال الذرّي نفسه: أول begin_cancelling ينجح والثاني يفشل
    await _seed_completed(db)
    assert await db.deals.begin_cancelling("d1") is True
    assert await db.deals.begin_cancelling("d1") is False       # الحالة لم تعد completed


async def test_double_cancel_second_rejected(db):
    await _enable_storage(db)
    writer = _RecWriter()
    pipe = _pipeline(db, writer)
    await _seed_completed(db)

    await pipe._handle_cancellation(_cancel_raw(key="c1"), NOW + timedelta(seconds=10))
    await pipe._handle_cancellation(_cancel_raw(key="c2"), NOW + timedelta(seconds=20))

    assert len(writer.jobs) == 1                                # كُتب قيد عكسي واحد فقط
    assert (await db.deals.get("d1")).status == Status.CANCELLED
    assert any("مُلغاة مسبقًا" in t for _, t in await _texts(db))   # الثاني رُفض


# ═════════════════════════════════════════════════════════════════════════════
# FIFO + العزل: رسالة الإلغاء لا تفتح خانة مُرسِل ولا تُنشئ صفقة (تُعالَج كإلغاء فقط)
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancellation_does_not_create_deal_or_slot(db):
    await _enable_storage(db)
    pipe = _pipeline(db)
    await _seed_completed(db)
    await pipe.capture(_cancel_raw(at=NOW))
    await pipe.process_inbox(NOW + timedelta(seconds=2))

    # لم تُنشأ صفقة جديدة (فقط الأصلية)، ولا خانة مُرسِل
    assert await db.deals.col.count_documents({}) == 1
    assert await db.sender_slots.find_open(CENTRAL, EMP, NOW + timedelta(seconds=2)) is None
    assert (await db.deals.get("d1")).status == Status.CANCELLED   # نُفّذ الإلغاء بعد الفرز
