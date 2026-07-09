"""
هجرة صغيرة آمنة (idempotent): تضمن وجود خزينة «فودافون بالخصم» بكود 85، EGP، sell_and_buy،
نشطة — الخزينة الافتراضية لطرف مورد حوالة A الثانية (§6.1). تحذف أي خزينة أخرى بكود 85
(ازدواج). سببها: seed_if_empty يبذر المجموعة الفارغة فقط، فقاعدة بيانات مأهولة لا تتحدّث تلقائيًا.

التشغيل:  .venv/Scripts/python.exe tools/add_vodafone_discount_treasury.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymongo import MongoClient

from core.config import get_settings

TREASURY = {
    "name": "فودافون بالخصم",
    "code": "85",
    "currency": "EGP",
    "type": "sell_and_buy",
    "aliases": ["فودافون بالخصم", "فودافون خصم"],
    "active": True,
}


def main() -> None:
    s = get_settings()
    print(f"DB: {s.mongo_db} @ {s.mongo_uri}")
    db = MongoClient(s.mongo_uri)[s.mongo_db]
    code_in = {"$in": ["85", 85]}

    print("قبل الهجرة — سجلات كود 85 أو «فودافون بالخصم»:")
    for d in db.treasuries.find({"$or": [{"code": code_in}, {"name": "فودافون بالخصم"}]}):
        print(f"  name={d.get('name')!r} code={d.get('code')!r} active={d.get('active')}")

    # (1) احذف أي خزينة **غير** «فودافون بالخصم» تشغل كود 85 (ازدواج لنفس الحساب).
    dup = db.treasuries.delete_many({"code": code_in, "name": {"$ne": "فودافون بالخصم"}})
    print(f"\nحُذف من ازدواج كود 85 (غير «فودافون بالخصم»): {dup.deleted_count}")

    # (2) ثبّت «فودافون بالخصم» بالاسم (unambiguous) — كود 85، EGP، sell_and_buy، نشطة.
    r = db.treasuries.update_one({"name": "فودافون بالخصم"}, {"$set": TREASURY}, upsert=True)
    print(f"«فودافون بالخصم»: matched={r.matched_count} modified={r.modified_count} upserted={r.upserted_id}")

    after = list(db.treasuries.find({"code": code_in}))
    print(f"\nبعد الهجرة — سجلات كود 85: {len(after)}")
    for d in after:
        print(f"  name={d.get('name')!r} code={d.get('code')!r} active={d.get('active')} "
              f"type={d.get('type')!r}")
    if len(after) != 1:
        print("⚠️ عدد سجلات كود 85 ليس 1 — راجع يدويًا.")


if __name__ == "__main__":
    main()
