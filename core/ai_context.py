"""
السياق الحيّ لطبقة الفهم الذكي — «قاموس حيّ + سياق المُرسِل».

المبدأ: النموذج **لا يستقبل قوائم ثابتة**. كل نداء يبني سياقه من حالة قاعدة البيانات
**لحظتها**، مضافًا إليه تاريخُ المُرسِل الشخصيّ. لا cache، لا نسخة في الذاكرة، لا webhook
لإبطالها — فخزينةٌ أُضيفت قبل ثانية تظهر في النداء التالي حتمًا (وهذا ما يختبره
`test_ai_live_context.py`).

🔴 السياق **يُرجِّح ولا يُقرِّر**: كل ما يخرج من النموذج يمرّ بالتحقّق الحتميّ في
`ai_understanding.validate_*` قبل أن يمسّ أيّ حقل ماليّ.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

log = logging.getLogger(__name__)

HISTORY_DEALS = 20          # آخر ٢٠ حوالة مكتملة لنفس المُرسِل
PAIR_WINDOW_DAYS = 7        # نافذة إحصاء (المُرسِل × الخزينة)


@dataclass
class SenderContext:
    """صورة مختصرة عن عادات المُرسِل — تُبنى لحظة النداء ولا تُخزَّن."""
    sender_jid: str
    deals_seen: int = 0
    treasuries: list[tuple[str, int]] = field(default_factory=list)   # (اسم، عدد)
    suppliers: list[tuple[str, int]] = field(default_factory=list)
    prices: list[str] = field(default_factory=list)                   # أسعار معتادة
    shapes: list[tuple[str, int]] = field(default_factory=list)       # (نمط، عدد)
    no_phone_ratio: float = 0.0        # نسبة حوالاته بلا رقم مستلم (بريد)
    pair_stats: list[dict] = field(default_factory=list)              # (مُرسِل × خزينة) ٧ أيام

    def is_new_sender(self) -> bool:
        """مُرسِل بلا تاريخ كافٍ — لا يجوز ترجيح أنماط عليه (يُصعَّد بدل الافتراض)."""
        return self.deals_seen < 3

    def as_prompt_block(self) -> str:
        """نصّ عربيّ مضغوط يُحقَن في الـprompt. فارغ إن لا تاريخ (فلا نوهم النموذج بسياق)."""
        if not self.deals_seen:
            return "تاريخ هذا المُرسِل: لا حوالات سابقة (مُرسِل جديد — لا تُرجّح أنماطًا)."
        parts = [f"تاريخ المُرسِل (آخر {self.deals_seen} حوالة مكتملة):"]
        if self.treasuries:
            parts.append("- خزائنه المعتادة: " +
                         "، ".join(f"{n} ({c})" for n, c in self.treasuries))
        if self.suppliers:
            parts.append("- مورّدوه المعتادون: " +
                         "، ".join(f"{n} ({c})" for n, c in self.suppliers))
        if self.prices:
            parts.append("- أسعاره المعتادة: " + "، ".join(self.prices))
        if self.shapes:
            parts.append("- أنماط رسائله: " +
                         "، ".join(f"{s} ({c})" for s, c in self.shapes))
        parts.append(f"- نسبة حوالاته **بلا رقم مستلم** (بريد): {self.no_phone_ratio:.0%}")
        if self.pair_stats:
            parts.append(f"- إحصاء آخر {PAIR_WINDOW_DAYS} أيام (المُرسِل × الخزينة):")
            for p in self.pair_stats:
                parts.append(f"    • {p['treasury']}: {p['count']} حوالة، "
                             f"متوسط السعر {p['avg_price']}")
        return "\n".join(parts)


def _shape_of(sell: dict, deal: dict) -> str:
    if sell.get("is_si_format"):
        return "SI معنونة"
    if deal.get("is_two_legged"):
        return "طرفان (زبون+مورد)"
    if not (sell.get("phone") or "").strip():
        return "بريد بلا رقم"
    return "رسالة واحدة برقم"


def _top(counter: dict[str, int], n: int = 5) -> list[tuple[str, int]]:
    return sorted(counter.items(), key=lambda kv: -kv[1])[:n]


async def build_sender_context(db: Any, sender_jid: Optional[str],
                               now: datetime) -> SenderContext:
    """يبني سياق المُرسِل من DB **لحظة النداء** (استعلامان مفهرسان، بلا cache).

    فشل أيّ استعلام لا يُسقط النداء: يُرجَع سياقٌ فارغ (السياق ترجيحٌ لا شرط) — T5.
    """
    ctx = SenderContext(sender_jid=sender_jid or "")
    if not sender_jid:
        return ctx
    try:
        cur = db.deals.col.find(
            {"$or": [{"sell_leg.sender_jid": sender_jid}, {"buy_leg.sender_jid": sender_jid}],
             "status": {"$in": ["completed", "manual_completed", "sell_done"]}},
        ).sort("created_at", -1).limit(HISTORY_DEALS)
        deals = [d async for d in cur]
    except Exception as exc:      # T5 — لا نبتلع؛ سياق فارغ أأمن من نداء فاشل
        log.warning("(الفهم الذكي) تعذّر بناء تاريخ المُرسِل %s: %s", sender_jid, exc)
        return ctx

    tre: dict[str, int] = {}
    sup: dict[str, int] = {}
    shapes: dict[str, int] = {}
    prices: list[str] = []
    no_phone = 0
    for d in deals:
        sell = d.get("sell_leg") or {}
        t = (sell.get("treasury") or {}).get("name")
        if t:
            tre[t] = tre.get(t, 0) + 1
        s = (sell.get("supplier") or {}).get("name") or \
            ((d.get("buy_leg") or {}).get("supplier") or {}).get("name")
        if s:
            sup[s] = sup.get(s, 0) + 1
        p = (sell.get("price_normalized") or "").strip()
        if p and p not in prices:
            prices.append(p)
        shp = _shape_of(sell, d)
        shapes[shp] = shapes.get(shp, 0) + 1
        if not (sell.get("phone") or "").strip():
            no_phone += 1

    ctx.deals_seen = len(deals)
    ctx.treasuries = _top(tre)
    ctx.suppliers = _top(sup)
    ctx.prices = prices[:6]
    ctx.shapes = _top(shapes)
    ctx.no_phone_ratio = (no_phone / len(deals)) if deals else 0.0

    # (المُرسِل × الخزينة) خلال ٧ أيام — متوسط السعر وعدد الحوالات
    try:
        since = now - timedelta(days=PAIR_WINDOW_DAYS)
        if since.tzinfo is not None:
            since = since.replace(tzinfo=None)
        cur2 = db.deals.col.find(
            {"sell_leg.sender_jid": sender_jid, "created_at": {"$gte": since},
             "status": {"$in": ["completed", "manual_completed", "sell_done"]}})
        agg: dict[str, list[float]] = {}
        async for d in cur2:
            sell = d.get("sell_leg") or {}
            name = (sell.get("treasury") or {}).get("name")
            if not name:
                continue
            agg.setdefault(name, [])
            try:
                agg[name].append(float(sell.get("price_normalized")))
            except (TypeError, ValueError):
                pass
        ctx.pair_stats = [
            {"treasury": k,
             "count": len(v) if v else 0,
             "avg_price": (f"{sum(v)/len(v):.4g}" if v else "؟")}
            for k, v in sorted(agg.items(), key=lambda kv: -len(kv[1]))[:5]
        ]
    except Exception as exc:      # T5 — الإحصاء تحسينٌ لا شرط
        log.warning("(الفهم الذكي) تعذّر إحصاء (مُرسِل×خزينة) لـ%s: %s", sender_jid, exc)
    return ctx
