"""
قاموس التصحيحات الحيّ — استبدالٌ نصّيّ على مستوى الرمز يسري **قبل** أيّ حلّ (§4.5).

المبدأ (طلب المالك): «الديناميّ يغلب الثابت دائمًا». الرمز الخاطئ يُستبدَل في النصّ الخام قبل أن
يبلغ الـaliases الثابتة أو الكاشف، فيضمن التقدّم عليهما مهما كان تسجيلهما. حيٌّ بلا إعادة تشغيل:
`db.corrections.all_active()` تُقرأ لكل رسالة كـtreasuries تمامًا.

نقيّة بلا DB — تُختبَر وحدها. الأنبوب يجلب القائمة ويمرّرها، ثمّ يزيد times_used لما أطلق فعليًّا.
"""
from __future__ import annotations

import re
from typing import Iterable

from .models import CorrectionRecord
from .parsing.normalize import normalize_ar

# فاصلٌ حدوديّ للرمز: مسافة/سطر/«/» أو التصاقٌ برقم («طه6.07»). الاستبدال على **رمزٍ كامل** لا
# سلسلةٍ جزئيّة — كي لا يكسر «فودا» كلمةً أطول تحويه («فودافون» يبقى)، وكي يلتقط الملتصق بالسعر.
_TOKEN_SPLIT = re.compile(r"([^\s/]+)")


def _norm_tokens(s: str) -> list[str]:
    """رموز النصّ مطبَّعةً (نفس تقسيم _TOKEN_SPLIT: مسافة/سطر/«/»)."""
    return [normalize_ar(m.group(1)) for m in _TOKEN_SPLIT.finditer(s or "")]


def is_non_idempotent_correction(wrong: str, correct: str) -> bool:
    """قاعدةٌ **غير مُتَّسِقة**: `wrong_text` تتابعُ رموزٍ **داخل** `correct_value` (بادئة/رمز)، فتطبيقُها
    فوق ناتجها يُضاعِف: «بلاس»→«بلاس فون» على «بلاس فون» ينتج «بلاس فون فون» (بلاغ SI5225/SI5227)،
    و«فودافون»→«فودافون بالخصم» على «فودافون بالخصم» ينتج تكرارًا. تُرفَض **تعلّمًا** (ai_auto)
    وتُتخطّى **تطبيقًا** (الحارس الموضعيّ في apply_corrections). القواعد التصحيحيّة الحقيقيّة
    (خطأٌ إملائيّ ≠ رمزٌ من الصواب، مثل «فدفون»→«فودافون بالخصم») تبقى سليمة."""
    w = _norm_tokens(wrong)
    cv = _norm_tokens(correct)
    if not w or not cv or w == cv or len(w) >= len(cv):
        return False
    return any(cv[i:i + len(w)] == w for i in range(len(cv) - len(w) + 1))


def build_correction_map(corrections: Iterable[CorrectionRecord]) -> dict[str, str]:
    """{الرمز الخاطئ المطبَّع: القيمة الصحيحة} — النشطة فقط، مع حارس التباس (§0).

    رمزٌ خاطئٌ واحد بقيمتين مختلفتين = التباسٌ يُسقَط كلاهما (لا تخمين). نادرٌ لأنّ المفتاح فريدٌ
    في DB، لكنّنا نحرسه هنا أيضًا كي تبقى الدالّة النقيّة آمنةً على أيّ مدخل."""
    seen: dict[str, str] = {}
    ambiguous: set[str] = set()
    for c in corrections:
        if not getattr(c, "active", True):
            continue
        key = normalize_ar(c.wrong_text)
        val = (c.correct_value or "").strip()
        if not key or not val:
            continue
        if key in seen and seen[key] != val:
            ambiguous.add(key)
        else:
            seen[key] = val
    for k in ambiguous:
        seen.pop(k, None)
    return seen


def apply_corrections(text: str, corrections: Iterable[CorrectionRecord]) -> tuple[str, list[str]]:
    """يستبدل الرموز الخاطئة في `text` بقيمها الصحيحة، رمزًا كاملًا لا سلسلةً جزئيّة.

    يُرجِع (النصّ المصحَّح، قائمة الرموز الخاطئة **الأصليّة** التي أُطلِقت) — الثانية لزيادة
    times_used. الملتصق بالسعر يُعالَج: «طه6.07» يُقسَم رمزًا «طه» + «6.07» فيُصحَّح «طه» وحده."""
    cmap = build_correction_map(corrections)
    if not text or not cmap:
        return text, []
    fired: list[str] = []
    tokens = list(_TOKEN_SPLIT.finditer(text))

    def _already_present(cv: str, tok_i: int) -> bool:
        """(حارس الاتّساق) هل `correct_value` **حاضرٌ سلفًا** بدءًا من هذا الرمز؟ أي هل الرموز
        التالية (بما فيها الحاليّ) تُهجّي `cv` كاملًا؟ إن نعم، فالتطبيق يُضاعِف — نتخطّاه
        (X: «بلاس فون»+قاعدة«بلاس→بلاس فون» = «بلاس فون فون»). cv أحاديّ الرمز لا يُضاعِف فلا يُحرَس."""
        cv_toks = _norm_tokens(cv)
        if len(cv_toks) <= 1:
            return False
        ahead = [normalize_ar(tokens[j].group(1)) for j in range(tok_i, min(tok_i + len(cv_toks), len(tokens)))]
        return ahead == cv_toks

    out: list[str] = []
    last = 0
    for i, mo in enumerate(tokens):
        tok = mo.group(1)
        start, end = mo.span(1)
        out.append(text[last:start])                          # الفاصل قبل الرمز
        last = end
        # التقاط بادئةٍ عربيّةٍ ملتصقةٍ برقم/سعر: «طه6.07» → جزّئ «طه» عن «6.07» وصحّح الأوّل.
        m = re.match(r"^([^\d]+?)(\d.*)$", tok)
        head, tail = (m.group(1), m.group(2)) if m else (tok, "")
        key = normalize_ar(head)
        if key in cmap:
            orig, cv, suffix = head, cmap[key], (" " + tail if tail else "")
        elif normalize_ar(tok) in cmap:
            orig, cv, suffix = tok, cmap[normalize_ar(tok)], ""
        else:
            out.append(tok)
            continue
        # (حارس الاتّساق) لا تُطبّق إن كان الصواب حاضرًا سلفًا عند الموضع → منع التضاعف.
        if _already_present(cv, i):
            out.append(tok)
            continue
        fired.append(orig)
        out.append(cv + suffix)
    out.append(text[last:])
    return "".join(out), fired
