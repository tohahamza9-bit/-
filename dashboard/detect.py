"""
كشف الاحتيال (لوحة V2 م٢) — **قراءة فقط في طبقة الداشبورد**.

أربع قواعد تُحسَب على `deals`/القوائم المُدارة عند بناء قائمة الانتباه:
1. تجزئة الحوالات — مجموع/عدد حوالات نفس الهاتف خلال نافذة.
2. إعادة استخدام مرجع — Axxxx بهوية (هاتف/اسم) مختلفة خلال نافذة.
3. انحراف نسبة الخصم — نسبة تنحرف عن معتاد نفس الخزينة (يُشتقّ حيًّا).
4. كيان جديد — خزينة أوّل ظهور لها لم تُشاهَد قبل النافذة.

لا تكتب حالة حوالة ولا تلمس pipeline/matching/writer — كشف قراءة صرفة يغذّي العرض فقط.
كل «إشارة»: {deal_id, rule, severity, detail, related_deal_ids}.
"""
from __future__ import annotations

import re
import statistics
from datetime import datetime, timedelta, timezone

from core.db import Database
from core.models import DetectionConfig

RULE_STRUCTURING = "structuring"
RULE_REF_REUSE = "ref_reuse"
RULE_DISCOUNT = "discount_deviation"
RULE_NEW_ENTITY = "new_entity"


def _naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _digits(s) -> str:
    return re.sub(r"\D", "", s or "")


def _norm_name(s) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _lead_leg(d: dict) -> dict:
    return d.get("sell_leg") or d.get("buy_leg") or {}


def _created(d: dict):
    ca = d.get("created_at")
    return _naive(ca) if isinstance(ca, datetime) else None


def _discount_ratio(leg: dict):
    """نسبة الخصم = (قبل - بعد)/قبل، أو |العمولة|/المبلغ. None إن تعذّر."""
    amt = leg.get("amount")
    if not amt or amt <= 0:
        return None
    after = leg.get("amount_after_discount")
    if after is not None:
        return max(0.0, (amt - after) / amt)
    comm = leg.get("commission")
    if comm is not None:
        return abs(comm) / amt
    return None


async def _recent_deals(db: Database, horizon: datetime) -> list[dict]:
    """الحوالات ضمن نافذة المسح (الأحدث أولًا؛ الترشيح الزمنيّ بايثونيًّا لتفادي التباس المنطقة)."""
    lo = _naive(horizon)
    out: list[dict] = []
    cur = db.deals.col.find({}).sort([("created_at", -1), ("_id", -1)]).limit(5000)
    async for d in cur:
        d.pop("_id", None)
        c = _created(d)
        if c is not None and c >= lo:
            out.append(d)
    return out


def _structuring(recent: list[dict], cfg: DetectionConfig, out: list[dict]) -> None:
    window = timedelta(minutes=cfg.structuring_window_minutes)
    groups: dict[tuple, list[dict]] = {}
    for d in recent:
        leg = _lead_leg(d)
        phone = _digits(leg.get("phone"))
        if not phone or _created(d) is None:
            continue
        groups.setdefault((phone, leg.get("currency") or ""), []).append(d)
    for (phone, _cur), ds in groups.items():
        ds.sort(key=_created)
        trigger = None
        for i in range(len(ds)):
            end = _created(ds[i])
            win = [ds[j] for j in range(i + 1) if end - _created(ds[j]) <= window]
            total = sum((_lead_leg(x).get("amount") or 0) for x in win)
            if total >= cfg.structuring_sum_threshold or len(win) >= cfg.structuring_count_threshold:
                trigger = (win, total)
                break
        if trigger:
            win, total = trigger
            latest = max(win, key=_created)
            out.append({
                "deal_id": latest["deal_id"], "rule": RULE_STRUCTURING, "severity": "high",
                "detail": (f"تجزئة: {len(win)} حوالات لنفس الهاتف {phone} بمجموع {total:g} "
                           f"خلال {cfg.structuring_window_minutes}د"),
                "related_deal_ids": [x["deal_id"] for x in win if x["deal_id"] != latest["deal_id"]],
            })


