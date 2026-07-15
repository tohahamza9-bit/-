"""
نظام الكيانات الموحّد — تحليل نقيّ (docs/FX_RATES_SPEC.md §4).
يربط نصًّا (خطأ إملائيّ/اختصار/حيّ) باسم معياريّ لغرض التسعير الجغرافيّ/القنويّ.

**مستقلّ تمامًا** عن إملاءات الخزائن/الموردين القائمة (لا يستبدلها ولا يلمسها) وعن
core.parsing (تطبيع محليّ خفيف هنا). دوالّ نقيّة تعمل على قائمة EntityAlias (من
db.entity_aliases.all_active) — نمط resolve_treasury القائم. **لا ربط بالـpipeline**.

قاعدة الغموض (§4): نصٌّ يطابق أكثر من canonical_name ⇒ None (لا تخمين). ردّ فعل التسعير
(أقل سعر + 🔴 عند confidence=low) يأتي في sanity_gate (الخطوة ٤)، لا هنا.
"""
from __future__ import annotations

from typing import Optional

from .models import EntityAlias

_CONFIDENCE_RANK = {"high": 3, "medium": 2, "low": 1}


def _norm(s: str) -> str:
    """تطبيع خفيف محليّ (لا يمسّ core.parsing.normalize): تشذيب + توحيد المسافات + تصغير لاتينيّ."""
    return " ".join((s or "").split()).strip().lower()


def resolve_entity(
    term: str, aliases: list[EntityAlias], entity_type=None
) -> Optional[EntityAlias]:
    """يُحلّ نصًّا إلى كيان معياريّ. يُرجِع EntityAlias المطابق (بأعلى ثقة عند التكرار)، أو None.

    None في حالتين (§4):
      - لا تطابق إطلاقًا.
      - غموض: النصّ يطابق أكثر من canonical_name مختلف ⇒ **لا تخمين**.
    entity_type (اختياريّ): يحصر البحث بنوع (city/district/…)؛ قيمته EntityType أو نصّها.
    """
    t = _norm(term)
    if not t:
        return None
    et = getattr(entity_type, "value", entity_type)
    matches = [
        a for a in aliases
        if a.active and _norm(a.alias) == t
        and (et is None or getattr(a.entity_type, "value", a.entity_type) == et)
    ]
    if not matches:
        return None
    canonicals = {_norm(a.canonical_name) for a in matches}
    if len(canonicals) > 1:
        return None                              # غموض عبر canonical_names مختلفة → لا تخمين (§4)
    return max(matches, key=lambda a: _CONFIDENCE_RANK.get(a.confidence, 0))


def canonical_for(
    term: str, aliases: list[EntityAlias], entity_type=None
) -> Optional[str]:
    """غلاف مريح: الاسم المعياريّ لنصّ (أو None عند عدم التطابق/الغموض)."""
    match = resolve_entity(term, aliases, entity_type)
    return match.canonical_name if match else None
