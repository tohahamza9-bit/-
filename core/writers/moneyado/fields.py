"""
منطق «أي حقل يُملأ بأي قيمة وبأي ترتيب» — دوال نقيّة بلا pywinauto (تُختبر وحدها §11.1).

كل دالة تُرجع قائمة FieldOp مرتّبة حسب ترتيب التعبئة المؤكّد من صاحب العمل.
لا تلمس هذه الوحدة الشاشة إطلاقًا — الكتابة الفعلية في screens.py.
"""
from __future__ import annotations

from typing import NamedTuple, Optional

from ...constants import (
    MONEYADO_CURRENCY_CODE,
    MONEYADO_CURRENCY_LABEL,
    MONEYADO_PAYMENT_CODE,
    Currency,
)
from ...models import ParsedLeg
from ...parsing.normalize import normalize_ar

# طرق الإدخال (§11.3): type_keys يحاكي كتابة حقيقية فيُفعّل البحث التلقائي؛ select لقائمة منسدلة.
# enter_only: Enter فقط على الحقل النشط بلا تحديد/كتابة/مسح — لحقل يحسبه البرنامج (المبلغ المخصوم).
TYPE_KEYS = "type_keys"
SELECT = "select"
ENTER_ONLY = "enter_only"

# ── خانة «البلد» ComboBox (§11.1): تُختار الدولة دائمًا لا المدينة (قرار المستخدم) ──────
# نطابق المدينة/الدولة المُفهَّمة → دولة القائمة في MONEYADO (تونس/مصر/ليبيا). المفاتيح
# بصيغة normalize_ar (ة→ه، توحيد الألف…) لتطابق ناتج leg.country المطبَّع.
_COUNTRY_BY_PLACE = {
    # تونس ومدنها
    "تونس": "تونس", "العاصمه": "تونس", "سوسه": "تونس", "صفاقس": "تونس",
    "جربه": "تونس", "حمامات": "تونس", "نابل": "تونس", "قابس": "تونس",
    "مدنين": "تونس", "بنزرت": "تونس", "القيروان": "تونس", "المنستير": "تونس",
    "توزر": "تونس", "بنقردان": "تونس", "جرجيس": "تونس",
    # مصر ومدنها
    "مصر": "مصر", "القاهره": "مصر", "الاسكندريه": "مصر", "اسكندريه": "مصر",
    "الفيوم": "مصر", "كفر الشيخ": "مصر", "دمياط": "مصر", "دومياط": "مصر",
    "مطروح": "مصر", "مرسي مطروح": "مصر", "السلوم": "مصر",
    # ليبيا
    "ليبيا": "ليبيا", "طرابلس": "ليبيا", "بنغازي": "ليبيا", "مصراته": "ليبيا",
}


def _country_label(leg: ParsedLeg) -> Optional[str]:
    """دولة خانة «البلد» (تونس/مصر/ليبيا) — دائمًا الدولة لا المدينة (قرار المستخدم §11.1).

    يُملأ فقط حين ذُكر مكان في الرسالة (كما سابقًا) — لكن المدينة تُحوَّل إلى دولتها.
    الأولوية للنص المُفهَّم (مدينة/دولة → دولتها)، ثم العملة كمرجع للمكان المجهول (TND→تونس،
    EGP→مصر). None حين لا مكان مذكور أو تعذّر الحسم → لا تُلمَس الخانة (البلد غير حرج).
    """
    if not leg.country:                       # لا مكان مذكور → لا نملأ الخانة (سلوك سابق)
        return None
    place = normalize_ar(leg.country)
    if place in _COUNTRY_BY_PLACE:
        return _COUNTRY_BY_PLACE[place]
    # مكان مذكور لكن غير معروف في الخريطة → استعن بالعملة لتحديد الدولة
    if leg.currency == Currency.TND:
        return "تونس"
    if leg.currency == Currency.EGP:
        return "مصر"
    return None


class FieldOp(NamedTuple):
    """أمر تعبئة خانة واحدة: المفتاح، القيمة، الطريقة، وهل نضغط Enter بعدها (بحث تلقائي §11.1)."""
    key: str
    value: str
    method: str
    enter: bool = False


def _num(value) -> str:
    """يحوّل رقمًا إلى نصّ نظيف بلا فاصل آلاف (§6.2) — الأعداد الصحيحة بلا كسور."""
    if value is None:
        return ""
    f = float(value)
    if f.is_integer():
        return str(int(f))
    # كسر حقيقي: صيغة عشرية نظيفة بلا أصفار زائدة ولا فاصل آلاف
    return format(f, "f").rstrip("0").rstrip(".")


