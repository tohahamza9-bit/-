"""
هجرة صغيرة آمنة (idempotent): تضمن أن خزينة «خصم 1%» بكود 85 (مؤقّت، كان 72)، EGP،
sell_and_buy، نشطة — وتحذف أي خزينة أخرى بكود 85 (ازدواج لنفس الحساب، قرار صاحب العمل).
سببها: seed_if_empty يبذر المجموعة الفارغة فقط، فقاعدة بيانات مأهولة لا تتحدّث تلقائيًا
(§4، ملحق ب-3).

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
    "code": "85",   # 🔴 مؤقّت: 72→85
    "currency": "EGP",
    "type": "sell_and_buy",
    "aliases": ["خصم", "خصم1", "خصم 1", "خصم 1%", "خصم1%"],
    "active": True,
}


def main() -> None:
    s = get_settings()
    print(f"DB: {s.mongo_db} @ {s.mongo_uri}")
    db = MongoClient(s.mongo_uri)[s.mongo_db]

    code_in = {"$in": ["85", 85]}

    print("قبل الهجرة — سجلات كود 85/72 أو «خصم 1%»:")
    for d in db.treasuries.find({"$or": [{"code": code_in}, {"code": {"$in": ["72", 72]}},
                                         {"name": "خصم 1%"}]}):
        print(f"  name={d.get('name')!r} code={d.get('code')!r} active={d.get('active')}")

    # (1) احذف أي ازدواج بكود 85 لخزينة **غير** «خصم 1%» (نفس الحساب — قرار صاحب العمل).
    dup = db.treasuries.delete_many({"code": code_in, "name": {"$ne": "خصم 1%"}})
    print(f"\nحُذف من ازدواج كود 85 (غير «خصم 1%»): {dup.deleted_count}")

    # (2) ثبّت «خصم 1%» بالاسم (unambiguous) — كود 85، EGP، sell_and_buy، نشطة.
    r = db.treasuries.update_one({"name": "خصم 1%"}, {"$set": TREASURY}, upsert=True)
    print(f"«خصم 1%»: matched={r.matched_count} modified={r.modified_count} upserted={r.upserted_id}")

    # (3) الحالة النهائية
    after = list(db.treasuries.find({"code": code_in}))
    print(f"\nبعد الهجرة — سجلات كود 85: {len(after)}")
    for d in after:
        print(f"  name={d.get('name')!r} code={d.get('code')!r} active={d.get('active')} "
              f"type={d.get('type')!r} aliases={d.get('aliases')}")
    left72 = db.treasuries.count_documents({"code": {"$in": ["72", 72]}})
    print(f"سجلات كود 72 المتبقّية: {left72}")
    if len(after) != 1:
        print("⚠️ عدد سجلات كود 85 ليس 1 — راجع يدويًا.")


if __name__ == "__main__":
    main()
