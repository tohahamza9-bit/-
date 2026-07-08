# خدمة WhatsApp (Baileys) — جسر MONEYADO

جسر Node.js يربط رقم واتساب البوت بنواة Python عبر **MongoDB**:

- **الالتقاط** (§7.1): كل رسالة (المركزية/غرف الزبائن/الخزائن) → تُخزَّن خام فورًا في `raw_messages`.
- **الإرسال** (§2.2 §8.3): يستهلك طابور `outgoing` الذي تكتبه النواة، ويرسله عبر Reply/Reaction **حصرًا** للمركزية وغرفة المسؤول.

> 🔴 **قاعدة الإخراج الصارمة (§2.2):** البوت لا يكتب إلا للمركزية وغرفة المسؤول. غرف الزبائن/الخزائن قراءة صامتة مطلقة. الحماية مبنيّة في `src/whitelist.js` وتمرّ منها كل رسالة صادرة.

---

## التكامل مع النواة (Mongo)

| المجموعة | الاتجاه | المنتِج | المستهلِك | الشكل |
|---|---|---|---|---|
| `raw_messages` | ننتج | هذا الجسر | نواة Python | `core/models.py::RawMessage` |
| `outgoing` | نستهلك | نواة Python (`core/bus.py`) | هذا الجسر | `{chat_jid, text, reply_to_key, reaction, sent}` |

- `raw_messages`: upsert على `message_key` (نفس فهرس النواة — لا ازدواج §9/§12).
- `outgoing`: نقرأ `{sent:false}` مرتّبة بـ `created_at` → نرسل → نعلّم `sent:true` + `sent_at`.
  - بند فيه `reaction` → تفاعل صامت (🔸/✅). غير ذلك → Reply بمفتاح `reply_to_key`.

`message_key` يُرمَّز `remoteJid|id|fromMe|participant` (انظر `src/keys.js`) كي يعيد المُرسِل بناء `WAMessageKey` بدقّة دون الرسالة الأصلية، وكي تصحّ مطابقة الردود على جانب النواة.

---

## الإعداد (env — §14.3)

يقرأ من نفس `.env` الجذري للمشروع:

| المتغيّر | الوصف |
|---|---|
| `MONGO_URI`, `MONGO_DB` | اتصال MongoDB (مصدر الحقيقة) |
| `CENTRAL_ROOM_JID` | المركزية — قراءة + Reply |
| `ADMIN_ROOM_JID` | غرفة المسؤول — التصعيد |
| `CUSTOMER_ROOM_JIDS`, `TREASURY_ROOM_JIDS` | قوائم JID (قراءة صامتة، مفصولة بفواصل) |
| `INTERNAL_TOKEN` | SEC-002 (احتياطي HTTP) |
| `WA_SESSION_DIR` | مجلد جلسة Baileys (افتراضي `./session`) |
| `LOG_LEVEL` | مستوى pino (افتراضي `info`) |
| `WA_SENDER_INTERVAL_MS` | فاصل جولات الإرسال (افتراضي 2000) |

---

## التشغيل

```bash
cd whatsapp
npm install            # تثبيت التبعيات (Baileys, mongodb, pino)
npm start              # node src/index.js
```

- **أول مرّة:** يُطبع رمز QR في الطرفية — امسحه من واتساب (الأجهزة المرتبطة). تُحفظ الجلسة في `WA_SESSION_DIR` فلا يتكرّر.
- **إنتاج (PM2):**
  ```bash
  NODE_ENV=production pm2 start src/index.js --name moneyado-wa
  pm2 save
  ```

---

## حمايات عدم الحظر والاستقرار (§14) — `src/antiban.js`

| الرمز | التطبيق |
|---|---|
| **W0** | قاطع دائرة: حد **5 QR**، توقّف بعد **3 رفض** → تدخّل يدوي (`CircuitBreaker`) |
| **W1** | تأخير عشوائي **4–9s** بتوزيع gaussian قبل كل إرسال |
| **W1-B** | فاصل **1.5–4s** بين الغرف المختلفة |
| **W2** | تنويع بصمة الجهاز (`browser variants`) + `markOnlineOnConnect=false` |
| **warm-up** | سقف متدرّج: 10/ساعة → 50 → 150 → … → 1500 |
| **T3** | إغلاق آمن على SIGTERM/SIGINT (graceful drain + حفظ الجلسة) |
| **T5** | لا silent catches — كل خطأ يُسجَّل عبر pino |

- رقم البوت **جديد نظيف**؛ قراءة أكثر من كتابة.
- عند فتح القاطع (W0): يتوقّف الإرسال ويُطلب تدخّل يدوي (`CircuitBreaker.reset()` بعد الإصلاح).
- **تسجيل خروج (loggedOut):** لا إعادة اتصال تلقائي — احذف مجلد الجلسة وأعد الربط يدويًا.

---

## الاختبار

```bash
npm test               # node --test test/
```

يغطّي المنطق النقيّ بلا Baileys/Mongo: whitelist (§2.2)، W0/W1/W1-B/warm-up (§14)، ترميز المفاتيح ومطابقة الردود، والتقاط/تعديل الرسائل «الحرف» (§7.2). **25 اختبارًا — كلها تنجح.**

---

## بنية الملفات

```
whatsapp/
├── package.json
├── src/
│   ├── index.js       التركيب: Baileys + capture + sender + antiban + إغلاق آمن
│   ├── config.js      قراءة env (نقيّة)
│   ├── logger.js      pino (T5)
│   ├── db.js          اتصال Mongo + المجموعات
│   ├── whitelist.js   🔴 قاعدة الإخراج الصارمة (§2.2) — نقيّة
│   ├── keys.js        ترميز/فكّ WAMessageKey ↔ نص — نقيّة
│   ├── messages.js    تحويل رسالة Baileys → RawMessage — نقيّة
│   ├── capture.js     الالتقاط + التعديل «الحرف» (§7.1/§7.2)
│   ├── sender.js      استهلاك outgoing (Reply/Reaction) عبر whitelist + antiban
│   └── antiban.js     🔴 W0/W1/W1-B/W2 + warm-up + قاطع الدائرة — نقيّة
└── test/
    └── whitelist.test.js
```