def _treasury_value(leg: ParsedLeg) -> str:
    """قيمة الحساب الأجنبي (الخزينة): الكود أضمن (§1.1)، الاسم بديل."""
    if leg.treasury is None:
        return ""
    return leg.treasury.code or leg.treasury.name or ""


def build_sell_fields(leg: ParsedLeg) -> list[FieldOp]:
    """يبني خانات شاشة «بيع عملة» بالترتيب المؤكّد (§11.1)."""
    ops: list[FieldOp] = []

    # (1) الحساب الأجنبي (الخزينة) → type_keys ثم Enter (يرفع للرقم الإشاري)
    ops.append(FieldOp("foreign_account", _treasury_value(leg), TYPE_KEYS, enter=True))

    # (2) الرقم الإشاري A6xxx
    if leg.reference_number:
        ops.append(FieldOp("reference_number", leg.reference_number, TYPE_KEYS))

    # (3) الزبون: الكود أولًا ثم Enter — الاسم يظهر تلقائيًا (§11.2)
    ops.append(FieldOp("customer", leg.customer_code or "", TYPE_KEYS, enter=True))

    # (4) المبلغ الأجنبي قبل الخصم، فاصل الآلاف مُشال (§6.2)
    ops.append(FieldOp("foreign_amount", _num(leg.amount), TYPE_KEYS))

    # (5) نوع العملة: اختيار بالاسم كما بالقائمة — «دينار تونسي»/«جنيه مصري» (§4.1)
    if leg.currency in MONEYADO_CURRENCY_LABEL:
        ops.append(FieldOp("currency_type", MONEYADO_CURRENCY_LABEL[leg.currency], SELECT))

    # (6) السعر × الضارب = 1 دائمًا (ثابت §11.1)
    ops.append(FieldOp("rate_multiply", "1", TYPE_KEYS))

    # (7) السعر / القسمة = السعر المطبَّع (§3.6)
    ops.append(FieldOp("rate_divide", leg.price_normalized or "", TYPE_KEYS))

    # (8) نسبة العمولة = 0 دائمًا (§6.2). Enter بعدها: يفعّل خانة «العمولة» التالية —
    #     الخانات متسلسلة التفعيل، وبلا Enter تبقى «العمولة» معطّلة فتُتخطّى (§11.3).
    ops.append(FieldOp("commission_rate", "0", TYPE_KEYS, enter=True))

    # (9) العمولة = الفرق بالسالب، أو 0 حين لا فرق (§6.2). تُملأ دائمًا (0 عند None) مع Enter
    #     كي يكتمل التفعيل المتسلسل ويصل التركيز لخانة «المبلغ المخصوم» — قرار صاحب العمل.
    commission_value = _num(leg.commission) if leg.commission is not None else "0"
    ops.append(FieldOp("commission", commission_value, TYPE_KEYS, enter=True))

    # (9.5) المبلغ المخصوم من الحساب — يحسبه البرنامج تلقائيًا. 🔴 Enter دائمًا على الحقل النشط
    #       (حتى بلا عمولة): هذا الضغط هو ما يُظهر قيمة المبلغ الأجنبي في الشاشة (قرار صاحب العمل).
    #       لا كتابة ولا مسح (كي لا نُفسد القيمة المحسوبة §0): Enter فقط لتأكيدها والانتقال/تفعيل
    #       «البلد» (الخانات متسلسلة التفعيل). لا يحتاج إحداثيًا.
    ops.append(FieldOp("amount_deducted", "", ENTER_ONLY))

    # (10) البلد — ComboBox: تُختار الدولة (تونس/مصر/ليبيا) بالاسم، لا المدينة كنص (§11.1)
    country = _country_label(leg)
    if country:
        ops.append(FieldOp("country", country, SELECT))

    # (11) وسيلة الدفع = رقم الهاتف
    if leg.phone:
        ops.append(FieldOp("payment_method", leg.phone, TYPE_KEYS))

    # (12) ملاحظات = اسم المستلم إن وُجد، وإلا فارغة
    if leg.recipient_name:
        ops.append(FieldOp("notes", leg.recipient_name, TYPE_KEYS))

    return ops


def _buy_payment_code(leg: ParsedLeg) -> Optional[str]:
    """كود وسيلة الدفع لخانة «البلد» في شاشة الشراء (فودافون=17): يُطابَق على نصّ وسيلة الدفع
    المطبَّع. None إن لم تُذكر وسيلة دفع معروفة → لا تُملأ الخانة (غير حرجة)."""
    pm = normalize_ar(leg.payment_method or "")
    if not pm:
        return None
    for name, code in MONEYADO_PAYMENT_CODE.items():
        if normalize_ar(name) in pm:
            return code
    return None


