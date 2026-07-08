# برومبت التعديلات على الكود القارئ — MONEYADO Writer

**الملف المستهدف:** `core/writers/moneyado/screens.py`
**الهدف:** تعديل `MoneyadoScreen` ليتّصل بنافذة MONEYADO **بلا عنوان**، ويحدّد الحقول **بالإحداثيات الثابتة** بدل `auto_id` (غير الموجود)، مع الحفاظ على كل منطق الأمان القائم.

> **السياق الحاسم (من الفحص الفعلي على الجهاز):**
> - MONEYADO تطبيق VB6. نوافذ الفورمات من صنف `ThunderRT6FormDC` وعنوانها **فارغ** (`window_text()==''`).
> - لذلك `connect(title_re=".*بيع عملة.*")` **يفشل حتميًا** — لا يوجد عنوان يطابقه.
> - الحقول (`ThunderRT6TextBox` / `ThunderRT6ComboBox`) **بلا auto_id ثابت**؛ الترقيم التسلسلي (`EditN`) هشّ ويتغيّر. المرساة الوحيدة الموثوقة = **إحداثيات الحقل داخل الفورم (L/T)**.
> - يوجد **عدّة نسخ من MONEYADO** قد تعمل؛ التمييز بين الشاشتين يكون بعدد/تخطيط الحقول، لا بالعنوان.

---

## القاعدة العليا التي لا تُنتهك

**لا تغيّر أيًّا من منطق الأمان القائم.** يبقى كما هو حرفيًا:
- `type_keys` وليس `set_text` (البحث التلقائي).
- `wait("ready visible enabled")` وليس `sleep`.
- حارس الأزرار الممنوعة في `press_store` (رفض طباعة/معاينة/إيصال — §2.3).
- `check_unexpected_window` (النوافذ الطارئة).
- لا silent catches (§T5) — كل استثناء يُسجَّل.
- مطابقة الأزرار بالعنوان (`title_re`) **تبقى** — الأزرار لها عناوين ثابتة («تخزين»/«رجوع») وتعمل صحيحًا.

التعديل **يقتصر على**: (1) كيفية الاتصال بالنافذة، (2) كيفية تحديد الحقل. لا شيء غيرهما.

---

## التعديل 1 — الاتصال بالنافذة بلا عنوان (دالة `connect`)

**المشكلة:** الدالة الحالية تطابق بالعنوان:
```python
title_re = self._screen(operation)["window_title_re"]
self._app = Application(backend="win32").connect(title_re=title_re, timeout=self._timeout)
self._window = self._app.window(title_re=title_re)
```
هذا يفشل لأن النافذة بلا عنوان.

**المطلوب:** استبدل جسم `connect` بالمنطق التالي — الاتصال بعملية MONEYADO، ثم التقاط الفورم النشط من صنف `ThunderRT6FormDC`:

- اقرأ من إعداد الشاشة مفتاحًا جديدًا `process_name` (افتراضيًا `"stock.exe"` — اسم تنفيذي MONEYADO الفعلي) ومفتاح `form_class` (افتراضيًا `"ThunderRT6FormDC"`).
- اتصل بالعملية عبر اسمها: `Application(backend="win32").connect(path=process_name, timeout=...)`. لو تعدّدت العمليات، pywinauto يتصل بأحدثها؛ إن لزم لاحقًا نمرّر `process` (PID) صريحًا — اتركه قابلًا للتمرير عبر بارامتر اختياري `pid=None`.
- التقط الفورم: `self._window = self._app.window(class_name=form_class)`.
- **تمييز الشاشة الصحيحة (بيع مقابل شراء):** بما أن الفورمين من نفس الصنف وبلا عنوان، ميّزهما ببصمة تخطيط: عدد حقول الإدخال، أو وجود زر مميّز. أضف تحقّقًا: بعد التقاط الفورم، تأكّد أن عدد `descendants(class_name="ThunderRT6TextBox")` ضمن المدى المتوقّع للشاشة (يُقرأ من الإعداد `expected_field_count`). إن لم يطابق → استثناء واضح (لا تكمل على شاشة غلط — §0 القاعدة الذهبية).
- أبقِ `self._window.wait("ready visible enabled", timeout=self._timeout)`.

