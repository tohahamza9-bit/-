"""
هجرة صغيرة آمنة (idempotent): تضمن وجود خزينة «خصم 1%» (كود 72، EGP، sell_and_buy) في
db.treasuries — upsert لا يمسّ باقي الخزائن. سببها: seed_if_empty يبذر المجموعة الفارغة فقط،
فقاعدة بيانات مأهولة قبل إضافة كود 72 لا تتحدّث تلقائيًا (§4، ملحق ب-3).

الاتصال يُشتقّ من إعداد المشروع (نفس DB التي يستخدمها البوت). التشغيل:
    .venv/Scripts/python.exe tools/migrate_khasm_treasury.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # جذر المشروع → استيراد core

from pymongo import MongoClient

from core.config import get_settings

TREASURY = {
    "name": "خصم 1%",
    "code": "72",
    "currency": "EGP",
    "type": "sell_and_buy",
    "aliases": ["خصم", "خصم1", "خصم 1", "خصم 1%", "خصم1%"],
    "active": True,
}


def main() -> None:
    s = get_settings()
    print(f"DB: {s.mongo_db} @ {s.mongo_uri}")
    db = MongoClient(s.mongo_uri)[s.mongo_db]

    # قبل: هل هي موجودة؟ (كشف كود مخزَّن نصًّا أو رقمًا، أو بالاسم)
    q = {"$or": [{"code": "72"}, {"code": 72}, {"name": "خصم 1%"}]}
    before = list(db.treasuries.find(q))
    print(f"قبل الهجرة — سجلات مطابقة: {len(before)}")
    for d in before:
        print(f"  _id={d.get('_id')} name={d.get('name')!r} code={d.get('code')!r} "
              f"currency={d.get('currency')!r} type={d.get('type')!r} active={d.get('active')}")

    r = db.treasuries.update_one(q, {"$set": TREASURY}, upsert=True)
    print(f"\nmatched={r.matched_count} modified={r.modified_count} upserted_id={r.upserted_id}")

    # بعد: الحالة النهائية
    after = list(db.treasuries.find({"code": "72"}))
    print(f"\nبعد الهجرة — سجلات كود 72: {len(after)}")
    for d in after:
        print(f"  _id={d.get('_id')} name={d.get('name')!r} code={d.get('code')!r} "
              f"currency={d.get('currency')!r} type={d.get('type')!r} aliases={d.get('aliases')}")
    if len(after) > 1:
        print("⚠️ أكثر من سجل بكود 72 — شغّل db.treasuries.dedupe_by_code لإزالة التكرار.")


if __name__ == "__main__":
    main()
