"""
حلّ الخزينة/المورد بالاسم الجزئي (§4.5) مع تسامح إملائي عربي + مطابقة تقريبية (fuzzy).

المراحل بالترتيب (resolve_treasury / resolve_supplier):
  ١. تطبيع عربي للمطابقة (normalize_arabic_for_matching): تشكيل/همزات/ة-ى + إزالة «ال» + مسافات.
  ٢. مطابقة تامّة (exact) بعد التطبيع — تبقى الأدقّ (تشمل fallback المدن للخزينة).
  ٣. مطابقة البداية (prefix): النص يبدأ بالاسم أو العكس.
  ٤. مطابقة الكلمات (subsequence): كل كلمات الاسم موجودة في النص.
  ٥. مطابقة تقريبية (fuzzy) بـ rapidfuzz WRatio، عتبة 80، تتخطّى ما دون 3 أحرف.
  ٦. فشل كلّ شيء → None (تسجيل الكلمة المجهولة يتمّ في طبقة الأنبوب حيث تتوفّر DB — §db.unknown_terms).

🔴 أمان ماليّ (§0): المطابقة الفضفاضة (3-5) تُجرّب **فقط بعد فشل التامّة تمامًا**، وأيّ **التباس**
   (أكثر من سجلّ مطابق) → None (لا تخمين) — كي لا تُدخَل الحوالة في خزينة خاطئة.
"""
from __future__ import annotations

import re
from typing import Optional

from core.logging_setup import get_logger
from core.models import SupplierRecord, TreasuryRecord

from .normalize import normalize_ar

log = get_logger(__name__)

try:
    from rapidfuzz import fuzz
except ImportError:  # المطابقة التقريبية اختيارية — المراحل 1-4 تعمل بدونها (T5: نسجّل، لا نبتلع)
    fuzz = None
    log.warning("rapidfuzz غير مثبّت — المطابقة التقريبية (fuzzy §5) معطّلة؛ المراحل 1-4 تعمل.")

_FUZZY_THRESHOLD = 80        # عتبة التطابق التقريبي (§5)
_MATCH_MIN_LEN = 3          # لا مطابقة بادئة/تقريبية على نصوص أقصر من 3 أحرف
# «ال» التعريف في بداية كلمة (يتبعها حرفان على الأقلّ) — تُزال للمطابقة («العاصمه»→«عاصمه»)
_AL_PREFIX = re.compile(r"^ال(?=..)")

# 🔴 كلمات تنظيميّة عامّة (شركة/مكتب/…) — تُجرَّد **في مرحلة fuzzy وحدها** كي لا تضخّم كلمةٌ
#   عامّة مشتركة درجةَ التطابق فتُخلَط هويّتان مختلفتان: زبون «شركة القن» ⇔ مورّد «شركة النور»
#   (WRatio=80 من «شركه» المشتركة وحدها). الأشكال هنا **بعد** normalize_ar (ة→ه، ؤ→و). المطابقة
#   التامّة/البادئة/الكلمات (2-4) لا تتأثّر — الخلط الوحيد كان في fuzzy (§0 لا تخمين على العامّ).
_GENERIC_ORG = {"شركه", "مكتب", "محل", "موسسه"}


def _strip_generic(qn: str) -> str:
    """يُزيل الكلمات التنظيميّة العامّة (شركة/مكتب/…) من نصّ مطبَّع — للمقارنة التقريبيّة على
    الجزء المميِّز وحده. يُرجِع الباقي (قد يكون فارغًا لو كان النصّ كلمةً عامّةً فقط)."""
    return " ".join(t for t in qn.split() if t not in _GENERIC_ORG)


def normalize_arabic_for_matching(s: Optional[str]) -> str:
    """تطبيع عربيّ للمطابقة التقريبية (§1): يبني على normalize_ar (تشكيل/همزات/ة→ه/ى→ي/مسافات)
    ويزيل «ال» التعريف من بداية كل كلمة، ثم يوحّد المسافات."""
    base = normalize_ar(s)
    if not base:
        return ""
    words = [_AL_PREFIX.sub("", w) for w in base.split()]
    return " ".join(w for w in words if w)


