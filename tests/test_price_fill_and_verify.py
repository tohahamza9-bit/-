"""
السعر في شاشتَي البيع والشراء + تحقّق الامتلاء بعد التعبئة (م: X1323).

الحادثة: الرسالة الثانية «745 ابراهيم عون 5.96 / مومن عريبي**6.02**» — السعر ملتصق بالاسم
بلا مسافة، فابتُلع في الاسم وضاع (price=None)، بينما المورّد حُلّ رغم ذلك بالمطابقة التقريبية
فلم يبدُ أيّ خلل. فخُلِّق طرف شراء بلا سعر، فتُرك حقل rate_divide فارغًا في شاشة الشراء،
فلم يقبل MONEYADO الحفظ، فانتهت المحاولة بـ«غير مؤكّد التخزين» — تشخيصٌ مضلّل مرّتين.
"""
from __future__ import annotations

from core.constants import Currency, OperationType, TreasuryType
from core.models import ParsedLeg, SupplierRecord, SupplierRef, TreasuryRef
from core.parsing import extract_code_name_price_lines
from core.writers.moneyado.fields import build_buy_fields, build_sell_fields

_T = TreasuryRef(code="85", name="فودافون بالخصم", type=TreasuryType.SELL_AND_BUY,
                 currency=Currency.EGP)
_SUPS = [SupplierRecord(name="مومن عريبي ", code="1163",
                        aliases=["مومن عريبي", "مومن"])]


# ═══════════════════════════════════════════════════════════════════════════
# (1) الجذر: السعر الملتصق بالاسم لا يضيع
# ═══════════════════════════════════════════════════════════════════════════
def test_glued_price_is_extracted():
    """🔴 X1323: «مومن عريبي6.02» بلا مسافة → يجب استخراج السعر 6.02 لا ابتلاعه في الاسم."""
    pairs = extract_code_name_price_lines("745 ابراهيم عون 5.96\n\nمومن عريبي6.02", _SUPS)
    assert len(pairs) == 2, f"لم يُستخرج سطران: {pairs}"
    assert pairs[1][0] == "1163" and pairs[1][2] == "6.02", f"السعر ضاع: {pairs[1]}"


def test_glued_price_matches_spaced_form():
    """الملتصق والمفصول يعطيان النتيجة نفسها (لا فرق سلوكيّ)."""
    glued = extract_code_name_price_lines("745 ابراهيم عون 5.96\n\nمومن عريبي6.02", _SUPS)
    spaced = extract_code_name_price_lines("745 ابراهيم عون 5.96\n\nمومن عريبي 6.02", _SUPS)
    assert glued == spaced


def test_glued_price_on_customer_line():
    """نفس العلّة على سطر الزبون: «972 طه الحافي35» → اسم نظيف + سعر 35 (م: X1328)."""
    pairs = extract_code_name_price_lines("972 طه الحافي35", [])
    assert pairs and pairs[0][1] == "طه الحافي" and pairs[0][2] == "35"


def test_code_glued_to_keyword_matches_spaced_form():
    """«كود1284» الملتصقة تعطي ما تعطيه «كود 1284» المفصولة — اتّساق لا انحدار.

    (على master كانت الملتصقة تُعيد [] تمامًا؛ الفصل جعلها تساوي المفصولة. بقاء كلمة «كود»
    داخل الاسم سلوكٌ قائم في الصيغة المفصولة أصلًا، خارج نطاق هذا الإصلاح.)"""
    glued = extract_code_name_price_lines("مروان الشاوش كود1284 5.90", [])
    spaced = extract_code_name_price_lines("مروان الشاوش كود 1284 5.90", [])
    assert glued == spaced
    assert glued and glued[0][0] == "1284" and glued[0][2] == "5.90"


# ═══════════════════════════════════════════════════════════════════════════
# (2) السعر يصل فعلًا إلى حقل الشاشة في الشاشتين
# ═══════════════════════════════════════════════════════════════════════════
def _leg(**over) -> ParsedLeg:
    base = dict(operation=OperationType.SELL, reference_number="X1323",
                customer_code="745", customer_name="ابراهيم عون", amount=48900.0,
                currency=Currency.EGP, price_normalized="5.96", treasury=_T)
    base.update(over)
    return ParsedLeg(**base)


