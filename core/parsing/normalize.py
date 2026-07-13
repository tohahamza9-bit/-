"""
التطبيع (§3.4) + قواعد الأرقام (§3.5) + السعر حسب العملة (§3.6).
مصدر الحقيقة للقواعد: المواصفات §3. لا تخمين — عند الغموض تُرجع الدالة None.
"""
from __future__ import annotations

import re
from typing import Optional

from core.constants import Currency
from core.logging_setup import get_logger

log = get_logger(__name__)

# ── تطبيع الحروف العربية (تسامح إملائي §3.4) ─────────────────────────────────
# التشكيل (ً-ْ، ٰ) + التطويل (ـ) — يُزال قبل المطابقة
_DIACRITICS = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")


# الأرقام العربية-الهندية (٠-٩) والفارسية (۰-۹) → لاتينية 0-9 (§3.5) — قبل أي مطابقة/استخراج
_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# شرطة/مسافة غير فاصلة (§3.4): «‑» (U+2011) → «-» عادية، و«NBSP» (U+00A0) → مسافة عادية — تُطبَّع
# قبل الاستخراج كي لا تكسر مجرى أرقام الهاتف/المبلغ («0100‑8235046» كان يُرجع None).
_SPECIAL_PUNCT = str.maketrans({"‑": "-", " ": " "})

# علامات bidi البصرية (RLM/LRM/التضمين) — واتساب RTL يحقنها حول الأرقام فتقلب ترتيبها البصريّ.
_BIDI_MARKS = re.compile("[‎‏‪-‮]")


def repair_bidi_digits(s: Optional[str]) -> str:
    """يزيل علامات bidi (RLM ‏ / LRM ‎ / \\u202a-\\u202e) قبل أي معالجة — تمنع انعكاس/تشويه الأرقام
    في رسائل واتساب RTL (§3.5). يُطبَّق ضمن normalize_digits فيغطّي كل مسارات الاستخراج."""
    return _BIDI_MARKS.sub("", s) if s else (s or "")


def normalize_digits(s: Optional[str]) -> str:
    """ينظّف علامات bidi ثم يحوّل الأرقام العربية-الهندية/الفارسية إلى لاتينية (٠١٠→010) — يُطبَّق
    قبل استخراج الهاتف/المبلغ/الكود إذ الأنماط الرقمية تطابق `\\d` اللاتينية فقط (§3.5). ويطبّع
    الشرطة غير الفاصلة U+2011→«-» والمسافة غير الفاصلة NBSP→مسافة كي لا تكسرا مجرى الأرقام (§3.4)."""
    if not s:
        return s or ""
    return repair_bidi_digits(s).translate(_ARABIC_INDIC_DIGITS).translate(_SPECIAL_PUNCT)