> **مبرّر:** الاتصال بالعملية + صنف الفورم بديل موثوق عن العنوان المفقود. تحقّق عدد الحقول حارس ضد فتح الشاشة الخطأ.

---

## التعديل 2 — تحديد الحقل بالإحداثيات (دالة `_control`)

**المشكلة:** الدالة الحالية تبحث بـ `control_id`/`auto_id`:
```python
kwargs = {"control_id": auto_id} if isinstance(auto_id, int) else {"auto_id": str(auto_id)}
ctrl = self._window.child_window(**kwargs)
```
لا يوجد auto_id/control_id موثوق في MONEYADO.

**المطلوب:** غيّر آلية التحديد لتعتمد **إحداثيات الحقل (left, top) داخل الفورم**، مع class_name، كمفتاح مركّب:

- بدّل توقيع المُعرّف: بدل `auto_id` رقمي/نصّي، كل حقل في `moneyado_fields.json` يحمل الآن:
  - `coord`: `[left, top]` — إحداثيات الزاوية العليا-اليسرى للحقل **نسبةً للفورم**.
  - `class`: `"ThunderRT6TextBox"` أو `"ThunderRT6ComboBox"`.
  - `tolerance`: هامش تطابق بالبكسل (افتراضي 8).
- أعد كتابة `_control` بحيث:
  1. تجمع كل الأبناء من الـ `class` المطلوب: `self._window.descendants(class_name=field_class)`.
  2. تحسب لكل عنصر إحداثيه **نسبةً للفورم**: `rel_left = ctrl.rectangle().left - form_rect.left` (وكذلك top). خزّن `form_rect = self._window.rectangle()` مرة واحدة في `connect`.
  3. تختار العنصر الذي `abs(rel_left - target_left) <= tolerance and abs(rel_top - target_top) <= tolerance`.
  4. **حارس التفرّد (§0):** إن طابق **أكثر من عنصر واحد** ضمن الهامش → استثناء صريح (`RuntimeError` مع تفاصيل الإحداثيات) — لا تخمّن أيّهما. إن لم يطابق **أي** عنصر → استثناء «الحقل غير موجود عند الإحداثي X,Y».
  5. `ctrl.wait("ready visible enabled", timeout=self._timeout)` ثم أرجعه.

> **مبرّر:** الإحداثي النسبي ثابت (التصميم ثابت) ولا يتأثر بحركة الفورم على الشاشة ولا بإعادة ترتيب pywinauto. حارس التفرّد يمنع الكتابة في حقل مجاور خطأً — حرج ماليًا.

---

## التعديل 3 — نفس التغيير على دالة `_button` (تحديد الزر)

`_button` حاليًا تدعم مسارين: `auto_id` (غير مستخدم) و `title_re` (يعمل). **أبقِ مسار `title_re` كما هو** — الأزرار لها عناوين ثابتة. فقط:
- احذف/عطّل فرع `auto_id` الرقمي (لن يُستخدم).
- تأكّد أن `control_type="Button"` يعمل مع صنف VB6 `ThunderRT6CommandButton`. إن لم يلتقطه، بدّل البحث إلى `child_window(title_re=cfg["title_re"], class_name="ThunderRT6CommandButton")`.

---

## التعديل 4 — `from_settings` و بنية الإعداد

`from_settings` تبقى كما هي (تحمّل `moneyado_fields.json`). لكن **بنية الملف تغيّرت** من `auto_id` إلى `coord/class`. حدّث أي منطق يقرأ `auto_id` من الحقول ليقرأ `coord`. القيمة `null` في `coord` تظل تمنع البوت من التعبئة (نفس دور `auto_id: null` سابقًا — §11.1).

---

## ملف الإعداد الجديد `config/moneyado_fields.json` (معبّأ بالإحداثيات المؤكّدة)

