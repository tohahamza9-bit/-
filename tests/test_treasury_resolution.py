"""
اختبارات حلّ الخزينة (§4.5 §0) — مطابقة تامّة فقط (#2) + aliases دقيقة (#4).
🔴 حرج ماليًا: لا تُطابَق خزينة داخل اسم زبون/مدينة (كان يُدخِل خزينة خاطئة).
"""
from __future__ import annotations

import logging

from core.constants import SEED_TREASURIES, TreasuryType
from core.models import TreasuryRecord
from core.parsing import parse_message
from core.parsing.resolve import resolve_treasury

TREAS = [TreasuryRecord(**t) for t in SEED_TREASURIES]


# ── مطابقة تامّة صحيحة ──────────────────────────────────────────────────────
def test_exact_name_and_alias_resolve():
    assert resolve_treasury("وليد", TREAS).code == "51"        # alias
    assert resolve_treasury("طلال", TREAS).code == "76"        # alias
    assert resolve_treasury("بلاس", TREAS).code == "74"        # alias
    assert resolve_treasury("بلاس فون", TREAS).code == "74"    # الاسم الكامل
    assert resolve_treasury("صافي", TREAS).name == "صافي"      # الاسم


def test_misspelled_alias_resolves_fix4():
    # #4: «بلس» (إملاء ناقص) → بلاس فون — alias دقيق، لا مطابقة فضفاضة
    assert resolve_treasury("بلس", TREAS).code == "74"


# ── الخزائن الخارجية sell_and_buy: خصم 1% مستكمَلة الكود (72، EGP) ────────────
def test_khasm_1pct_seeded_code_and_currency():
    from core.constants import Currency
    rec = next(t for t in TREAS if t.name == "خصم 1%")
    assert rec.code == "85"                 # مؤقّت (كان 72)
    assert rec.currency == Currency.EGP
    assert rec.type == TreasuryType.SELL_AND_BUY


def test_khasm_1pct_resolves_by_name_and_aliases():
    # الاسم وكل الـaliases تحلّ للكود 85 (يشمل «خصم 1%»/«خصم1%» الصريحة)
    for token in ("خصم 1%", "خصم1%", "خصم 1", "خصم1", "خصم"):
        assert resolve_treasury(token, TREAS).code == "85", token


def test_all_sell_and_buy_treasuries_present():
    # كل الخزائن الخارجية (sell_and_buy) موجودة في البذرة
    sab = {t.name: t for t in TREAS if t.type == TreasuryType.SELL_AND_BUY}
    assert set(sab) == {"خصم 1%", "صافي", "تونسي خارجي"}
    assert sab["خصم 1%"].code == "85"       # مستكمَل (مؤقّت، كان 72)
    assert sab["صافي"].code is None         # معلّق (ملحق ب-3)
    assert sab["تونسي خارجي"].code is None  # معلّق (ملحق ب-3)


# ── منع false-positive (#2) — اسم زبون/مدينة لا يُطابِق خزينة ────────────────
def test_customer_name_not_matched_as_treasury():
    # «محمد عبدالرحيم» (اسم مستلم) لا يُطابِق خزينة «محمد حمامات» (كان code 79 خطأً)
    assert resolve_treasury("الاسم: محمد عبدالرحيم", TREAS) is None
    assert resolve_treasury("محمد عبدالرحيم", TREAS) is None


def test_city_not_matched_as_treasury():
    # «تونس العاصمة» (مدينة) لا يُطابِق «وليد تونس العاصمة» بالـsubstring (كان code 51 خطأً)
    # حتى مع تجريد لاحقة المدينة (ب): «تونس العاصمة»→«تونس»→لا مطابقة تامّة → None.
    assert resolve_treasury("تونس العاصمة", TREAS) is None


