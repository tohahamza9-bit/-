# دليل النقل والتشغيل على الجهاز الفرعي

من النسخ إلى التشغيل الحيّ. اتبع المراحل بالترتيب. **لا تفعّل التخزين إلا في المرحلة 7.**

---

## المرحلة 0 — ماذا تنسخ
انسخ مجلد `moneyado-bot/` كاملًا **ما عدا** (تُعاد على الجهاز الفرعي):
- `.venv/` (بيئة بايثون — تُبنى من جديد)
- `whatsapp/node_modules/` (إن وُجد)
- `artifacts/` (سجلّات/لقطات مؤقتة)
- أي `.env` أو `config/*.json` حقيقي (لا يوجد بعد — فقط أمثلة).

> نسخ عبر USB/شبكة يكفي. لا تنسخ `.venv` (خاص بالمسار ويكبر بلا داعٍ).

---

## المرحلة 1 — المتطلّبات على الجهاز الفرعي (تُثبَّت مرّة)
1. **Python 3.11** (نفس الإصدار) — فعّل «Add to PATH».
2. **Node.js 20**.
3. **MongoDB Community** — ثبّته **كخدمة Windows** (تعمل تلقائيًا).
4. **ODBC Driver 17 for SQL Server** (لـ pyodbc — للتحقّق §11.4).
5. **MONEYADO** مثبّت ويعمل (موجود أصلًا على الجهاز).

---

## المرحلة 2 — بناء البيئة
```powershell
cd moneyado-bot
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt      # يشمل pyodbc/pywinauto/pywin32 (Windows)
cd whatsapp
npm install
cd ..
```
تحقّق سريع:
```powershell
.venv\Scripts\python -m pytest -q                   # يجب أن تنجح كل الاختبارات
```

---

## المرحلة 3 — الإعدادات والأسرار
```powershell
copy .env.example .env
copy config\moneyado_fields.example.json config\moneyado_fields.json
copy config\sql_queries.example.json config\sql_queries.json
```
افتح `.env` واملأ (اترك ما لا تعرفه الآن، تُكمَّل لاحقًا):
- `MONGO_URI=mongodb://localhost:27017` · `MONGO_DB=moneyado`
- `INTERNAL_TOKEN=` ← **رمز عشوائي طويل** (يحمي اللوحة). ولّده مثلًا:
  `.venv\Scripts\python -c "import secrets; print(secrets.token_urlsafe(32))"`
- `DRY_RUN=true` (يبقى true أثناء المراقبة)
- الغرف (`CENTRAL_ROOM_JID` ...) → **تُملأ في المرحلة 5** بعد معرفة معرّفاتها.
- `SQL_ENABLED=false` مؤقتًا → يُفعَّل في المرحلة 6.

---

## المرحلة 4 — استخراج `auto_id` للشاشتين (§11.3، ملحق ب-1)
1. افتح MONEYADO يدويًا على شاشة **«بيع عملة»**.
2. شغّل (قراءة فقط — آمن تمامًا):
   ```powershell
   .venv\Scripts\python tools\extract_auto_ids.py --screen sell
   ```
   يُنتج `artifacts\sell_identifiers.txt` و`artifacts\sell_fields_scaffold.json`.
3. كرّر لشاشة **«شراء عملة»**: `--screen buy`.
4. من `*_scaffold.json` اربط كل دور (`foreign_account`, `customer`, `foreign_amount`, ...) بـ `auto_id`
   الصحيح، وانقل القيم إلى `config\moneyado_fields.json`. **لا تخمّن** — بدون auto_id صحيح الكاتب يرفض التعبئة.

---

## المرحلة 5 — تشغيل WhatsApp ومعرفة معرّفات الغرف
1. شغّل خدمة واتساب (برقم البوت الجديد النظيف):
   ```powershell
   cd whatsapp
   node --env-file=..\.env src\index.js       # أو npm start إن هيّأت السكربت لقراءة .env
   ```
   امسح **QR** أول مرة من واتساب البوت.
2. أضِف رقم البوت إلى: المركزية + غرفة المسؤول + غرف الزبائن + غرف الخزائن.
3. لمعرفة معرّفات الغرف (JID): بعد أن يلتقط البوت أول رسالة من كل غرفة، اقرأ `chat_jid` من:
   ```powershell
   # عبر mongosh:  db.raw_messages.distinct("chat_jid")
   ```
   طابق كل JID بغرفته (من نصّ الرسائل)، ثم املأ `.env`:
   `CENTRAL_ROOM_JID`, `ADMIN_ROOM_JID`, `CUSTOMER_ROOM_JIDS` (مفصولة بفواصل), `TREASURY_ROOM_JIDS`.
4. أعد تشغيل خدمة واتساب بعد ملء الغرف.

> **الأمان (§14.1):** رقم جديد نظيف + warm-up متدرّج مبني في الخدمة. لا ترسل يدويًا من رقم البوت.

