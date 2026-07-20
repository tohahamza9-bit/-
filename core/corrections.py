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
    # عكسُ الخريطة: من المطبَّع إلى الرمز الأصليّ (لأوّل تصحيح بذلك المفتاح) — لتسجيل times_used
    # بالنصّ كما خزّنه المالك. غير حرج (best-effort) فالمفتاح المطبَّع كافٍ للمطابقة في bump_usage.

    def _sub_token(tok: str) -> str:
        # التقاط بادئةٍ عربيّةٍ ملتصقةٍ برقم/سعر: «طه6.07» → جزّئ «طه» عن «6.07» وصحّح الأوّل.
        m = re.match(r"^([^\d]+?)(\d.*)$", tok)
        head, tail = (m.group(1), m.group(2)) if m else (tok, "")
        key = normalize_ar(head)
        if key in cmap:
            fired.append(head)
            return cmap[key] + (" " + tail if tail else "")
        # الرمز كاملًا (بلا التصاق رقميّ) — طابِق المطبَّع
        key_full = normalize_ar(tok)
        if key_full in cmap:
            fired.append(tok)
            return cmap[key_full]
        return tok

    out = _TOKEN_SPLIT.sub(lambda mo: _sub_token(mo.group(1)), text)
    return out, fired
