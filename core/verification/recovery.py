"""
استرجاع الحالة بعد الإطفاء المفاجئ (§12) + منع الازدواج (§9) + الفشل النصفي (§11.4) 🔴.

> الجهاز يُطفأ ليلًا أحيانًا. بعد العودة وقبل أي فعل: يفحص آخر حوالة كانت قيد الإدخال
> في الدفتر (status ∈ SELL_DONE / READY)، يستعلم SQL هل حُفظت، ويحدّث الدفتر لمنع
> الإدخال المزدوج.

منطق «الفشل النصفي» (§11.4) — الترتيب ثابت: **بيع أولًا ثم شراء**:
  1. الدفتر يعرف بدقّة ما نزل (بيع نزل «SELL_DONE» / تمّت «COMPLETED»).
  2. البيع إن نزل → **لا يُعاد أبدًا** (لا re-sell) مهما حدث.
  3. الشراء إن لزم → **محاولة واحدة** (يُعاد لأول الطابور بواسطة الأنبوب)، فشل ثانيةً → تصعيد.
  4. حالة READY (لم يُسجَّل شيء في الدفتر): قبل إعادة الإدخال يُفحص SQL هل حُفظ فعلًا رغم
     التعطّل — إن حُفظ سُجّل في الدفتر بلا إعادة إدخال (لا ازدواج §9)؛ إن لم يُحفظ فهو آمن
     لإعادة المحاولة. إن كان SQL معطّلًا فلا يمكن الجزم → تصعيد (القاعدة الذهبية §0).

هذه الوحدة **لا تنفّذ RPA ولا تُخزّن** — قرارها: ما نزل يُثبَّت في الدفتر، وما يلزم إكماله
يُوصَف في التقرير ليأخذه الأنبوب. لا تعيد البيع إطلاقًا.
"""
from __future__ import annotations

import uuid
from typing import Any, Optional

from ..constants import OperationType, Status
from ..db import Database, utcnow
from ..logging_setup import get_logger
from ..models import LedgerEntry, ParsedLeg

log = get_logger(__name__)


def _sell_entry(entries: list[LedgerEntry]) -> Optional[LedgerEntry]:
    for e in entries:
        if e.operation == OperationType.SELL and not e.is_reversal:
            return e
    return None


def _buy_entry(entries: list[LedgerEntry]) -> Optional[LedgerEntry]:
    for e in entries:
        if e.operation == OperationType.BUY and not e.is_reversal:
            return e
    return None


async def _record_from_leg(
    db: Database, deal_id: str, leg: ParsedLeg, operation: OperationType,
    status: Status, moneyado_ref: Optional[str],
) -> None:
    """يسجّل قيدًا في الدفتر لحوالة اكتُشف أنها حُفظت في SQL (منع الازدواج §9)."""
    entry = LedgerEntry(
        entry_id=str(uuid.uuid4()),
        deal_id=deal_id,
        message_key=leg.source_message_key or deal_id,
        reference_number=leg.reference_number,
        operation=operation,
        amount=leg.amount or 0.0,
        currency=leg.currency,  # type: ignore[arg-type]
        customer_code=leg.customer_code,
        treasury_code=leg.treasury.code if leg.treasury else None,
        moneyado_ref=moneyado_ref,
        status=status,
        sql_verified=True,
        created_at=utcnow(),
    )
    await db.ledger.append(entry)


