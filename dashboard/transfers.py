"""
طبقة قراءة الحوالات للوحة V2 (المرحلة ١) — **قراءة فقط لدورة حياة الحوالة**.

لا تكتب على `deals` ولا تلمس `pipeline`/`matching`/`writer` إطلاقًا. تجمّع العرض من:
- `deals` (الحالة + `sell_leg`/`buy_leg` + `amendments[]` + الإلغاء)
- `raw_messages` (الرسالة الخام الأصلية عبر `source_message_keys`)
- `ledger` (القيود، بما فيها العكسية `is_reversal`)
- `dashboard_reviews` (تعليق «راجعتُها» — سجل داشبورد-محليّ)

قائمة الانتباه: الحوالات المحتاجة فعلًا بشريًا، مرتّبة بالإلحاح ثم الأقدم أولًا (FIFO).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from core.constants import Mark, Status
from core.db import Database
from core.models import DetectionConfig

from . import detect

# حالات «تحتاج فعلًا بشريًا» + رتبة الإلحاح (قرار المالك):
# HELD (بانتظار قرار) → ESCALATED (صُعِّدت) → TECH_FAILED (فشل تقني) → SELL_DONE (نصف-منفّذ).
_URGENCY = [Status.HELD, Status.ESCALATED, Status.TECH_FAILED, Status.SELL_DONE]
ATTENTION_STATUSES = [s.value for s in _URGENCY]
_URGENCY_RANK = {s.value: i for i, s in enumerate(_URGENCY)}
_STATUS_AR = {
    Status.HELD.value: "معلّقة — بانتظار قرار",
    Status.ESCALATED.value: "مُصعّدة للمسؤول",
    Status.TECH_FAILED.value: "فشل تقنيّ",
    Status.SELL_DONE.value: "بيع مُنفّذ — الشراء ناقص",
}


def _naive(dt: datetime) -> datetime:
    """توحيد UTC-naive للمقارنة (mongomock/motor قد يُرجعان أوقاتًا بلا منطقة)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _iso(dt: Any) -> Optional[str]:
    return dt.isoformat() if isinstance(dt, datetime) else None


def _leg_summary(leg: Optional[dict]) -> Optional[dict]:
    """ملخّص طرف واحد (بيع/شراء) للعرض — JSON-safe."""
    if not leg:
        return None
    tre = leg.get("treasury") or {}
    return {
        "operation": leg.get("operation"),
        "amount": leg.get("amount"),
        "amount_after_discount": leg.get("amount_after_discount"),
        "commission": leg.get("commission"),
        "currency": leg.get("currency"),
        "phone": leg.get("phone"),
        "reference_number": leg.get("reference_number"),
        "customer_code": leg.get("customer_code"),
        "customer_name": leg.get("customer_name"),
        "payment_method": leg.get("payment_method"),
        "treasury_code": tre.get("code"),
        "treasury_name": tre.get("name"),
    }


def _deal_summary(d: dict) -> dict:
    """ملخّص الحوالة للقوائم — يشتقّ الحقول البارزة من طرف البيع (أو الشراء)."""
    sell = d.get("sell_leg") or {}
    buy = d.get("buy_leg") or {}
    lead = sell or buy
    tre = (lead.get("treasury") or {})
    return {
        "deal_id": d.get("deal_id"),
        "status": d.get("status"),
        "mark": d.get("mark"),
        "is_two_legged": d.get("is_two_legged", False),
        "reference_number": lead.get("reference_number"),
        "phone": lead.get("phone"),
        "customer_name": lead.get("customer_name"),
        "customer_code": lead.get("customer_code"),
        "amount": lead.get("amount"),
        "amount_after_discount": lead.get("amount_after_discount"),
        "currency": lead.get("currency"),
        "treasury_name": tre.get("name"),
        "treasury_code": tre.get("code"),
        "created_at": _iso(d.get("created_at")),
        "first_received_at": _iso(d.get("first_received_at")),
        "updated_at": _iso(d.get("updated_at")),
        "cancelled_at": _iso(d.get("cancelled_at")),
        "cancellation_reason": d.get("cancellation_reason"),
        "deal_margin": d.get("deal_margin"),         # §8 عرض فقط (None حتى ربط التسعير)
    }


