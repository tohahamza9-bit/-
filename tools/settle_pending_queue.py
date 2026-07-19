"""
تسوية طابور الانتظار (قرار المالك 2026-07-19): الـ70 صفقة المنتظرة أدخلها المالك يدويًّا في
MONEYADO أثناء إيقاف البوت → تُعلَّم نهائيًّا فلا تُنزَّل في أي إعادة تشغيل مستقبليّة.

🔴 التصنيف **بدليل الدفتر لا بالمرجع** (§9 الدفتر مصدر الحقيقة):
  (١) لها قيود دفتر (البوت نزّل نصفها فعلًا) → MANUAL_COMPLETED + sell_was_written=True
      + needs_review=True. **لا تُوسَم «مهمَلة» أبدًا** — إخفاء قيدٍ منزَّل يفقد أثر §11.4 ويخفي
      ازدواجًا ماليًّا مرجَّحًا (البوت كتب البيع + المالك أدخلها كاملة يدويًّا).
  (٢) بلا قيد + مرجع مكرّر داخل الطابور أو مرجع None (بقايا تجارب/إعادة تشغيل) → IGNORED
      (استبعاد موثَّق، بلا حذف من القاعدة — تبقى بالسجلّ بشارة «استُبعد يدويًّا»).
  (٣) بلا قيد + مرجع فريد → MANUAL_COMPLETED.

كل صفقة تحمل Deal.manual_settlement (سبب إلزاميّ + متى + مَن + previous_status للإرجاع).

التشغيل:
  .venv/Scripts/python.exe tools/settle_pending_queue.py            # خطّة فقط (لا كتابة)
  .venv/Scripts/python.exe tools/settle_pending_queue.py --apply    # ينفّذ بعد عرض الخطّة
"""
from __future__ import annotations

import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo import MongoClient

from core.config import get_settings
from core.constants import Status

PENDING = [Status.PARSED.value, Status.READY.value, Status.MATCHED.value, Status.SELL_DONE.value]
REASON = "أُدخلت يدويًّا في MONEYADO أثناء إيقاف البوت (قرار المالك 2026-07-19)"
REASON_RESIDUE = ("بقايا تجارب/تكرار مرجعيّ بلا قيد دفتر — استُبعدت من التنزيل "
                  "(قرار المالك 2026-07-19)")
BY = "owner-decision/settle_pending_queue"


def _first_leg(deal: dict) -> dict:
    return deal.get("sell_leg") or deal.get("buy_leg") or {}


def build_plan(db) -> list[dict]:
    deals = list(db.deals.find({"status": {"$in": PENDING}}).sort("created_at", 1))
    ref_counts = Counter(_first_leg(d).get("reference_number") for d in deals)
    plan = []
    for d in deals:
        leg = _first_leg(d)
        ref = leg.get("reference_number")
        ledger_n = db.ledger.count_documents({"deal_id": d["deal_id"]})
        if ledger_n > 0:                                   # (١) دليل كتابة فعليّ — يعلو على كل شيء
            target, reason, review = Status.MANUAL_COMPLETED.value, REASON, True
        elif ref is None or ref_counts[ref] > 1:           # (٢) بلا قيد + مكرّر/بلا مرجع
            target, reason, review = Status.IGNORED.value, REASON_RESIDUE, False
        else:                                              # (٣) بلا قيد + مرجع فريد
            target, reason, review = Status.MANUAL_COMPLETED.value, REASON, False
        plan.append({
            "deal_id": d["deal_id"], "ref": ref, "amount": leg.get("amount"),
            "currency": leg.get("currency"), "customer_code": leg.get("customer_code"),
            "from": d.get("status"), "to": target, "ledger": ledger_n,
            "reason": reason, "needs_review": review,
        })
    return plan


def print_plan(plan: list[dict]) -> None:
    print("=" * 104)
    print("خطّة تسوية الطابور — لا شيء يُكتب قبل --apply")
    print("=" * 104)
    print(f"{'ref':9} {'from':10} → {'to':17} {'amount':>11} {'cur':4} {'ldg':4} {'مراجعة':7}")
    print("-" * 104)
    for p in plan:
        amt = f"{p['amount']:,.0f}" if p["amount"] is not None else "?"
        print(f"{str(p['ref']):9} {p['from']:10} → {p['to']:17} {amt:>11} "
              f"{str(p['currency'] or ''):4} {p['ledger']:4} {'🔴 نعم' if p['needs_review'] else '—':7}")
    c = Counter(p["to"] for p in plan)
    review = [p for p in plan if p["needs_review"]]
    print("-" * 104)
    print(f"الإجمالي: {len(plan)} صفقة | {dict(c)}")
    print(f"🔴 تحتاج فحص المالك (ازدواج مرجَّح — البوت كتب + المالك أدخل يدويًّا): {len(review)}")
    tot: dict = {}
    for p in review:
        tot[p["currency"]] = tot.get(p["currency"], 0) + (p["amount"] or 0)
    if tot:
        print("   مبالغ المزدوج المرجَّح:", {k: f"{v:,.0f}" for k, v in tot.items()})


def apply_plan(db, plan: list[dict]) -> None:
    now = datetime.now(timezone.utc)
    n = 0
    for p in plan:
        settlement = {
            "reason": p["reason"], "settled_at": now, "settled_by": BY,
            "sell_was_written": p["ledger"] > 0, "ledger_entries": p["ledger"],
            "needs_review": p["needs_review"], "previous_status": p["from"],
        }
        res = db.deals.update_one(
            {"deal_id": p["deal_id"], "status": p["from"]},   # حارس: لا نلمس ما تغيّر بيننا
            {"$set": {"status": p["to"], "manual_settlement": settlement, "updated_at": now}},
        )
        n += res.modified_count
    print(f"\nطُبِّقت التسوية على {n} صفقة من {len(plan)}.")
    if n != len(plan):
        print("⚠️ فرقٌ في العدد — صفقات تغيّرت حالتها أثناء التنفيذ. أعد التشغيل بلا --apply للفحص.")


def main() -> None:
    s = get_settings()
    print(f"DB: {s.mongo_db} @ {s.mongo_uri}\n")
    db = MongoClient(s.mongo_uri)[s.mongo_db]
    plan = build_plan(db)
    print_plan(plan)
    if "--apply" not in sys.argv:
        print("\nخطّة فقط — لم يُكتب شيء. للتنفيذ: أعد التشغيل مع --apply")
        return
    apply_plan(db, plan)
    left = db.deals.count_documents({"status": {"$in": PENDING}})
    print(f"المتبقّي في الطابور بعد التسوية: {left} (يجب أن يكون 0)")


if __name__ == "__main__":
    main()
