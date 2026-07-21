"""
العرض 2-B — الادّعاء الذرّيّ لحقّ إكمال صفقة (منع الابتلاع المزدوج تحت السباق).

الجذر: اختيار هدف التكملة غير ذرّيّ (waiting_in_room → cands[0] → absorb، بلا حجز)، فكيرنلان
متزامنان (أو إعادة معالجة) يمسكان نفس أقدم معلّقة ويبتلعانها مرّتين → إدخال/إزاحة خاطئة.

الإصلاح: DealRepo.claim_completion_attempt — ادّعاء ذرّيّ (status=WAITING + attempted_at=None)،
أوّل مُطالِب يفوز فقط (نمط begin_cancelling). الخاسر يتخطّى الهدف.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from core.constants import Status
from core.models import Deal

NOW = datetime(2026, 7, 21, 18, 0, 0, tzinfo=timezone.utc)


def _waiting(deal_id: str) -> Deal:
    return Deal(deal_id=deal_id, status=Status.WAITING_SECOND_LEG,
               created_at=NOW, updated_at=NOW, first_received_at=NOW, chat_jid="c@g.us")


async def test_claim_first_wins_only_once(db):
    """مُطالِبان متزامنان على نفس الصفقة → واحدٌ فقط يفوز (إقصاء متبادل)."""
    await db.deals.upsert(_waiting("d1"))
    r1, r2 = await asyncio.gather(
        db.deals.claim_completion_attempt("d1", NOW),
        db.deals.claim_completion_attempt("d1", NOW),
    )
    assert sorted([r1, r2]) == [False, True], f"لا بدّ من فائزٍ واحد فقط: {(r1, r2)}"
    d = await db.deals.get("d1")
    assert d.completion_attempt_count == 1          # زِيد مرّةً واحدة رغم الاستدعاءين
    assert d.completion_attempted_at is not None


async def test_claim_fails_when_not_waiting(db):
    """صفقةٌ غادرت الانتظار (اكتملت/مُصعَّدة) لا يمكن ادّعاؤها."""
    d = _waiting("d2")
    d.status = Status.PARSED
    await db.deals.upsert(d)
    assert await db.deals.claim_completion_attempt("d2", NOW) is False


async def test_second_message_cannot_reclaim_attempted_deal(db):
    """بعد محاولةٍ أولى (attempted_at مضبوط) لا تُعاد الصفقة ادّعاءً لتكملةٍ عديمة مرجع أخرى."""
    await db.deals.upsert(_waiting("d3"))
    assert await db.deals.claim_completion_attempt("d3", NOW) is True    # الأولى تفوز
    # الصفقة ما زالت WAITING (لم يُكمِلها الدمج بعد) لكنها مُدّعاة سلفًا → الثانية تخسر
    assert await db.deals.claim_completion_attempt("d3", NOW) is False