async def search_transfers(db: Database, q: str = "", limit: int = 50) -> list[dict]:
    """بحث فوريّ في أرشيف الحوالات بالمرجع/الهاتف/الاسم (أو الأحدث عند غياب الاستعلام)."""
    limit = max(1, min(int(limit or 50), 200))
    query: dict = {}
    q = (q or "").strip()
    if q:
        rx = {"$regex": re.escape(q), "$options": "i"}
        query = {"$or": [
            {"deal_id": rx}, {"grouping_key": rx},
            {"sell_leg.reference_number": rx}, {"buy_leg.reference_number": rx},
            {"sell_leg.phone": rx}, {"buy_leg.phone": rx},
            {"sell_leg.customer_name": rx}, {"buy_leg.customer_name": rx},
            {"sell_leg.customer_code": rx}, {"buy_leg.customer_code": rx},
        ]}
    cur = db.deals.col.find(query).sort([("created_at", -1), ("_id", -1)]).limit(limit)
    out: list[dict] = []
    async for d in cur:
        out.append(_deal_summary(d))
    return out


async def get_timeline(db: Database, deal_id: str) -> Optional[dict]:
    """الخط الزمني الكامل لحوالة: خام ← مُستخرَج ← تعديلات ← قيود دفتر ← إلغاء (قراءة فقط).

    التاريخ الكامل إلزاميّ (لا الحالة النهائية فقط) — لتفسير أي فرق بين الرقم النهائي والرسالة الأصلية.
    """
    d = await db.deals.col.find_one({"deal_id": deal_id})
    if not d:
        return None
    d.pop("_id", None)

    events: list[dict] = []

    # ١) الرسائل الخام الأصلية
    raws: list[dict] = []
    for key in d.get("source_message_keys") or []:
        rm = await db.raw.get(key)
        if rm is None:
            continue
        item = {"message_key": rm.message_key, "chat_jid": rm.chat_jid,
                "sender_jid": rm.sender_jid, "text": rm.text,
                "received_at": _iso(rm.received_at), "edited_at": _iso(rm.edited_at)}
        raws.append(item)
        events.append({"type": "raw", "at": rm.received_at, "at_iso": _iso(rm.received_at),
                       "text": rm.text, "sender_jid": rm.sender_jid, "message_key": rm.message_key})

    # ٢) ما استُخرج (القيم الحيّة الحالية على الطرفين)
    sell = _leg_summary(d.get("sell_leg"))
    buy = _leg_summary(d.get("buy_leg"))
    extract_at = d.get("first_received_at") or d.get("created_at")
    events.append({"type": "extracted", "at": extract_at, "at_iso": _iso(extract_at),
                   "sell": sell, "buy": buy})

    # ٣) التعديلات بالتسلسل (المبلغ/العمولة قبل/بعد لكل خطوة)
    amendments: list[dict] = []
    for a in d.get("amendments") or []:
        item = {"amended_at": _iso(a.get("amended_at")), "amended_by_key": a.get("amended_by_key"),
                "old_net": a.get("old_net"), "new_net": a.get("new_net"),
                "old_commission": a.get("old_commission"), "new_commission": a.get("new_commission"),
                "reason": a.get("reason")}
        amendments.append(item)
        events.append({"type": "amendment", "at": a.get("amended_at"), "at_iso": _iso(a.get("amended_at")),
                       "old_net": a.get("old_net"), "new_net": a.get("new_net"),
                       "old_commission": a.get("old_commission"),
                       "new_commission": a.get("new_commission"), "reason": a.get("reason")})

    # ٤) قيود الدفتر (بما فيها العكسية عند التعديل/الإلغاء)
    ledger: list[dict] = []
    for e in await db.ledger.entries_for_deal(deal_id):
        item = {"entry_id": e.entry_id, "operation": e.operation.value if e.operation else None,
                "is_reversal": e.is_reversal, "amount": e.amount,
                "currency": e.currency.value if e.currency else None,
                "reference_number": e.reference_number, "moneyado_ref": e.moneyado_ref,
                "status": e.status.value if e.status else None, "sql_verified": e.sql_verified,
                "created_at": _iso(e.created_at)}
        ledger.append(item)
        events.append({"type": "ledger", "at": e.created_at, "at_iso": _iso(e.created_at),
                       "operation": item["operation"], "is_reversal": e.is_reversal,
                       "amount": e.amount, "currency": item["currency"],
                       "sql_verified": e.sql_verified, "moneyado_ref": e.moneyado_ref})

    # ٥) الإلغاء إن وُجد
    cancellation = None
    if d.get("cancelled_at"):
        cancellation = {"cancelled_at": _iso(d.get("cancelled_at")),
                        "cancelled_by_key": d.get("cancelled_by_key"),
                        "cancellation_reason": d.get("cancellation_reason")}
        events.append({"type": "cancellation", "at": d.get("cancelled_at"),
                       "at_iso": _iso(d.get("cancelled_at")),
                       "reason": d.get("cancellation_reason")})

    # ترتيب زمنيّ حتميّ (الأحداث بلا ختم توضع أوّلًا)
    events.sort(key=lambda ev: _naive(ev["at"]) if isinstance(ev.get("at"), datetime) else datetime.min)
    for ev in events:
        ev.pop("at", None)

    review = await db.reviews.get(deal_id)
    return {
        "deal": _deal_summary(d),
        "raw_messages": raws,
        "extracted": {"sell": sell, "buy": buy},
        "amendments": amendments,
        "ledger": ledger,
        "cancellation": cancellation,
        "timeline": events,
        "review": ({"reviewed_by": review.reviewed_by, "reviewed_at": _iso(review.reviewed_at),
                    "note": review.note} if review else None),
    }


