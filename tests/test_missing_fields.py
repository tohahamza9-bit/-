"""
رسالة «ناقص: …» الديناميكية + علامة is_alert للتنبيهات الحرجة (⚠️/🔴) — تُعفى من warm-up.

sweep_incomplete_a يُنبّه على الحوالة الناقصة؛ الرسالة تذكر **الحقول الناقصة فعلًا** فقط
(لا نصّ ثابت)، وتُعلَّم is_alert=True فتصل عبر الجسر رغم إشباع سقف warm-up (لا تعلق).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.constants import OperationType, Status, TreasuryType
from core.models import ParsedLeg, RawMessage, TreasuryRef, WriteResult
from core.pipeline import Pipeline
from core.queue.service import missing_mandatory_fields

NOW = datetime(2026, 7, 12, 21, 21, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
EMP = "20100@s.whatsapp.net"
_TREAS = TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY)


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
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    return Pipeline(db, bus, _W(), _V(), customer_room_jids=[], treasury_room_jids=[])


def _leg(**over):
    base = dict(operation=OperationType.SELL, reference_number="A9004", amount=60000.0)
    base.update(over)
    return ParsedLeg(**base)


# ═════════════════════════════════════════════════════════════════════════════
# missing_mandatory_fields — الحقول الناقصة فعلًا
# ═════════════════════════════════════════════════════════════════════════════
def test_missing_all_fields():
    # A9004 الفعلية: رقم إشاري + هاتف + مبلغ + وسيلة فقط → كل الحقول ناقصة
    leg = _leg(phone="01000064315", payment_method="فودافون كاش")
    assert missing_mandatory_fields(leg) == ["كود الزبون", "الاسم", "السعر", "الخزينة"]


def test_missing_one_field_treasury_only():
    # كود + اسم + سعر حاضرة، الخزينة فقط ناقصة
    leg = _leg(customer_code="1208", customer_name="فداء", price_normalized="5.90", treasury=None)
    assert missing_mandatory_fields(leg) == ["الخزينة"]


def test_missing_two_fields_code_and_price():
    # اسم + خزينة حاضران؛ الكود والسعر ناقصان
    leg = _leg(customer_name="فداء", treasury=_TREAS)
    assert missing_mandatory_fields(leg) == ["كود الزبون", "السعر"]


def test_recipient_name_counts_as_name():
    # اسم مستلم منفرد (بلا كود) → «الاسم» ليس ناقصًا؛ الكود/السعر/الخزينة ناقصة
    leg = _leg(recipient_name="فايزه")
    m = missing_mandatory_fields(leg)
    assert "الاسم" not in m and m == ["كود الزبون", "السعر", "الخزينة"]


def test_missing_none_leg():
    assert missing_mandatory_fields(None) == ["كود الزبون", "الاسم", "السعر", "الخزينة"]


# ═════════════════════════════════════════════════════════════════════════════
# تكامل: A9004 الناقصة تمامًا → ⚠️ «ناقص: كل الحقول» + is_alert=True
# ═════════════════════════════════════════════════════════════════════════════
async def test_a9004_dynamic_warn_is_alert(db):
    pipe = _pipeline(db)
    # A9004: رقم إشاري + هاتف + مبلغ + وسيلة فقط (بلا كود/اسم/سعر/خزينة)
    await pipe.capture(RawMessage(message_key="m1", chat_jid=CENTRAL, sender_jid=EMP,
                                  text="A9004\n01000064315\n60000 جني مصري\nفدفون كاش", received_at=NOW))
    await pipe.process_inbox(NOW + timedelta(seconds=2))
    d = await db.deals.find_by_source_key("m1")
    assert d is not None and d.status == Status.WAITING_SECOND_LEG

    await pipe.tick(NOW + timedelta(seconds=95))
    outs = await db.outgoing.next_unsent(100)
    warns = [o for o in outs if o["chat_jid"] == CENTRAL and "ناقص:" in (o.get("text") or "")]
    assert warns, "لم يصل تنبيه A9004"
    txt = warns[0]["text"]
    assert "A9004 — ناقص: كود الزبون، الاسم، السعر، الخزينة" in txt   # الحقول الناقصة فعلًا
    assert warns[0].get("is_alert") is True                          # تنبيه حرج → يُعفى من warm-up


# ═════════════════════════════════════════════════════════════════════════════
# علامة is_alert: التنبيهات حرجة؛ الرسائل العادية لا
# ═════════════════════════════════════════════════════════════════════════════
async def test_notify_admin_is_alert(db):
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    await bus.notify_admin("🔴 تصعيد تجريبيّ", "k1")
    o = (await db.outgoing.next_unsent(10))[0]
    assert o["chat_jid"] == ADMIN and o["is_alert"] is True


async def test_normal_reply_not_alert(db):
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    await bus.reply_central("✅ تأكيد عاديّ", "k1")                    # افتراضي is_alert=False
    o = (await db.outgoing.next_unsent(10))[0]
    assert o["is_alert"] is False


async def test_alert_reply_flag(db):
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    await bus.reply_central("🔴 الحوالة مُلغاة مسبقًا.", "k1", is_alert=True)
    o = (await db.outgoing.next_unsent(10))[0]
    assert o["is_alert"] is True
