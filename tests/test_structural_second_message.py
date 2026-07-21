"""
البند 4 — تمييز زبون/مورد/خزينة بالموضع البنيويّ في الرسالة الثانية.

القواعد (الموضع يحدّد النوع، بلا بحث متقاطع):
  ① كود+اسم+سعر → زبون بسعر البيع (الكود هو الهويّة — حرفيّ، لا قائمة بيضاء).
  ② اسم+سعر بلا كود → مورّد بسعر الشراء — يُحلّ في قائمة الموردين حصرًا.
  ③ اسم فقط بلا سعر → خزينة.
  ⑤ التصعيد يسمّي النوع: «المورد X غير معروف» (لا «لا خزينة» المضلِّل).

حادثة X1277: «1277 شركة القن / شركة البراق» — كان يخلط النوعين.
"""
from __future__ import annotations

import contextlib
import io
from datetime import timedelta

import pytest

from core.models import RawMessage, SupplierRecord
from tests.test_burst_linking import CENTRAL, NOW, S1, _pipe


async def _seed_braq(db):
    """«البراق» مورّد مُدرَج (1280) كما في الإنتاج — يُكوّن سطر المورّد بلا كود."""
    await db.suppliers.col.insert_one(SupplierRecord(
        code="1280", name="البراق", aliases=["البراق", "شركة البراق", "براق"], active=True
    ).model_dump())


async def _run_pair(db, ref, m1, m2):
    """حوالة A (msg1) ثمّ رسالتها الثانية (msg2) — نبضاتٌ تتخطّى الاستقرار."""
    pipe = _pipe(db)
    with contextlib.redirect_stderr(io.StringIO()):
        await pipe.capture(RawMessage(message_key=ref + "a", chat_jid=CENTRAL, sender_jid=S1,
                                      text=m1, received_at=NOW))
        await pipe.process_inbox(NOW + timedelta(seconds=95))
        await pipe.capture(RawMessage(message_key=ref + "b", chat_jid=CENTRAL, sender_jid=S1,
                                      text=m2, received_at=NOW + timedelta(seconds=96)))
        for t in (97, 160, 210):
            await pipe.process_inbox(NOW + timedelta(seconds=t))
    d = await db.deals.col.find_one({"sell_leg.reference_number": ref})
    outs = [(o.get("text") or "") async for o in db.outgoing.col.find({})]
    return d, outs


# ═════════════════════════════════════════════════════════════════════════════
# ① + ② الحالات الموجبة: كود+اسم+سعر = زبون، اسم+سعر بلا كود = مورّد
# ═════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("ref,m1,m2,cust,price,sup", [
    ("X1277", "X1277\nسلم الي\n01018062422\nأحمد\n20000 جني مصري\nانستاباي\nبدون خصم",
     "1277 شركة القن 6.02\nشركة البراق 5.93", "1277", "6.02", "1280"),
    ("X1254", "X1254\nارجو تسليم انستابي 50105 ج م\n01019308024",
     "1140 القيصر لصرافه 5.98\nشركة البراق 5.95", "1140", "5.98", "1280"),
    ("X1243", "X1243\n01118887681\nمصر انستاباي\n17400 ج م",
     "492 عبد المنعم ظهره 5.98\nالبراق 5.95", "492", "5.98", "1280"),
])
async def test_customer_and_supplier_by_position(db, ref, m1, m2, cust, price, sup):
    """السطر الأول (كود+اسم+سعر) = زبون بسعر البيع؛ الثاني (اسم+سعر بلا كود) = مورّد بسعر الشراء."""
    await _seed_braq(db)
    d, _ = await _run_pair(db, ref, m1, m2)
    sl = (d or {}).get("sell_leg") or {}
    assert sl.get("customer_code") == cust, f"الزبون: {sl.get('customer_code')}"
    assert sl.get("price_normalized") == price, f"سعر البيع: {sl.get('price_normalized')}"
    assert (sl.get("supplier") or {}).get("code") == sup, f"المورّد: {sl.get('supplier')}"


# ═════════════════════════════════════════════════════════════════════════════
# ⑤ التصعيد يسمّي النوع الصحيح: مورّد مجهول → «المورد X غير معروف» لا «لا خزينة»
# ═════════════════════════════════════════════════════════════════════════════
async def test_unknown_supplier_escalation_names_type(db):
    """اسم+سعر بلا كود لم يُحلّ موردًا → تصعيد «المورد «X» غير معروف» (لا «لا خزينة محلولة»)."""
    await _seed_braq(db)
    d, outs = await _run_pair(
        db, "X1300", "X1300\n01018062422\n5000 ج م\nانستاباي\nبدون خصم",
        "1277 شركة القن 6.02\nشركة مجهولة 5.93")
    assert (d or {}).get("status") == "escalated"
    fail = [o for o in outs if o.startswith("🔴")]
    assert any("المورد «شركة مجهولة» غير معروف" in o for o in fail), f"التصعيد لم يسمِّ المورد: {fail}"
    assert not any("لا خزينة محلولة" in o for o in fail), "ما زال يقول «لا خزينة» المضلِّل"
    # الزبون التُقِط رغم جهل المورّد (لا سقوط للهويّة)
    assert ((d or {}).get("sell_leg") or {}).get("customer_code") == "1277"


# ═════════════════════════════════════════════════════════════════════════════
# ④ الحلّ حصريّ داخل النوع (بلا بحث متقاطع)
# ═════════════════════════════════════════════════════════════════════════════
def test_unresolved_supplier_helper_strict_by_type():
    """unresolved_supplier_in_second: اسم+سعر بلا كود غير محلول = مورّد مجهول؛ الكود=زبون،
    والاسم بلا سعر=خزينة — كلاهما ليس موردًا مجهولًا (لا بحث متقاطع §0)."""
    from core.parsing import unresolved_supplier_in_second
    sup = [SupplierRecord(code="1280", name="البراق", aliases=["شركة البراق", "براق"], active=True)]
    assert unresolved_supplier_in_second("1277 شركة القن 6.02\nشركة مجهولة 5.93", sup) == "شركة مجهولة"
    assert unresolved_supplier_in_second("1277 شركة القن 6.02\nشركة البراق 5.93", sup) is None  # محلول
    assert unresolved_supplier_in_second("1277 شركة القن 6.02\nبلاس", sup) is None              # اسم بلا سعر = خزينة
    assert unresolved_supplier_in_second("1277 شركة القن 6.02", sup) is None                    # زبون فقط
