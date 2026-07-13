"""
إحصاءات لوحة المدير (لوحة V2 م٤) — **قراءة فقط** على `deals`. لا كتابة، لا لمس pipeline.

يحسب: حجم اليوم (إجمالي/مكتملة/انتباه/ملغاة + مجاميع المبالغ بالعملة)، الأسبوع (إجمالي/مكتملة/
فشل تقنيّ + نسبة نجاح)، اتجاه ٧ أيام، وعدّاد الانتباه الحاليّ. حدود اليوم بتوقيت ليبيا UTC+2
(اتّساقًا مع منطق الإلغاء/النوافذ القائم). نسبة النجاح = مكتملة ÷ (مكتملة + فشل تقنيّ).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.constants import Status
from core.db import Database

LIBYA = timezone(timedelta(hours=2))   # UTC+2 — نفس اتّفاق cancellation.py
ATTENTION = [Status.HELD.value, Status.ESCALATED.value,
             Status.TECH_FAILED.value, Status.SELL_DONE.value]


def _naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _to_libya(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LIBYA)


def _day_key(dt: datetime) -> str:
    return _to_libya(dt).strftime("%Y-%m-%d")


async def compute_stats(db: Database, now: datetime) -> dict:
    """تجميعات قراءة فقط لصحة اللوحة — نافذة ٧ أيام محلّية + عدّاد انتباه حيّ."""
    now_l = _to_libya(now)
    today_key = now_l.strftime("%Y-%m-%d")
    days = [(now_l - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    trend = {k: {"day": k, "total": 0, "completed": 0} for k in days}
    today = {"total": 0, "completed": 0, "attention": 0, "cancelled": 0, "amount": {}}
    week = {"total": 0, "completed": 0, "tech_failed": 0}

    now_n = _naive(now)
    cur = db.deals.col.find({}).sort([("created_at", -1), ("_id", -1)]).limit(20000)
    async for d in cur:
        ca = d.get("created_at")
        if not isinstance(ca, datetime):
            continue
        if (now_n - _naive(ca)).total_seconds() > 8 * 86400:   # خارج نافذة ٨ أيام → توقّف نافذة
            continue
        k = _day_key(ca)
        status = d.get("status")
        if k in trend:
            trend[k]["total"] += 1
            week["total"] += 1
            if status == Status.COMPLETED.value:
                trend[k]["completed"] += 1
                week["completed"] += 1
            elif status == Status.TECH_FAILED.value:
                week["tech_failed"] += 1
        if k == today_key:
            today["total"] += 1
            if status == Status.COMPLETED.value:
                today["completed"] += 1
            if status in ATTENTION:
                today["attention"] += 1
            if status in (Status.CANCELLED.value, Status.CANCELLING.value):
                today["cancelled"] += 1
            leg = d.get("sell_leg") or d.get("buy_leg") or {}
            amt, ccy = leg.get("amount"), leg.get("currency")
            if amt and ccy:
                today["amount"][ccy] = round(today["amount"].get(ccy, 0) + amt, 2)

    denom = week["completed"] + week["tech_failed"]
    success_rate = round(week["completed"] / denom, 4) if denom > 0 else None
    attention_now = await db.deals.col.count_documents({"status": {"$in": ATTENTION}})

    return {
        "generated_at": now.isoformat(),
        "timezone": "UTC+2 (ليبيا)",
        "today": today,
        "week": {"total": week["total"], "completed": week["completed"],
                 "tech_failed": week["tech_failed"], "success_rate": success_rate},
        "trend": [trend[k] for k in days],
        "attention_now": attention_now,
    }
