"""
اختبارات الحارس (§9) والإلغاء/التعديل/التصحيح (§10).
أوقات صريحة، بيانات واقعية من المواصفات (§5.5 A6779). fixture `db` من conftest.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.constants import Currency, OperationType, Status, TreasuryType
from core.guard import build_reversal, is_out_of_active_window
from core.guard.idempotency import Guard
from core.models import Deal, LedgerEntry, ParsedLeg, RawMessage, TreasuryRef

T0 = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)


# ── مُساعدات بناء ────────────────────────────────────────────────────────────
def _sell_leg(*, customer="53", amount=10000.0, ref="A6779") -> ParsedLeg:
    """طرف بيع كامل بخزينة بلاس فون (74) — لبناء صفقة اختبار."""
    return ParsedLeg(
        operation=OperationType.SELL,
        customer_code=customer,
        customer_name="احمد العكاري",
        amount=amount,
        currency=Currency.EGP,
        reference_number=ref,
        phone="01115233493",
        treasury=TreasuryRef(
            code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY, currency=Currency.EGP
        ),
    )


def _deal(deal_id="D1", sell_leg=None, keys=None) -> Deal:
    return Deal(
        deal_id=deal_id,
        status=Status.COMPLETED,
        sell_leg=sell_leg if sell_leg is not None else _sell_leg(),
        created_at=T0,
        updated_at=T0,
        source_message_keys=keys or ["MK-sell"],
    )


def _ledger_entry(
    *, entry_id, deal_id="D1", message_key="MK-sell", operation=OperationType.SELL,
    amount=10000.0, customer="53", ref="A6779", treasury="74", is_reversal=False,
) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        deal_id=deal_id,
        message_key=message_key,
        reference_number=ref,
        operation=operation,
        is_reversal=is_reversal,
        amount=amount,
        currency=Currency.EGP,
        customer_code=customer,
        treasury_code=treasury,
        status=Status.COMPLETED,
        created_at=T0,
    )


# ── §9: منع التكرار عبر الدفتر + message key ────────────────────────────────
async def test_already_downloaded_blocks_repeat(db):
    await db.ledger.append(_ledger_entry(entry_id="E1", message_key="MK1"))
    guard = Guard(db)
    assert await guard.already_downloaded("MK1") is True
    assert await guard.already_downloaded("MK-unknown") is False


async def test_reversal_entry_does_not_count_as_downloaded(db):
    # القيد العكسي ليس تنزيلًا أصليًا (was_downloaded يشترط is_reversal=False)
    await db.ledger.append(
        _ledger_entry(entry_id="Erev", message_key="MK2", is_reversal=True)
    )
    assert await Guard(db).already_downloaded("MK2") is False


# ── §9: تصنيف الرقم الإشاري — الحكم من الدفتر ────────────────────────────────
async def test_classify_reference_two_legs_vs_duplicate(db):
    # طرف بيع منزّل بالرقم A6779 (زبون 53، مبلغ 8475)
    await db.ledger.append(
        _ledger_entry(entry_id="E-sell", operation=OperationType.SELL,
                      amount=8475.0, customer="53", ref="A6779")
    )
    guard = Guard(db)

    # نفس الرقم + محتوى مختلف (مورد 760، مبلغ 8391) = طرفان
    buy_leg = ParsedLeg(
        operation=OperationType.BUY, customer_code="760", amount=8391.0,
        currency=Currency.EGP, reference_number="A6779",
    )
    assert await guard.classify_reference("A6779", buy_leg) == "two_legs"

    # نفس الرقم + نسخة طبق الأصل (نفس الزبون والمبلغ) = تكرار يُتجاهل
    dup_leg = ParsedLeg(
        operation=OperationType.SELL, customer_code="53", amount=8475.0,
        currency=Currency.EGP, reference_number="A6779",
    )
    assert await guard.classify_reference("A6779", dup_leg) == "duplicate"

    # رقم غير موجود في الدفتر = مستقلة
    new_leg = ParsedLeg(
        operation=OperationType.SELL, customer_code="99", amount=500.0,
        currency=Currency.EGP, reference_number="A9999",
    )
    assert await guard.classify_reference("A9999", new_leg) == "independent"
    assert await guard.classify_reference(None, new_leg) == "independent"


# ── §9/§10: is_cancelled + guard_before_write ───────────────────────────────
async def test_is_cancelled_detects_reply(db):
    await db.raw.insert(RawMessage(
        message_key="MK-cancel", chat_jid="markazia", text="إلغاء",
        received_at=T0, reply_to_key="MK-orig",
    ))
    guard = Guard(db)
    assert await guard.is_cancelled("MK-orig") is True
    assert await guard.is_cancelled("MK-other") is False


async def test_guard_before_write_blocks_downloaded_and_cancelled(db):
    guard = Guard(db)

    # صفقة نظيفة → مسموح
    allowed, reason = await guard.guard_before_write(_deal(keys=["MK-clean"]))
    assert allowed is True and reason is None

    # نُزّلت من قبل → ممنوع
    await db.ledger.append(_ledger_entry(entry_id="Ed", message_key="MK-dl"))
    allowed, reason = await guard.guard_before_write(_deal(keys=["MK-dl"]))
    assert allowed is False and "نُزّلت" in reason

    # عليها إلغاء → ممنوع
    await db.raw.insert(RawMessage(
        message_key="MK-c", chat_jid="markazia", text="الغاء الحوالة",
        received_at=T0, reply_to_key="MK-active",
    ))
    allowed, reason = await guard.guard_before_write(_deal(keys=["MK-active"]))
    assert allowed is False and "إلغاء" in reason


# ── §10: إلغاء — عكس بكامل القيمة، نفس الخزينة ──────────────────────────────
async def test_build_reversal_cancel_full_amount(db):
    deal = _deal(sell_leg=_sell_leg(amount=10000.0))
    await db.ledger.append(_ledger_entry(entry_id="E1", amount=10000.0))

    jobs = await build_reversal("cancel", None, deal, db)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.is_reversal is True
    assert job.operation == OperationType.BUY          # عكس البيع = شراء
    assert job.leg.amount == 10000.0                    # كامل القيمة (تصفير)
    assert job.leg.treasury.code == "74"               # نفس الخزينة


async def test_build_reversal_cancel_both_legs(db):
    # صفقة طرفين: بيع 8475 + شراء 8391 → إلغاء = عكس الطرفين
    deal = _deal(sell_leg=_sell_leg(amount=8475.0))
    deal.buy_leg = ParsedLeg(
        operation=OperationType.BUY, customer_code="760", amount=8391.0,
        currency=Currency.EGP, reference_number="A6779",
        treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY),
    )
    deal.is_two_legged = True
    await db.ledger.append(_ledger_entry(entry_id="E-s", operation=OperationType.SELL, amount=8475.0))
    await db.ledger.append(
        _ledger_entry(entry_id="E-b", operation=OperationType.BUY, amount=8391.0, customer="760")
    )

    jobs = await build_reversal("cancel", None, deal, db)
    ops = {j.operation for j in jobs}
    assert len(jobs) == 2
    assert ops == {OperationType.BUY, OperationType.SELL}   # عكس البيع=شراء، عكس الشراء=بيع


# ── §10: تعديل — عكس بالفرق فقط ─────────────────────────────────────────────
async def test_build_reversal_edit_difference_only(db):
    deal = _deal(sell_leg=_sell_leg(amount=10000.0))
    await db.ledger.append(_ledger_entry(entry_id="E1", amount=10000.0))

    jobs = await build_reversal("edit", 9000.0, deal, db)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.is_reversal is True
    assert job.operation == OperationType.BUY
    assert job.leg.amount == 1000.0                     # الفرق فقط (10000 − 9000)
    assert job.leg.treasury.code == "74"


async def test_build_reversal_correct_increase_same_direction(db):
    # تصحيح على «تمّت» بقيمة أكبر → قيد إضافي بنفس الاتجاه بالفرق (يُحسب من الدفتر)
    deal = _deal(sell_leg=_sell_leg(amount=10000.0))
    await db.ledger.append(_ledger_entry(entry_id="E1", amount=10000.0))

    jobs = await build_reversal("correct", 10500.0, deal, db)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.operation == OperationType.SELL          # نفس اتجاه الأصل (زيادة)
    assert job.is_reversal is False
    assert job.leg.amount == 500.0


async def test_build_reversal_no_downloaded_entries(db):
    # لا قيود منزّلة (تصحيح على «ملغاة») → البوت لا يبني قيدًا
    deal = _deal()
    jobs = await build_reversal("cancel", None, deal, db)
    assert jobs == []


# ── §10/§12: خارج نافذة 15 يومًا ────────────────────────────────────────────
def test_is_out_of_active_window():
    created = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 7, 5, 12, 0, 0, tzinfo=timezone.utc)   # 34 يومًا
    assert is_out_of_active_window(created, now) is True

    # داخل النافذة (4 أيام)
    recent = datetime(2026, 7, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert is_out_of_active_window(recent, now) is False

    # بالضبط 15 يومًا → داخل النافذة (النشاط يشمل 15)
    exactly = datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc)
    assert is_out_of_active_window(exactly, now) is False