def build_buy_fields(leg: ParsedLeg) -> list[FieldOp]:
    """يبني خانات شاشة «شراء عملة» بنفس منطق البيع (§11.1 آخر فقرة).

    رقم المعاملة والمبلغ الصافي يحسبهما البرنامج تلقائيًا — البوت لا يلمسهما (افتراض آمن).
    """
    ops: list[FieldOp] = []

    # (1) الحساب الأجنبي (الخزينة) → type_keys ثم Enter
    ops.append(FieldOp("foreign_account", _treasury_value(leg), TYPE_KEYS, enter=True))

    # (2) الرقم الإشاري (نفس رقم البيع للطرف المشتقّ) — يُملأ إن وُجد. رقم المعاملة يولّده البرنامج.
    if leg.reference_number:
        ops.append(FieldOp("reference_number", leg.reference_number, TYPE_KEYS))

    # (3) نوع العملة — **بالكود لا بالاسم** في شاشة الشراء (قرار صاحب العمل من الشاشة الحقيقية):
    #     4=مصري، 3=تونسي — تُكتب في خانة رمز العملة (شاشة البيع تبقى باختيار الاسم عبر SELECT).
    if leg.currency in MONEYADO_CURRENCY_CODE:
        ops.append(FieldOp("currency_type", MONEYADO_CURRENCY_CODE[leg.currency], TYPE_KEYS))

    # (4) السعر × الضارب = 1 دائمًا (ثابت §11.1) — مطابقة شاشة الشراء الفعلية (× و/)
    ops.append(FieldOp("rate_multiply", "1", TYPE_KEYS))

    # (5) السعر / القسمة = السعر المطبَّع (§3.6). Enter بعدها يفعّل «المبلغ الصافي» (تسلسل §11.3).
    ops.append(FieldOp("rate_divide", leg.price_normalized or "", TYPE_KEYS, enter=True))

    # (6) الكمية (المبلغ الأجنبي)، فاصل الآلاف مُشال
    ops.append(FieldOp("quantity", _num(leg.amount), TYPE_KEYS))

    # (7) نسبة العمولة: لا تُكتب قيمة (🔴 [750,290] = نسبة العمولة الحقيقية) — Enter فقط لتفعيل
    #     تسلسل «العمولة» ثم «المبلغ المسلّم» (خانات متسلسلة التفعيل §11.3).
    ops.append(FieldOp("commission_rate", "", TYPE_KEYS, enter=True))

    # (8) العمولة: فارغة عند None (شراء طرف sell_and_buy بلا عمولة §6) — لا تُكتب قيمة، لكن Enter
    #     يبقى ليفعّل «المبلغ المسلّم» (يحسبه البرنامج). للطرف بمورد تُكتب قيمتها.
    commission_value = _num(leg.commission) if leg.commission is not None else ""
    ops.append(FieldOp("commission", commission_value, TYPE_KEYS, enter=True))

    # (9) المبلغ المسلّم للحساب — يحسبه البرنامج تلقائيًا. Enter فقط على الحقل النشط (بلا كتابة/
    #     مسح كي لا نُفسد القيمة المحسوبة §0) — نظير «المبلغ المخصوم» في البيع. لا يحتاج إحداثيًا.
    ops.append(FieldOp("amount_delivered", "", ENTER_ONLY))

    # (10) الزبون/الحساب: يُكتب كود المورد حين وُجد طرف مورد حقيقي (buy_leg.customer_code مضبوط:
    #      طرفان بمورد «760 طه»، أو SI مع مورد مشتقّ). الطرف المشتقّ لخزينة sell_and_buy بلا مورد
    #      يترك customer_code فارغًا → **يُتخطّى** (البرنامج لا يشترط الزبون في شراء sell_and_buy §5.3).
    if leg.customer_code:
        ops.append(FieldOp("customer", leg.customer_code, TYPE_KEYS, enter=True))

    # (11) البلد — في شاشة الشراء يُعاد استخدام الحقل لكود **وسيلة الدفع** (فودافون=17، قرار
    #      صاحب العمل من الشاشة الحقيقية): يُكتب رمزًا من MONEYADO_PAYMENT_CODE حسب وسيلة دفع
    #      الحوالة، لا اسم الدولة. (شاشة البيع تبقى ببلد حقيقي عبر _country_label.)
    pay_code = _buy_payment_code(leg)
    if pay_code:
        ops.append(FieldOp("country", pay_code, TYPE_KEYS))

    # (12) وسيلة الدفع = رقم الهاتف
    if leg.phone:
        ops.append(FieldOp("payment_method", leg.phone, TYPE_KEYS))

    # (11) ملاحظات = اسم المستلم إن وُجد
    if leg.recipient_name:
        ops.append(FieldOp("notes", leg.recipient_name, TYPE_KEYS))

    return ops