def _candidates(name: str, aliases: list[str]) -> list[str]:
    """كل الأشكال المطبَّعة (normalize_ar) للمطابقة التامّة: الاسم + الإملاءات البديلة."""
    cands = [normalize_ar(name)]
    cands.extend(normalize_ar(a) for a in aliases)
    return [c for c in cands if c]


def _match_forms(rec: TreasuryRecord | SupplierRecord) -> list[str]:
    """أشكال الاسم + aliases مطبَّعة لمطابقة (normalize_arabic_for_matching) — للمراحل 3-5."""
    forms = [normalize_arabic_for_matching(rec.name)]
    forms.extend(normalize_arabic_for_matching(a) for a in getattr(rec, "aliases", []))
    return [f for f in forms if f]


def _distinct(records: list) -> list:
    """سجلّات مميّزة بالاسم — aliases متعدّدة لنفس السجلّ لا تُعدّ التباسًا."""
    seen: set[str] = set()
    out: list = []
    for r in records:
        if r.name not in seen:
            seen.add(r.name)
            out.append(r)
    return out


def _loose_match(token: Optional[str], records: list):
    """المراحل 3-5 (بادئة → كلمات → تقريبيّ) على النص المطبَّع للمطابقة. تُرجع سجلًّا وحيدًا، أو
    None عند غياب المطابقة **أو التباسها** (أكثر من سجلّ — §0 لا تخمين)."""
    qn = normalize_arabic_for_matching(token)
    if not qn:
        return None
    qtok = set(qn.split())

    # (٣) البادئة: النص يبدأ بالاسم أو الاسم يبدأ بالنص (بحدّ أدنى للطول)
    if len(qn) >= _MATCH_MIN_LEN:
        def _prefix(cn: str) -> bool:
            return len(cn) >= _MATCH_MIN_LEN and (qn.startswith(cn) or cn.startswith(qn))
        hits = _distinct([r for r in records if any(_prefix(c) for c in _match_forms(r))])
        if hits:
            return hits[0] if len(hits) == 1 else _ambiguous(token, hits)

    # (٤) الكلمات: كل كلمات الاسم موجودة في النص (subsequence)
    def _subseq(cn: str) -> bool:
        ctok = set(cn.split())
        return bool(ctok) and ctok <= qtok
    hits = _distinct([r for r in records if any(_subseq(c) for c in _match_forms(r))])
    if hits:
        return hits[0] if len(hits) == 1 else _ambiguous(token, hits)

    # (٥) 🔴 **مرحلة fuzzy مُلغاة** (قرار المالك 2026-07-19): درجةُ تشابهٍ (WRatio) على اسمٍ
    #     = تخمينٌ في الهويّة، وقد أنزلت حوالات على كيانات خاطئة. الخزائن كانت ملغاةً أصلًا
    #     (مطابقة تامّة فقط)؛ الموردون يلحقون بها الآن. البدائل بلا تخمين: alias يدويّ من
    #     اللوحة، أو طبقة القروب (room_match)، وإلّا **تصعيد** بالرسالة الإلزامية.
    return None


