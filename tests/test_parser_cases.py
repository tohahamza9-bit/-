"""
اختبارات الصيغ الحقيقية (pr.md المصدر ٤). حالة لكل صيغة بالقيم المتوقّعة.
الحالات ذات الفجوات المتبقّية (خارج الإصلاحات 1-5) مُعلّمة xfail بسبب صريح.
"""
from __future__ import annotations

import pytest

from core.constants import Currency, OperationType, TreasuryType
from core.parsing import parse_message


async def _treas(db):
    return await db.treasuries.all_active()


# ── أ. صيغة عادية (بيع مفرد) — الفاصلة العربية في السعر (Fix 4) ───────────────
async def test_case_a_simple_sell(db):
    text = ("1208 فداء شاكونه 5،84\nبلس\nA5169\n010954227116\n"
            "1600 ج م\nفودافون\nبدون خصم")
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "A5169"
    assert leg.customer_code == "1208"
    assert leg.amount == 1600
    assert leg.currency == Currency.EGP
    assert leg.price_normalized == "5.84"      # «5،84» → «5.84» (Fix 4)
    assert leg.phone == "010954227116"
    assert leg.operation == OperationType.SELL
    assert leg.expects_pair is False


# ── ج. بيع فقط + خصم تونسي (A6604) ──────────────────────────────────────────
async def test_case_c_tnd_sell_only(db):
    text = ("A6604\nتونس/العاصمه\nالاسم: محمد عبدالرحيم\nالهاتف: 0925135252\n"
            "القيمه: 2000 دت\n526 بكر همالي 35.75\nوليد")
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "A6604"
    assert leg.customer_code == "526"
    assert leg.amount == 2000
    assert leg.currency == Currency.TND
    assert leg.price_normalized == "0.3575"    # تونسي 35.75 → 0.3575
    assert leg.operation == OperationType.SELL
    assert leg.expects_pair is False           # 🔴 لا انتظار (Fix 3)
    # #2 exact-match: «الاسم: محمد عبدالرحيم» لم يعد يُطابِق خزينة «محمد» خطأً →
    # تُحلّ الخزينة الصحيحة «وليد» (51) من سطرها المستقل (خزينة حقيقية، ليست مؤشّر خصم).
    assert leg.treasury is not None
    assert leg.treasury.code == "51"
    assert leg.treasury.type == TreasuryType.SELL_ONLY


# ── هـ. صيغة المرسل/المستلم (A5172) ─────────────────────────────────────────
async def test_case_e_sender_recipient(db):
    text = ("بلس\nA5172\nالمرسل : محمد دمياط\nالمستلم : 01044673940\n"
            "4.826 جنيه مصري\nفودافون كاش")
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.reference_number == "A5172"
    assert res.leg.amount == 4826             # 4.826 → فاصل آلاف يُشال
    assert res.leg.currency == Currency.EGP


# ── ي. «صافي» = بيع مفرد بلا عمولة ──────────────────────────────────────────
async def test_case_j_saafi_single(db):
    # صيغة أسطر نظيفة (بلا عملة ملتصقة) — «صافي» خزينة بيع/شراء لكن الحوالة مفردة
    text = "A5178\nفودافون\n01036326497\n45.000 جنيه مصر\nصافي"
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.amount == 45000
    assert res.leg.expects_pair is False


# ── المبلغ ملصق بالعملة بلا مسافة («مصر50000»/«50000م.ج») → يُلتقط في _fish_a_anchors ──
@pytest.mark.parametrize("glued", ["مصر50000", "50000مصر", "50000م.ج", "50000مج", "50,000مصر"])
async def test_glued_currency_amount(db, glued):
    # «مصر» وحدها (لا «مصري») لا يلتقطها detect_currency → fallback المبلغ الملصق بالعملة
    res = parse_message(f"A1\n{glued}\n01000000000", await _treas(db), [])
    assert res.leg.amount == 50000
    assert res.leg.currency == Currency.EGP


# ── عملة ومبلغ على سطرين منفصلين («مصر» ثم «50000») → يُلتقطان في _fish_a_anchors ──
@pytest.mark.parametrize("text, amount", [
    ("A8154\n01029051735\nمصر\n50000\n562 بوجناح 5.96", 50000),   # «مصر» + «50000» سطران
    ("A8154\n01029051735\nمصر\n49.500\nبلاس فون", 49500),         # «49.500» بنقطة = 49500 لا 49.5
    ("A9\n01029051735\nمصري\n50000\n562 بوجناح 5.96", 50000),     # «مصري» سطر مستقلّ → EGP
])
async def test_standalone_currency_and_amount_lines(db, text, amount):
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.amount == amount
    assert res.leg.currency == Currency.EGP


# ── #1 عملة ملتصقة بالرقم «541ج» → المبلغ يُلتقط (كان xfail) ──────────────────
async def test_case_j_glued_currency_slash(db):
    text = "بلس / A5183 / فودافون / 01094589619 / 541ج / صافي"
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.amount == 541           # «541ج» → 541 (Fix #1)
    assert res.leg.currency == Currency.EGP


# ── فجوة متبقّية خارج الثغرات الأربع: شراء صريح بلا مبلغ/كود/ref ──────────────
@pytest.mark.xfail(reason="خارج الأربع: شراء صريح «شراء مؤمن عريبي 6.30» بلا مبلغ/كود/ref → noise",
                   strict=False)
async def test_case_w_explicit_buy(db):
    from core.models import SupplierRecord
    await db.suppliers.upsert(SupplierRecord(code="900", name="مؤمن عريبي", aliases=["مؤمن عريبي"]))
    res = parse_message("شراء مؤمن عريبي 6.30\nبلاس فون",
                        await _treas(db), await db.suppliers.all_active())
    assert res.kind == "transfer"
    assert res.leg.operation == OperationType.BUY
