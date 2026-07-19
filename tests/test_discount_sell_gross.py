"""
قاعدة قيد البيع للحوالة المخصومة (حكم المالك النهائي — م: X1322).

**القاعدة الموحّدة:** قيد **البيع** لأيّ حوالة مخصومة يُكتب بـ:
    المبلغ الأجنبي = الإجمالي (GROSS)   +   العمولة = الفرق **بالسالب**
وMONEYADO يشتقّ الصافي بنفسه (GROSS − |عمولة| = NET). لا يُكتب الصافي مباشرةً بلا عمولة.

**لماذا:** خانة العمولة في MONEYADO **تُطرَح فعليًّا** وليست توثيقيّة (تأكيد محاسبيّ مباشر،
أُثبِت في a1e8e7c). القاعدة القديمة «الطرفان NET» (2680f6f) كانت مبنيّة على الافتراض المعاكس،
فسقطت بسقوطه. الآن كل المسارات متّسقة: خصم بيع-فقط (§6.2)، خصم ثنائيّ (§6.1)، الإلغاء، التعديل.

**طرف الشراء** يبقى بالصافي بلا عمولة (عرف MONEYADO القائم — لم يُغيَّر).
"""
from __future__ import annotations

from datetime import datetime, timezone

from core.constants import Currency, OperationType, Status, TreasuryType
from core.models import Deal, ParsedLeg, SupplierRef, TreasuryRef

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
# خزينة الخصم الثنائيّة (فودافون بالخصم) — الطرفان فيها.
_DISC_T = TreasuryRef(code="85", name="فودافون بالخصم", type=TreasuryType.SELL_AND_BUY,
                      currency=Currency.EGP)


def _pipe(db):
    from core.pipeline import Pipeline

    class _Bus:
        central_jid, admin_jid = "c@g.us", "a@g.us"
        async def notify_admin(self, *a, **k): ...
        async def reply_central(self, *a, **k): ...

    class _W:
        name = "w"
        async def write(self, job, *, commit): ...
    return Pipeline(db, _Bus(), _W(), None, customer_room_jids=[], treasury_room_jids=[])


def _x1322_deal() -> Deal:
    """X1322 كما وردت: msg1 إجمالي 8700 + زبون 65 احمد خليل 6.04،
    msg2 صافي 8613 + مورد 760 طه 6.05 (خصم 1% = 87)."""
    sell = ParsedLeg(operation=OperationType.SELL, reference_number="X1322",
                     customer_code="65", customer_name="احمد خليل", amount=8700.0,
                     amount_after_discount=8613.0, commission=-87.0, currency=Currency.EGP,
                     price_normalized="6.04", treasury=_DISC_T, source_message_key="m1")
    buy = ParsedLeg(operation=OperationType.BUY, reference_number="X1322",
                    customer_code="760", customer_name="طه", amount=8613.0,
                    currency=Currency.EGP, is_supplier_counterpart=True,
                    price_normalized="6.05", treasury=_DISC_T, source_message_key="m2")
    return Deal(deal_id="d-x1322", status=Status.PARSED, sell_leg=sell, buy_leg=buy,
                is_two_legged=True, created_at=NOW, updated_at=NOW,
                source_message_keys=["m1", "m2"])


# ═══════════════════════════════════════════════════════════════════════════
# (1) الحالة الحيّة X1322 — ثنائية مخصومة
# ═══════════════════════════════════════════════════════════════════════════
async def test_x1322_sell_written_gross_with_negative_commission(db):
    """🔴 الحالة الحيّة: البيع = 8700 بعمولة −87 (لا 8613 بلا عمولة)، والشراء = 8613 بلا عمولة."""
    deal = _x1322_deal()
    await _pipe(db)._resolve_two_leg(deal)
    assert deal.sell_leg.amount == 8700.0, "قيد البيع كُتب بالصافي بدل الإجمالي"
    assert deal.sell_leg.commission == -87.0, "عمولة البيع السالبة مفقودة"
    assert deal.sell_leg.commission_rate == 0.0
    # الصافي الذي يشتقّه MONEYADO
    assert deal.sell_leg.amount + deal.sell_leg.commission == 8613.0
    # طرف الشراء: صافي بلا عمولة (بلا تغيير — عرف MONEYADO القائم)
    assert deal.buy_leg.amount == 8613.0 and deal.buy_leg.commission is None


async def test_x1322_screen_fields_match_owner_ruling(db):
    """ما يُكتب فعليًّا في شاشة البيع: المبلغ الأجنبي 8700 والعمولة −87 ونسبة العمولة 0."""
    from core.writers.moneyado.fields import build_sell_fields
    deal = _x1322_deal()
    await _pipe(db)._resolve_two_leg(deal)
    ops = {op.key: op.value for op in build_sell_fields(deal.sell_leg)}
    assert ops["foreign_amount"] == "8700"
    assert ops["commission"] == "-87"
    assert ops["commission_rate"] == "0"