---

## المرحلة 6 — SQL Server للتحقّق (§11.4)
> **يُمنع الاختبار على القاعدة الحيّة.** استخدم نسخة مسترجَعة معزولة.
1. استرجِع نسخة تجريبية بأداة **REST_ADO** إلى SQL Server تجريبي معزول.
2. أنشئ مستخدم **قراءة فقط** (ليس sa).
3. من فحص القاعدة، املأ في `config\sql_queries.json`: أسماء جدولَي الحوالات والزبائن + الأعمدة.
4. في `.env`: `SQL_DSN=...` (بالمستخدم القراءة-فقط) و`SQL_ENABLED=true`.
5. تحقّق: شغّل النواة (المرحلة 7) وراقب سجلّ `artifacts\logs\bot.log` أن التحقّق يعمل بلا أخطاء.

---

## المرحلة 7 — تشغيل النواة (Kill Switch = إيقاف افتراضيًا)
```powershell
.venv\Scripts\uvicorn core.app:get_app --factory --host 0.0.0.0 --port 8000
```
- افتح اللوحة: `http://localhost:8000/` (أو من موبايل على نفس الشبكة).
- **التخزين افتراضيًا إيقاف** (§13) + `DRY_RUN=true` → البوت **يقرأ ويفهم ويطابق لكن لا يخزّن**.
- من اللوحة أضِف (تُفعَّل فورًا):
  - **أكواد الخزائن المعلّقة:** خصم 1%، صافي، هادم مصر، تونسي خارجي (من ملحق ب-3).
  - **الموردين** (مؤمن، طه، البراق...) بأكوادهم وإملاءاتهم البديلة.
  - **الموظفين المعتمدين** (رقم واتساب + اسم) — «تم»/الإلغاء تُقبل منهم فقط.

---

## المرحلة 8 — المراقبة ثم الإطلاق التدريجي (§16 م2)
1. **يوم–يومان مراقبة:** `DRY_RUN=true`، تخزين إيقاف. راقب في اللوحة/السجلّ أن الفهم والمطابقة والعلامات صحيحة على الحوالات الحقيقية، بلا تخزين.
2. **التفعيل التدريجي** (بعد الاطمئنان): اضبط `DRY_RUN=false`، ثم فعّل التخزين من اللوحة، وابدأ بنوع واحد:
   **مصري بيع عادي بلا خصم** → خصم → تونسي → طرفان → إلغاء/تعديل.
3. **Kill Switch جاهز دائمًا:** أي خلل → أوقف التخزين من اللوحة فورًا.

---

## المرحلة 9 (لاحقًا) — التشغيل التلقائي الدائم (§2.1، §12)
بعد الاستقرار، للتشغيل بعد كل إقلاع:
- MongoDB: خدمة Windows (تلقائية).
- خدمة واتساب: **PM2** — 🔴 **شغّلها بمجلد عمل حزمة whatsapp حتى تُستأنف الجلسة** (لا QR كل إعادة تشغيل):
  `pm2 start src/index.js --name moneyado-wa --cwd whatsapp` ثم `pm2 save` + `pm2-startup`.
  أو ملف `ecosystem.config.js` بـ `cwd: './whatsapp'`. بديل أمتن: اضبط `WA_SESSION_DIR` إلى **مسار مطلق**
  ثابت في `.env` (مثل `D:\moneyado\wa-session`) — عندها لا يهمّ مجلد التشغيل إطلاقًا.
  (مسار الجلسة صار مطلقًا مربوطًا بجذر حزمة whatsapp تلقائيًا، لكن ضبط `cwd`/`WA_SESSION_DIR` يوثّق النية.)
- النواة: **NSSM** أو Task Scheduler لتشغيل أمر uvicorn عند الإقلاع.
- فتح MONEYADO تلقائيًا + إدخال كلمة سرّه (مشفّرة DPAPI §2.1) — يُجهَّز في هذه المرحلة.
- صمّام الاسترجاع `recover_pending` يعمل تلقائيًا عند بدء النواة (يفحص SQL قبل أي إدخال — §12).

---

## تحقّق سريع من الجاهزية قبل تفعيل التخزين
- [ ] `pytest` ينجح على الجهاز الفرعي.
- [ ] `config/moneyado_fields.json` مكتمل (لا `auto_id: null`).
- [ ] `config/sql_queries.json` مكتمل و`SQL_ENABLED=true` والتحقّق يعمل.
- [ ] معرّفات الغرف الأربعة في `.env` صحيحة.
- [ ] أكواد الخزائن المعلّقة + الموردون + الموظفون مُدخَلون من اللوحة.
- [ ] اللوحة تفتح ومحميّة بـ `INTERNAL_TOKEN`.
- [ ] MONEYADO مفتوح، والجهاز مخصّص للبوت (لا يستخدمه موظف بالتوازي).
