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

# علامات bidi البصرية (RLM/LRM/التضمين) — واتساب RTL يحقنها حول الأرقام فتقلب ترتيبها البصريّ.
_BIDI_MARKS = re.compile("[‎‏‪-‮]")


def repair_bidi_digits(s: Optional[str]) -> str:
    """يزيل علامات bidi (RLM ‏ / LRM ‎ / \\u202a-\\u202e) قبل أي معالجة — تمنع انعكاس/تشويه الأرقام
    في رسائل واتساب RTL (§3.5). يُطبَّق ضمن normalize_digits فيغطّي كل مسارات الاستخراج."""
    return _BIDI_MARKS.sub("", s) if s else (s or "")


def normalize_digits(s: Optional[str]) -> str:
    """ينظّف علامات bidi ثم يحوّل الأرقام العربية-الهندية/الفارسية إلى لاتينية (٠١٠→010) — يُطبَّق
    قبل استخراج الهاتف/المبلغ/الكود إذ الأنماط الرقمية تطابق `\\d` اللاتينية فقط (§3.5)."""
    if not s:
        return s or ""
    return repair_bidi_digits(s).translate(_ARABIC_INDIC_DIGITS)


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


# ── رقم الهاتف (§3.4: رقم ملصوق بنص) ─────────────────────────────────────────
def classify_phone(raw: Optional[str]) -> Optional[str]:
    """يصنّف مجرى أرقام ويُرجع هاتف المستلم أو None (§3.4):

    - **ليبي** (218 / 00218 / +218) → None (يُرفَض دائمًا — ليس مستلمًا مصريًّا/تونسيًّا).
    - **تونسي دوليّ** (216 / 00216 / +216) → يُقشَّر إلى المحلّي (8 خانات).
    - **مصري** 11 خانة يبدأ 01x، أو **تونسي محلّي** 8 خانات يبدأ 2/5/9 → يُقبَل.
    - غير ذلك: مجرى ≥9 خانات يُقبَل (توافق مع الصيغ المصرية/الدولية القائمة).
    """
    if not raw:
        return None
    d = re.sub(r"\D", "", normalize_digits(raw))
    if d.startswith("00"):            # بادئة الاتصال الدوليّ
        d = d[2:]
    if d.startswith("218"):          # ليبي → يُرفَض (§3.4)
        return None
    if d.startswith("216"):          # تونسي دوليّ → المحلّي
        d = d[3:]
    elif d.startswith("20") and len(d) == 12:   # مصري دوليّ (+20) → المحلّي (0…)
        d = "0" + d[2:]
    if not d:
        return None
    if len(d) == 8 and d[0] in "259":   # تونسي محلّي (2/5/9)
        return d
    if len(d) >= 9:                     # مصري 11 وغيره (توافق)
        return d
    return None


def extract_phone(s: Optional[str]) -> Optional[str]:
    """يستخرج هاتف المستلم (`0916174679واتس`→`0916174679`)؛ يرفض الليبي ويقبل التونسي 8-خانات.

    مرحلتان لعزل الهاتف عن مبلغ ملاصق (#2): (أ) بلا دمج المسافات — «01… 5000» يأخذ الهاتف وحده
    (المسافة تكسر المجرى)؛ (ب) إن فشل، بدمج المسافات — للهاتف الدوليّ المكتوب مجموعات «+20 100 745 3278»."""
    if not s:
        return None
    text = normalize_digits(s)                       # bidi + أرقام عربية-هندية (§3.5)
    m = re.search(r"\d{8,13}", text.replace("-", ""))    # (أ) المسافات حدود → تعزل الهاتف عن المبلغ
    if m:
        ph = classify_phone(m.group())
        # نقبل هنا الهاتف **النظيف** فقط (تونسي 8 / مصري 11) كي لا يُلتقط جزءُ رقمٍ مفصولٍ بمسافة
        # (ليبي «+218 91-…» جزؤه 9 خانات) — الغامض يُترَك للمرحلة (ب) بالدمج فتظهر بادئته.
        if ph and len(ph) in (8, 11):
            return ph
    m2 = re.search(r"\d{8,13}", re.sub(r"[\s-]", "", text))   # (ب) دمج المسافات (دوليّ/بمجموعات)
    return classify_phone(m2.group()) if m2 else None


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
