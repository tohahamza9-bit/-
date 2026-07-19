"""
جرد ضرر الحلّ بالتشابه + تطهير الـaliases المتعلَّمة تلقائيًّا (قرار المالك 2026-07-19).

خلفية: مسار «الحلّ الجريء» (resolve_bold) فُعِّل في c812d12/988cdea (2026-07-18) للخزائن ثم
للموردين. كل حلّ سجّل في الصفقة deviation_log بندًا method="similarity" وتعلّم alias بإلحاقه
لمكدّس aliases الكيان المطابَق. **الـaliases مخزَّنة كنصوص بلا حقل source**، فالتمييز الوحيد
الممكن بين اليدويّ والتلقائيّ هو أثر deviation_log — وهو ما يعتمده هذا السكربت: لا يُحذف alias
إلا إذا كان له بندُ تشابهٍ يوثّق أنّ البوت هو من ألحقه. أي alias بلا أثر = يدويّ = لا يُمَسّ.

التشغيل:
  .venv/Scripts/python.exe tools/audit_similarity.py            # عرض فقط (جرد + قائمة الحذف)
  .venv/Scripts/python.exe tools/audit_similarity.py --purge    # يحذف بعد عرض القائمة
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo import MongoClient

from core.config import get_settings
from core.parsing.normalize import normalize_ar

_LEGS = ("sell_leg", "buy_leg")


def _similarity_events(db) -> list[dict]:
    """كل بنود deviation_log بـmethod=similarity عبر طرفَي كل صفقة، مع سياق الصفقة."""
    q = {"$or": [{f"{lg}.deviation_log.method": "similarity"} for lg in _LEGS]}
    events: list[dict] = []
    for deal in db.deals.find(q).sort("created_at", 1):
        for lg in _LEGS:
            leg = deal.get(lg) or {}
            for d in leg.get("deviation_log") or []:
                if d.get("method") != "similarity":
                    continue
                events.append({
                    "deal_id": deal.get("deal_id"),
                    "created_at": deal.get("created_at"),
                    "status": deal.get("status"),
                    "leg": lg,
                    "reference": leg.get("reference_number"),
                    "field": d.get("field"),            # treasury | supplier
                    "raw": d.get("raw_value"),
                    "matched": d.get("extracted_value"),
                    "score": d.get("confidence"),
                    "amount": leg.get("amount"),
                    "currency": leg.get("currency"),
                    "customer_code": leg.get("customer_code"),
                })
    return events


def _in_ledger(db, deal_id: str) -> str:
    """هل كُتبت الصفقة في الدفتر فعلًا؟ (مصدر الحقيقة §9) — مع تأكيد SQL إن وُجد."""
    entries = list(db.ledger.find({"deal_id": deal_id}))
    if not entries:
        return "لا"
    verified = sum(1 for e in entries if e.get("sql_verified"))
    return f"نعم ({len(entries)} قيد، مؤكَّد SQL: {verified})"


def _print_audit(db, events: list[dict]) -> None:
    print("=" * 100)
    print("جرد الضرر — الحوالات المحلولة بالتشابه (resolved_by=similarity) منذ التفعيل 2026-07-18")
    print("=" * 100)
    if not events:
        print("لا توجد أي حوالة حُلّت بالتشابه. لا ضرر.\n")
        return
    for i, e in enumerate(events, 1):
        print(f"\n[{i}] المرجع: {e['reference'] or '؟'}   (deal_id={e['deal_id']}, {e['leg']})")
        print(f"    التاريخ      : {e['created_at']}   الحالة: {e['status']}")
        print(f"    الحقل        : {e['field']}")
        print(f"    النصّ الأصليّ : {e['raw']!r}")
        print(f"    المطابَق      : {e['matched']!r}   (score {e['score']})")
        print(f"    المبلغ        : {e['amount']} {e['currency'] or ''}   كود الزبون: {e['customer_code']}")
        print(f"    في الدفتر     : {_in_ledger(db, e['deal_id'])}")
        print("    حكم المالك    : [ ] صحيح   [ ] غلط ← يحتاج خطة تصحيح (عكس + قيد صحيح، نموذج X1257)")
    print(f"\nالإجمالي: {len(events)} حالة تشابه.\n")


def _auto_alias_candidates(db, events: list[dict]) -> list[tuple[str, str, str]]:
    """(المجموعة، اسم الكيان، الـalias) لكل alias موجود فعلًا ويُوثّق أثرُ تشابهٍ أنّ البوت ألحقه."""
    col_of = {"treasury": "treasuries", "supplier": "suppliers"}
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for e in events:
        col = col_of.get(e["field"])
        name, raw = e["matched"], e["raw"]
        if not col or not name or not raw:
            continue
        learned = normalize_ar(raw)
        # نفس شرط التعلّم في الأنبوب: لا يُلحَق alias مطابقٌ للاسم نفسه بعد التطبيع
        if not learned or learned == normalize_ar(name):
            continue
        key = (col, name, learned)
        if key in seen:
            continue
        doc = db[col].find_one({"name": name})
        if doc and learned in (doc.get("aliases") or []):
            seen.add(key)
            out.append(key)
    return out


def main() -> None:
    s = get_settings()
    print(f"DB: {s.mongo_db} @ {s.mongo_uri}\n")
    db = MongoClient(s.mongo_uri)[s.mongo_db]
    purge = "--purge" in sys.argv

    events = _similarity_events(db)
    _print_audit(db, events)

    cands = _auto_alias_candidates(db, events)
    print("=" * 100)
    print("الـaliases المتعلَّمة تلقائيًّا (مصدر auto) — مرشَّحة للحذف")
    print("=" * 100)
    if not cands:
        print("لا يوجد alias متعلَّم تلقائيًّا. الـaliases اليدوية لا تُمَسّ.\n")
    else:
        for col, name, alias in cands:
            print(f"  {col:11} | {name:28} | alias: {alias!r}")
        print(f"\nالإجمالي: {len(cands)} alias.")
        print("ملاحظة: كل alias بلا أثر تشابه يُعتبر يدويًّا ولا يُحذف.\n")

    if not purge:
        print("عرض فقط — لم يُحذف شيء. للحذف: أعِد التشغيل مع --purge")
        return
    if not cands:
        return
    removed = 0
    for col, name, alias in cands:
        r = db[col].update_one({"name": name}, {"$pull": {"aliases": alias}})
        removed += r.modified_count
        print(f"حُذف: {col}/{name} ← {alias!r} (modified={r.modified_count})")
    print(f"\nتمّ حذف {removed} alias متعلَّم تلقائيًّا. اليدوية سليمة.")


if __name__ == "__main__":
    main()
