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


def normalize_ar(s: Optional[str]) -> str:
    """يوحّد الهمزات/التاء المربوطة/الألف المقصورة ويزيل التشكيل — للمطابقة المتسامحة."""
    if not s:
        return ""
    s = _DIACRITICS.sub("", s)
    s = (
        s.replace("أ", "ا").replace("إ", "ا").replace("آ", "ا").replace("ٱ", "ا")
        .replace("ة", "ه").replace("ى", "ي")
        .replace("ؤ", "و").replace("ئ", "ي").replace("ء", "")
    )
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ── العملة (§3.4) ────────────────────────────────────────────────────────────
_EGP_TOKENS = {"ج", "جم", "جنيه", "مصري"}
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
    if any(k in compact for k in ("فودافون", "فودفون", "فدفون", "فدافون")):
        return "فودافون كاش"
    if "انستا" in compact:
        return "إنستا باي"
    return None


# ── رقم الهاتف (§3.4: رقم ملصوق بنص) ─────────────────────────────────────────
def extract_phone(s: Optional[str]) -> Optional[str]:
    """يستخرج رقم الهاتف من نص قد يكون ملصوقًا (`0916174679واتس`→`0916174679`)."""
    if not s:
        return None
    cleaned = s.replace(" ", "").replace("-", "")
    m = re.search(r"\d{9,}", cleaned)
    return m.group() if m else None


def is_phone_like(s: str) -> bool:
    """السطر هاتف إن كان مجرى أرقام طويلًا (≥9) بلا بنية كود+اسم."""
    return len(re.sub(r"\D", "", s)) >= 9


# ── قواعد الأرقام: المبلغ (§3.5) 🔴 ──────────────────────────────────────────
# «.» «،» «'» والفراغات = فاصل آلاف يُشال (5.000=5000، 17.400=17400، 5.802=5802).
# قاعدة الصحّة: كل مجموعة بعد الأولى يجب أن تكون 3 أرقام؛ غيرها «غير معتاد» → تصحيح + تحذير
# (مثال: 60.0000 → 60000، لا 600000). لا تصعيد — البوت يصحّح وينبّه.
_AMOUNT_TOKEN_RE = re.compile(r"-?\d[\d.،,'‏‎\s]*")   # المقطع الرقمي مع فواصله
_AMOUNT_SPLIT_RE = re.compile(r"[.،,'‏‎\s]+")          # فواصل الآلاف


def parse_amount(raw: Optional[str]) -> Optional[float]:
    """المبلغ: الفواصل فواصل آلاف تُشال دائمًا (§3.5). يُرجع None إن تعذّر.

    كل مجموعة بعد الأولى = 3 أرقام (فاصل آلاف). مجموعة ≠ 3 = صيغة غير معتادة →
    تُصحّح (>3: تُقصّ لأول 3؛ <3: تبقى) مع log.warning، بلا تصعيد.
    """
    if raw is None:
        return None
    m = _AMOUNT_TOKEN_RE.search(str(raw))
    if not m:
        return None
    token = m.group().strip()
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
