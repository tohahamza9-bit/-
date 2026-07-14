"""
قاعدة GROSS/NET لقيود الخصم (إصلاح باغ الإلغاء) — اختبارات مباشرة على build_cancellation_jobs.

القاعدة المؤكَّدة من MONEYADO: عند إلغاء حوالة خصم بيع-فقط (A أو SI) → خانة الكمية = **GROSS**
(المبلغ قبل الخصم = leg.amount) والعمولة = abs موجبة؛ MONEYADO يطرحها تلقائيًّا (GROSS − commission = NET).
بيع+شراء → كلا الطرفين NET بلا عمولة (لا يتغيّر).
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.cancellation import build_cancellation_jobs
from core.constants import OperationType, Status, TreasuryType
from core.models import Deal, ParsedLeg, TreasuryRef

NOW = datetime(2026, 7, 14, 12, 0, 0, tzinfo=timezone.utc)
_T = TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY)


def _disc(gross, disc, ref, si=False):
    """طرف بيع بخصم: leg.amount = GROSS (قبل الخصم)، commission سالب (خصم)."""
    return ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code="1208",
                     customer_name="زبون", amount=float(gross), amount_after_discount=float(gross - disc),
                     commission=float(-disc), treasury=_T, source_message_key="orig", is_si_format=si)


def _deal(sell=None, buy=None, amendments=None):
    return Deal(deal_id="d1", status=Status.COMPLETED, sell_leg=sell, buy_leg=buy,
                is_two_legged=buy is not None, created_at=NOW, updated_at=NOW,
                amendments=amendments or [], source_message_keys=["orig"])


# ── إلغاء خصم بيع-فقط بدون تعديل → الكمية=GROSS، العمولة=+abs ─────────────────
def test_cancel_A_discount_no_amend_uses_gross():
    jobs = build_cancellation_jobs(_deal(sell=_disc(1600, 16, "A100")), NOW, "A100")
    assert len(jobs) == 1 and jobs[0].operation == OperationType.BUY
    assert jobs[0].leg.amount == 1600.0        # GROSS (قبل الخصم) — لا 1584 (NET)
    assert jobs[0].leg.commission == 16.0      # abs موجب — يُطرَح فعليًّا


def test_cancel_SI_discount_no_amend_uses_gross():
    jobs = build_cancellation_jobs(_deal(sell=_disc(5060, 50, "SI900", si=True)), NOW, "SI900")
    assert jobs[0].leg.amount == 5060.0        # نفس قاعدة A بالضبط
    assert jobs[0].leg.commission == 50.0


# ── إلغاء خصم بيع-فقط بعد تعديل → آخر GROSS + ملاحظة التحوّل ───────────────────
def test_cancel_A_discount_after_amend_uses_last_gross_and_note():
    # بعد تعديل: leg.amount = GROSS الجديد (7070)، commission=-70؛ الأصلي GROSS=10100
    leg = _disc(7070, 70, "A100").model_copy(update={"amount_after_discount": None})
    deal = _deal(sell=leg, amendments=[
        {"old_net": 10100, "old_commission": -101, "new_net": 7070, "new_commission": 70}])
    jobs = build_cancellation_jobs(deal, NOW, "A100")
    assert jobs[0].leg.amount == 7070.0        # آخر GROSS (لا 7140، لا 7000)
    assert jobs[0].leg.commission == 70.0
    assert "بعد تعديل من 10100 إلى 7070" in (jobs[0].leg.recipient_name or "")


def test_cancel_SI_discount_after_amend_uses_last_gross_and_note():
    leg = _disc(3030, 30, "SI900", si=True).model_copy(update={"amount_after_discount": None})
    deal = _deal(sell=leg, amendments=[
        {"old_net": 5060, "old_commission": -50, "new_net": 3030, "new_commission": 30}])
    jobs = build_cancellation_jobs(deal, NOW, "SI900")
    assert jobs[0].leg.amount == 3030.0
    assert jobs[0].leg.commission == 30.0
    assert "بعد تعديل من 5060 إلى 3030" in (jobs[0].leg.recipient_name or "")


# ── بيع+شراء → كلا الطرفين NET بلا عمولة (لا يتغيّر) ──────────────────────────
def test_cancel_two_leg_both_net_no_commission():
    sell = ParsedLeg(operation=OperationType.SELL, reference_number="A200", customer_code="1",
                     amount=20271.0, treasury=_T, source_message_key="orig")   # NET، بلا عمولة
    buy = ParsedLeg(operation=OperationType.BUY, reference_number="A200", customer_code="2",
                    amount=20271.0, treasury=_T, source_message_key="orig")
    jobs = build_cancellation_jobs(_deal(sell=sell, buy=buy), NOW, "A200")
    assert len(jobs) == 2
    assert jobs[0].operation == OperationType.BUY and jobs[0].leg.amount == 20271.0
    assert jobs[0].leg.commission is None      # NET بلا عمولة — بلا تحويل GROSS
    assert jobs[1].operation == OperationType.SELL and jobs[1].leg.amount == 20271.0
