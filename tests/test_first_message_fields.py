"""
مؤشّرات الخصم في حوالة A (§3.2 §6.1، تصحيح بلاغ A11–A16):

الكلمات «صافي / خصم / خصم 1% / بدون خصم» مؤشّرات على نوع الخصم — لا خزائن وجهة — فتُوجَّه
إلى الملاحظات ولا تُحلّ خزينةً. أمّا الخزائن الحقيقية (بلس/وليد/طلال/بلاس فون…) فتُحلّ عاديًا،
والخزينة الحقيقية قد تأتي من الرسالة الثانية (بلا رقم إشاري).

جوهر الإصلاح: «صافي» كانت تُحلّ خزينةً معلّقة (code=None) فتحجب الخزينة الحقيقية وتُعلَّق الصفقة
«كود ناقص»؛ الآن تصير ملاحظة، فتفوز خزينة الرسالة الثانية الحقيقية.
"""
from __future__ import annotations

from core.parsing import parse_message


async def _treas(db):
    return await db.treasuries.all_active()


# ═════════════════════════════════════════════════════════════════════════════
# مؤشّرات الخصم → ملاحظات لا خزينة
# ═════════════════════════════════════════════════════════════════════════════
async def test_saafi_is_note_not_treasury(db):
    leg = parse_message("A13\nفودافون\n010972989\n60000 مصري\nصافي", await _treas(db), []).leg
    assert leg.treasury is None
    assert "صافي" in (leg.notes or "")


async def test_bedoun_khasm_is_note_not_treasury(db):
    leg = parse_message("A21\nانستا باي\n01000000000\n7000 مصري\nبدون خصم", await _treas(db), []).leg
    assert leg.treasury is None
    assert "بدون خصم" in (leg.notes or "")


async def test_city_is_not_resolved_as_treasury(db):
    # «القاهرة» ليست خزينة — لا تُحلّ خزينةً (المطلب: لا تُلتقَط خزينة خاطئة)
    leg = parse_message("A20\nفودافون\n01000000000\n5000 مصري\nالقاهرة", await _treas(db), []).leg
    assert leg.treasury is None


# ═════════════════════════════════════════════════════════════════════════════
# الخزائن الحقيقية تبقى تُحلّ عاديًا (لم تنكسر الحوالة الكاملة برسالة واحدة)
# ═════════════════════════════════════════════════════════════════════════════
async def test_real_treasury_still_resolves_with_saafi_indicator(db):
    # «بلس … صافي»: الخزينة = بلاس فون (74)، و«صافي» مؤشّر خصم → ملاحظة (لا تحجب الخزينة)
    leg = parse_message("بلس / A5183 / فودافون / 01094589619 / 541ج / صافي",
                        await _treas(db), []).leg
    assert leg.treasury is not None and leg.treasury.code == "74"
    assert "صافي" in (leg.notes or "")


async def test_complete_single_message_still_resolves(db):
    # حوالة كاملة برسالة واحدة (صورة A7351): خزينة حقيقية + كود زبون → تُحلّ كلاهما
    leg = parse_message("مهيمن تنفيذ\nA7351\nفودافون\n01025642842\n3950ج\n603 بن ناصر 5.77\nبلس",
                        await _treas(db), []).leg
    assert leg.treasury is not None and leg.treasury.code == "74"   # بلس → بلاس فون
    assert leg.customer_code == "603"


# ═════════════════════════════════════════════════════════════════════════════
# الخزينة الحقيقية من الرسالة الثانية تفوز (جوهر إصلاح A13)
# ═════════════════════════════════════════════════════════════════════════════
async def test_real_treasury_from_second_message(db):
    first = parse_message("A23\nفودافون\n010972989\n60000 مصري\nصافي", await _treas(db), []).leg
    assert first.treasury is None                       # صافي ملاحظة

    second = parse_message("570 ايهاب 5.70\nابو يوسف", await _treas(db), []).leg
    assert second.treasury is not None and second.treasury.code == "77"   # أبو يوسف جديد
    assert second.customer_code == "570"
    assert second.price_normalized == "5.70"


# ═════════════════════════════════════════════════════════════════════════════
# استثناء SI: تبقى رسالة واحدة كاملة — تُحلّ خزينتها وزبونها
# ═════════════════════════════════════════════════════════════════════════════
async def test_si_message_still_resolves_treasury_and_customer(db):
    text = (
        "رقم العملية: SI0464\nرقم المستلم: 01093232832\n"
        "اسم الزبون: مروان الشاوش كود 1284\nالقيمة قبل الخصم: 3540 ج.م\n"
        "القيمة بعد الخصم 1%: 3505 ج.م\nالسعر: 5.9\n"
        "نوع التحويل: فودافون كاش\nالخزينة: بلاس فون"
    )
    leg = parse_message(text, await _treas(db), []).leg
    assert leg.reference_number == "SI0464"
    assert leg.treasury is not None and leg.treasury.code == "74"   # بلاس فون
    assert leg.customer_code == "1284"
