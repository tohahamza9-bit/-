"""
رفض أمر التحكّم من غير معتمد **لا يكون صامتًا** (قاعدة الرسائل الإلزامية).

الحادثة (م: X1323، 2026-07-19 21:29 و21:30): ردّ المالك بـ«أعد» رُفض مرّتين لأن قائمة
«الموظفين المعتمدين» فارغة — بسطر لوق فقط، بلا أيّ ردّ. فبدا البوت وكأنه معطَّل.
القاعدة: كل رفض يُقابَل بسببٍ للمُرسِل + إشعارٍ للمسؤول يحمل **معرّف المُرسِل حرفيًّا**
(المعرّف قد يكون @lid لا رقم هاتف، فنسخه حرفيًّا هو الطريق الوحيد لاعتماده).
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.constants import OperationType, Status
from core.models import Deal, EmployeeRecord, ParsedLeg, RawMessage

NOW = datetime(2026, 7, 19, 21, 29, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
OWNER_LID = "111110871117915@lid"
ORIG_KEY = "orig-x1323"


class _RecBus:
    def __init__(self):
        self.central_jid, self.admin_jid = CENTRAL, ADMIN
        self.central_msgs: list[str] = []
        self.admin_msgs: list[str] = []

    async def notify_admin(self, text, reply_to_key=None, forward_key=None):
        self.admin_msgs.append(text)

    async def reply_central(self, text, reply_to_key=None, *, is_alert=False):
        self.central_msgs.append(text)


def _pipe(db, bus):
    from core.pipeline import Pipeline

    class _W:
        name = "w"
        async def write(self, job, *, commit): ...
    return Pipeline(db, bus, _W(), None, customer_room_jids=[], treasury_room_jids=[])


async def _seed_deal(db):
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="X1323",
                    customer_code="745", customer_name="ابراهيم عون", amount=48900.0,
                    source_message_key=ORIG_KEY)
    await db.deals.upsert(Deal(deal_id="d-x1323", status=Status.SELL_DONE, sell_leg=leg,
                               created_at=NOW, updated_at=NOW,
                               source_message_keys=[ORIG_KEY]))


def _rerun_msg() -> RawMessage:
    return RawMessage(message_key="ctl-1", chat_jid=CENTRAL, sender_jid=OWNER_LID,
                      text="أعد", received_at=NOW, reply_to_key=ORIG_KEY)


async def test_unauthorized_control_is_not_silent(db):
    """🔴 المُرسِل غير معتمد → ردّ بالسبب في المركزية + إشعار المسؤول (لا صمت)."""
    await _seed_deal(db)
    bus = _RecBus()
    await _pipe(db, bus)._handle_control("rerun", None, _rerun_msg(), NOW)
    assert bus.central_msgs, "رُفض الأمر بصمت — لا ردّ للمُرسِل (خرق القاعدة الإلزامية)"
    assert any("غير مُدرَج" in m or "مرفوض" in m for m in bus.central_msgs)
    assert bus.admin_msgs, "لا إشعار للمسؤول عن الرفض"


async def test_rejection_notice_carries_sender_identifier(db):
    """إشعار المسؤول يحمل معرّف المُرسِل حرفيًّا — لينسخه إلى قائمة المعتمدين."""
    await _seed_deal(db)
    bus = _RecBus()
    await _pipe(db, bus)._handle_control("rerun", None, _rerun_msg(), NOW)
    assert any(OWNER_LID in m for m in bus.admin_msgs), \
        "المعرّف غير مذكور — المالك لا يعرف ماذا يضيف للمعتمدين"


async def test_authorized_control_proceeds(db):
    """المعتمد يمرّ: «أعد» على صفقة لها قيد نازل تُرفض بسبب الازدواج لا بسبب الاعتماد."""
    await db.employees.upsert(EmployeeRecord(whatsapp_number=OWNER_LID, name="المالك"))
    await _seed_deal(db)
    bus = _RecBus()
    await _pipe(db, bus)._handle_control("rerun", None, _rerun_msg(), NOW)
    # لا قيد في الدفتر هنا ⇒ تُعاد للطابور (لا رفض اعتماد)
    assert any("أُعيدت للطابور" in m for m in bus.central_msgs), bus.central_msgs
    assert not any("غير مُدرَج" in m for m in bus.central_msgs)


async def test_rerun_refuses_when_ledger_has_entry(db):
    """🔴 حارس الازدواج: صفقة لها قيد نازل → «أعد» تُرفض بسبب صريح (لا تُعيد كتابة البيع)."""
    from core.constants import Currency, Status as _St
    from core.models import LedgerEntry
    await db.employees.upsert(EmployeeRecord(whatsapp_number=OWNER_LID, name="المالك"))
    await _seed_deal(db)
    await db.ledger.append(LedgerEntry(
        entry_id="e1", deal_id="d-x1323", operation=OperationType.SELL,
        amount=48900.0, created_at=NOW, message_key=ORIG_KEY,
        currency=Currency.EGP, status=_St.COMPLETED))
    bus = _RecBus()
    await _pipe(db, bus)._handle_control("rerun", None, _rerun_msg(), NOW)
    assert any("ازدواج" in m for m in bus.central_msgs), bus.central_msgs
