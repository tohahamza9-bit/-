"""
اختبارات «خارج النطاق» (§0) — تسليم يدوي/باليد → تصعيد؛ صرف/قبض → تجاهل صامت.
المصدر: pr.md المصدر ٤ (ز، ح) + الصور (١، ٢، ٣). Fix 2.
"""
from __future__ import annotations

import pytest

from core.parsing import is_out_of_scope, parse_message


async def _treas(db):
    return await db.treasuries.all_active()


@pytest.mark.parametrize("text", [
    "A3418 / ارجو تسليم باليد / احمد السيد / 8.800 ج م / قاهرة باليد",   # صورة ٢
    "جمعه تنفيذ / A7354 / صلاح / 22800ج م / الاسكندريه فى يد",           # صورة ١
    "A6953 / قاهره بيد / 40.000 جنيه مصري",                              # صورة ٣
    "A6363\nتسليم\nانستا باي\nمحمود ابراهيم\nالقيمة 59574 ج م",          # حالة ح
    "A5187\nتسليم السيد علي الطيب مبلغ 100 د ت جربة",                     # حالة ز
])
async def test_reference_overrides_out_of_scope(text, db):
    # 🔴 قرار المستخدم: وجود رقم إشاري (Axxxx) مرساة معاملة قاطعة → حوالة (pattern-fishing)
    # وتتقدّم على «تسليم يدوي/باليد». (كانت out_of_scope؛ الآن transfer لأنها تحمل ref.)
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"


@pytest.mark.parametrize("text", [
    "ارجو تسليم باليد / احمد السيد / 8.800 ج م / قاهرة باليد",   # نفس صورة ٢ بلا ref
    "قاهره بيد / 40.000 جنيه مصري",                              # نفس صورة ٣ بلا ref
    "تسليم السيد علي الطيب مبلغ 100 د ت جربة",                    # نفس الحالة ز بلا ref
])
async def test_manual_delivery_without_reference_is_out_of_scope(text, db):
    # بلا رقم إشاري → التسليم اليدوي يبقى out_of_scope (يُصعَّد لا يُدخَل §0)
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "out_of_scope"


@pytest.mark.parametrize("text", ["صرف نقدي", "قبض مبلغ من العميل"])
async def test_sarf_qabd_stay_silent_ignore(text, db):
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "silent_ignore"


@pytest.mark.parametrize("text", [
    "عبيد الله محمد",          # يحوي «بيد» كجزء من كلمة — ليس خارج نطاق
    "سعيد صافي 69000 مصري",    # اسم فيه «يد»؟ لا — حوالة عادية
    "A07\nفودافون\n69000 مصري\nصافي",   # حوالة صافي مفردة عادية
])
def test_no_false_positive(text):
    assert is_out_of_scope(text) is False


def test_out_of_scope_words_and_phrases():
    assert is_out_of_scope("تسليم يدوي") is True
    assert is_out_of_scope("الحوالة باليد") is True
    assert is_out_of_scope("قاهره فى يد") is True
    assert is_out_of_scope("بيد الله") is False   # «بيد» وحدها لا تُصعّد