async def attention_items(db: Database, now: datetime,
                          config: Optional[DetectionConfig] = None) -> list[dict]:
    """قائمة الانتباه: الحوالات المحتاجة فعلًا بشريًا (حالة + إشارات كشف احتيال م٢).

    قراءة فقط. الترتيب (قرار المالك): **الحالة أوّلًا** (HELD→ESCALATED→TECH_FAILED→SELL_DONE،
    ثم «خارج الحالات النشطة» للمُشار إليها احتياليًا فقط)؛ **داخل كل مستوى** ترتفع المشبوهة
    (إشارة احتيال + غير مراجَعة)، ثم غير المراجَعة، ثم المراجَعة، وأخيرًا FIFO (الأقدم أوّلًا).
    """
    signals = await detect.detect_signals(db, now, config)
    sig_by_deal: dict[str, list[dict]] = {}
    for s in signals:
        sig_by_deal.setdefault(s["deal_id"], []).append(s)

    docs: dict[str, dict] = {}
    async for d in db.deals.col.find({"status": {"$in": ATTENTION_STATUSES}}):
        d.pop("_id", None)
        docs[d["deal_id"]] = d
    # حوالات مُشار إليها احتياليًا خارج الحالات النشطة (مثل completed) — تُضمّ لئلّا يفوت التلاعب
    for did in sig_by_deal:
        if did not in docs:
            extra = await db.deals.col.find_one({"deal_id": did})
            if extra:
                extra.pop("_id", None)
                docs[did] = extra

    reviews = await db.reviews.map_for(list(docs.keys()))
    now_n = _naive(now)
    items: list[dict] = []
    for did, d in docs.items():
        status = d.get("status")
        rank = _URGENCY_RANK.get(status, 4)          # 4 = خارج الحالات النشطة (مُشار إليها فقط)
        fifo_at = d.get("first_received_at") or d.get("created_at")
        age_seconds = None
        if isinstance(fifo_at, datetime):
            age_seconds = max(0, int((now_n - _naive(fifo_at)).total_seconds()))
        rv = reviews.get(did)
        sigs = sig_by_deal.get(did, [])
        reviewed, has_sig = rv is not None, bool(sigs)
        summary = _deal_summary(d)
        summary.update({
            "urgency_rank": rank,
            "status_label": _STATUS_AR.get(status, f"{status} — مُشار إليها"),
            "reason": d.get("hold_reason"),
            "age_seconds": age_seconds,
            "signals": [{"rule": s["rule"], "severity": s["severity"], "detail": s["detail"],
                         "related_deal_ids": s.get("related_deal_ids", [])} for s in sigs],
            "review": ({"reviewed_by": rv.get("reviewed_by"),
                        "reviewed_at": _iso(rv.get("reviewed_at")),
                        "note": rv.get("note")} if rv else None),
            # رتبة فرعية داخل المستوى: مشبوهة+غير مراجَعة → غير مراجَعة → مراجَعة (إشارة بصرية لا تغيّر الحالة)
            "_sub": 0 if (has_sig and not reviewed) else (1 if not reviewed else 2),
            "_fifo": _naive(fifo_at) if isinstance(fifo_at, datetime) else datetime.min,
        })
        items.append(summary)

    items.sort(key=lambda it: (it["urgency_rank"], it.pop("_sub"), it.pop("_fifo")))
    return items


def build_escalation_text(summary: dict, actor: str) -> str:
    """نصّ تنبيه المالك عند «صعّد» — يُدرَج في طابور outgoing (is_alert) لا يُرسَل مباشرة."""
    ref = summary.get("reference_number") or "—"
    phone = summary.get("phone") or "—"
    name = summary.get("customer_name") or "—"
    amount = summary.get("amount")
    tre = summary.get("treasury_name") or "—"
    status = _STATUS_AR.get(summary.get("status"), summary.get("status"))
    amount_s = f"{amount}" if amount is not None else "—"
    return (f"🚨 تصعيد يدويّ من اللوحة (بواسطة {actor})\n"
            f"الحالة: {status}\n"
            f"المرجع: {ref} · الهاتف: {phone} · الاسم: {name}\n"
            f"المبلغ: {amount_s} · الخزينة: {tre}")
