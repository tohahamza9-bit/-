"""كاشف الغموض المبكر (§1) — بلا API، ميلي ثوانٍ.

المعمار (قرار المالك 2026-07-20): سؤال واحد قبل المسار الحتميّ: «هل هذه الرسالة نظيفة 100%؟»
غامضة ⇒ تذهب للذكاء بالرسالتين معًا فورًا، بدل أن تُجرَّب القواعد فتفشل ثم تُصعَّد.

🔴 لماذا الكاشف **بعد** التفكيك السريع لا قبله (قياس على 2418 رسالة حقيقية، 2026-07-19):
   القياس على النصّ الخام وحده صنّف **97.7%** من رسائل يوم كامل «غامضة»، وشدُّ القواعد
   إلى نيّتها رفعها إلى 99.4% — أي تعطيلٌ عمليّ للمسار الحتميّ. السبب بنيويّ لا معايرة:
     • «حرف ملتصق برقم» أطلق على 92.5% لأن الالتصاق هو **الشكل الطبيعيّ** للصيغة
       (كود + اسم + سعر): «6150ج.م» و«اساور33.25» سليمتان، و«6.02يبيرع» غامضة،
       ولا شيء في النصّ الخام يفصل بينها.
     • «اسم غير موجود في القوائم» أطلق على 77.4% لأن القوائم 14 خزينة و8 موردين،
       وأسماء الزبائن ليست فيها ولا يُفترض أن تكون.
   الجذر: قبل التفكيك لا يُعرَف أيُّ رمزٍ يُفترض أن يكون خزينةً وأيُّه اسم زبون — تلك وظيفة
   المفكِّك. لذا: التفكيك الحتميّ أوّلًا (مجّانيّ، ميلي ثوانٍ)، وإشاراته البنيويّة هي أدقّ
   تعريف للشكّ (17.7% فقط ⇒ ~50-60 نداء/يوم بضمّ الرسالتين).

يبقى من قواعد «ما قبل» اثنتان فقط — قياسًا، لا تُطلقان كاذبًا:
   (١) كلمة عملة غير معياريّة (جنى/فودفوان) — 2.1%.
   (٢) تصادم اسم المستلم مع خزينة/مورد مسجَّل — 0% تاريخيًّا، لكنه أسوأ الأخطاء إن وقع
       (حوالة تنزل على كيان بدل زبون)، فتُدفع للذكاء وقايةً.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from core.models import ParseResult, SupplierRecord, TreasuryRecord
from core.parsing.normalize import detect_currency, normalize_ar
from core.parsing.resolve import resolve_supplier, resolve_treasury

# جذور تدلّ على نيّة ذكر عملة بصيغة غير معياريّة (detect_currency يفشل عليها).
# مغلقة عمدًا: كل جذر هنا رآه القياس فعلًا أو هو تحريف مباشر لرمز معياريّ.
_CURRENCY_HINT = re.compile(r"(جن[ىيا](?!ه)|فودفو|فودا|دينر|دنا(?!نير)|تونث|مصرى ?ي)")

# أدنى طول لاسم مرشّح قبل فحص التصادم — أقصر من ذلك ضجيج.
_MIN_NAME_LEN = 3


@dataclass
class AmbiguityVerdict:
    """حُكم الكاشف. `reasons` تُسجَّل حرفيًّا في deviation_log (§6)."""

    ambiguous: bool = False
    reasons: list[str] = field(default_factory=list)
    stage: str = ""          # "pre" | "post" | "" (نظيفة)

    def as_note(self) -> str:
        return "؛ ".join(self.reasons) if self.reasons else "نظيفة"


# ═════════════════════════════════════════════════════════════════════════════
# (١) قواعد ما قبل التفكيك — نصّيّة، لا تعتمد على نتيجة المفكِّك
# ═════════════════════════════════════════════════════════════════════════════
def detect_pre(text: str, treasuries: list[TreasuryRecord],
               suppliers: list[SupplierRecord],
               leg=None) -> list[str]:
    """القاعدتان الباقيتان من §1. تُرجِع أسباب الغموض (فارغة = نظيفة)."""
    reasons: list[str] = []
    raw = text or ""

    # (١) كلمة عملة غير معياريّة: النصّ ينوي عملةً والمفكِّك لا يراها.
    if detect_currency(raw) is None and _CURRENCY_HINT.search(normalize_ar(raw) or ""):
        reasons.append("كلمة عملة غير معياريّة")

    # (٢) تصادم: اسم المستلم يطابق خزينةً أو موردًا مسجَّلًا حرفيًّا.
    #     أسوأ الأخطاء الممكنة — حوالةٌ تنزل على كيانٍ بدل زبون. الفحص على الاسم
    #     المستخرَج (لا على كل كلمة) كي لا يُطلق كاذبًا.
    name = getattr(leg, "customer_name", None) if leg is not None else None
    if name and len(name.strip()) >= _MIN_NAME_LEN:
        if resolve_treasury(name, treasuries) is not None:
            reasons.append(f"اسم المستلم «{name}» يطابق خزينةً مسجَّلة")
        elif resolve_supplier(name, suppliers) is not None:
            reasons.append(f"اسم المستلم «{name}» يطابق موردًا مسجَّلًا")

    return reasons


# ═════════════════════════════════════════════════════════════════════════════
# (٢) إشارات ما بعد التفكيك — المفكِّك نفسه يعرف متى فشل
# ═════════════════════════════════════════════════════════════════════════════
def detect_post(result: Optional[ParseResult], *, min_confidence: float = 0.7) -> list[str]:
    """إشارات الغموض البنيويّة من نتيجة التفكيك الحتميّ."""
    reasons: list[str] = []
    if result is None:
        return ["تعذّر التفكيك"]

    leg = result.leg
    if leg is not None:
        if getattr(leg, "ambiguous_amount", None):
            vals = "، ".join(f"{v:g}" for v in leg.ambiguous_amount)
            reasons.append(f"مبلغ ملتبس ({vals})")
        if getattr(leg, "unresolved_treasury", None):
            reasons.append(f"خزينة لم تُحلّ «{leg.unresolved_treasury}»")
        if getattr(leg, "unresolved_supplier", None):
            reasons.append(f"مورد لم يُحلّ «{leg.unresolved_supplier}»")

    # ثقة تفكيك منخفضة — الإشارة الأوسع (17.7% قياسًا). تُفحص للحوالات فقط:
    # «noise» له مساراته (شظايا/تكملات) ولا يُقاس بثقة الحوالة.
    if result.kind == "transfer" and float(result.confidence or 0.0) < min_confidence:
        reasons.append(f"ثقة تفكيك منخفضة ({float(result.confidence or 0.0):.2f})")

    return reasons


def detect(text: str, result: Optional[ParseResult],
           treasuries: list[TreasuryRecord], suppliers: list[SupplierRecord],
           *, min_confidence: float = 0.7) -> AmbiguityVerdict:
    """التسلسل المعتمد: تفكيك سريع (تمّ) → قاعدتا «ما قبل» → إشارات «ما بعد».

    أوّل مرحلة تُطلق تحسم — لا نُكمل الفحص بلا داعٍ.
    """
    leg = result.leg if result is not None else None
    pre = detect_pre(text, treasuries, suppliers, leg=leg)
    if pre:
        return AmbiguityVerdict(True, pre, "pre")

    post = detect_post(result, min_confidence=min_confidence)
    if post:
        return AmbiguityVerdict(True, post, "post")

    return AmbiguityVerdict(False, [], "")