استبدل بنية `auto_id: null` بالإحداثيات التالية (مستخرجة ومؤكّدة بصريًا بـ `draw_outline` على الجهاز، بايثون 32-bit). الإحداثيات **نسبية للفورم** [left, top]:

```json
{
  "sell_screen": {
    "process_name": "stock.exe",
    "form_class": "ThunderRT6FormDC",
    "expected_field_count": 30,
    "fields": {
      "foreign_account":       {"coord": [441, 80],  "class": "ThunderRT6TextBox",  "order": 1,  "enter": true,  "note": "الحساب الأجنبي (الخزينة) ثم Enter"},
      "reference_number":      {"coord": [11, 130],  "class": "ThunderRT6TextBox",  "order": 2,  "note": "الرقم الإشاري A6xxx"},
      "customer":              {"coord": [361, 180], "class": "ThunderRT6TextBox",  "order": 3,  "enter": true,  "note": "كود الزبون فقط + Enter — الاسم يظهر تلقائيًا"},
      "customer_name_display": {"coord": [451, 180], "class": "ThunderRT6TextBox",  "order": 3,  "read_only": true, "note": "اسم الزبون الظاهر — قراءة وتحقّق فقط، لا يُكتب"},
      "foreign_amount":        {"coord": [581, 230], "class": "ThunderRT6TextBox",  "order": 4,  "note": "المبلغ قبل الخصم، فاصل الآلاف مُشال"},
      "currency_type":         {"coord": [111, 230], "class": "ThunderRT6ComboBox", "order": 5,  "note": "3=تونسي، 4=مصري"},
      "rate_multiply":         {"coord": [651, 280], "class": "ThunderRT6TextBox",  "order": 6,  "note": "الضارب × — 1 دائمًا"},
      "rate_divide":           {"coord": [381, 280], "class": "ThunderRT6TextBox",  "order": 7,  "note": "القسمة / — السعر المطبَّع (§3.6)"},
      "commission_rate":       {"coord": [661, 330], "class": "ThunderRT6TextBox",  "order": 8,  "note": "نسبة العمولة — 0 دائمًا"},
      "commission":            {"coord": [461, 330], "class": "ThunderRT6TextBox",  "order": 9,  "note": "العمولة — الفرق بالسالب أو فارغة"},
      "country":               {"coord": [451, 380], "class": "ThunderRT6TextBox",  "order": 10, "note": "البلد — غير حرج"},
      "payment_method":        {"coord": [361, 430], "class": "ThunderRT6TextBox",  "order": 11, "note": "رقم الهاتف"},
      "notes":                 {"coord": [361, 480], "class": "ThunderRT6TextBox",  "order": 12, "note": "اسم المستلم إن وُجد"},
      "date_field":            {"coord": null,       "class": "MSMaskWndClass",     "read_only": true, "note": "التاريخ تلقائي — لا يُلمس"}
    },
    "_computed_not_filled": {
      "note": "حقول يحسبها/يولّدها البرنامج — البوت لا يكتبها (§11.1): رقم المعاملة، المبلغ المحلي، رمز الحساب، رمز العملة، المبلغ المخصوم، كود البلد، الأرصدة."
    },
    "buttons": {
      "store": {"title_re": "^تخزين$",        "class": "ThunderRT6CommandButton", "note": "زر «تخزين» فقط (§2.3)"},
      "stop":  {"title_re": "STOP|رجوع|إلغاء", "class": "ThunderRT6CommandButton", "note": "صمّام الأمان — يلغي بلا حفظ"}
    },
    "forbidden_buttons": ["طباعة وتخزين", "معاينة وتخزين", "ايصال قبض", "ايصال صرف"]
  },

  "buy_screen": {
    "process_name": "stock.exe",
    "form_class": "ThunderRT6FormDC",
    "expected_field_count": 20,
    "fields": {
      "foreign_account":  {"coord": [27, 27],   "class": "ThunderRT6TextBox",  "order": 1, "enter": true, "note": "راجع ملاحظة الإحداثيات أدناه"},
      "reference_number": {"coord": null,        "class": "ThunderRT6TextBox",  "order": 2, "note": "الرقم الإشاري"},
      "currency_type":    {"coord": null,        "class": "ThunderRT6ComboBox", "order": 3, "note": "3=تونسي، 4=مصري"},
      "rate_multiply":    {"coord": null,        "class": "ThunderRT6TextBox",  "order": 4, "note": "الضارب × — 1 دائمًا"},
      "rate_divide":      {"coord": null,        "class": "ThunderRT6TextBox",  "order": 5, "note": "القسمة / — السعر المطبَّع"},
      "quantity":         {"coord": null,        "class": "ThunderRT6TextBox",  "order": 6, "note": "الكمية = المبلغ الأجنبي"},
      "commission":       {"coord": null,        "class": "ThunderRT6TextBox",  "order": 7, "note": "العمولة"},
      "customer":         {"coord": null,        "class": "ThunderRT6TextBox",  "order": 8, "enter": true, "note": "كود المورد + Enter"},
      "country":          {"coord": null,        "class": "ThunderRT6TextBox",  "order": 9, "note": "البلد"},
      "payment_method":   {"coord": null,        "class": "ThunderRT6TextBox",  "order": 10, "note": "رقم الهاتف"},
      "notes":            {"coord": null,        "class": "ThunderRT6TextBox",  "order": 11, "note": "ملاحظات"},
      "date_field":       {"coord": null,        "class": "MSMaskWndClass",     "read_only": true, "note": "التاريخ تلقائي"}
    },
    "_computed_not_filled": {
      "note": "يحسبها/يولّدها البرنامج: رقم المعاملة، المبلغ الصافي، المبلغ المسلّم، رمز الحساب، رمز العملة، كود البلد، الأرصدة."
    },
    "buttons": {
      "store": {"title_re": "^تخزين$",        "class": "ThunderRT6CommandButton"},
      "stop":  {"title_re": "STOP|رجوع|إلغاء", "class": "ThunderRT6CommandButton"}
    },
    "forbidden_buttons": ["طباعة وتخزين", "معاينة وتخزين", "ايصال قبض", "ايصال صرف"]
  },

  "unexpected_window_titles": ["رصيد", "خطأ", "تنبيه", "Crystal", "معاينة", "Print Preview", "غير موجود"]
}
```