def test_sell_screen_writes_price():
    ops = {o.key: o.value for o in build_sell_fields(_leg())}
    assert ops["rate_divide"] == "5.96"


def test_buy_screen_writes_price():
    """طرف الشراء المشتقّ (كود المورد + سعره) يكتب السعر في نفس الحقل rate_divide."""
    buy = _leg(operation=OperationType.BUY, customer_code="1163", customer_name="مومن عريبي ",
               price_normalized="6.02", is_supplier_counterpart=True,
               supplier=SupplierRef(code="1163", name="مومن عريبي "))
    ops = {o.key: o.value for o in build_buy_fields(buy)}
    assert ops["rate_divide"] == "6.02", f"السعر لم يصل لحقل الشراء: {ops.get('rate_divide')!r}"


def test_buy_screen_price_empty_when_leg_has_none():
    """توثيق العطل: طرف شراء بلا سعر يُنتج حقلًا فارغًا — وهو ما يلتقطه فحص الامتلاء أدناه."""
    buy = _leg(operation=OperationType.BUY, customer_code="1163", price_normalized=None,
               is_supplier_counterpart=True)
    ops = {o.key: o.value for o in build_buy_fields(buy)}
    assert (ops.get("rate_divide") or "") == ""


# ═══════════════════════════════════════════════════════════════════════════
# (3) تحقّق الامتلاء يرفض حقلًا إلزاميًّا فارغًا **قبل** الضغط
# ═══════════════════════════════════════════════════════════════════════════
class _FakeScreen:
    """شاشة وهميّة: تُرجِع ما «كُتب» فعلًا لكل حقل (blanks = حقول لم تستقبل شيئًا)."""

    def __init__(self, blanks=()):
        self.blanks = set(blanks)

    def read_text(self, cfg):
        key = cfg.get("key")
        return "" if key in self.blanks else "قيمة"


def _writer():
    from core.writers.moneyado.writer import MoneyadoWriter
    return MoneyadoWriter.__new__(MoneyadoWriter)     # بلا __init__ (لا حاجة لإعدادات/شاشة)


def _ops_and_cfg(price="6.02"):
    buy = _leg(operation=OperationType.BUY, customer_code="1163", price_normalized=price,
               is_supplier_counterpart=True,
               supplier=SupplierRef(code="1163", name="مومن عريبي "))
    ops = build_buy_fields(buy)
    fcfg = {o.key: {"key": o.key, "tab_index": i} for i, o in enumerate(ops)}
    return ops, fcfg


def test_verify_filled_flags_missing_price_value():
    """🔴 سعر غائب من بيانات الحوالة → يُبلَّغ عنه بالاسم قبل أي ضغط (لا «غير مؤكّد» غامض)."""
    ops, fcfg = _ops_and_cfg(price=None)
    blank = _writer()._verify_filled(_FakeScreen(), fcfg, ops)
    assert any("rate_divide" in b for b in blank), blank


def test_verify_filled_is_pure_and_reads_no_screen():
    """الفحص نقيّ: لا يقرأ الشاشة إطلاقًا (لا استعلامات GUI قبل كل تخزين)."""
    class _Boom:
        def read_text(self, cfg):
            raise AssertionError("لا يجوز قراءة الشاشة في فحص الامتلاء")
    ops, fcfg = _ops_and_cfg(price=None)
    assert any("rate_divide" in b for b in _writer()._verify_filled(_Boom(), fcfg, ops))


def test_verify_filled_passes_when_all_present():
    """كل الحقول ممتلئة → لا اعتراض (لا فشل زائف)."""
    ops, fcfg = _ops_and_cfg()
    assert _writer()._verify_filled(_FakeScreen(), fcfg, ops) == []


def test_verify_filled_ignores_optional_empty_fields():
    """حقل اختياريّ بقيمة فارغة (ملاحظات/هاتف) لا يُعدّ عطلًا."""
    ops, fcfg = _ops_and_cfg()
    ops = [o for o in ops]
    blank = _writer()._verify_filled(_FakeScreen(blanks={"notes"}), fcfg, ops)
    assert "notes" not in blank
