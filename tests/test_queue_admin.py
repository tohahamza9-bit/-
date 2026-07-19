"""
الميزة ٢ — إدارة الطابور من اللوحة + الحالة MANUAL_COMPLETED (قرار المالك 2026-07-19).

العقود المُختبَرة:
  • الاستبعاد لا يحذف: الصفقة تبقى في القاعدة بحالة manual_completed + توثيق كامل.
  • سبب إلزاميّ (لا استبعاد بلا سبب).
  • أثر §11.4 لا يُمحى: ذات قيود الدفتر تُوسَم sell_was_written + needs_review.
  • الإرجاع مشروط بصفر قيود دفتر (منع ازدواج التنزيل).
  • **الأهمّ**: manual_completed حالة نهائيّة — لا يلتقطها أي منتقي عمل في الأنبوب،
    فلا تُنزَّل في أي إعادة تشغيل مستقبليّة.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.constants import Currency, OperationType, Status, TreasuryType
from core.models import Deal, LedgerEntry, ParsedLeg, TreasuryRef
from dashboard import queue_admin as qa

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"


def _deal(did: str, ref: str, status: Status = Status.PARSED, amount: float = 1000.0) -> Deal:
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code="570",
                    customer_name="زبون", amount=amount, phone="01011", price_normalized="6.0",
                    treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY))
    return Deal(deal_id=did, status=status, sell_leg=leg, created_at=NOW, updated_at=NOW,
                chat_jid=CENTRAL)


async def _ledger(db, deal_id: str) -> None:
    await db.ledger.append(LedgerEntry(
        entry_id=f"e-{deal_id}", deal_id=deal_id, message_key="k", reference_number="R",
        operation=OperationType.SELL, amount=1000.0, currency=Currency.EGP,
        status=Status.COMPLETED, created_at=NOW))


# ── الاستبعاد ────────────────────────────────────────────────────────────────────
async def test_exclude_marks_manual_completed_without_deleting(db):
    """الاستبعاد يحوّل الحالة ويوثّق — **ولا يحذف الصفقة من القاعدة** إطلاقًا."""
    await db.deals.upsert(_deal("d1", "X1"))
    out = await qa.exclude(db, qa.QueueExcludeIn(deal_ids=["d1"], reason="أُدخلت يدويًّا"), "mgr")
    assert out["excluded_count"] == 1
    doc = await db.deals.col.find_one({"deal_id": "d1"})
    assert doc is not None                                   # لم تُحذف
    assert doc["status"] == Status.MANUAL_COMPLETED.value
    ms = doc["manual_settlement"]
    assert ms["reason"] == "أُدخلت يدويًّا" and ms["settled_by"] == "mgr"
    assert ms["previous_status"] == Status.PARSED.value      # للإرجاع لاحقًا


def test_exclude_requires_a_reason():
    """سبب إلزاميّ — لا استبعاد بسبب فارغ/قصير (يُرفض عند التحقّق لا في القاعدة)."""
    with pytest.raises(Exception):
        qa.QueueExcludeIn(deal_ids=["d1"], reason="")
    with pytest.raises(Exception):
        qa.QueueExcludeIn(deal_ids=[], reason="سبب كافٍ")


async def test_exclude_preserves_half_written_trace(db):
    """🔴 صفقة نزّل البوت نصفها: تُوسَم sell_was_written + needs_review (أثر §11.4 لا يُمحى)."""
    await db.deals.upsert(_deal("d2", "X2", status=Status.SELL_DONE))
    await _ledger(db, "d2")
    await qa.exclude(db, qa.QueueExcludeIn(deal_ids=["d2"], reason="أُدخلت يدويًّا"), "mgr")
    ms = (await db.deals.col.find_one({"deal_id": "d2"}))["manual_settlement"]
    assert ms["sell_was_written"] is True and ms["needs_review"] is True and ms["ledger_entries"] == 1


async def test_exclude_skips_deals_not_in_queue(db):
    """صفقة مكتملة/غير موجودة لا تُستبعَد — تُذكَر في skipped بلا تعديل."""
    await db.deals.upsert(_deal("d3", "X3", status=Status.COMPLETED))
    out = await qa.exclude(db, qa.QueueExcludeIn(deal_ids=["d3", "ghost"], reason="سبب"), "mgr")
    assert out["excluded_count"] == 0 and len(out["skipped"]) == 2
    assert (await db.deals.col.find_one({"deal_id": "d3"}))["status"] == Status.COMPLETED.value


# ── الإرجاع ──────────────────────────────────────────────────────────────────────
async def test_restore_returns_deal_to_previous_status(db):
    """«أعد للتنفيذ» يُرجع الحالة السابقة ويمسح التوثيق (ضغطة خاطئة)."""
    await db.deals.upsert(_deal("d4", "X4", status=Status.MATCHED))
    await qa.exclude(db, qa.QueueExcludeIn(deal_ids=["d4"], reason="غلط"), "mgr")
    out = await qa.restore(db, qa.QueueRestoreIn(deal_ids=["d4"]), "mgr")
    assert out["restored_count"] == 1
    doc = await db.deals.col.find_one({"deal_id": "d4"})
    assert doc["status"] == Status.MATCHED.value and "manual_settlement" not in doc


async def test_restore_refused_when_ledger_entries_exist(db):
    """🔴 لها قيد دفتر → الإرجاع مرفوض بالسبب (إرجاعها يعيد التنزيل فيزدوج القيد §9)."""
    await db.deals.upsert(_deal("d5", "X5", status=Status.SELL_DONE))
    await _ledger(db, "d5")
    await qa.exclude(db, qa.QueueExcludeIn(deal_ids=["d5"], reason="أُدخلت يدويًّا"), "mgr")
    out = await qa.restore(db, qa.QueueRestoreIn(deal_ids=["d5"]), "mgr")
    assert out["restored_count"] == 0
    assert "قيد دفتر" in out["rejected"][0]["why"]
    assert (await db.deals.col.find_one({"deal_id": "d5"}))["status"] == Status.MANUAL_COMPLETED.value


# ── العرض ────────────────────────────────────────────────────────────────────────
async def test_list_queue_splits_pending_and_excluded(db):
    """القائمة تفصل المنتظرات عن المستبعدات وتحمل عدد قيود الدفتر (دليل الازدواج)."""
    await db.deals.upsert(_deal("d6", "X6", status=Status.READY))
    await db.deals.upsert(_deal("d7", "X7", status=Status.PARSED))
    await qa.exclude(db, qa.QueueExcludeIn(deal_ids=["d7"], reason="أُدخلت يدويًّا"), "mgr")
    out = await qa.list_queue(db)
    assert [r["deal_id"] for r in out["pending"]] == ["d6"]
    assert [r["deal_id"] for r in out["excluded"]] == ["d7"]
    assert out["excluded"][0]["excluded"] is True


# ── 🔴 العقد الأهمّ: نهائيّة الحالة ────────────────────────────────────────────────
async def test_manual_completed_never_picked_up_by_any_worker(db):
    """صفقة مستبعدة لا يلتقطها أي منتقي عمل — لا تُنزَّل في أي إعادة تشغيل مستقبليّة."""
    await db.deals.upsert(_deal("d8", "X8", status=Status.PARSED))
    await qa.exclude(db, qa.QueueExcludeIn(deal_ids=["d8"], reason="أُدخلت يدويًّا"), "mgr")
    for statuses in [(Status.PARSED,), (Status.MATCHING, Status.HELD),
                     (Status.WAITING_SECOND_LEG,), (Status.SELL_DONE, Status.READY)]:
        picked = await db.deals.by_status(*statuses)
        assert all(d.deal_id != "d8" for d in picked), f"التُقطت في {statuses}!"


def test_manual_completed_is_a_distinct_terminal_status():
    """الحالة موجودة ومتميّزة عن completed (شارة «أُدخلت يدويًّا» تعتمد عليها)."""
    assert Status.MANUAL_COMPLETED.value == "manual_completed"
    assert Status.MANUAL_COMPLETED != Status.COMPLETED
