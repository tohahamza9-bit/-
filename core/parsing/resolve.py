"""
حلّ الخزينة/المورد بالاسم الجزئي (§4.5) مع تسامح إملائي عربي.
- الخزينة: الاسم الجزئي يكفي («بلاس»=بلاس فون، «وليد»=وليد تونس العاصمة) — §4.5.
- المورد: قائمة بيضاء صارمة (§5.4) — مطابقة دقيقة أو كلمة واحدة كاملة (لا substring فضفاض).
"""
from __future__ import annotations

from typing import Optional

from core.logging_setup import get_logger
from core.models import SupplierRecord, TreasuryRecord

from .normalize import normalize_ar

log = get_logger(__name__)


def _candidates(name: str, aliases: list[str]) -> list[str]:
    """كل الأشكال المطبَّعة للمطابقة: الاسم + الإملاءات البديلة."""
    cands = [normalize_ar(name)]
    cands.extend(normalize_ar(a) for a in aliases)
    return [c for c in cands if c]


# مدن تونسية معروفة (مطبَّعة) — تُجرَّد من **نهاية** النص كـfallback فقط حين تفشل المطابقة
# التامّة، فيصير «وليد العاصمة»→«وليد» فيطابق aliasه. لا subset فضفاض: التجريد يقتصر على
# هذه الكلمات المعروفة وفي النهاية فقط، والمطابقة بعده تبقى تامّة.
_TN_CITIES = {"العاصمه", "سوسه", "صفاقس", "جربه", "نابل", "حمامات", "بنقردان"}


def _find_exact(q: str, treasuries: list[TreasuryRecord]) -> dict[str, TreasuryRecord]:
    """مطابقة تامّة: q (مطبَّع) يساوي بالضبط اسم خزينة أو أحد aliasها."""
    matches: dict[str, TreasuryRecord] = {}
    for t in treasuries:
        if any(q == c for c in _candidates(t.name, t.aliases)):
            matches[t.name] = t
    return matches


def _strip_trailing_cities(q: str) -> str:
    """يُجرّد كلمات المدن المعروفة من نهاية النص (فقط) — «وليد العاصمه»→«وليد»."""
    toks = q.split()
    while toks and toks[-1] in _TN_CITIES:
        toks.pop()
    return " ".join(toks)


def resolve_treasury(token: Optional[str], treasuries: list[TreasuryRecord]) -> Optional[TreasuryRecord]:
    """يحلّ الخزينة بمطابقة **تامّة فقط** (§0 — لا تخمين): السطر (بعد التطبيع) يساوي بالضبط
    اسم خزينة أو أحد aliasها.

    🔴 أُلغيت المطابقة الفضفاضة (كلمة-داخل-اسم / substring / subset) — كانت تُطابِق اسم
    الزبون أو المدينة بخزينة خطأً فتُدخِل الحوالة في خزينة خاطئة (خطر مالي). التنويعات
    والأخطاء الإملائية تُغطّى حصريًا عبر aliases دقيقة. التباس (تطابُق خزينتين مختلفتين) →
    None + تحذير (تُترك لبوابة الثقة → تعليق/تصعيد، لا تخمين).

    fallback المدن (الرسالة التونسية «وليد العاصمة»): إن فشلت المطابقة التامّة تمامًا،
    تُجرَّد لاحقة المدينة المعروفة من نهاية النص وتُعاد المطابقة **تامّةً** على الباقي.
    يُطبَّق فقط عند غياب أي تطابق (لا يمسّ التباسًا قائمًا) فيبقى «تونس العاصمة»→«تونس»→None.
    """
    q = normalize_ar(token)
    if not q:
        return None
    matches = _find_exact(q, treasuries)
    if not matches:                          # فشل تامّ فقط → جرّب بعد تجريد لاحقة المدينة
        stripped = _strip_trailing_cities(q)
        if stripped and stripped != q:
            matches = _find_exact(stripped, treasuries)
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        log.warning(
            "خزينة ملتبسة للنص %r: %s — لا تخمين (§0)، تُترك للمراجعة.",
            token, list(matches.keys()),
        )
    return None


def resolve_supplier(token: Optional[str], suppliers: list[SupplierRecord]) -> Optional[SupplierRecord]:
    """يحلّ اسمًا إلى مورد من القائمة البيضاء (§5.4) — مطابقة صارمة لتفادي الخلط بالزبائن."""
    q = normalize_ar(token)
    if not q:
        return None
    qtok = set(q.split())
    for s in suppliers:
        for c in _candidates(s.name, s.aliases):
            ctok = c.split()
            if q == c:
                return s
            if len(ctok) == 1 and ctok[0] in qtok:   # اسم مورد كلمة واحدة يظهر كاملًا
                return s
            if len(ctok) > 1 and set(ctok) <= qtok:   # كل كلمات المورد موجودة
                return s
    return None
