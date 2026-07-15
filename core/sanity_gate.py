"""
البوّابة العقلانية — تقييم نقيّ لسعر مُقترَح (docs/FX_RATES_SPEC.md §6 §9).

**وحدة نقيّة معزولة تمامًا:** لا DB، لا I/O، ولا استيراد من pipeline/matching/writer/parser.
تأخذ مدخلات جاهزة (السعر + السياق، وGROSS مُستخرَج مسبقًا) وتُرجِع حكمًا — **لا تكتب حالة ولا
تُحلّل نصًّا**. المُنادي (خطوة ربط لاحقة، مسيّجة بـfx_rates_enabled) يقرّر متى يستدعيها.

الأحكام الأربعة (§1 §9): HELD (يتوقّف) · 🔴 ALERT (ينزل + عاجل) · ⚠️ WARN (ينزل + تنبيه مسؤول)
· OK (high). كل عتبة من FxRatesConfig — لا ثابت بالكود. «أقل سعر متاح» عند low يقرّره الـlookup
(خطوة الربط)، لا هنا؛ البوّابة تُصدر العلامة فقط.
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel

from .models import FxRatesConfig

MARK_WARN = "⚠️"
MARK_ALERT = "🔴"
_EPS = 1e-6                                       # تساوي الأسعار (6.10 ≠ 6.15 بفارق 0.05)


class SanityOutcome(str, Enum):
    OK = "ok"        # high — يُنزَّل مباشرةً (§1)
    WARN = "warn"    # medium — يُنزَّل + ⚠️ للمسؤول (§9)
    ALERT = "alert"  # low/خسارة — يُنزَّل + 🔴 عاجل (§9)
    HELD = "held"    # خارج الحدّ المطلق/سعر غير صالح — لا يُنزَّل (§6 §5 بند ٥)


class SanityVerdict(BaseModel):
    """حكم البوّابة. mark: ⚠️/🔴/None. proceeds=False فقط لـHELD."""
    outcome: SanityOutcome
    mark: Optional[str] = None
    reason: str = ""
    rate: Optional[float] = None

    @property
    def proceeds(self) -> bool:
        return self.outcome is not SanityOutcome.HELD


def _bounds(currency, cfg: FxRatesConfig) -> Optional[tuple[float, float]]:
    cur = getattr(currency, "value", str(currency)).upper()
    if cur == "EGP":
        return (cfg.rate_min_egp, cfg.rate_max_egp)
    if cur == "TND":
        return (cfg.rate_min_tnd, cfg.rate_max_tnd)
    return None                                  # عملة غير مدعومة


def deviation_band(values) -> Optional[tuple[float, float]]:
    """الحدّ الديناميكيّ (§6): (المتوسّط، الانحراف المعياريّ للمجتمع) من قائمة أسعار تاريخية تُمرَّر.

    **نقيّة تمامًا** — جلب القيم من deals مؤجّل لخطوة الربط. None إن أقلّ من قيمتين."""
    vals = [float(v) for v in (values or []) if v is not None]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    return (mean, var ** 0.5)


def evaluate(
    *,
    currency,
    rate: Optional[float],
    amount: Optional[float],
    cfg: FxRatesConfig,
    confidence: str = "high",
    employee_rate: Optional[float] = None,
    room_rate: Optional[float] = None,
    dynamic_band: Optional[tuple[float, float]] = None,
    loss: bool = False,
    amount_tier_unclear: bool = False,
    price_changed: bool = False,
) -> SanityVerdict:
    """يُقيّم سعرًا مُقترَحًا ويُرجِع SanityVerdict. الأولوية (§5): HELD > 🔴 > ⚠️ > OK.

    - currency: EGP/TND (أو Currency). rate: السعر المُقترَح. amount: GROSS مُستخرَج مسبقًا (§شرط).
    - confidence: high/medium/low (حسم الغموض §4). employee_rate/room_rate: للمقارنة (§9).
    - dynamic_band: (mean, std) من deviation_band (§6). loss: خسارة (بيع<شراء §8).
    - amount_tier_unclear / price_changed: إشارات §9.
    """
    bounds = _bounds(currency, cfg)

    # ── ١) HELD — يفوق كل شيء (§6 §5 بند ٥) ──────────────────────────────────
    if bounds is None:
        return SanityVerdict(outcome=SanityOutcome.HELD, reason="عملة غير مدعومة", rate=rate)
    if rate is None or rate <= 0:
        return SanityVerdict(outcome=SanityOutcome.HELD, reason="سعر غير صالح (≤ 0)", rate=rate)
    lo, hi = bounds
    if rate < lo or rate > hi:
        return SanityVerdict(
            outcome=SanityOutcome.HELD,
            reason=f"خارج الحدّ المطلق [{lo}, {hi}] — السعر {rate}", rate=rate)

    # ── ٢) 🔴 ALERT (ينزل + عاجل §9) ─────────────────────────────────────────
    if str(confidence).lower() == "low":
        return SanityVerdict(outcome=SanityOutcome.ALERT, mark=MARK_ALERT,
                             reason="ثقة منخفضة (لا تطابق) — أقلّ سعر متاح", rate=rate)
    if loss and amount is not None and amount > cfg.loss_alert_threshold:
        return SanityVerdict(outcome=SanityOutcome.ALERT, mark=MARK_ALERT,
                             reason=f"خسارة + مبلغ {amount} > حدّ الخسارة {cfg.loss_alert_threshold}",
                             rate=rate)

    # ── ٣) ⚠️ WARN (ينزل + تنبيه مسؤول §9) ───────────────────────────────────
    if employee_rate is not None and room_rate is not None and abs(employee_rate - room_rate) > _EPS:
        return SanityVerdict(outcome=SanityOutcome.WARN, mark=MARK_WARN,
                             reason=f"سعر الموظف {employee_rate} ≠ سعر الغرفة {room_rate}", rate=rate)
    if dynamic_band is not None:
        mean, std = dynamic_band
        if abs(rate - mean) > std:
            return SanityVerdict(outcome=SanityOutcome.WARN, mark=MARK_WARN,
                                 reason=f"خارج الحدّ الديناميكيّ (متوسّط {mean:.4f} ± {std:.4f})",
                                 rate=rate)
    if amount_tier_unclear:
        return SanityVerdict(outcome=SanityOutcome.WARN, mark=MARK_WARN,
                             reason="شريحة مبلغ كبيرة غير واضحة", rate=rate)
    if price_changed and amount is not None and amount > cfg.price_change_notification_threshold:
        return SanityVerdict(
            outcome=SanityOutcome.WARN, mark=MARK_WARN,
            reason=f"تغيّر السعر + مبلغ {amount} > حدّ الإشعار {cfg.price_change_notification_threshold}",
            rate=rate)
    if str(confidence).lower() == "medium":
        return SanityVerdict(outcome=SanityOutcome.WARN, mark=MARK_WARN,
                             reason="ثقة متوسّطة", rate=rate)

    # ── ٤) OK (high) ─────────────────────────────────────────────────────────
    return SanityVerdict(outcome=SanityOutcome.OK, reason="ضمن الحدود، ثقة عالية", rate=rate)