# ── تجريد لاحقة المدينة (ب): «وليد العاصمة»→«وليد»→alias بلا مطابقة فضفاضة ─────
def test_trailing_city_stripped_then_alias_resolves():
    # الرسالة الثانية التونسية تكتب «وليد العاصمة»؛ لا alias بهذا الشكل في SEED — تُحلّ
    # بتجريد «العاصمة» ثم مطابقة alias «وليد» تامّةً (code 51).
    assert resolve_treasury("وليد العاصمة", TREAS).code == "51"
    # «طلال العاصمة» → «طلال» (code 76) — يثبت أن الآلية عامّة لا خاصّة بـ«وليد».
    assert resolve_treasury("طلال العاصمة", TREAS).code == "76"
    # مدن أخرى من القائمة تُجرَّد أيضًا من النهاية.
    assert resolve_treasury("وليد سوسة", TREAS).code == "51"


def test_city_alone_resolves_to_none():
    # «العاصمة» وحدها تُجرَّد بالكامل → فارغ → None (مدينة ليست خزينة).
    assert resolve_treasury("العاصمة", TREAS) is None
    assert resolve_treasury("سوسة", TREAS) is None


def test_partial_word_not_matched():
    assert resolve_treasury("بلا", TREAS) is None        # ليست «بلاس»/«بلس» تمامًا
    assert resolve_treasury("محمد سعيد علي", TREAS) is None


# ── الالتباس → None + تحذير (§0 لا تخمين) ───────────────────────────────────
def test_ambiguous_match_returns_none_with_warning(caplog):
    t = [
        TreasuryRecord(code="1", name="خزينة أ", type=TreasuryType.SELL_ONLY, aliases=["مشترك"]),
        TreasuryRecord(code="2", name="خزينة ب", type=TreasuryType.SELL_ONLY, aliases=["مشترك"]),
    ]
    with caplog.at_level(logging.WARNING, logger="core.parsing.resolve"):
        assert resolve_treasury("مشترك", t) is None
    assert any("ملتبسة" in r.message for r in caplog.records)


# ── الخزينة في سطر الترويسة «/» تُلتقَط خزينة لا اسم مستلم (ترتيب الخطوة 8 قبل 9) ─
async def test_header_slash_saafi_is_note_not_treasury(db):
    # 🔴 تصحيح (بلاغ A11–A16): «… / صافي» → «صافي» مؤشّر خصم → ملاحظة، لا خزينة ولا اسم مستلم.
    treas = await db.treasuries.all_active()
    r = parse_message("A08 / فودافون / 01097298988 / 69000 مصري / صافي", treas, [])
    assert r.leg.treasury is None
    assert "صافي" in (r.leg.notes or "")
    assert r.leg.recipient_name != "صافي"


async def test_header_slash_baalas_treasury_saafi_indicator(db):
    # «بلس / … / صافي»: الخزينة = بلاس فون (74)؛ «صافي» مؤشّر بلا-خصم (ليست خزينة ثانية)
    treas = await db.treasuries.all_active()
    r = parse_message("بلس / A5183 / فودافون / 01094589619 / 541ج / صافي", treas, [])
    assert r.leg.treasury is not None and r.leg.treasury.code == "74"
    assert r.leg.amount == 541


async def test_header_slash_real_recipient_not_treasury(db):
    # exact-match محفوظ: اسم مستلم حقيقي في سطر «/» لا يُطابِق خزينة → الخزينة الصحيحة «وليد»
    treas = await db.treasuries.all_active()
    r = parse_message("A6604 / محمد عبدالرحيم / 0925135252 / القيمه: 2000 دت / وليد", treas, [])
    assert r.leg.treasury is not None and r.leg.treasury.code == "51"
    assert r.leg.recipient_name == "محمد عبدالرحيم"


# ── الحالة الحقيقية عبر parse_message: الخزينة الصحيحة رغم اسم مستلم مشابه ───
async def test_case_c_resolves_walid_not_muhammad(db):
    text = ("A6604\nتونس/العاصمه\nالاسم: محمد عبدالرحيم\nالهاتف: 0925135252\n"
            "القيمه: 2000 دت\n526 بكر همالي 35.75\nوليد")
    leg = parse_message(text, await db.treasuries.all_active(), []).leg
    assert leg.treasury is not None
    assert leg.treasury.code == "51"       # وليد، لا محمد (79)
