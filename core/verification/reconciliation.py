"""
التدقيق الدوري (Reconciliation) — كشف انحراف الكتابة بعد الحدث (§ إصلاح ٤).

يقارن كل حوالة **مكتملة** في DB خلال آخر نافذة (ساعة) بما هو مكتوب فعليًّا في MONEYADO (قراءة SQL
فقط). أي تعارض (كود/اسم/مبلغ/سعر/عمولة) → تقرير منفصل + تنبيه للمسؤول للمراجعة.

مبادئ:
- **خارج المسار الحيّ تمامًا:** مهمّة خلفية مستقلّة (interval خاص بها) — لا تلمس سرعة التنزيل الحيّ.
- **قراءة فقط:** على DB (completed_since) وعلى SQL (verifier.reconcile) — لا تعديل على الصفقات.
- **كشف لا منع:** إصلاحات ١-٣ تمنع الأخطاء المعروفة وقت الكتابة؛ هذا يكتشف الانحراف بعد وقوعه.
- حدّ النطاق: يقارن DB↔MONEYADO؛ لا يكشف أخطاء الربط (حيث DB نفسه مغلوط فيتطابق مع MONEYADO).
- التعطّل/الغياب في SQL → لا يدّعي تعارضًا (نتيجة محايدة آمنة §0) فلا إنذار كاذب.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from ..logging_setup import get_logger
from ..matching.fuzzy import names_match, normalize_ar
from ..models import Deal, ParsedLeg

log = get_logger(__name__)

_AMOUNT_EPSILON = 0.01


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(str(v).replace("،", ".").replace(",", "."))
    except (ValueError, TypeError):
        return None


def _num_differs(a: Any, b: Any) -> bool:
    """يختلفان عدديًّا (مع تسامح فاصلة عائمة)؟ أحدهما None → لا نقارن (لا نجزم)."""
    fa, fb = _to_float(a), _to_float(b)
    if fa is None or fb is None:
        return False
    return abs(fa - fb) > _AMOUNT_EPSILON


def compare_leg_to_actual(leg: ParsedLeg, actual: dict) -> list[dict]:
    """يقارن طرف البيع (DB) بما في MONEYADO (actual). يُرجع قائمة التعارضات (فارغة = متطابق).
    الحقل الغائب في MONEYADO (None) لا يُقارَن — كشفٌ محافظ بلا إنذار كاذب."""
    out: list[dict] = []

    def add(field: str, dbv: Any, sqlv: Any) -> None:
        out.append({"field": field, "db_value": dbv, "sql_value": sqlv})

    # الكود — مرساة الهوية (§1.1): مقارنة نصّية بعد إزالة الفراغ
    a_code = actual.get("customer_code")
    if a_code is not None and (leg.customer_code or "").strip() != str(a_code).strip():
        add("code", leg.customer_code, a_code)

    # الاسم — مقارنة متسامحة (خطأ إملاء لا يُعدّ تعارضًا §8.2)؛ اختلاف تامّ = تعارض
    a_name = actual.get("customer_name")
    if a_name is not None and normalize_ar(a_name) and normalize_ar(leg.customer_name or "") \
            and not names_match(leg.customer_name or "", a_name):
        add("name", leg.customer_name, a_name)

    # المبلغ / السعر / العمولة — عدديًّا
    if _num_differs(leg.amount, actual.get("amount")):
        add("amount", leg.amount, actual.get("amount"))
    if _num_differs(leg.price_normalized, actual.get("price")):
        add("price", leg.price_normalized, actual.get("price"))
    if _num_differs(leg.commission, actual.get("commission")):
        add("commission", leg.commission, actual.get("commission"))
    return out


def _fmt(mismatches: list[dict]) -> str:
    return "؛ ".join(f"{m['field']}: DB={m['db_value']} ≠ MONEYADO={m['sql_value']}" for m in mismatches)


async def reconcile_recent(db, verifier, bus, now: datetime, *, window_seconds: int = 3600) -> list[dict]:
    """يدقّق الصفقات المكتملة خلال النافذة مقابل MONEYADO. يُرجع قائمة التقارير الجديدة (المُنبَّه عنها).
    قراءة فقط على DB وSQL؛ التنبيه عبر آلية is_alert القائمة (notify_admin)."""
    if not getattr(verifier, "enabled", False):
        log.info("التدقيق الدوري: SQL معطّل — تخطٍّ (لا يُدّعى تحقّق §0).")
        return []
    since = now - timedelta(seconds=window_seconds)
    reported: list[dict] = []
    for deal in await db.deals.completed_since(since):
        leg = deal.sell_leg
        if leg is None:
            continue
        ref = leg.reference_number
        actual = await verifier.reconcile(ref)
        if actual is None:
            continue                      # غير موجود/تعذّر — لا نجزم (قد يكون تأخّر تزامن)
        mismatches = compare_leg_to_actual(leg, actual)
        if not mismatches:
            continue
        is_new = await db.reconciliation.record(
            deal_id=deal.deal_id, reference_number=ref, mismatches=mismatches, detected_at=now)
        if is_new:                        # أوّل اكتشاف فقط → تنبيه (لا تكرار كل ساعة)
            await bus.notify_admin(
                f"🔴 تدقيق دوري — تعارض بين DB وMONEYADO للحوالة {ref}: {_fmt(mismatches)}. مراجعة يدوية."
            )
            log.warning("تدقيق دوري: تعارض %s — %s", ref, _fmt(mismatches))
            reported.append({"deal_id": deal.deal_id, "reference_number": ref, "mismatches": mismatches})
    return reported
