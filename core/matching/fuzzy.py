"""
مطابقة عربية متسامحة — دوال نقيّة بلا حالة (§8.2, §3.4).

المبدأ (§1.1): الكود هو مرساة الهوية؛ الاسم قد يخطئ إملاؤه. لذا تطابق الأسماء
«متساهل»: يتسامح مع خطأ إملائي («الشاوش»/«الشتوش»، «أحمد»/«أحمر») واختصار
(«بكر»/«بوبكر») — لكنه يرفض اسمًا مختلفًا تمامًا (§8.2 آخر بند).

بلا اعتماد على مكتبات خارجية (لا rapidfuzz) — مسافة تحرير Levenshtein نقيّة.
"""
from __future__ import annotations

import re

# ── عتبات المطابقة (مضبوطة على أمثلة §8.2) ──────────────────────────────────
# نسبة تشابه الكلمة الواحدة (1 − مسافة/الأطول): «بكر»/«بوبكر» = 0.6 بالضبط.
_TOKEN_RATIO_THRESHOLD = 0.6
# نسبة الكلمات المتطابقة داخل الاسم متعدّد الكلمات.
_TOKEN_FRACTION_THRESHOLD = 0.6
# تشابه السلسلة كاملة (بمسافات وبلا مسافات) — احتياط للتقطيع المختلف.
_WHOLE_RATIO_THRESHOLD = 0.8


# ── التطبيع العربي (§3.4) ────────────────────────────────────────────────────
# التشكيل والحركات (تُشال): ً-ٟ + التطويل ـ + الألف الخنجرية ٰ
_TASHKEEL_RE = re.compile(r"[ً-ٰٟـ]")
_WS_RE = re.compile(r"\s+")

# توحيد الحروف المتقاربة
_UNIFY = {
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",   # الألف بأشكالها → ا
    "ة": "ه",                                     # التاء المربوطة → ه
    "ى": "ي",                                     # الألف المقصورة → ي
    "ؤ": "و",                                     # الهمزة على الواو → و
    "ئ": "ي",                                     # الهمزة على الياء → ي
    "ء": "",                                       # الهمزة المفردة تُشال
}
_UNIFY_TABLE = str.maketrans(_UNIFY)

# تحويل الأرقام العربية-الهندية → لاتينية (لاستخراج الهاتف/المبلغ من نص الغرف)
_DIGITS_TABLE = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def normalize_ar(s: str) -> str:
    """
    تطبيع عربي متسامح: إزالة التشكيل والتطويل، توحيد الألف/الهمزة/التاء المربوطة/الياء،
    وضغط المسافات. يُرجع سلسلة صغيرة نظيفة صالحة للمقارنة.
    """
    if not s:
        return ""
    s = _TASHKEEL_RE.sub("", s)
    s = s.translate(_UNIFY_TABLE)
    s = _WS_RE.sub(" ", s).strip()
    return s


def normalize_digits(s: str) -> str:
    """يحوّل الأرقام العربية-الهندية إلى لاتينية (§3.4 — أرقام الهواتف/المبالغ)."""
    return (s or "").translate(_DIGITS_TABLE)


# ── مسافة التحرير (Levenshtein) — نقيّة ─────────────────────────────────────
def _levenshtein(a: str, b: str) -> int:
    """أقل عدد إدراج/حذف/استبدال يحوّل a إلى b (برمجة ديناميكية بصف واحد)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost))
        prev = cur
    return prev[-1]


def _ratio(a: str, b: str) -> float:
    """نسبة تشابه في [0,1]: 1 − مسافة/طول الأطول."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    longest = max(len(a), len(b))
    return 1.0 - _levenshtein(a, b) / longest


def token_ratio(a: str, b: str) -> float:
    """تشابه كلمتين مطبّعتين — يُستخدم لمطابقة كلمات الاسم فردًا فردًا."""
    return _ratio(normalize_ar(a), normalize_ar(b))


def _best_token_fraction(small: list[str], large: list[str]) -> float:
    """نسبة كلمات القائمة الأقصر التي وجدت شريكًا مقبولًا في الأطول (تطابق أُحادي)."""
    if not small:
        return 0.0
    used = [False] * len(large)
    matched = 0
    for t in small:
        best_j, best_r = -1, 0.0
        for j, u in enumerate(large):
            if used[j]:
                continue
            r = _ratio(t, u)
            if r > best_r:
                best_r, best_j = r, j
        if best_j >= 0 and best_r >= _TOKEN_RATIO_THRESHOLD:
            used[best_j] = True
            matched += 1
    return matched / len(small)


def names_match(a: str, b: str) -> bool:
    """
    تطابق تقريبي بين اسمين (§8.2). يتسامح مع أخطاء الإملاء والاختصار،
    ويرفض اسمًا مختلفًا تمامًا (ليس خطأ إملاء).

    أمثلة مطابقة: «الشاوش»/«الشتوش»، «أحمد»/«أحمر»، «بكر الهمالي»/«بوبكر الهمالي».
    أمثلة رفض: «مروان الشاوش»/«خالد العماري».
    """
    na, nb = normalize_ar(a), normalize_ar(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    # 1) تطابق على مستوى الكلمات (يلتقط الاختصار «بكر»/«بوبكر»)
    ta, tb = na.split(), nb.split()
    small, large = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if _best_token_fraction(small, large) >= _TOKEN_FRACTION_THRESHOLD:
        return True

    # 2) تشابه السلسلة كاملة (بمسافات وبلا مسافات — احتياط لاختلاف التقطيع)
    if _ratio(na, nb) >= _WHOLE_RATIO_THRESHOLD:
        return True
    if _ratio(na.replace(" ", ""), nb.replace(" ", "")) >= _WHOLE_RATIO_THRESHOLD:
        return True

    return False
