"""
اختبارات البوّابة العقلانية — الخطوة ٤ (core/sanity_gate.py، دالّة نقيّة).
كل حكم (HELD/🔴/⚠️/OK) + الأولوية (§5) + الحدّ الديناميكيّ. لا DB، لا fastapi.
"""
from __future__ import annotations

from core.constants import Currency
from core.models import FxRatesConfig
from core.sanity_gate import (
    SanityOutcome,
    deviation_band,
    evaluate,
)

CFG = FxRatesConfig()   # الافتراضات §12: EGP 5.5–8.0 · TND 0.28–0.45 · حدود التنبيه 200000


def _ev(**over):
    base = dict(currency=Currency.EGP, rate=6.10, amount=1000, cfg=CFG)
    base.update(over)
    return evaluate(**base)


# ── OK ───────────────────────────────────────────────────────────────────────
def test_ok_within_bounds_high_confidence():
    v = _ev()
    assert v.outcome is SanityOutcome.OK and v.mark is None and v.proceeds is True


def test_ok_tnd_within_bounds():
    v = _ev(currency=Currency.TND, rate=0.34)
    assert v.outcome is SanityOutcome.OK


# ── HELD (§6) ────────────────────────────────────────────────────────────────
def test_held_above_max():
    v = _ev(rate=9.0)
    assert v.outcome is SanityOutcome.HELD and v.proceeds is False and "الحدّ المطلق" in v.reason


def test_held_below_min():
    assert _ev(rate=4.0).outcome is SanityOutcome.HELD


def test_held_non_positive_rate():
    assert _ev(rate=0).outcome is SanityOutcome.HELD
    assert _ev(rate=None).outcome is SanityOutcome.HELD


def test_held_unsupported_currency():
    assert evaluate(currency="USD", rate=6.1, amount=1000, cfg=CFG).outcome is SanityOutcome.HELD


def test_held_tnd_out_of_bounds():
    assert _ev(currency=Currency.TND, rate=0.50).outcome is SanityOutcome.HELD


# ── 🔴 ALERT (§9) ────────────────────────────────────────────────────────────
def test_alert_low_confidence():
    v = _ev(confidence="low")
    assert v.outcome is SanityOutcome.ALERT and v.mark == "🔴" and v.proceeds is True


def test_alert_loss_over_threshold():
    v = _ev(loss=True, amount=250000)
    assert v.outcome is SanityOutcome.ALERT and v.mark == "🔴"


def test_no_alert_loss_under_threshold():
    # خسارة لكن المبلغ تحت الحدّ → لا 🔴 (ولا سبب آخر) → OK
    assert _ev(loss=True, amount=100000).outcome is SanityOutcome.OK


# ── ⚠️ WARN (§9) ─────────────────────────────────────────────────────────────
def test_warn_employee_differs_from_room():
    v = _ev(employee_rate=6.10, room_rate=6.15)
    assert v.outcome is SanityOutcome.WARN and v.mark == "⚠️"


def test_no_warn_when_rates_equal():
    assert _ev(employee_rate=6.10, room_rate=6.10).outcome is SanityOutcome.OK


def test_warn_outside_dynamic_band():
    band = (6.00, 0.05)                     # متوسّط 6.00 ± 0.05 → 6.20 خارج
    assert _ev(rate=6.20, dynamic_band=band).outcome is SanityOutcome.WARN
    assert _ev(rate=6.03, dynamic_band=band).outcome is SanityOutcome.OK  # داخل النطاق


def test_warn_amount_tier_unclear():
    assert _ev(amount_tier_unclear=True).outcome is SanityOutcome.WARN


def test_warn_price_changed_large_amount():
    assert _ev(price_changed=True, amount=250000).outcome is SanityOutcome.WARN
    assert _ev(price_changed=True, amount=100000).outcome is SanityOutcome.OK  # تحت الحدّ


def test_warn_medium_confidence():
    assert _ev(confidence="medium").outcome is SanityOutcome.WARN


# ── الأولوية (§5): HELD > 🔴 > ⚠️ ─────────────────────────────────────────────
def test_held_beats_low_confidence():
    """خارج الحدّ المطلق يفوق كل شيء — حتى مع ثقة منخفضة (§5 بند ٥)."""
    assert _ev(rate=9.0, confidence="low").outcome is SanityOutcome.HELD


def test_alert_beats_warn():
    """🔴 (ثقة منخفضة) يفوق ⚠️ (اختلاف السعر) عند اجتماعهما."""
    v = _ev(confidence="low", employee_rate=6.10, room_rate=6.15)
    assert v.outcome is SanityOutcome.ALERT and v.mark == "🔴"


# ── deviation_band (§6) ──────────────────────────────────────────────────────
def test_deviation_band_needs_two_values():
    assert deviation_band([]) is None
    assert deviation_band([6.1]) is None


def test_deviation_band_computes_mean_std():
    mean, std = deviation_band([6.0, 6.0, 6.0, 6.0])
    assert mean == 6.0 and std == 0.0
    mean2, std2 = deviation_band([5.0, 7.0])
    assert mean2 == 6.0 and abs(std2 - 1.0) < 1e-9
