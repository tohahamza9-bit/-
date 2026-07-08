# بوت MONEYADO — أتمتة إدخال الحوالات

ربط غرفة المركزية (واتساب) ببرنامج **MONEYADO** عبر أتمتة الشاشة (RPA).
شركة أبناء حمزة. المرجع الكامل: [`../مواصفات_بوت_MONEYADO_للتنفيذ.md`](../مواصفات_بوت_MONEYADO_للتنفيذ.md).

> **القاعدة الذهبية:** لو شكّيت — لا تُنزّل. علّق ⚠️، نبّه (Reply)، وكمّل.
> البوت لا يوقف الطابور أبدًا، ولا يُنزّل غلط أبدًا.

## البنية

```
whatsapp/   خدمة Node.js (Baileys) — التقاط + Reply مقيّد (whitelist §2.2)
core/       خدمة Python (FastAPI) — الفهم، الطابور، المطابقة، الكُتّاب، التحقّق، الحارس
dashboard/  واجهة ويب — Kill Switch + إدارة الخزائن/الموردين/الموظفين (§13)
config/     ملفات المعلّقات التقنية (auto_id، استعلامات SQL) — ملحق ب
```

### وحدات `core/`
| الوحدة | المسؤولية | المرجع |
|---|---|---|
| `parsing/` | وحدة الفهم: تفكيك A/SI، تطبيع، أرقام، سعر، حلّ الخزائن/الموردين، نوع العملية | §3 §4 §5 |
| `queue/` | الطابور الصارم، انتظار الاستقرار، تجميع الطرفين، الخصم | §6 §7 |
| `matching/` | مطابقة الغرف، بوابة الثقة، العلامات، التصعيد | §8 |
| `writers/moneyado/` | MoneyadoWriter (pywinauto RPA) | §11 |
| `verification/` | تحقّق SQL Server، استرجاع بعد الإطفاء | §11.4 §12 |
| `guard/` | منع التكرار، الإلغاء/التعديل/التصحيح | §9 §10 |
| `pipeline.py` | تدفّق البوت الكامل خطوة بخطوة | §15 |
| `db.py` `bus.py` `models.py` `constants.py` `config.py` | العقود المشتركة | — |

## العقود المشتركة (لا تُعدَّل من الوحدات)
- `core/models.py` — نماذج البيانات (pydantic). حقول جديدة تُضاف Optional فقط.
- `core/constants.py` — العملات، الخزائن، العلامات، الحالات، المهل.
- `core/db.py` — مستودعات MongoDB (`db.raw`, `db.deals`, `db.ledger`, `db.outbox`, ...).
- `core/bus.py` — ناقل الإرسال المقيّد (whitelist §2.2) — كل مخرجات البوت تمرّ منه.
- `core/writers/base.py` — واجهة الكاتب القابل للتبديل (§2.1).
- `core/config.py` — الإعدادات (env). `core/logging_setup.py` — التسجيل (T5).

## التشغيل (تطوير)
```bash
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
cp .env.example .env            # ثم املأ القيم
cp config/moneyado_fields.example.json config/moneyado_fields.json   # واستكمل auto_id
cp config/sql_queries.example.json config/sql_queries.json
pytest                          # اختبارات الوحدات
```

## المعلّقات التقنية (ملحق ب) — تُستكمل وقت التنفيذ
1. `auto_id` لخانات شاشتَي بيع/شراء → `config/moneyado_fields.json` (فحص `print_control_identifiers`).
2. أسماء جداول SQL → `config/sql_queries.json`.
3. أكواد خزائن هادم/خصم1%/صافي/تونسي خارجي → من Dashboard.
4. معرّفات الغرف → `.env` + Dashboard.

## الأمان (§14.3)
- المفاتيح في `.env` فقط — لا في الكود، لا في GitHub.
- مستخدم MONEYADO محدود الصلاحيات؛ مستخدم SQL قراءة فقط.
- قاعدة الإخراج (§2.2) whitelist صارمة في `bus.py` — غير قابلة للكسر برمجيًا.
- `DRY_RUN=true` افتراضيًا — لا كتابة فعلية حتى التفعيل الصريح.
