"""
اختبارات قاعدة المبلغ (§3.5) — فاصل الآلاف يُشال دائمًا + قاعدة «المجموعة 3 أرقام».
دوال نقيّة (بلا DB). المصدر: pr.md المصدر ١.
"""
from __future__ import annotations

import logging

import pytest

from core.parsing.normalize import parse_amount


@pytest.mark.parametrize("raw,expected", [
    ("5.000", 5000), ("5،000", 5000), ("5.802", 5802), ("17.400", 17400),
    ("50.000", 50000), ("99.000", 99000), ("40.000", 40000), ("8.800", 8800),
    ("45.000", 45000), ("22800", 22800), ("8355", 8355), ("3950", 3950),
    ("9700", 9700), ("100000", 100000), ("1750", 1750),
    ("69.000 مصري", 69000), ("2000 دت", 2000),
])
def test_amount_thousands_separator_stripped(raw, expected):
    assert parse_amount(raw) == expected


def test_amount_unusual_group_corrected_with_warning(caplog):
    # 60.0000: المجموعة «0000» ليست 3 أرقام → تُصحّح إلى 60000 مع تحذير (لا 600000، لا تصعيد)
    with caplog.at_level(logging.WARNING, logger="core.parsing.normalize"):
        assert parse_amount("60.0000") == 60000
    assert any("غير معتادة" in r.message for r in caplog.records)


def test_amount_leading_digits_with_glued_currency():
    # parse_amount نفسها تلتقط الأرقام الأمامية حتى مع عملة ملتصقة (541ج → 541)
    assert parse_amount("541ج") == 541
    assert parse_amount("3950ج") == 3950


def test_glued_currency_detected_fix1():
    # #1: فصل الحرف عن الرقم قبل الكشف → العملة تُكتشَف مع الالتصاق
    from core.constants import Currency
    from core.parsing.normalize import detect_currency
    assert detect_currency("3950ج") == Currency.EGP
    assert detect_currency("541ج") == Currency.EGP
    assert detect_currency("22800ج م") == Currency.EGP
    assert detect_currency("2000دت") == Currency.TND


def test_egp_short_meem_after_or_before_number():
    """«م» وحدها اختصار مصري إذا كانت token منفصلة مجاورة لرقم (§3.4)."""
    from core.constants import Currency
    from core.parsing.normalize import detect_currency, parse_amount
    # المطلوب الجديد: «م» منفردة بعد/قبل رقم → EGP
    assert detect_currency("1000 م") == Currency.EGP
    assert detect_currency("م 1000") == Currency.EGP
    assert detect_currency("1000م") == Currency.EGP          # ملتصقة → تُفصَل ثم تُكشَف
    assert parse_amount("1000 م") == 1000
    # صيغ «ج م» القائمة تبقى EGP
    assert detect_currency("م ج 1000") == Currency.EGP
    assert detect_currency("1000 م ج") == Currency.EGP
    # 🔴 حذر: «م» داخل كلمة أو بلا رقم مجاور → لا تُلتقط
    assert detect_currency("محمد") is None
    assert detect_currency("مصر") is None
    assert detect_currency("760 طه محمد") is None            # «م» ليست token منفصلة


def test_amount_negative_becomes_abs():
    # مرحلة أ (§3.5): السالب → abs() + تنبيه best-effort (القيمة المالية أولوية، لا تُفقد لإشارة)
    assert parse_amount("-35") == 35
    assert parse_amount("34.540-") == 34540
    from core.parsing.normalize import scan_amount_deviations
    devs = scan_amount_deviations("القيمة 34.540- ج م")
    assert devs and devs[0]["method"] == "abs_negative" and devs[0]["extracted_value"] == 34540


def test_amount_none_when_no_digits():
    assert parse_amount(None) is None
    assert parse_amount("بدون أرقام") is None