async def _recover_deal(db: Database, verifier: Any, deal: Any) -> dict:
    """يقرّر مصير صفقة واحدة كانت قيد الإدخال لحظة التعطّل. لا يعيد البيع أبدًا."""
    report: dict[str, Any] = {
        "deal_id": deal.deal_id,
        "status": deal.status.value if isinstance(deal.status, Status) else deal.status,
        "reference_number": deal.sell_leg.reference_number if deal.sell_leg else None,
        "sell": None,
        "buy": None,
    }
    entries = await db.ledger.entries_for_deal(deal.deal_id)
    sell_led = _sell_entry(entries)
    buy_led = _buy_entry(entries)

    # ── البيع ────────────────────────────────────────────────────────────────
    if deal.status == Status.SELL_DONE or sell_led is not None:
        # الدفتر يشهد أن البيع نزل → لا نعيده أبدًا (§11.4).
        report["sell"] = "already_landed_no_resell"
        if sell_led is not None and not sell_led.sql_verified and deal.sell_leg:
            # حالة نادرة: نزل في SQL لكن انقطع قبل تأكيد الدفتر → نؤكّد ونثبّت المرجع.
            verified, ref = await verifier.verify_transaction(
                sell_led.reference_number, sell_led.amount,
                sell_led.customer_code, OperationType.SELL,
            )
            if verified:
                await db.ledger.mark_sql_verified(sell_led.entry_id, ref or "")
                report["sell"] = "confirmed_in_sql_no_resell"
                log.info("استرجاع: البيع %s مؤكّد في SQL — لن يُعاد.", sell_led.reference_number)
    else:
        # READY: لا قيد بيع في الدفتر. قبل إعادة الإدخال نفحص SQL هل حُفظ رغم التعطّل (§9).
        if not getattr(verifier, "enabled", False):
            report["sell"] = "cannot_verify_escalate"
            log.warning(
                "استرجاع: الصفقة %s READY وSQL معطّل — تعذّر الجزم بالحفظ → تصعيد (§0).",
                deal.deal_id,
            )
        elif deal.sell_leg is not None:
            verified, ref = await verifier.verify_transaction(
                deal.sell_leg.reference_number, deal.sell_leg.amount,
                deal.sell_leg.customer_code, OperationType.SELL,
            )
            if verified:
                # حُفِظ فعلًا! نثبّته في الدفتر بلا إعادة إدخال (لا ازدواج §9).
                await _record_from_leg(
                    db, deal.deal_id, deal.sell_leg, OperationType.SELL,
                    Status.SELL_DONE, ref,
                )
                report["sell"] = "found_in_sql_recorded_no_reentry"
                log.info("استرجاع: البيع %s وُجد في SQL — سُجّل بلا إعادة إدخال.",
                         deal.sell_leg.reference_number)
            else:
                # لم يُحفظ → آمن لإعادة المحاولة عبر الأنبوب (البيع أولًا §7.3).
                report["sell"] = "not_saved_safe_to_retry"
        else:
            report["sell"] = "no_sell_leg"

    # ── الشراء (الفشل النصفي §11.4) ──────────────────────────────────────────
    if getattr(deal, "is_two_legged", False) and deal.buy_leg is not None:
        if buy_led is not None and buy_led.sql_verified:
            report["buy"] = "already_completed"
        else:
            verified = False
            ref = None
            if getattr(verifier, "enabled", False):
                verified, ref = await verifier.verify_transaction(
                    deal.buy_leg.reference_number, deal.buy_leg.amount,
                    deal.buy_leg.customer_code, OperationType.BUY,
                )
            if verified:
                # الشراء نزل فعلًا (رغم انقطاع قبل تأكيد الدفتر) → نثبّته، لا نعيده.
                if buy_led is not None:
                    await db.ledger.mark_sql_verified(buy_led.entry_id, ref or "")
                else:
                    await _record_from_leg(
                        db, deal.deal_id, deal.buy_leg, OperationType.BUY,
                        Status.COMPLETED, ref,
                    )
                report["buy"] = "found_in_sql_no_reentry"
            else:
                # الشراء لم ينزل → يلزم إكماله (محاولة واحدة، يُعاد لأول الطابور §11.4).
                report["buy"] = "needs_completion_one_retry"
                log.warning(
                    "استرجاع: الصفقة %s فشل نصفي (بيع نزل، شراء ناقص) — يُكمَّل الشراء مرّة واحدة.",
                    deal.deal_id,
                )

    return report


async def recover_pending(db: Database, verifier: Any) -> list[dict]:
    """
    نقطة الدخول بعد العودة من الإطفاء وقبل أي فعل (§12).

    يمرّ على كل صفقة كانت قيد الإدخال (SELL_DONE / READY)، ويقرّر مصيرها بلا ازدواج
    وبلا إعادة بيع. يُرجع قائمة تقارير (dict لكل صفقة) يستهلكها الأنبوب لإكمال ما يلزم.
    """
    deals = await db.deals.by_status(Status.SELL_DONE, Status.READY)
    if not deals:
        log.info("استرجاع الحالة: لا توجد صفقات قيد الإدخال — لا شيء لاسترجاعه.")
        return []

    log.info("استرجاع الحالة: فحص %d صفقة كانت قيد الإدخال بعد الإطفاء (§12).", len(deals))
    reports: list[dict] = []
    for deal in deals:
        try:
            reports.append(await _recover_deal(db, verifier, deal))
        except Exception:  # لا silent catch (T5) — نسجّل ونكمل باقي الصفقات
            log.exception("فشل استرجاع الصفقة %s — تُترك كما هي للمراجعة اليدوية.", deal.deal_id)
            reports.append({"deal_id": deal.deal_id, "sell": "recovery_error", "buy": None})
    return reports