def _ambiguous(token: Optional[str], hits: list):
    """التباس مطابقة فضفاضة (§0): يُسجَّل تحذير ويُرجَع None (لا تخمين)."""
    log.warning(
        "مطابقة فضفاضة ملتبسة للنص %r: %s — لا تخمين (§0)، تُترك للمراجعة.",
        token, [r.name for r in hits],
    )
    return None


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
    """يحلّ الخزينة (§4.5): مطابقة تامّة أولًا (الأدقّ)، ثم تقريبية (بادئة/كلمات/fuzzy §80) عند فشلها.

    🔴 أمان ماليّ (§0): التقريبية تُجرّب **فقط بعد فشل التامّة تمامًا**، وأيّ التباس → None (لا تخمين)
       كي لا تُدخَل الحوالة في خزينة خاطئة. التنويعات الشائعة تبقى مُغطّاة بـ aliases دقيقة أيضًا.

    fallback المدن (الرسالة التونسية «وليد العاصمة»): إن فشلت المطابقة التامّة تمامًا، تُجرَّد لاحقة
    المدينة المعروفة من نهاية النص وتُعاد المطابقة **تامّةً** على الباقي — قبل اللجوء للتقريبية.
    """
    q = normalize_ar(token)
    if not q:
        return None
    # (١-٢) مطابقة تامّة + fallback المدن (الأدقّ، السلوك القائم)
    matches = _find_exact(q, treasuries)
    if not matches:                          # فشل تامّ فقط → جرّب بعد تجريد لاحقة المدينة
        stripped = _strip_trailing_cities(q)
        if stripped and stripped != q:
            matches = _find_exact(stripped, treasuries)
    if len(matches) == 1:
        return next(iter(matches.values()))
    if len(matches) > 1:
        log.warning(
            "خزينة ملتبسة (مطابقة تامّة) للنص %r: %s — لا تخمين (§0)، تُترك للمراجعة.",
            token, list(matches.keys()),
        )
    # 🔴 الخزائن: **مطابقة تامّة فقط** (لا fuzzy) — الفضفاضة تُدخِل الحوالة في خزينة خاطئة
    #    (خطر ماليّ مُثبَت: «محمد»→«محمد حمامات»). التنويعات تُغطّى بـ aliases + التقاط المجهول
    #    (§db.unknown_terms) وإسناده يدويًّا من اللوحة — لا تخمين تلقائيّ (§0).
    return None


def resolve_supplier(token: Optional[str], suppliers: list[SupplierRecord]) -> Optional[SupplierRecord]:
    """يحلّ اسمًا إلى مورد من القائمة البيضاء (§5.4): مطابقة تامّة أولًا، ثم تقريبية (بادئة/كلمات/fuzzy)
    عند فشلها — مع حارس التباس (§0) كي لا يُخلَط مورد بآخر."""
    q = normalize_ar(token)
    if not q:
        return None
    # (٢) مطابقة تامّة
    for s in suppliers:
        if any(q == c for c in _candidates(s.name, s.aliases)):
            return s
    # (٣-٥) مطابقة تقريبية
    return _loose_match(token, suppliers)


def resolve_bold(token: Optional[str], records: list, threshold: int = 70):
    """(الحل الجريء §5، قرار المالك) أفضل مرشّح تقريبيّ للاسم بعد **فشل الحل الصارم** — عتبة منخفضة
    (WRatio ≥ threshold، افتراضي ٧٠) على **الجزء المميِّز** وحده (بعد التطبيع وتجريد العامّ شركة/مكتب/…).
    يُرجِع (record, score) لأعلى مرشّح فوق العتبة، أو (None, 0).

    🔴 الخطّان الأحمران (حماية القن، بلا كلفة):
      • تُستدعى على **قوائم الخزائن/الموردين فقط** — أسماء العملاء ليست مرشّحات إطلاقًا (المُستدعي يمرّر
        القائمة الصحيحة).
      • **الكلمة العامّة وحدها لا تصنع مطابقة**: إن كان الجزء المميِّز بعد التجريد < ٣ أحرف → (None, 0)."""
    if fuzz is None:
        return None, 0
    qn = normalize_arabic_for_matching(token)
    if not qn:
        return None, 0
    q_core = _strip_generic(qn)
    if len(q_core) < _MATCH_MIN_LEN:          # عامّ وحده / أقصر من ٣ → لا تخمين (§0 القن)
        return None, 0
    best_rec, best_score = None, 0.0
    for r in records:
        for c in _match_forms(r):
            c_core = _strip_generic(c)
            if len(c_core) >= _MATCH_MIN_LEN:
                s = fuzz.WRatio(q_core, c_core)
                if s > best_score:
                    best_rec, best_score = r, s
    if best_rec is not None and best_score >= threshold:
        return best_rec, int(best_score)
    return None, 0
