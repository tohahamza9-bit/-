"""
إدارة الطابور من اللوحة (الميزة ٢، قرار المالك 2026-07-19) — منطق نقيّ يستدعيه الراوتر.

القواعد الحاكمة:
  • **ممنوع الحذف الفعليّ** من القاعدة. الاستبعاد = تحويل الحالة إلى MANUAL_COMPLETED مع
    توثيق `manual_settlement` (سبب إلزاميّ + مَن + متى + الحالة السابقة) — تبقى الصفقة
    بالسجلّ بشارة «استُبعد يدويًّا».
  • **سبب إلزاميّ**: لا استبعاد بلا سبب يكتبه المدير (يُفرَض بـmin_length في المخطّط).
  • **أثر §11.4 لا يُمحى**: صفقة لها قيود دفتر تُوسَم sell_was_written + needs_review — البوت
    كان قد نزّل نصفها، والمالك يفحص الازدواج.
  • **الإرجاع مشروط بصفر قيود دفتر** (§9): إرجاع صفقة لها قيد يعيد تنزيلها فيزدوج القيد،
    لذا تُرفض بالاسم مع السبب بدل أن تُنفَّذ صامتةً.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from core.constants import Status
from core.db import Database, utcnow
from core.logging_setup import get_logger

log = get_logger(__name__)

# حالات «منتظِرة» تُعرَض في الطابور (نفس تعريف أداة التسوية — مصدر واحد للحقيقة)
PENDING_STATUSES = (Status.PARSED, Status.READY, Status.MATCHED, Status.SELL_DONE)


class QueueExcludeIn(BaseModel):
    deal_ids: list[str] = Field(min_length=1)
    reason: str = Field(min_length=3, max_length=500)   # إلزاميّ — لا استبعاد بلا سبب


class QueueRestoreIn(BaseModel):
    deal_ids: list[str] = Field(min_length=1)


def _first_leg(deal: dict) -> dict:
    return deal.get("sell_leg") or deal.get("buy_leg") or {}


def _row(deal: dict, ledger_n: int) -> dict:
    leg = _first_leg(deal)
    created = deal.get("created_at")
    age_h = None
    if created is not None:
        delta = utcnow() - (created if created.tzinfo else created.replace(tzinfo=utcnow().tzinfo))
        age_h = round(delta.total_seconds() / 3600, 1)
    ms = deal.get("manual_settlement") or {}
    return {
        "deal_id": deal.get("deal_id"),
        "reference": leg.get("reference_number"),
        "customer_code": leg.get("customer_code"),
        "customer_name": leg.get("customer_name"),
        "amount": leg.get("amount"),
        "currency": leg.get("currency"),
        "treasury": (leg.get("treasury") or {}).get("name"),
        "status": deal.get("status"),
        "created_at": created.isoformat() if created else None,
        "age_hours": age_h,
        "ledger_entries": ledger_n,
        "excluded": bool(ms),
        "settlement_reason": ms.get("reason"),
        "settled_by": ms.get("settled_by"),
        "sell_was_written": ms.get("sell_was_written", False),
        "needs_review": ms.get("needs_review", False),
        "previous_status": ms.get("previous_status"),
    }


async def list_queue(db: Database, *, excluded_limit: int = 200) -> dict:
    """المنتظرات + آخر المستبعدات يدويًّا، مع عدد قيود الدفتر لكل صفقة (دليل الازدواج)."""
    pending, excluded = [], []
    cur = db.deals.col.find({"status": {"$in": [s.value for s in PENDING_STATUSES]}}).sort("created_at", 1)
    async for d in cur:
        n = await db.ledger.col.count_documents({"deal_id": d.get("deal_id")})
        pending.append(_row(d, n))
    cur2 = (db.deals.col.find({"status": Status.MANUAL_COMPLETED.value})
            .sort("updated_at", -1).limit(excluded_limit))
    async for d in cur2:
        n = await db.ledger.col.count_documents({"deal_id": d.get("deal_id")})
        excluded.append(_row(d, n))
    return {"pending": pending, "excluded": excluded,
            "pending_count": len(pending), "excluded_count": len(excluded)}


async def exclude(db: Database, body: QueueExcludeIn, username: str) -> dict:
    """استبعاد صفقات من التنزيل (manual_completed موثَّق). يتخطّى ما ليس في حالة منتظِرة."""
    now = utcnow()
    done, skipped = [], []
    pend = [s.value for s in PENDING_STATUSES]
    for deal_id in body.deal_ids:
        deal = await db.deals.col.find_one({"deal_id": deal_id})
        if deal is None or deal.get("status") not in pend:
            skipped.append({"deal_id": deal_id,
                            "why": "ليست في الطابور (حالتها تغيّرت أو غير موجودة)"})
            continue
        n = await db.ledger.col.count_documents({"deal_id": deal_id})
        settlement = {
            "reason": body.reason, "settled_at": now, "settled_by": username,
            "sell_was_written": n > 0, "ledger_entries": n, "needs_review": n > 0,
            "previous_status": deal.get("status"),
        }
        res = await db.deals.col.update_one(
            {"deal_id": deal_id, "status": deal.get("status")},   # حارس تزامن
            {"$set": {"status": Status.MANUAL_COMPLETED.value,
                      "manual_settlement": settlement, "updated_at": now}})
        if res.modified_count:
            done.append(deal_id)
        else:
            skipped.append({"deal_id": deal_id, "why": "تغيّرت الحالة أثناء التنفيذ"})
    log.info("استبعاد يدويّ من الطابور: %s نجحت، %s تُخطّيت (بواسطة %s)",
             len(done), len(skipped), username)
    return {"excluded": done, "skipped": skipped,
            "excluded_count": len(done), "needs_review_note":
                "الصفقات ذات قيود الدفتر وُسمت needs_review — البوت نزّل نصفها، افحص الازدواج."}


async def restore(db: Database, body: QueueRestoreIn, username: str) -> dict:
    """إرجاع مستبعدة للطابور — **يُرفض** لو لها أي قيد دفتر (§9، منع ازدواج التنزيل)."""
    now = utcnow()
    done, rejected = [], []
    for deal_id in body.deal_ids:
        deal = await db.deals.col.find_one({"deal_id": deal_id})
        if deal is None or deal.get("status") != Status.MANUAL_COMPLETED.value:
            rejected.append({"deal_id": deal_id, "why": "ليست مستبعدة"})
            continue
        n = await db.ledger.col.count_documents({"deal_id": deal_id})
        if n > 0:
            rejected.append({"deal_id": deal_id,
                             "why": f"لها {n} قيد دفتر — الإرجاع يعيد التنزيل فيزدوج القيد"})
            continue
        ms = deal.get("manual_settlement") or {}
        prev = ms.get("previous_status") or Status.PARSED.value
        res = await db.deals.col.update_one(
            {"deal_id": deal_id, "status": Status.MANUAL_COMPLETED.value},
            {"$set": {"status": prev, "updated_at": now},
             "$unset": {"manual_settlement": ""}})
        if res.modified_count:
            done.append({"deal_id": deal_id, "restored_to": prev})
        else:
            rejected.append({"deal_id": deal_id, "why": "تغيّرت الحالة أثناء التنفيذ"})
    log.info("إرجاع للطابور: %s نجحت، %s رُفضت (بواسطة %s)", len(done), len(rejected), username)
    return {"restored": done, "rejected": rejected, "restored_count": len(done)}
