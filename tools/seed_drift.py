#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""كاشف انحراف البذرة ↔ القاعدة الحيّة (خزائن/موردون).

لماذا: `seed_if_empty` (core/db.py) لا يعمل إلا على مجموعة **فارغة**. فكل تعديل من اللوحة
(إضافة خزينة، إضافة/حذف alias) ينحرف بالقاعدة الحيّة عن `SEED_TREASURIES`/`SEED_SUPPLIERS`
بلا كاشف — وقاعدةٌ جديدة تُرجِع سلوكًا قديمًا سقط سابقًا (بلاغ X1840: alias «محمد الحمامات»
كان يعيش في القاعدة الحيّة وحدها، فضاع كلّما بُذرت قاعدة نظيفة).

لا يصلح هذا اختبارًا في السويت: كل اختبارات المستودع على mongomock ولا ترى قاعدةً حيّة.

الاستعمال:
    python tools/seed_drift.py                 # تقرير كامل
    python tools/seed_drift.py --quiet         # الخلاصة فقط
    python tools/seed_drift.py --json          # ناتج آليّ

رمز الخروج: 0 = بلا انحراف، 1 = يوجد انحراف (صالح لبوّابة CI/فحص صحّة).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import get_settings                       # noqa: E402
from core.constants import SEED_SUPPLIERS, SEED_TREASURIES  # noqa: E402


def _norm_aliases(doc: dict) -> set[str]:
    return {a for a in (doc.get("aliases") or []) if a}


def _key(doc: dict) -> str:
    """المفتاح هو الاسم — نفس ما يستعمله `seed_if_empty` في upsert."""
    return doc.get("name") or ""


def compare(seed: list[dict], live: list[dict]) -> dict:
    """يقارن البذرة بالقاعدة الحيّة ويُرجِع تقريرًا مُهيكلًا."""
    seed_by = {_key(s): s for s in seed}
    live_by = {_key(d): d for d in live}

    report = {
        "missing_in_live": sorted(set(seed_by) - set(live_by)),   # مبذورة وغائبة عن الحيّة
        "absent_from_seed": sorted(set(live_by) - set(seed_by)),  # حيّة ولا تعود بعد بذر نظيف
        "alias_drift": [],
        "field_drift": [],
        "no_code": sorted(n for n, d in live_by.items() if not d.get("code")),
    }
    for name in sorted(set(seed_by) & set(live_by)):
        s, d = seed_by[name], live_by[name]
        sa, la = _norm_aliases(s), _norm_aliases(d)
        if sa != la:
            report["alias_drift"].append({
                "name": name,
                "live_only": sorted(la - sa),   # يضيع عند بذر قاعدة نظيفة 🔴
                "seed_only": sorted(sa - la),   # حُذف من الحيّة يدويًّا
            })
        for f in ("code", "currency", "type"):
            if s.get(f) != d.get(f):
                report["field_drift"].append(
                    {"name": name, "field": f, "seed": s.get(f), "live": d.get(f)})
    return report


def _has_drift(r: dict) -> bool:
    return any(r[k] for k in ("missing_in_live", "absent_from_seed", "alias_drift",
                              "field_drift", "no_code"))


def render(title: str, r: dict, quiet: bool = False) -> None:
    mark = "🔴" if _has_drift(r) else "✅"
    print(f"\n{mark} {title}")
    if not _has_drift(r):
        print("   بلا انحراف.")
        return
    counts = (f"   غائبة عن الحيّة={len(r['missing_in_live'])}  "
              f"غائبة عن البذرة={len(r['absent_from_seed'])}  "
              f"aliases منحرفة={len(r['alias_drift'])}  "
              f"حقول منحرفة={len(r['field_drift'])}  بلا كود={len(r['no_code'])}")
    print(counts)
    if quiet:
        return
    if r["missing_in_live"]:
        print("   ── مبذورة وغائبة عن القاعدة الحيّة (حُذفت يدويًّا؟):")
        for n in r["missing_in_live"]:
            print(f"      • {n}")
    if r["absent_from_seed"]:
        print("   ── حيّة وغائبة عن البذرة 🔴 (تختفي عند بذر قاعدة نظيفة):")
        for n in r["absent_from_seed"]:
            print(f"      • {n}")
    if r["alias_drift"]:
        print("   ── انحراف aliases:")
        for x in r["alias_drift"]:
            bits = []
            if x["live_only"]:
                bits.append(f"حيّ-فقط 🔴 {x['live_only']}")
            if x["seed_only"]:
                bits.append(f"بذرة-فقط {x['seed_only']}")
            print(f"      • {x['name']}: " + "  ".join(bits))
    if r["field_drift"]:
        print("   ── انحراف حقول:")
        for x in r["field_drift"]:
            print(f"      • {x['name']}.{x['field']}: بذرة={x['seed']!r} حيّ={x['live']!r}")
    if r["no_code"]:
        print("   ── بلا كود MONEYADO (تُعلَّق في بوّابة الثقة):")
        for n in r["no_code"]:
            print(f"      • {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description="كاشف انحراف البذرة عن القاعدة الحيّة")
    ap.add_argument("--quiet", action="store_true", help="الخلاصة فقط")
    ap.add_argument("--json", action="store_true", dest="as_json", help="ناتج JSON")
    args = ap.parse_args()

    try:
        from pymongo import MongoClient
    except ImportError:
        print("pymongo غير متاح.", file=sys.stderr)
        return 2

    st = get_settings()
    try:
        db = MongoClient(st.mongo_uri, serverSelectionTimeoutMS=4000)[st.mongo_db]
        live_t = list(db.treasuries.find({}))
        live_s = list(db.suppliers.find({}))
    except Exception as exc:                       # لا نُسقِط الأداة على عطل اتّصال
        print(f"تعذّر الاتّصال بـMongo ({st.mongo_uri}): {exc}", file=sys.stderr)
        return 2

    rep = {
        "treasuries": compare(SEED_TREASURIES, live_t),
        "suppliers": compare(SEED_SUPPLIERS, live_s),
    }
    if args.as_json:
        print(json.dumps(rep, ensure_ascii=False, indent=1))
    else:
        print(f"قاعدة: {st.mongo_uri}/{st.mongo_db}")
        render(f"الخزائن (بذرة={len(SEED_TREASURIES)}، حيّة={len(live_t)})",
               rep["treasuries"], args.quiet)
        render(f"الموردون (بذرة={len(SEED_SUPPLIERS)}، حيّة={len(live_s)})",
               rep["suppliers"], args.quiet)
    return 1 if (_has_drift(rep["treasuries"]) or _has_drift(rep["suppliers"])) else 0


if __name__ == "__main__":
    raise SystemExit(main())
