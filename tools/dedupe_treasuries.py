"""
أداة تنظيف لمرّة واحدة: إزالة الخزائن المكرّرة بنفس الكود من MongoDB الحقيقي (§4، §13).

تحتفظ بسجلّ واحد لكل كود (الأقدم بالإدراج) وتحذف الباقي. السجلات بلا كود
(code=None/فارغ) شرعية ومتعدّدة (خزائن معلّقة الأكواد: هادم/خصم/صافي…) → لا تُلمَس.
آمنة للتشغيل المتكرّر (idempotent). تتّصل بالقاعدة من settings (mongo_uri/mongo_db).

الاستخدام:
    python tools/dedupe_treasuries.py            # يعاين ثم ينظّف فعليًا
    python tools/dedupe_treasuries.py --dry-run  # معاينة فقط بلا حذف

نفس المنطق المشغَّل تلقائيًا عند بدء البوت (TreasuryListRepo.dedupe_by_code)، لكنه
هنا يُشغَّل يدويًا الآن دون انتظار إعادة تشغيل الخدمة.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# إخراج UTF-8 دائمًا (تفادي تعطّل الطرفية على ويندوز/cp1256 مع العربية والرموز)
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")

from core.config import get_settings
from core.db import Database


async def _preview(db: Database) -> dict[str, list[dict]]:
    """يجمع السجلات المكرّرة (نفس الكود، أكثر من واحد) للعرض قبل الحذف."""
    by_code: dict[str, list[dict]] = defaultdict(list)
    async for d in db.treasuries.col.find({}).sort("_id", 1):
        code = d.get("code")
        if code:                        # None/"" مستثناة (شرعية ومتعدّدة)
            by_code[code].append(d)
    return {c: docs for c, docs in by_code.items() if len(docs) > 1}


async def main() -> None:
    parser = argparse.ArgumentParser(description="تنظيف الخزائن المكرّرة بنفس الكود (MongoDB).")
    parser.add_argument("--dry-run", action="store_true", help="معاينة فقط بلا حذف")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.mongo_uri, settings.mongo_db)
    print(f"الاتصال بـ MongoDB: {settings.mongo_uri} / {settings.mongo_db} ...")
    try:
        await db.connect()
        # تحقّق اتصال فعلي (motor كسول) — يفشل بوضوح إن كانت القاعدة غير متاحة
        await db.treasuries.col.count_documents({})
    except Exception as exc:  # T5 — لا ابتلاع صامت
        sys.exit(f"تعذّر الاتصال بـ MongoDB: {exc}\n"
                 f"تأكّد أن القاعدة تعمل وأن mongo_uri صحيح في البيئة (.env).")

    total = await db.treasuries.col.count_documents({})
    dups = await _preview(db)

    if not dups:
        print(f"لا تكرار. إجمالي الخزائن: {total}. لا شيء للتنظيف.")
        await db.close()
        return

    print(f"\nإجمالي الخزائن: {total} — أكواد مكرّرة: {len(dups)}")
    print("-" * 60)
    will_remove = 0
    for code, docs in dups.items():
        # نفس قاعدة الإبقاء في dedupe_by_code: النشط أولًا، وإلا الأقدم
        keep = next((x for x in docs if x.get("active")), docs[0])
        drop = [x for x in docs if x["_id"] != keep["_id"]]
        will_remove += len(drop)
        print(f"كود {code}: {len(docs)} سجلّات")
        print(f"  ✅ يبقى : {keep.get('name')}  (active={keep.get('active')})")
        for d in drop:
            print(f"  🗑️  يُحذف: {d.get('name')}  (active={d.get('active')})")
    print("-" * 60)

    if args.dry_run:
        print(f"[معاينة فقط] سيُحذف {will_remove} سجلًّا عند التشغيل بلا --dry-run.")
        await db.close()
        return

    removed = await db.treasuries.dedupe_by_code()
    remaining = await db.treasuries.col.count_documents({})
    print(f"\n✔ اكتمل التنظيف: حُذف {removed} سجلًّا مكرّرًا. الخزائن الآن: {remaining}.")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
