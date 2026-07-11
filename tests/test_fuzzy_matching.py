"""
مطابقة تقريبية (fuzzy) للموردين + التقاط الخزائن المجهولة (§4.5 §5.4).

القرار الأمنيّ (§0): الخزائن **مطابقة تامّة فقط** (الفضفاضة تُدخِل حوالة في خزينة خاطئة — خطر
ماليّ: «محمد»→«محمد حمامات»). تنويعات الخزينة تُلتقط مجهولةً وتُسنَد يدويًّا. الموردون (قائمة
بيضاء) يقبلون fuzzy.
"""
from __future__ import annotations

from core.constants import SEED_SUPPLIERS, SEED_TREASURIES
from core.models import SupplierRecord, TreasuryRecord
from core.parsing import parse_message
from core.parsing.resolve import (
    normalize_arabic_for_matching,
    resolve_supplier,
    resolve_treasury,
)

TREAS = [TreasuryRecord(**t) for t in SEED_TREASURIES]
SUP = [SupplierRecord(**s) for s in SEED_SUPPLIERS] + [
    SupplierRecord(code="900", name="مؤمن عريبي", aliases=["مؤمن عريبي"]),
]


def test_normalize_arabic_for_matching():
    assert normalize_arabic_for_matching("العاصمة") == "عاصمه"       # «ال» + ة→ه
    assert normalize_arabic_for_matching("أبو  يوسف") == "ابو يوسف"   # همزة + مسافات زائدة
    assert normalize_arabic_for_matching("الخصم") == "خصم"
    assert normalize_arabic_for_matching(None) == ""


# ── الخزائن: تامّة تعمل، الفضفاضة مرفوضة (أمان ماليّ) ──────────────────────────
def test_treasury_exact_alias_matches():
    assert resolve_treasury("بلاس", TREAS).code == "74"             # alias تامّ → بلاس فون
    assert resolve_treasury("ابو يوسف", TREAS).code == "77"         # alias تامّ → أبو يوسف جديد


def test_treasury_fuzzy_rejected_no_financial_guess():
    # «فودا فون» ليست alias مزروعًا → مع إيقاف fuzzy للخزائن تُرجَع None (تُلتقط مجهولةً لا تُخمَّن)
    assert resolve_treasury("فودا فون", TREAS) is None
    # اسم زبون غير مسجَّل لا يُطابَق أيّ خزينة (لا prefix/subsequence/fuzzy)
    assert resolve_treasury("عبد الله الشريف", TREAS) is None


# ── الموردون: fuzzy مسموح (قائمة بيضاء §5.4) ─────────────────────────────────
def test_supplier_fuzzy_and_exact():
    assert resolve_supplier("مؤمن عريبي", SUP).name == "مؤمن عريبي"  # تامّ
    assert resolve_supplier("مومن", SUP).name == "مؤمن عريبي"        # بادئة/تقريبيّ (§3/§5)


# ── مستودع الكلمات المجهولة (record/increment/list/remove) ────────────────────
async def test_unknown_terms_record_increment_list_remove(db):
    await db.unknown_terms.record("فودا فون", "treasury")
    await db.unknown_terms.record("فودا فون", "treasury")           # تكرار → count=2
    await db.unknown_terms.record("مورد مجهول", "supplier")
    await db.unknown_terms.record("", "treasury")                  # فارغ → يُتجاهَل
    rows = await db.unknown_terms.list_recent(20)
    by = {(r["term"], r["context"]): r for r in rows}
    assert by[("فودا فون", "treasury")]["count"] == 2
    assert ("مورد مجهول", "supplier") in by
    assert ("", "treasury") not in by
    assert await db.unknown_terms.remove("فودا فون", "treasury") == 1
    assert not any(r["term"] == "فودا فون" for r in await db.unknown_terms.list_recent(20))


# ── الالتقاط في التحليل: خزينة SI معنونة غير محلولة → unresolved_treasury ─────
async def test_si_unresolved_treasury_flagged(db):
    treas = await db.treasuries.all_active()
    leg = parse_message(
        "رقم العملية: SI9001\nرقم المستلم: 01000000000\n"
        "اسم الزبون: زبون تجريبي كود 1500\nالقيمة: 1000 ج.م\nالسعر: 5.9\n"
        "الخزينة: خزينه لا وجود لها", treas, []).leg
    assert leg.treasury is None
    assert leg.unresolved_treasury and "لا وجود" in leg.unresolved_treasury