# ═══════════════════════════════════════════════════════════════════════════
# (2) المسار B — طرف شراء مشتقّ من مورد صريح (SI بيع+شراء)
# ═══════════════════════════════════════════════════════════════════════════
async def test_synthesized_buy_leg_keeps_sell_gross(db):
    """اشتقاق طرف الشراء من «المورد: …» لا يحوّل البيع للصافي: يبقى GROSS بعمولة سالبة."""
    sell = ParsedLeg(operation=OperationType.SELL, reference_number="SI1417",
                     customer_code="570", customer_name="زبون", amount=20475.0,
                     amount_after_discount=20271.0, currency=Currency.EGP,
                     price_normalized="5.9", treasury=_DISC_T,
                     supplier=SupplierRef(code="760", name="طه"), supplier_price_raw="5.72",
                     is_si_format=True, source_message_key="m1")
    deal = Deal(deal_id="d-si", status=Status.PARSED, sell_leg=sell, created_at=NOW,
                updated_at=NOW, source_message_keys=["m1"])
    _pipe(db)._maybe_synthesize_buy_leg(deal)
    assert deal.is_two_legged is True
    assert deal.sell_leg.amount == 20475.0 and deal.sell_leg.commission == -204.0
    assert deal.buy_leg.amount == 20271.0 and deal.buy_leg.commission is None


# ═══════════════════════════════════════════════════════════════════════════
# (3) الحوالات بلا خصم لا تتأثّر إطلاقًا
# ═══════════════════════════════════════════════════════════════════════════
async def test_two_leg_without_discount_unchanged(db):
    """طرفان بلا خصم (المبلغان متساويان) → بلا عمولة على أيّ طرف، كما كان تمامًا."""
    sell = ParsedLeg(operation=OperationType.SELL, reference_number="A1",
                     customer_code="1", amount=1000.0, currency=Currency.EGP,
                     price_normalized="5.9", treasury=_DISC_T, source_message_key="m1")
    buy = ParsedLeg(operation=OperationType.BUY, reference_number="A1", customer_code="760",
                    amount=1000.0, currency=Currency.EGP, is_supplier_counterpart=True,
                    price_normalized="5.86", treasury=_DISC_T, source_message_key="m2")
    deal = Deal(deal_id="d-nodisc", status=Status.PARSED, sell_leg=sell, buy_leg=buy,
                is_two_legged=True, created_at=NOW, updated_at=NOW,
                source_message_keys=["m1", "m2"])
    await _pipe(db)._resolve_two_leg(deal)
    assert deal.sell_leg.amount == 1000.0 and deal.sell_leg.commission is None
    assert deal.buy_leg.amount == 1000.0 and deal.buy_leg.commission is None


async def test_sell_only_discount_path_unchanged(db):
    """خصم بيع-فقط (§6.2) كان صحيحًا أصلًا (GROSS + سالبة) — لم يمسّه التغيير."""
    from core.writers.moneyado.fields import build_sell_fields
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="A9", customer_code="1208",
                    customer_name="زبون", amount=8550.0, amount_after_discount=8465.0,
                    commission=-85.0, currency=Currency.EGP, price_normalized="5.9",
                    treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY),
                    source_message_key="m1")
    ops = {op.key: op.value for op in build_sell_fields(leg)}
    assert ops["foreign_amount"] == "8550" and ops["commission"] == "-85"


# ═══════════════════════════════════════════════════════════════════════════
# (4) الإلغاء والتعديل يبقيان متّسقين مع القاعدة الجديدة
# ═══════════════════════════════════════════════════════════════════════════
async def test_cancelling_discounted_two_leg_reverses_gross(db):
    """إلغاء ثنائية مخصومة بعد الإصلاح: القيد العكسيّ للبيع = GROSS 8700 + عمولة +87 (abs)
    — يطابق القيد الأصليّ تمامًا فيصفّره، بدل عكس 8613 الخاطئ."""
    from core.cancellation import build_cancellation_jobs
    deal = _x1322_deal()
    await _pipe(db)._resolve_two_leg(deal)
    jobs = build_cancellation_jobs(deal, NOW, "X1322")
    assert jobs[0].operation == OperationType.BUY
    assert jobs[0].leg.amount == 8700.0 and jobs[0].leg.commission == 87.0
    assert jobs[0].leg.amount - jobs[0].leg.commission == 8613.0


async def test_amendment_on_discounted_deal_still_consistent(db):
    """التعديل يعمل على الإجمالي (a1e8e7c) — يبقى متّسقًا مع البيع المكتوب إجماليًّا."""
    from core.amendment import amendment_ratio, floor_commission
    deal = _x1322_deal()
    await _pipe(db)._resolve_two_leg(deal)
    leg = deal.sell_leg
    ratio = amendment_ratio(deal)
    assert abs(ratio - 0.01) < 1e-6, "نسبة الخصم تُشتقّ من الإجمالي والعمولة"
    # تعديل إلى إجماليّ جديد 5000 ⇒ عمولة 50 (1%)
    assert floor_commission(5000.0, ratio) == 50.0