def _ref_reuse(recent: list[dict], cfg: DetectionConfig, out: list[dict]) -> None:
    window = timedelta(hours=cfg.ref_reuse_window_hours)
    groups: dict[str, list[dict]] = {}
    for d in recent:
        ref = re.sub(r"\s+", "", (_lead_leg(d).get("reference_number") or "")).upper()
        if ref and _created(d) is not None:
            groups.setdefault(ref, []).append(d)
    for ref, ds in groups.items():
        if len(ds) < 2:
            continue
        ds.sort(key=_created)
        flagged = False
        for i in range(len(ds)):
            for j in range(i):
                if _created(ds[i]) - _created(ds[j]) > window:
                    continue
                a, b = _lead_leg(ds[i]), _lead_leg(ds[j])
                pa, pb = _digits(a.get("phone")), _digits(b.get("phone"))
                na, nb = _norm_name(a.get("customer_name")), _norm_name(b.get("customer_name"))
                if (pa and pb and pa != pb) or (na and nb and na != nb):
                    out.append({
                        "deal_id": ds[i]["deal_id"], "rule": RULE_REF_REUSE, "severity": "high",
                        "detail": (f"مرجع {ref} مُعاد بهوية مختلفة (هاتف/اسم) "
                                   f"خلال {cfg.ref_reuse_window_hours}س"),
                        "related_deal_ids": [ds[j]["deal_id"]],
                    })
                    flagged = True
                    break
            if flagged:
                break


def _discount_deviation(recent: list[dict], cfg: DetectionConfig, out: list[dict]) -> None:
    by_tre: dict[str, list[tuple]] = {}
    for d in recent:
        leg = _lead_leg(d)
        code = (leg.get("treasury") or {}).get("code")
        r = _discount_ratio(leg)
        if code and r is not None:
            by_tre.setdefault(code, []).append((d, r))
    for code, items in by_tre.items():
        if len(items) < cfg.discount_min_samples:
            continue
        med = statistics.median([r for _, r in items])
        if med <= 0:
            continue
        for d, r in items:
            if abs(r - med) / med > cfg.discount_deviation_tolerance:
                out.append({
                    "deal_id": d["deal_id"], "rule": RULE_DISCOUNT, "severity": "medium",
                    "detail": (f"نسبة خصم {r:.3f} تنحرف عن معتاد الخزينة {med:.3f} "
                               f"(> {cfg.discount_deviation_tolerance:.0%})"),
                    "related_deal_ids": [],
                })


async def _new_entity(db: Database, recent: list[dict], cfg: DetectionConfig,
                      now: datetime, out: list[dict]) -> None:
    horizon = _naive(now - timedelta(hours=cfg.new_entity_lookback_hours))
    by_code: dict[str, list[dict]] = {}
    for d in recent:
        code = (_lead_leg(d).get("treasury") or {}).get("code")
        if code:
            by_code.setdefault(code, []).append(d)
    for code, ds in by_code.items():
        # أقدم ظهور لهذه الخزينة عبر كل الحوالات — إن كان ضمن النافذة فهي «جديدة»
        oldest = await db.deals.col.find_one(
            {"$or": [{"sell_leg.treasury.code": code}, {"buy_leg.treasury.code": code}]},
            sort=[("created_at", 1)],
        )
        oc = _created(oldest) if oldest else None
        if oc is not None and oc < horizon:
            continue  # شوهدت قبل النافذة → ليست جديدة
        latest = max(ds, key=_created)
        name = (_lead_leg(latest).get("treasury") or {}).get("name") or code
        out.append({
            "deal_id": latest["deal_id"], "rule": RULE_NEW_ENTITY, "severity": "medium",
            "detail": f"خزينة جديدة «{name}» (كود {code}) لم تُشاهَد قبل {cfg.new_entity_lookback_hours}س",
            "related_deal_ids": [x["deal_id"] for x in ds if x["deal_id"] != latest["deal_id"]],
        })


async def detect_signals(db: Database, now: datetime,
                         config: DetectionConfig | None = None) -> list[dict]:
    """يُشغّل القواعد الأربع (قراءة فقط) ويُرجع كل الإشارات المكتشَفة ضمن نافذة المسح."""
    cfg = config or DetectionConfig()
    if not cfg.enabled:
        return []
    recent = await _recent_deals(db, now - timedelta(hours=cfg.scan_window_hours))
    signals: list[dict] = []
    _structuring(recent, cfg, signals)
    _ref_reuse(recent, cfg, signals)
    _discount_deviation(recent, cfg, signals)
    await _new_entity(db, recent, cfg, now, signals)
    return signals