def normalize_ar(s: Optional[str]) -> str:
    """يوحّد الهمزات/التاء المربوطة/الألف المقصورة ويزيل التشكيل — للمطابقة المتسامحة."""
    if not s:
        return ""
    s = normalize_digits(s)          # أرقام عربية-هندية → لاتينية (§3.5)
    s = _DIACRITICS.sub("", s)
    s = (
        s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ٱ", "ا")
        .replace("ة", "ه").replace("ى", "ي")
        .replace("ؤ", "و").replace("ئ", "ي").replace("ء", "")
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ── العملة (§3.4) ────────────────────────────────────────────────────────────
_EGP_TOKENS = {"ج", "جم", "جنيه", "مصري", "دم"}   # «دم» = درهم/دينار مصري (§3.4)
_TND_TOKENS = {"دت", "تونسي", "دينار", "دنانير"}


def detect_currency(s: Optional[str]) -> Optional[Currency]:
    """يكشف العملة من نص المبلغ: ج م/ج.م/جنيه→EGP، د.ت/دت/تونسي→TND (§3.4)."""
    if not s:
        return None
    # «ج.م» → «ج م» ليصير رمز العملة رمزًا مستقلًا (تجنّب مطابقة «جمال»)
    norm = normalize_ar(s).replace(".", " ").replace("،", " ").replace(",", " ")
    # #1: افصل الحرف العربي عن الرقم الملتصق («3950ج»→«3950 ج»، «22800ج م»→«22800 ج م»)
    norm = re.sub(r"(\d)([ء-ي])", r"\1 \2", norm)
    norm = re.sub(r"([ء-ي])(\d)", r"\1 \2", norm)
    tokens = set(norm.split())
    if _TND_TOKENS & tokens or ("د" in tokens and "ت" in tokens):
        return Currency.TND
    if _EGP_TOKENS & tokens or ("ج" in tokens and "م" in tokens):
        return Currency.EGP
    # «م» وحدها اختصار «مصري» (§3.4) — لكن **فقط token منفصلة مجاورة لرقم**: «1000 م» / «م 1000».
    # الحصر بالمجاورة يمنع التقاطها داخل كلمة («محمد»/«مصر» token واحدة، ولا مجاورة لرقم مباشر).
    if re.search(r"\d\s+م(?:\s|$)|(?:^|\s)م\s+\d", norm):
        return Currency.EGP
    return None


# ── وسيلة الدفع (§3.4) ───────────────────────────────────────────────────────
def normalize_payment(s: Optional[str]) -> Optional[str]:
    """فودافون/فدفون/فدافون/فود فون كاش→«فودافون كاش»؛ انستاباي/انستا باي→«إنستا باي»."""
    if not s:
        return None
    compact = normalize_ar(s).replace(" ", "")
    if any(k in compact for k in ("فودافون", "فودفون", "فدفون", "فدافون", "فودا")):
        return "فودافون كاش"
    if "انستا" in compact:
        return "إنستا باي"
    return None


# ── رقم الهاتف (§3.4، قرار الجولة ٤: كما كُتب، بلا تحويل محلّي↔دوليّ لأي دولة) ──────────────────
def _is_libyan_phone(d: str) -> bool:
    """رقم ليبيّ (دوليّ 218/00218 أو محلّي 09x بـ10 خانات) — أولويّته أدنى في الاختيار (§3.4)."""
    return d.startswith("00218") or d.startswith("218") or (len(d) == 10 and d.startswith("09"))


def _is_complete_phone(d: str) -> bool:
    """مجرى أرقام يطابق **شكل هاتف كامل** بالطول+البادئة (§3.4) — يميّزه عن مجموعةٍ جزئية من هاتفٍ
    دوليّ مكتوبٍ بمجموعات، وعن مبلغٍ مجاور. يُستعمَل في المرحلة (أ) لعزل الهاتف بلا دمج مسافات."""
    n = len(d)
    if n == 8:
        return d[0] in "259"                                # تونسي محلّي
    if n == 10:
        return d.startswith("09")                           # ليبي محلّي
    if n == 11:
        return d.startswith("01") or d.startswith("216")    # مصري محلّي / تونسي دوليّ
    if n == 12:
        return d[:3] in ("218", "216") or d.startswith("20")  # ليبي/تونسي/مصري دوليّ
    if n in (13, 14):                                       # دوليّ ببادئة 00 أو صيغ أطول
        return d.startswith(("00", "20", "216", "218"))
    return False


def classify_phone(raw: Optional[str]) -> Optional[str]:
    """يصنّف مجرى أرقام ويُرجعه هاتفًا **كما كُتب** أو None (§3.4، قرار الجولة ٤). لا تحويل بين
    المحلّي والدوليّ لأي دولة — يُنظَّف الترقيم فقط (عبر normalize_digits) وتُحفَظ الصيغة كما أدخلها
    الموظّف. يُقبَل كل مجرى بطول هاتف صالح (8-14 خانة)؛ أولويّة الليبيّ تُدار في extract_phone."""
    if not raw:
        return None
    d = re.sub(r"\D", "", normalize_digits(raw))
    return d if 8 <= len(d) <= 14 else None


def _pick_phone(candidates: list[str]) -> Optional[str]:
    """يفضّل أوّل مرشّح مصريّ/تونسيّ؛ فإن غاب فأوّل ليبيّ (يُقبَل عند عدم وجود غيره، §3.4)."""
    non_libyan = [c for c in candidates if not _is_libyan_phone(c)]
    if non_libyan:
        return non_libyan[0]
    return candidates[0] if candidates else None


def extract_phone_candidates(s: Optional[str]) -> list[str]:
    """كل مرشّحي الهاتف المميّزين في النصّ (لكشف التعدّد §0) — نفس منطق extract_phone المزدوج لكن
    يُرجِع القائمة كاملةً بلا اختيار، فيحسم المُنادي. (أ) مجاري كاملة الشكل بلا دمج؛ (ب) بدمج داخل
    السطر. الترتيب يحفظ ظهورها؛ المبالغ ≤7 خانات لا تدخل (طول الهاتف 8-14)."""
    if not s:
        return []
    text = normalize_digits(s)
    out: list[str] = []
    for m in re.finditer(r"\d{8,14}", text.replace("-", "")):
        if _is_complete_phone(m.group()) and m.group() not in out:
            out.append(m.group())
    for line in text.splitlines():
        for m in re.finditer(r"\d{8,14}", re.sub(r"[\s\-+]", "", line)):
            ph = classify_phone(m.group())
            if ph and ph not in out:
                out.append(ph)
    return out


def extract_phone(s: Optional[str]) -> Optional[str]:
    """يستخرج هاتف المستلم **كما كُتب** (بلا تحويل محلّي↔دوليّ، §3.4 قرار الجولة ٤). مرحلتان لعزله
    عن مبلغٍ مجاور: (أ) بلا دمج المسافات — تُلتقَط المجاري ذات **شكل الهاتف الكامل** فقط، فينفصل
    «01029051735 50000» → الهاتف وحده؛ (ب) إن فشلت، بدمج المسافات **داخل كل سطر** (لا عبر الأسطر)
    — للهاتف الدوليّ المكتوب بمجموعات «+218 91 3035690». يُفضَّل المصريّ/التونسيّ على الليبيّ."""
    if not s:
        return None
    text = normalize_digits(s)
    # (أ) المسافات/الأسطر حدود → مجاري كاملة الشكل فقط (تعزل الهاتف عن مبلغٍ مجاور)
    a = [m.group() for m in re.finditer(r"\d{8,14}", text.replace("-", ""))
         if _is_complete_phone(m.group())]
    if (pick := _pick_phone(a)) is not None:
        return pick
    # (ب) دمج المسافات **داخل السطر** (هاتف دوليّ بمجموعات) — السطر الجديد حدّ صارم (لا يدمج مبلغًا)
    b: list[str] = []
    for line in text.splitlines():
        b += [m.group() for m in re.finditer(r"\d{8,14}", re.sub(r"[\s\-+]", "", line))
              if classify_phone(m.group())]
    return _pick_phone(b)


def is_phone_like(s: str) -> bool:
    """السطر هاتف إن كان مجرى أرقام (≥8 خانات — يشمل التونسي المحلّي) بلا بنية كود+اسم."""
    return len(re.sub(r"\D", "", s)) >= 8


# ── قواعد الأرقام: المبلغ (§3.5) 🔴 ──────────────────────────────────────────
# «.» «،» «'» والفراغات = فاصل آلاف يُشال (5.000=5000، 17.400=17400، 5.802=5802).
# قاعدة الصحّة: كل مجموعة بعد الأولى يجب أن تكون 3 أرقام؛ غيرها «غير معتاد» → تصحيح + تحذير
# (مثال: 60.0000 → 60000، لا 600000). لا تصعيد — البوت يصحّح وينبّه.
_AMOUNT_TOKEN_RE = re.compile(r"-?\d[\d.،,'‏‎\s]*")   # المقطع الرقمي مع فواصله
_AMOUNT_SPLIT_RE = re.compile(r"[.،,'‏‎\s]+")          # فواصل الآلاف


def _parse_one_amount(token: str, raw: object) -> Optional[float]:
    """يحوّل مقطعًا رقميًّا واحدًا إلى قيمة بقاعدة فاصل الآلاف (§3.5): كل مجموعة بعد الأولى = 3 أرقام؛
    غيرها تُصحّح (>3→أول 3؛ <3→كما هي) مع log.warning، بلا تصعيد."""
    token = token.strip()
    neg = token.startswith("-")
    groups = [g for g in _AMOUNT_SPLIT_RE.split(token.lstrip("-")) if g]
    if not groups:
        return None
    if len(groups) == 1:
        digits = groups[0]
    else:
        norm = [groups[0]]
        for g in groups[1:]:
            if len(g) != 3:
                log.warning(
                    "مبلغ بصيغة غير معتادة %r: المجموعة «%s» ليست 3 أرقام (§3.5) — "
                    "تُصحّح إلى 3، بلا تصعيد.", raw, g,
                )
                g = g[:3] if len(g) > 3 else g  # >3 → أول 3 (60.0000→60000)؛ <3 → كما هي
            norm.append(g)
        digits = "".join(norm)
    try:
        val = float(digits)
        return -val if neg else val
    except (ValueError, TypeError) as exc:  # T5: لا silent catch
        log.error("تعذّر تحويل المبلغ %r: %s", raw, exc)
        return None


def parse_amount(raw: Optional[str]) -> Optional[float]:
    """المبلغ: الفواصل فواصل آلاف تُشال دائمًا (§3.5). يُرجع None إن تعذّر.

    🔴 مرشّحون (§3.5 §0): تُجمَع كل المقاطع الرقمية المحتملة. مرشّح واحد → يُؤخَذ مباشرة؛ أكثر من
    مرشّح → **مبلغ ملتبس** فيُسجَّل تحذير ويُؤخَذ **الأكبر** (الأرجح أنّه المبلغ لا كود/سعر)."""
    if raw is None:
        return None
    text = normalize_digits(str(raw))                        # bidi + أرقام عربية-هندية (§3.5)
    values = [v for tok in _AMOUNT_TOKEN_RE.findall(text)
              if (v := _parse_one_amount(tok, raw)) is not None]
    if not values:
        return None
    if len(values) > 1:
        log.warning("مبلغ ملتبس %r: مرشّحون %s — يُؤخَذ الأكبر (§3.5 §0).", raw, values)
        return max(values, key=abs)
    return values[0]


# ── التباس «ألف/آلاف» (§3.5) 🔴 ───────────────────────────────────────────────
# «32 ألف» كانت تُقرأ 32 (الكلمة تُهمَل) بدل 32000 — سبّبت عملية شبح بمبلغ خاطئ. القاعدة: عدد
# صحيح (بلا نقطة/فاصلة/رقم قبله) + «ألف/آلاف» ككلمة تامّة → يُضرَب ×1000. حدّ الكلمة (?![ء-ي])
# يمنع إشعال أسماء تبدأ بـ«الف» («الفرجاني/الفوني»)؛ «1.000 ألف»/«10.000»/«32,000» تمرّ (فاصل قبله).
_ALF_EXPAND_RE = re.compile(r"(?<![.,،٫٬\d])(\d+)\s*(ألف|الف|آلاف|الاف)(?![ء-ي])")


def expand_alf_amounts(raw: Optional[str]) -> tuple[str, list[dict]]:
    """يوسّع «عدد + ألف/آلاف» → عدد×1000 داخل النصّ (§3.5). يُرجع (النصّ المُوسَّع، سجلّ التوسّعات
    [{original, value}]؛ فارغ إن لا توسّع). يُطبَّق على نصّ بأرقام لاتينية (بعد normalize_digits)."""
    if not raw:
        return raw or "", []
    expansions: list[dict] = []

    def _repl(m: "re.Match[str]") -> str:
        value = int(m.group(1)) * 1000
        expansions.append({"original": m.group(0).strip(), "value": value})
        return str(value)

    return _ALF_EXPAND_RE.sub(_repl, raw), expansions


# ── قواعد الأرقام: السعر حسب العملة (§3.6) 🔴 ────────────────────────────────
def normalize_price(raw: Optional[str], currency: Optional[Currency]) -> tuple[Optional[str], Optional[str]]:
    """السعر: EGP كما هو؛ TND يُطبَّع إلى `0.xxxx` (35.75→0.3575، 33→0.33) (§3.6).

    يُرجع (price_raw, price_normalized)."""
    if raw is None:
        return None, None
    price_raw = str(raw).strip()
    # صيغة رقمية مبدئية: توحيد الفاصلة العربية والفراغات
    s = price_raw.replace("،", ".").replace("'", "").replace(" ", "")
    if currency == Currency.TND:
        if s.startswith("0."):
            norm = s
        else:
            digits = re.sub(r"[.,]", "", s)
            norm = "0." + digits if digits else None
    else:
        # EGP (وغير المحدّد): كما هو، مع توحيد الفاصلة العربية إلى نقطة
        norm = s.replace(",", ".") if "," in s else s
    return price_raw, norm
