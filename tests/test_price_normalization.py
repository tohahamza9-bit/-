"""
اختبارات تطبيع السعر (§3.6) — تونسي → 0.xxxx، مصري كما هو، والفاصلة العربية «،».
دوال نقيّة (بلا DB). المصدر: pr.md المصدر ١.
"""
from __future__ import annotations

import pytest

from core.constants import Currency
from core.parsing.normalize import normalize_price


@pytest.mark.parametrize("raw,expected", [
    ("35.25", "0.3525"), ("35.75", "0.3575"), ("35.7", "0.357"),
    ("35.5", "0.355"), ("33", "0.33"), ("0.3525", "0.3525"),
])
def test_tnd_price_normalized_to_zero_point(raw, expected):
    assert normalize_price(raw, Currency.TND)[1] == expected


@pytest.mark.parametrize("raw", ["5.9", "5.84", "5.90", "6.01", "5.77", "5.83"])
def test_egp_price_kept_as_is(raw):
    assert normalize_price(raw, Currency.EGP)[1] == raw


def test_arabic_comma_price_becomes_decimal():
    # «5،84» (فاصلة عربية) → «5.84» عشري (Fix 4)
    assert normalize_price("5،84", Currency.EGP)[1] == "5.84"
