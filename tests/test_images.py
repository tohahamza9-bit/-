"""
اختبارات الصور الحقيقية (pr.md المصدر ٥). القيم المستخرجة من كل لقطة.
الصور خارج النطاق (تسليم/باليد) مغطّاة في test_out_of_scope؛ صيغ SI في test_si_format.
الفجوات المتبقّية (عملة ملتصقة / هاتف+مبلغ بمقطع واحد) مُعلّمة xfail.
"""
from __future__ import annotations

import pytest

from core.constants import Currency
from core.parsing import parse_message


async def _treas(db):
    return await db.treasuries.all_active()


# ── صورة ٤: A7347 — 8355 مصري، صافي ─────────────────────────────────────────
async def test_image4_a7347(db):
    text = ("جمعه تنفيذ\nA7347\nانستا باي\n01037560422\n"
            "شريف عبد المجيد\n8355 مصري\nصافي")
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.reference_number == "A7347"
    assert res.leg.amount == 8355
    assert res.leg.currency == Currency.EGP
    # 🔴 تصحيح (بلاغ A11–A16): «صافي» مؤشّر خصم → ملاحظة لا خزينة؛ هذه رسالة أولى ناقصة
    # (بلا كود زبون) تنتظر الرسالة الثانية (كود + خزينة حقيقية).
    assert res.leg.treasury is None
    assert "صافي" in (res.leg.notes or "")


# ── صورة ٥: A7344 — تونسي 1750 د.ت، السعر 35.5→0.355، الخزينة طلال ───────────
async def test_image5_a7344_tnd(db):
    text = ("مهيمن تنفيذ\nA7344\nتونس العاصمة\nواتس\n29464597\nبهاء الدين\n"
            "القيمة: 1750 د.ت\n168 محمد البوتشع 35.5\nطلال")
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "A7344"
    assert leg.amount == 1750
    assert leg.currency == Currency.TND
    assert leg.price_normalized == "0.355"        # تونسي 35.5 → 0.355
    assert leg.customer_code == "168"
    # #2 exact-match: «تونس العاصمة» لم يعد يُطابِق «وليد» خطأً → الخزينة الصحيحة «طلال» (76)
    assert leg.treasury is not None and leg.treasury.code == "76"


# ── صورة ٨: A7233 — 9700 ج.م ────────────────────────────────────────────────
async def test_image8_a7233(db):
    text = "A7233\nالرجاء تحويل\n01022703936\nالقيمة: 9700 ج.م\nفودافون كاش"
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.reference_number == "A7233"
    assert res.leg.amount == 9700
    assert res.leg.currency == Currency.EGP


# ── صورة ٦: A7351 — عملة ملتصقة «3950ج» (Fix #1) ────────────────────────────
async def test_image6_a7351_glued_currency(db):
    text = "مهيمن تنفيذ\nA7351\nفودافون\n01025642842\n3950ج\n603 بن ناصر 5.77\nبلس"
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.amount == 3950
    assert res.leg.currency == Currency.EGP
    assert res.leg.treasury is not None and res.leg.treasury.code == "74"   # «بلس»→بلاس فون (Fix #4)


# ── صورة ١٠: A7239 — هاتف+مبلغ بمقطع واحد «مصر 100000» (Fix #3) ──────────────
async def test_image10_a7239_raw_segment(db):
    from core.models import SupplierRecord
    await db.suppliers.upsert(SupplierRecord(code="760", name="طه", aliases=["طه"]))
    sup = await db.suppliers.all_active()
    sell = parse_message("A7239 / 01064074568 مصر 100000 / 562 بوجناح 5.84", await _treas(db), sup)
    assert sell.kind == "transfer"
    assert sell.leg.amount == 100000          # «مصر 100000» → 100000 (Fix #3)
    assert sell.leg.currency == Currency.EGP
    buy = parse_message("A7239 / 01064074568 مصر 99.000 / 760 طه 5.80", await _treas(db), sup)
    assert buy.leg.amount == 99000
