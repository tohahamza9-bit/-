"""
العرض 2-A (مُحسَّن) — التمييز الصريح بين «تضييقٍ جزئيّ» (التباسٌ حقيقيّ → تعليق+تصعيد) و«بلا
تضييق» (لا تمييز → FIFO الحتميّ عبر 1-A). الشرط صريح: مقارنة حجم المرشّحين قبل/بعد الإقصاء.

  • أقصى المحتوى بعضَهم وبقي >1  ⇒ تعليق+تصعيد (بلا تخمين الأقدم) — علاج X1706–X1711.
  • لم يُقصِ المحتوى أحدًا (>1)     ⇒ FIFO الأقدم الحتميّ — يحفظ إنتاجيّة الدفعة (X850).
  • أقصى المحتوى حتى بقي واحد      ⇒ ربطٌ بالمحتوى.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.constants import Currency, OperationType, Status
from core.models import Deal, ParsedLeg, RawMessage, WriteResult
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 21, 18, 0, 0, tzinfo=timezone.utc)
CENTRAL, ADMIN, S1 = "central@g.us", "admin@g.us", "s1@lid"


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


def _pipe(db):
    return Pipeline(db, Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN), _W(), _V(),
                   customer_room_jids=[], treasury_room_jids=[])


def _waiting(deal_id: str, cur: Currency, offset: int) -> Deal:
    """صفقة منتظِرة (بلا خزينة، بلا كود) — هدف صالح لتكملةٍ تحمل خزينة."""
    at = NOW + timedelta(seconds=offset)
    leg = ParsedLeg(operation=OperationType.SELL, amount=1000.0, currency=cur,
                    sender_jid=S1, source_message_key=f"{deal_id}-a")
    return Deal(deal_id=deal_id, status=Status.WAITING_SECOND_LEG, sell_leg=leg,
                created_at=at, updated_at=at, first_received_at=at, chat_jid=CENTRAL)


def _raw():
    return RawMessage(message_key="frag", chat_jid=CENTRAL, sender_jid=S1,
                      text="بلاس فون", received_at=NOW + timedelta(seconds=30))


def _egp_frag():
    """تكملة خزينة مصريّة (بلا كود) — عملتها EGP صراحةً."""
    return ParsedLeg(operation=OperationType.SELL, currency=Currency.EGP)


async def _admin_escalated(db) -> bool:
    outs = await db.outgoing.next_unsent(50)
    return any(o["chat_jid"] == ADMIN for o in outs)


# ═════════════════════════════════════════════════════════════════════════════
async def test_partial_narrow_holds_and_escalates(db):
    """3 معلّقات (EGP، EGP، TND) + تكملة EGP → العملة تُقصي TND (تضييقٌ جزئيّ) ويبقى EGPان
    متوافقان = التباسٌ حقيقيّ ⇒ **تعليق + تصعيد** بلا تخمين الأقدم."""
    pipe = _pipe(db)
    cands = [_waiting("egp1", Currency.EGP, 0), _waiting("egp2", Currency.EGP, 1),
             _waiting("tnd3", Currency.TND, 2)]
    target = await pipe._choose_link_target(
        _raw(), cands, [], [], NOW + timedelta(seconds=30), frag=_egp_frag(), stage="test")
    assert target is None, "التضييق الجزئيّ + التباس يجب أن يُعلَّق لا يُربَط بالأقدم"
    assert await _admin_escalated(db), "لا بدّ من تصعيد للمسؤول عند الالتباس"


async def test_content_narrow_to_one_links(db):
    """معلّقتان (EGP، TND) + تكملة EGP → العملة تحسم واحدةً (EGP) ⇒ ربطٌ بالمحتوى (لا تعليق)."""
    pipe = _pipe(db)
    cands = [_waiting("egp1", Currency.EGP, 0), _waiting("tnd2", Currency.TND, 1)]
    target = await pipe._choose_link_target(
        _raw(), cands, [], [], NOW + timedelta(seconds=30), frag=_egp_frag(), stage="test")
    assert target is not None and target.deal_id == "egp1"
    assert not await _admin_escalated(db), "الحسمُ بالمحتوى لا يُصعِّد"


async def test_no_narrow_uses_deterministic_fifo(db):
    """معلّقتان EGP (لا تمييز محتوائيّ) + تكملة EGP → لم يُقصِ المحتوى أحدًا ⇒ FIFO الأقدم الحتميّ
    (X850، إنتاجيّة الدفعة) — لا تعليق."""
    pipe = _pipe(db)
    cands = [_waiting("egp_old", Currency.EGP, 0), _waiting("egp_new", Currency.EGP, 5)]
    target = await pipe._choose_link_target(
        _raw(), cands, [], [], NOW + timedelta(seconds=30), frag=_egp_frag(), stage="test")
    assert target is not None and target.deal_id == "egp_old", "بلا تمييز → الأقدم حتميًّا"
    assert not await _admin_escalated(db), "التسلسل الشرعيّ لا يُصعَّد"
