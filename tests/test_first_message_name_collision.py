"""
تصادم اسم المستلم مع خزينة مسجّلة — تصنيف الرسالة الأولى (م: X1327، مبدأ X1257).

**المبدأ (قرار المالك): الموضع يحدّد النوع.** رسالةٌ تحمل **وجهة المستلم** (مدينة/بلد) هي
رسالة أولى قطعًا، مهما طابق أحدُ أسطرها اسمَ خزينة مسجّلة أو إملاءً بديلًا لها. اسم المستلم
لا يُحوِّل الرسالة إلى «ردّ خزينة» ولو كان alias مسجَّلًا. والوجهة — لا الهاتف — هي الفاصل:
الردّ الثاني الحقيقيّ **قد** يحمل هاتفًا (تسوية خصم بهاتف مختلف) لكنه لا يحمل وجهةً أبدًا.

الحادثة: «X1327 / +356 9938 5622 / 3000 دت / وليد / العاصمة» — «وليد» طابقت الخزينة المسجّلة
«وليد تونس العاصمة» (51، alias «وليد العاصمة»)، فصُنّفت الرسالة الأولى **ثانيةً**، فلم تُنشَأ
لها صفقة، فرَست تكملتها («طلال») على الحوالة الجارة X1328 فحملت خزينةً خاطئة.
"""
from __future__ import annotations

from core.constants import Currency, OperationType, TreasuryType
from core.models import ParsedLeg, TreasuryRef
from core.queue.service import is_treasury_only_reply, is_treasury_second_reply

_T_WALID = TreasuryRef(code="51", name="وليد تونس العاصمة",
                       type=TreasuryType.SELL_ONLY, currency=Currency.TND)
_T_MOHAMED = TreasuryRef(code="61", name="محمد حمامات",
                         type=TreasuryType.SELL_ONLY, currency=Currency.TND)


def _leg(**over) -> ParsedLeg:
    base = dict(operation=OperationType.SELL, reference_number="X1327",
                amount=3000.0, currency=Currency.TND, treasury=_T_WALID)
    base.update(over)
    return ParsedLeg(**base)


# ═══════════════════════════════════════════════════════════════════════════
# الحالتان المطلوبتان: «وليد» و«محمد»
# ═══════════════════════════════════════════════════════════════════════════
def test_walid_first_message_with_destination_is_not_second_reply():
    """🔴 X1327: «وليد»+«العاصمة» طابقتا خزينة 51 — لكن وجود الوجهة يحسمها رسالةً أولى."""
    leg = _leg(phone="35699385622", country="العاصمة")
    assert is_treasury_second_reply(leg) is False, \
        "الرسالة الأولى صُنّفت ردّ خزينة — يتكرّر عطل X1327"
    assert is_treasury_only_reply(leg) is False


def test_mohamed_first_message_with_destination_is_not_second_reply():
    """اسم المستلم «محمد» يطابق «محمد حمامات» (خطر ماليّ موثَّق) — الوجهة تحسمها أولى."""
    leg = _leg(reference_number="X1400", treasury=_T_MOHAMED, phone="21654810273",
               amount=340.0, country="الحمامات")
    assert is_treasury_second_reply(leg) is False


# ═══════════════════════════════════════════════════════════════════════════
# لا انحدار: الردود الثانية الحقيقيّة (بلا وجهة) تبقى كما هي
# ═══════════════════════════════════════════════════════════════════════════
def test_genuine_treasury_second_reply_still_detected():
    """ردّ خزينة حقيقيّ «رقم إشاري + خزينة» بلا وجهة → يبقى ردًّا ثانيًا (بلا انحدار)."""
    leg = _leg(amount=None, phone=None)
    assert is_treasury_second_reply(leg) is True
    assert is_treasury_only_reply(leg) is True


def test_genuine_discount_settlement_still_detected():
    """تسوية الخصم (خزينة + مبلغ بعد الخصم) تبقى ردًّا ثانيًا (§6.3) — ولو حملت هاتفًا مختلفًا."""
    leg = _leg(amount=8613.0, phone="01025946738")
    assert is_treasury_second_reply(leg) is True
    assert is_treasury_only_reply(leg) is False      # تحمل مبلغًا


def test_second_reply_with_customer_identity_still_excluded():
    """وجود هوية زبون يُخرِجها من تصنيف ردّ الخزينة (السلوك القائم، بلا تغيير)."""
    assert is_treasury_second_reply(_leg(customer_code="972", phone=None)) is False
    assert is_treasury_second_reply(_leg(customer_name="طه الحافي", phone=None)) is False
