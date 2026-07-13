"""
إنشاء/إدارة مستخدم لوحة التحكّم (§14.3 SEC-001) — المسار المعتمد للإعداد الأوّل.

لا كلمة مرور ثابتة في الكود أو ملف: تُدخَل تفاعليًّا وقت الإعداد (getpass) وتُخزَّن هاش
argon2id فقط. يتّصل بالقاعدة من settings (mongo_uri/mongo_db).

الاستخدام:
    python -m tools.create_admin                       # ينشئ «manager» ويطلب كلمة المرور
    python -m tools.create_admin --username sara --role reviewer
    python -m tools.create_admin --username manager --reset-password
    python -m tools.create_admin --username sara --disable   # تعطيل بلا حذف (§13)

الأدوار: manager (وصول كامل) · reviewer (قراءة) · data_entry (بلا وصول للإعدادات حاليًا).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# إخراج UTF-8 دائمًا (تفادي تعطّل الطرفية على ويندوز مع العربية)
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")

from core.config import get_settings
from core.constants import Role
from core.db import Database, utcnow
from core.models import UserRecord
from dashboard.auth import hash_password


def _prompt_password() -> str:
    """يطلب كلمة المرور مرّتين للتأكيد؛ حدّ أدنى ٨ محارف (مطابق للـ API)."""
    while True:
        pw = getpass.getpass("كلمة المرور: ")
        if len(pw) < 8:
            print("  ✗ كلمة المرور قصيرة (٨ محارف على الأقل). أعد المحاولة.")
            continue
        pw2 = getpass.getpass("تأكيد كلمة المرور: ")
        if pw != pw2:
            print("  ✗ الكلمتان غير متطابقتين. أعد المحاولة.")
            continue
        return pw


async def main() -> None:
    parser = argparse.ArgumentParser(description="إنشاء/إدارة مستخدم لوحة التحكّم (§14.3).")
    parser.add_argument("--username", default="manager", help="اسم المستخدم (افتراضي: manager)")
    parser.add_argument("--role", default="manager",
                        choices=[r.value for r in Role], help="الدور (افتراضي: manager)")
    parser.add_argument("--reset-password", action="store_true",
                        help="إعادة تعيين كلمة مرور مستخدم موجود (يُبطل جلساته)")
    parser.add_argument("--disable", action="store_true", help="تعطيل مستخدم (بلا حذف §13)")
    parser.add_argument("--enable", action="store_true", help="إعادة تفعيل مستخدم معطّل")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings.mongo_uri, settings.mongo_db)
    print(f"الاتصال بـ MongoDB: {settings.mongo_uri} / {settings.mongo_db} ...")
    try:
        await db.connect()
        await db.ensure_indexes()      # يضمن الفهرس الفريد على username
        await db.users.col.count_documents({})  # تحقّق اتصال فعلي (motor كسول)
    except Exception as exc:  # T5 — لا ابتلاع صامت
        sys.exit(f"تعذّر الاتصال بـ MongoDB: {exc}\n"
                 f"تأكّد أن القاعدة تعمل وأن mongo_uri صحيح في البيئة (.env).")

    username = args.username.strip()
    existing = await db.users.get(username)

    try:
        if args.disable:
            if existing is None:
                sys.exit(f"مستخدم غير موجود: {username}")
            await db.users.set_active(username, False)
            revoked = await db.sessions.delete_for_user(username)
            print(f"✔ عُطِّل «{username}» (أُبطلت {revoked} جلسة). لم يُحذف (§13).")
            return

        if args.enable:
            if existing is None:
                sys.exit(f"مستخدم غير موجود: {username}")
            await db.users.set_active(username, True)
            print(f"✔ فُعِّل «{username}».")
            return

        if args.reset_password:
            if existing is None:
                sys.exit(f"مستخدم غير موجود: {username} — استخدم الأمر بلا --reset-password لإنشائه.")
            pw = _prompt_password()
            await db.users.update_password(username, hash_password(pw))
            revoked = await db.sessions.delete_for_user(username)
            print(f"✔ حُدِّثت كلمة مرور «{username}» (أُبطلت {revoked} جلسة).")
            return

        # إنشاء جديد
        if existing is not None:
            sys.exit(f"المستخدم «{username}» موجود مسبقًا. استخدم --reset-password لتغيير كلمته.")
        pw = _prompt_password()
        rec = UserRecord(username=username, password_hash=hash_password(pw),
                         role=Role(args.role), active=True, created_at=utcnow())
        created = await db.users.create(rec)
        if not created:  # سباق نادر — أُنشئ بين الفحص والكتابة
            sys.exit(f"المستخدم «{username}» أُنشئ لتوّه من مكان آخر.")
        print(f"✔ أُنشئ المستخدم «{username}» بدور «{args.role}».")
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