> ⚠️ **إحداثيات شاشة الشراء (`coord: null`) لم تُحسب بعد نسبةً للفورم.** الأرقام التي استُخرجت للشراء كانت من جلسة سابقة وبمرجع مختلف. **قبل تفعيل الشراء:** أعد سكربت استخراج الإحداثيات على فورم «شراء عملة» (بايثون 32-bit) لتعبئة هذه القيم. `null` يمنع التعبئة تلقائيًا حتى تُستكمل (نفس مبدأ §11.1).

---

## قائمة تحقّق ما بعد التعديل

- [ ] `connect` يتصل بلا عنوان (بالعملية + صنف الفورم) وينجح على شاشة البيع المفتوحة.
- [ ] `expected_field_count` يرفض الشاشة الخطأ.
- [ ] `_control` يلتقط الحقل الصحيح بالإحداثي، ويرمي استثناءً عند التطابق المزدوج أو انعدام التطابق.
- [ ] الأزرار («تخزين»/«رجوع») تُلتقط بالعنوان كما قبل.
- [ ] حارس الأزرار الممنوعة يعمل (لا يُضغط زر طباعة/معاينة).
- [ ] `pytest` ينجح (الـ mock لا يتأثر — التغيير في `MoneyadoScreen` الحيّ فقط).
- [ ] إحداثيات الشراء مُستكملة (لا `null`) قبل تفعيل الطرفين.
- [ ] اختبار جافّ (`DRY_RUN=true`): تعبئة بيع كاملة بلا ضغط «تخزين»، ومراجعة بصرية أن كل قيمة دخلت خانتها الصحيحة.
