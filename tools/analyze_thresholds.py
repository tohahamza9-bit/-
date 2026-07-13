"""
اشتقاق حدود كشف الاحتيال (لوحة V2 م٢) من التحليل التاريخي — **قراءة فقط** (إلا مع --apply).

يستقبل ملف التحليل التاريخي (٨٥٥٨ حوالة) عبر --file (JSON) ويقترح حدودًا أكثر تمثيلًا من
البذرة المتحفّظة، مع إبقائها قابلة للتعديل من الداشبورد. عند غياب الملف يحلّل مجموعة deals الحيّة.

صيغة ملف JSON المتوقّعة — مصفوفة سجلّات، كل سجلّ:
    {"phone": "...", "amount": 1600, "amount_after_discount": 1584,
     "reference_number": "A6779", "customer_name": "...", "treasury_code": "74",
     "created_at": "2026-01-01T12:00:00+00:00"}
(الحقول الناقصة تُتجاوز بأمان.)

الاستخدام:
    python -m tools.analyze_thresholds --file historical.json           # يقترح فقط (لا يكتب)
    python -m tools.analyze_thresholds --file historical.json --apply   # يكتب البذرة في الداشبورد
    python -m tools.analyze_thresholds                                   # يحلّل deals الحيّة
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")

from core.config import get_settings
from core.db import Database
from core.models import DetectionConfig


def _naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _percentile(values: list[float], pct: float) -> float:
    """المئين البسيط (بلا numpy) — pct بين 0 و100."""
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((pct / 100.0) * (len(s) - 1)))))
    return float(s[k])


def _records_from_file(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        sys.exit("صيغة الملف غير صحيحة: يجب أن يكون مصفوفة JSON من السجلّات.")
    recs = []
    for r in data:
        ca = r.get("created_at")
        try:
            dt = _naive(datetime.fromisoformat(ca)) if ca else None
        except Exception:
            dt = None
        recs.append({"phone": str(r.get("phone") or ""), "amount": r.get("amount"),
                     "amount_after_discount": r.get("amount_after_discount"),
                     "reference_number": r.get("reference_number"),
                     "treasury_code": r.get("treasury_code"), "created_at": dt})
    return recs


async def _records_from_db(db: Database) -> list[dict]:
    recs = []
    async for d in db.deals.col.find({}):
        leg = d.get("sell_leg") or d.get("buy_leg") or {}
        recs.append({"phone": str(leg.get("phone") or ""), "amount": leg.get("amount"),
                     "amount_after_discount": leg.get("amount_after_discount"),
                     "reference_number": leg.get("reference_number"),
                     "treasury_code": (leg.get("treasury") or {}).get("code"),
                     "created_at": _naive(d["created_at"]) if isinstance(d.get("created_at"), datetime) else None})
    return recs


def _suggest(recs: list[dict], base: DetectionConfig) -> dict:
    window = timedelta(minutes=base.structuring_window_minutes)
    # تجزئة: أعلى مجموع/عدد لكل هاتف ضمن نافذة (التوزيع الفعليّ)
    by_phone: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        if r["phone"] and r["created_at"] is not None:
            by_phone[r["phone"]].append(r)
    sums, counts = [], []
    for ds in by_phone.values():
        ds.sort(key=lambda x: x["created_at"])
        for i in range(len(ds)):
            win = [ds[j] for j in range(i + 1) if ds[i]["created_at"] - ds[j]["created_at"] <= window]
            sums.append(sum((x["amount"] or 0) for x in win))
            counts.append(len(win))
    # الخصم: وسيط النسبة الإجماليّ (مرجع عام)
    ratios = []
    for r in recs:
        amt = r["amount"]
        if amt and amt > 0 and r["amount_after_discount"] is not None:
            ratios.append(max(0.0, (amt - r["amount_after_discount"]) / amt))
    import statistics
    sug = {
        "structuring_sum_threshold": round(_percentile(sums, 99), 2) or base.structuring_sum_threshold,
        "structuring_count_threshold": max(int(_percentile(counts, 99)), base.structuring_count_threshold),
        "discount_median_observed": round(statistics.median(ratios), 4) if ratios else None,
        "samples": len(recs), "phones": len(by_phone),
    }
    return sug


async def main() -> None:
    parser = argparse.ArgumentParser(description="اشتقاق حدود كشف الاحتيال (م٢) من التحليل التاريخي.")
    parser.add_argument("--file", help="ملف JSON للتحليل التاريخي (وإلا يحلّل deals الحيّة)")
    parser.add_argument("--apply", action="store_true", help="يكتب الحدود المقترحة في الداشبورد")
    args = parser.parse_args()

    base = DetectionConfig()
    settings = get_settings()
    db = Database(settings.mongo_uri, settings.mongo_db)

    if args.file:
        recs = _records_from_file(Path(args.file))
        print(f"حُمّل {len(recs)} سجلًّا من {args.file}")
    else:
        try:
            await db.connect()
            await db.deals.col.count_documents({})
        except Exception as exc:  # T5
            sys.exit(f"تعذّر الاتصال بـ MongoDB: {exc}")
        recs = await _records_from_db(db)
        print(f"حُلّلت {len(recs)} حوالة حيّة من deals")

    sug = _suggest(recs, base)
    print("\n── الحدود المقترحة (م٢) ──")
    print(f"  structuring_sum_threshold   : {sug['structuring_sum_threshold']}  (البذرة {base.structuring_sum_threshold})")
    print(f"  structuring_count_threshold : {sug['structuring_count_threshold']}  (البذرة {base.structuring_count_threshold})")
    print(f"  نسبة الخصم الوسيط المُلاحَظة  : {sug['discount_median_observed']}")
    print(f"  (عيّنات={sug['samples']}، هواتف={sug['phones']})")

    if args.apply:
        if db._client is None:  # الملف لم يتّصل بعد
            await db.connect()
        cfg = await db.detection.get()
        cfg.structuring_sum_threshold = float(sug["structuring_sum_threshold"])
        cfg.structuring_count_threshold = int(sug["structuring_count_threshold"])
        await db.detection.set(cfg)
        print("\n✔ كُتبت الحدود المقترحة في إعداد الداشبورد (قابلة للتعديل لاحقًا).")

    with contextlib.suppress(Exception):
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
