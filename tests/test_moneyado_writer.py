"""
اختبارات MoneyadoWriter — بلا pywinauto حقيقي (mock للشاشة) §11.

يغطّي:
- build_sell_fields / build_buy_fields: الترتيب، القيم (السعر×=1، نسبة العمولة=0،
  كود العملة، المبلغ بلا فاصل آلاف، طريقة type_keys للزبون/الخزينة، السعر التونسي المطبَّع).
- MoneyadoWriter.write: commit=False لا يخزّن؛ commit=True يخزّن؛ نافذة طارئة → مراجعة؛
  اسم زبون فارغ → مراجعة؛ coord=null → رفض.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.constants import Currency, OperationType, TreasuryType
from core.models import ParsedLeg, SupplierRef, TreasuryRef, WriteJob
from core.writers.moneyado.fields import (
    ENTER_ONLY,
    SELECT,
    TYPE_KEYS,
    build_buy_fields,
    build_sell_fields,
)
from core.writers.moneyado.writer import MoneyadoWriter

# كل مفاتيح الحقول الممكنة (بيع + شراء) — لبناء إعداد إحداثيات كامل في الـ mock
ALL_FIELD_KEYS = [
    "foreign_account", "reference_number", "customer", "customer_name_display",
    "foreign_amount", "currency_type", "rate_multiply", "rate_divide",
    "commission_rate", "commission", "country", "payment_method", "notes",
    "rate", "quantity", "transaction_number", "net_amount",
]


# ── تجهيزات ──────────────────────────────────────────────────────────────────
def make_sell_leg(**over) -> ParsedLeg:
    base = dict(
        operation=OperationType.SELL,
        customer_code="1208",
        customer_name="فداء شاكونه",
        amount=1600.0,
        currency=Currency.EGP,
        treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY, currency=Currency.EGP),
        reference_number="A5169",
        phone="010954227116",
        price_raw="5.84",
        price_normalized="5.84",
        payment_method="فودافون",
    )
    base.update(over)
    return ParsedLeg(**base)


def make_buy_leg(**over) -> ParsedLeg:
    base = dict(
        operation=OperationType.BUY,
        supplier=SupplierRef(code="760", name="طه"),
        amount=8391.0,
        currency=Currency.EGP,
        treasury=TreasuryRef(code="90", name="خصم 1%", type=TreasuryType.SELL_AND_BUY, currency=Currency.EGP),
        reference_number="A6779",
        phone="01115233493",
        price_raw="5.86",
        price_normalized="5.86",
    )
    base.update(over)
    return ParsedLeg(**base)


def make_job(leg: ParsedLeg) -> WriteJob:
    return WriteJob(
        job_id="job-1", deal_id="deal-1", operation=leg.operation, leg=leg,
        created_at=datetime.now(),
    )


def make_screen(coord=(1, 1), name="فداء شاكونه", unexpected=None) -> MagicMock:
    """شاشة وهمية بإعداد إحداثيات كامل (coord غير null) ما لم يُطلب خلاف ذلك."""
    coord_val = list(coord) if coord is not None else None
    screen = MagicMock()
    screen.field_config.return_value = {
        k: {"coord": coord_val, "class": "ThunderRT6TextBox"} for k in ALL_FIELD_KEYS
    }
    screen.button_config.return_value = {
        "store": {"title_re": "^تخزين$", "class": "ThunderRT6CommandButton"},
        "stop": {"title_re": "STOP|رجوع|إلغاء", "class": "ThunderRT6CommandButton"},
    }
    screen.check_unexpected_window.return_value = unexpected
    screen.read_text.return_value = name
    return screen


def make_writer(screen, *, dry_run=False, tmp_path=None) -> MoneyadoWriter:
    settings = SimpleNamespace(dry_run=dry_run, screenshot_dir=str(tmp_path or "."))
    w = MoneyadoWriter(screen=screen, settings=settings)
    w._NAME_POLL_INTERVAL = 0    # اختبارات سريعة: بلا انتظار فعلي
    w._ENTER_WAIT = 0            # مسار §11.2 المعطّل: بلا انتظار فعلي في الاختبار
    w._POST_STORE_WAIT = 0
    w._POST_STORE_CLOSE_WAIT = 0  # بلا مهلة إغلاق فعلية في الاختبار
    w._DRY_RUN_WAIT = 0          # DRY_RUN: بلا مهلة معاينة فعلية في الاختبار
    return w


def keys(ops) -> list[str]:
    return [o.key for o in ops]


def by_key(ops, key):
    return next(o for o in ops if o.key == key)


# ── اختبارات build_sell_fields ────────────────────────────────────────────────
def test_sell_fields_order():
    ops = build_sell_fields(make_sell_leg())
    assert keys(ops) == [
        "foreign_account", "reference_number", "customer", "foreign_amount",
        "currency_type", "rate_multiply", "rate_divide", "commission_rate",
        "commission", "amount_deducted", "payment_method",
    ]


def test_sell_fields_values_and_methods():
    ops = build_sell_fields(make_sell_leg())
    # الخزينة والزبون: type_keys + Enter (بحث تلقائي §11.3)
    fa = by_key(ops, "foreign_account")
    assert (fa.value, fa.method, fa.enter) == ("74", TYPE_KEYS, True)
    cust = by_key(ops, "customer")
    assert (cust.value, cust.method, cust.enter) == ("1208", TYPE_KEYS, True)
    # المبلغ بلا فاصل آلاف
    assert by_key(ops, "foreign_amount").value == "1600"
    # نوع العملة: «جنيه مصري» بالاسم، طريقة select
    ct = by_key(ops, "currency_type")
    assert (ct.value, ct.method) == ("جنيه مصري", SELECT)
    # السعر× = 1 دائمًا، نسبة العمولة = 0
    assert by_key(ops, "rate_multiply").value == "1"
    assert by_key(ops, "commission_rate").value == "0"
    # السعر/ = السعر المطبَّع (مصري كما هو)
    assert by_key(ops, "rate_divide").value == "5.84"


def test_sell_fields_thousands_separator_removed():
    ops = build_sell_fields(make_sell_leg(amount=50000.0))
    assert by_key(ops, "foreign_amount").value == "50000"
    ops2 = build_sell_fields(make_sell_leg(amount=17400.0))
    assert by_key(ops2, "foreign_amount").value == "17400"


def test_sell_fields_tunisian_currency_and_rate():
    leg = make_sell_leg(
        currency=Currency.TND, price_raw="35.75", price_normalized="0.3575", amount=2000.0,
        treasury=TreasuryRef(code="51", name="وليد تونس العاصمة", type=TreasuryType.SELL_ONLY, currency=Currency.TND),
    )
    ops = build_sell_fields(leg)
    assert by_key(ops, "currency_type").value == "دينار تونسي"   # تونسي بالاسم
    assert by_key(ops, "rate_divide").value == "0.3575"    # السعر المطبَّع (§3.6)
    assert by_key(ops, "foreign_amount").value == "2000"   # المبلغ عاديّ


def test_sell_fields_commission_negative_and_notes():
    leg = make_sell_leg(commission=-35.0, recipient_name="محمد المستلم", country="سوسة")
    ops = build_sell_fields(leg)
    comm = by_key(ops, "commission")
    assert comm.value == "-35"                             # الفرق بالسالب (§6.2)
    # Enter بعد العمولة يفعّل «المبلغ المخصوم من الحساب» (يحسبه البرنامج) قبل الانتقال
    assert comm.enter is True
    assert by_key(ops, "notes").value == "محمد المستلم"    # اسم المستلم → ملاحظات
    # المدينة «سوسة» → الدولة «تونس» عبر ComboBox (اختيار لا كتابة نص)
    c = by_key(ops, "country")
    assert (c.value, c.method) == ("تونس", SELECT)
    # الترتيب الكامل مع الخيارات
    assert keys(ops).index("commission") > keys(ops).index("commission_rate")


def test_country_maps_city_to_country_and_uses_select():
    """خانة «البلد» ComboBox: تُختار الدولة (تونس/مصر/ليبيا)، لا المدينة كنص (قرار المستخدم)."""
    from core.writers.moneyado.fields import _country_label
    # الحالة الحقيقية: «العاصمة» (تونس/العاصمة) → «تونس» بطريقة SELECT
    c = by_key(build_sell_fields(make_sell_leg(country="العاصمة", currency=Currency.TND)), "country")
    assert (c.value, c.method) == ("تونس", SELECT)
    # مدن مصرية → «مصر»؛ نص دولة صريح يتقدّم على العملة
    assert _country_label(make_sell_leg(country="القاهرة")) == "مصر"
    assert _country_label(make_sell_leg(country="ليبيا", currency=Currency.EGP)) == "ليبيا"
    # مكان مذكور لكن غير معروف → العملة تحسم الدولة
    assert _country_label(make_sell_leg(country="مكان مجهول", currency=Currency.TND)) == "تونس"
    # لا مكان مذكور → لا تُملأ الخانة (سلوك سابق محفوظ)
    assert _country_label(make_sell_leg(country=None)) is None
    assert "country" not in keys(build_sell_fields(make_sell_leg(country=None)))


def test_buy_country_holds_payment_code_not_country():
    """شاشة الشراء: حقل «البلد» يحمل كود وسيلة الدفع (فودافون=17) لا الدولة، ويُكتب type_keys."""
    c = by_key(build_buy_fields(make_buy_leg(payment_method="فودافون كاش")), "country")
    assert (c.value, c.method) == ("17", TYPE_KEYS)


def test_buy_country_skipped_when_no_known_payment():
    """بلا وسيلة دفع معروفة → لا تُملأ خانة «البلد» (غير حرجة)."""
    assert "country" not in keys(build_buy_fields(make_buy_leg(payment_method=None)))
    assert "country" not in keys(build_buy_fields(make_buy_leg(payment_method="إنستا باي")))


def test_buy_currency_is_code_not_name():
    """شاشة الشراء: نوع العملة بالكود (4=مصري، 3=تونسي) type_keys، لا بالاسم/SELECT."""
    egp = by_key(build_buy_fields(make_buy_leg(currency=Currency.EGP)), "currency_type")
    assert (egp.value, egp.method) == ("4", TYPE_KEYS)
    tnd = by_key(build_buy_fields(make_buy_leg(currency=Currency.TND)), "currency_type")
    assert (tnd.value, tnd.method) == ("3", TYPE_KEYS)


def test_buy_rate_divide_has_enter():
    """السعر / (rate_divide) بـ Enter لتفعيل «المبلغ الصافي»."""
    rd = by_key(build_buy_fields(make_buy_leg()), "rate_divide")
    assert rd.enter is True


def test_fill_presses_enter_after_value_when_enter_true():
    """screens.fill يقرأ field_op.enter ويضغط {ENTER} بعد القيمة (خانة العمولة)."""
    from core.writers.moneyado.fields import FieldOp
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0)
    ctrl = MagicMock()
    scr._control = lambda cfg, method="": ctrl        # تجاوز تحديد الحقل الفعلي (بلا pywinauto)
    scr.fill(FieldOp("commission", "-35", TYPE_KEYS, enter=True), {})
    sent = [c.args[0] for c in ctrl.type_keys.call_args_list]
    assert "-35" in sent                              # كُتبت القيمة
    assert "{ENTER}" in sent                          # ثم ضُغِط Enter
    assert sent.index("{ENTER}") > sent.index("-35")  # الترتيب: القيمة ثم Enter


def test_fill_no_enter_when_enter_false():
    """بلا enter=True لا يُضغط Enter (لا كبس زائد على الحقول العادية)."""
    from core.writers.moneyado.fields import FieldOp
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0)
    ctrl = MagicMock()
    scr._control = lambda cfg, method="": ctrl
    scr.fill(FieldOp("foreign_amount", "1600", TYPE_KEYS), {})
    sent = [c.args[0] for c in ctrl.type_keys.call_args_list]
    assert "{ENTER}" not in sent


def test_sell_commission_fieldop_carries_enter():
    """fields.py يُصدر commission_rate وcommission بـ enter=True (تفعيل متسلسل للخانات)."""
    ops = build_sell_fields(make_sell_leg(commission=-35.0))
    assert by_key(ops, "commission_rate").enter is True   # يفعّل خانة العمولة
    assert by_key(ops, "commission").enter is True         # يفعّل حساب «المخصوم» بالبرنامج
    # الترتيب: نسبة العمولة قبل العمولة (التفعيل المتسلسل)
    assert keys(ops).index("commission_rate") < keys(ops).index("commission")


def test_sell_amount_deducted_enter_only_between_commission_and_country():
    """amount_deducted: enter_only بلا قيمة، بين العمولة والبلد (يحسبه البرنامج، Enter للانتقال)."""
    from core.writers.moneyado.fields import ENTER_ONLY
    ops = build_sell_fields(make_sell_leg(commission=-35.0, country="العاصمة", currency=Currency.TND))
    ad = by_key(ops, "amount_deducted")
    assert ad.method == ENTER_ONLY
    assert ad.value == ""                     # لا قيمة تُكتب (يحسبها البرنامج §0)
    k = keys(ops)
    assert k.index("commission") < k.index("amount_deducted") < k.index("country")


def test_sell_amount_deducted_enter_always_even_without_commission():
    """المبلغ المخصوم: تمريرة enter_only تُضاف دائمًا (حتى بلا عمولة) — Enter يُظهر المبلغ الأجنبي."""
    from core.writers.moneyado.fields import ENTER_ONLY
    ad = by_key(build_sell_fields(make_sell_leg(commission=None)), "amount_deducted")
    assert ad.method == ENTER_ONLY
    assert ad.value == ""


def test_press_enter_on_active_sends_enter_via_form_window():
    """press_enter_on_active يضغط Enter على العنصر النشط عبر نافذة الفورم، بلا تحديد بإحداثي/مسح."""
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0)
    win = MagicMock()
    scr._window = win
    scr._control = MagicMock(side_effect=AssertionError("press_enter_on_active يجب ألا يحدّد حقلاً"))
    scr.press_enter_on_active()
    scr._control.assert_not_called()          # لا تحديد حقل بإحداثي
    win.type_keys.assert_called_once()        # Enter واحد على النافذة (الحقل النشط)
    assert win.type_keys.call_args.args[0] == "{ENTER}"


def test_confirm_store_on_main_presses_enter_on_top_window():
    """confirm_store_on_main يضغط Enter على النافذة الرئيسية للتطبيق (top_window) لا على الفورم."""
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0)
    app = MagicMock()
    top = MagicMock()
    app.top_window.return_value = top
    scr._app = app
    scr.confirm_store_on_main()
    app.top_window.assert_called_once()
    top.type_keys.assert_called_once()
    assert top.type_keys.call_args.args[0] == "{ENTER}"


def test_confirm_store_on_main_swallows_errors_after_store():
    """فشل Enter على النافذة الرئيسية لا يرمي (الحفظ تمّ فعلاً) — best-effort مسجَّل (T5)."""
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0)
    app = MagicMock()
    app.top_window.side_effect = RuntimeError("no top window")
    scr._app = app
    scr.confirm_store_on_main()   # لا يرمي


async def test_write_sell_commission_calls_press_enter_on_active(tmp_path):
    """التدفّق: عمولة → البوت يضغط Enter على «المخصوم» عبر press_enter_on_active (لا fill، لا رفض)."""
    screen = make_screen()                    # field_config لا يحوي amount_deducted (بلا إحداثي)
    writer = make_writer(screen, tmp_path=tmp_path)
    leg = make_sell_leg(commission=-35.0, country="العاصمة", currency=Currency.TND)
    res = await writer.write(make_job(leg), commit=True)
    assert res.ok is True                     # لم تُرفض رغم غياب إحداثي amount_deducted
    screen.press_enter_on_active.assert_called_once()   # ضُغط Enter على «المخصوم» فعلاً
    filled_keys = [c.args[0].key for c in screen.fill.call_args_list]
    assert "amount_deducted" not in filled_keys         # لم يمرّ عبر fill (لا تحديد بإحداثي)


def test_sell_fields_commission_zero_with_enter_when_none():
    """بلا عمولة → تُملأ خانة العمولة بـ 0 مع Enter (يكمل التفعيل المتسلسل لخانة «المخصوم»)."""
    comm = by_key(build_sell_fields(make_sell_leg(commission=None)), "commission")
    assert comm.value == "0"
    assert comm.enter is True


# ── اختبارات build_buy_fields ─────────────────────────────────────────────────
def test_buy_fields_order_and_supplier():
    ops = build_buy_fields(make_buy_leg())
    # يطابق شاشة الشراء الفعلية: الرقم الإشاري، × و/ للسعر، نسبة العمولة+العمولة، ثم «المبلغ المسلّم»
    assert keys(ops) == [
        "foreign_account", "reference_number", "currency_type", "rate_multiply",
        "rate_divide", "quantity", "commission_rate", "commission", "amount_delivered",
        "customer", "payment_method",
    ]
    # رقم المعاملة/المبلغ الصافي يحسبهما البرنامج → لا يُلمسان
    assert "transaction_number" not in keys(ops)
    assert "net_amount" not in keys(ops)
    # الرقم الإشاري (نفس رقم البيع)
    assert by_key(ops, "reference_number").value == "A6779"
    # المورد في خانة الزبون: كوده + Enter (أولوية المورد)
    cust = by_key(ops, "customer")
    assert (cust.value, cust.method, cust.enter) == ("760", TYPE_KEYS, True)
    # السعر ×=1 و/=المطبَّع (بدل مفتاح «rate» الواحد الذي كان يُرفض — لا وجود له في الإعداد)
    assert by_key(ops, "rate_multiply").value == "1"
    assert by_key(ops, "rate_divide").value == "5.86"
    assert by_key(ops, "quantity").value == "8391"
    assert by_key(ops, "currency_type").value == "4"     # كود العملة (مصري) لا الاسم


def test_buy_fields_commission_sequence_and_delivered():
    """نسبة العمولة فارغة(+Enter) ثم العمولة فارغة عند None(+Enter) ثم «المبلغ المسلّم» enter_only."""
    ops = build_buy_fields(make_buy_leg())               # make_buy_leg بلا عمولة → فارغة
    cr = by_key(ops, "commission_rate")
    assert (cr.value, cr.enter) == ("", True)             # لا تُكتب قيمة، Enter فقط لتفعيل التسلسل
    comm = by_key(ops, "commission")
    assert (comm.value, comm.enter) == ("", True)         # بلا عمولة → فارغة (لا تُكتب) لكن Enter
    ad = by_key(ops, "amount_delivered")
    assert (ad.method, ad.value) == (ENTER_ONLY, "")      # يحسبه البرنامج، Enter فقط
    k = keys(ops)
    assert k.index("commission_rate") < k.index("commission") < k.index("amount_delivered") < k.index("customer")


def test_buy_fields_commission_value_when_present():
    """لو للطرف عمولة (مسار الطرفين بمورد) تُكتب قيمتها لا فارغة."""
    comm = by_key(build_buy_fields(make_buy_leg(commission=-12.0)), "commission")
    assert comm.value == "-12"


def test_buy_fields_customer_skipped_for_synthesized_sell_and_buy():
    """الطرف المشتقّ (sell_and_buy بلا مورد): خانة الزبون تُتخطّى (البرنامج لا يشترطها)."""
    leg = make_buy_leg(supplier=None)     # خزينة make_buy_leg = «خصم 1%» sell_and_buy، بلا مورد
    assert "customer" not in keys(build_buy_fields(leg))


def test_buy_fields_customer_kept_for_supplier_leg():
    """الطرف بمورد: خانة الزبون = كود المورد + Enter (يبقى كما هو)."""
    cust = by_key(build_buy_fields(make_buy_leg()), "customer")   # make_buy_leg فيه مورد 760
    assert (cust.value, cust.enter) == ("760", True)


# ── اختبارات MoneyadoWriter.write ─────────────────────────────────────────────
@pytest.mark.asyncio
async def test_write_commit_false_does_not_store(tmp_path):
    screen = make_screen()
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=False)
    assert result.ok is True
    assert result.needs_review is False
    screen.press_store.assert_not_called()          # لا «تخزين»
    screen.press_stop.assert_called_once()          # STOP آمن بعد التعبئة


@pytest.mark.asyncio
async def test_write_commit_true_stores(tmp_path):
    screen = make_screen()
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is True
    assert result.needs_review is False
    screen.press_store.assert_called_once()         # «تخزين»
    screen.press_stop.assert_not_called()
    # عُبّئت كل الخانات المطلوبة بالترتيب (عدا enter_only مثل «المخصوم» — يمرّ عبر Enter لا fill)
    expected_fills = sum(1 for o in build_sell_fields(make_sell_leg()) if o.method != ENTER_ONLY)
    assert screen.fill.call_count == expected_fills


@pytest.mark.asyncio
async def test_write_dry_run_fills_no_store_no_stop(tmp_path):
    # DRY_RUN: تُعبّأ الشاشة وتبقى مفتوحة للمعاينة — لا «تخزين» ولا «خروج» (حتى مع Kill Switch ON)
    screen = make_screen()
    writer = make_writer(screen, dry_run=True, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is True
    assert result.dry_run is True                   # علامة DRY_RUN للأنبوب (✅ لاحقًا)
    expected_fills = sum(1 for o in build_sell_fields(make_sell_leg()) if o.method != ENTER_ONLY)
    assert screen.fill.call_count == expected_fills  # عُبّئت الحقول (عدا enter_only «المخصوم»)
    screen.press_store.assert_not_called()          # لا «تخزين»
    screen.press_stop.assert_not_called()           # لا «خروج» — الشاشة تبقى مفتوحة


@pytest.mark.asyncio
async def test_write_unexpected_window_needs_review(tmp_path):
    screen = make_screen(unexpected="معاينة الطباعة")
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is False
    assert result.needs_review is True
    assert result.screenshot_path is not None       # لقطة شاشة (dead-letter §11.3)
    assert "معاينة" in result.error
    screen.press_store.assert_not_called()          # لا تخزين عند الطارئ
    screen.press_stop.assert_called()               # STOP آمن
    screen.screenshot.assert_called_once()


@pytest.mark.asyncio
async def test_write_empty_customer_name_warns_and_continues(tmp_path):
    # الاسم لم يظهر بعد الكود+Enter (بحث فشل/بطيء) → تحذير ومتابعة (الاسم للتحقّق فقط)، يُخزَّن
    screen = make_screen(name="")
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is True and result.needs_review is False
    screen.press_store.assert_called_once()         # لم يُعطَّل الإدخال
    assert screen.read_text.call_count >= 2          # جرّب القراءة عدة مرات (retry)


@pytest.mark.asyncio
async def test_write_customer_name_appears_after_retry(tmp_path):
    # الاسم يظهر بعد قراءتين فارغتين (بحث تلقائي بطيء) → يُقرأ ويُقبل ثم يُخزَّن
    screen = make_screen()
    screen.read_text.side_effect = ["", "", "فداء شاكونه"]
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg(customer_name="فداء شاكونه")), commit=True)
    assert result.ok is True and result.needs_review is False
    assert screen.read_text.call_count == 3          # فارغ، فارغ، ثم ظهر
    screen.press_store.assert_called_once()


@pytest.mark.asyncio
async def test_write_post_store_popup_stops_and_reviews(tmp_path):
    # نافذة تظهر بعد «تخزين» (رصيد/خطأ) → إغلاق آمن + تصعيد للمراجعة (لا نجاح كاذب §11.3)
    screen = make_screen()
    stored = {"done": False}
    screen.press_store.side_effect = lambda op: stored.__setitem__("done", True)
    # النافذة الطارئة تظهر فقط بعد التخزين (None أثناء التعبئة)
    screen.check_unexpected_window.side_effect = lambda: ("نافذة رصيد" if stored["done"] else None)
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is False and result.needs_review is True
    assert "تخزين" in result.error and "رصيد" in result.error
    screen.press_store.assert_called_once()
    screen.press_stop.assert_called()               # أُغلقت النافذة
    screen.confirm_store_on_main.assert_not_called()  # نافذة طارئة → لا Enter تأكيد أعمى


@pytest.mark.asyncio
async def test_write_no_post_store_popup_stores_ok(tmp_path):
    # لا نافذة بعد التخزين → نجاح عاديّ (check_unexpected_window=None)
    screen = make_screen()
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is True and result.needs_review is False
    screen.press_store.assert_called_once()


@pytest.mark.asyncio
async def test_write_confirms_store_on_main_after_store(tmp_path):
    # بعد «تخزين» بلا نافذة طارئة → Enter على النافذة الرئيسية لإغلاق التأكيد والعودة للقائمة (§11.3)
    screen = make_screen()
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is True
    screen.press_store.assert_called_once()
    screen.confirm_store_on_main.assert_called_once()   # أُغلقت الشاشة بعد الحفظ


@pytest.mark.asyncio
async def test_write_buy_confirms_store_on_main(tmp_path):
    # نفس السلوك على شاشة الشراء (طرف ثانٍ): Enter على النافذة الرئيسية بعد «تخزين»
    screen = make_screen(name=None)   # الشراء لا يقرأ اسم زبون
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_buy_leg()), commit=True)
    assert result.ok is True
    screen.press_store.assert_called_once()
    screen.confirm_store_on_main.assert_called_once()


@pytest.mark.asyncio
async def test_dry_run_does_not_confirm_store_on_main(tmp_path):
    # DRY_RUN: بلا «تخزين» → بلا Enter تأكيد على النافذة الرئيسية (الشاشة تبقى مفتوحة للمعاينة)
    screen = make_screen()
    writer = make_writer(screen, dry_run=True, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.dry_run is True
    screen.press_store.assert_not_called()
    screen.confirm_store_on_main.assert_not_called()


@pytest.mark.asyncio
async def test_write_weird_customer_name_needs_review(tmp_path):
    # اسم مختلف تمامًا عن المتوقّع (ليس خطأ إملاء) → ⚠️
    screen = make_screen(name="عبدالله البعيد المختلف")
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg(customer_name="فداء شاكونه")), commit=True)
    assert result.ok is False and result.needs_review is True
    screen.press_store.assert_not_called()


@pytest.mark.asyncio
async def test_write_spelling_variant_name_passes(tmp_path):
    # تسامح مع خطأ الإملاء (§8.2): «الشاوش» ↔ «الشتوش» → يكمل
    screen = make_screen(name="احمد الشتوش")
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(
        make_job(make_sell_leg(customer_name="احمد الشاوش")), commit=True
    )
    assert result.ok is True and result.needs_review is False
    screen.press_store.assert_called_once()


@pytest.mark.asyncio
async def test_write_null_coord_rejected(tmp_path):
    screen = make_screen(coord=None)                # إحداثي الحقل غير مستكمل (coord=null)
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is False
    assert result.needs_review is True
    assert "coord" in result.error
    screen.connect.assert_not_called()              # لا نتصل أصلًا قبل استكمال الحقول
    screen.press_store.assert_not_called()


def make_screen_tab(name="فداء شاكونه", cndisplay_locatable=False) -> MagicMock:
    """شاشة تُحدَّد حقولها بترتيب Tab (coord=null + tab_index) — لاختبار قبول الكاتب لها."""
    screen = MagicMock()
    fields = {k: {"coord": None, "tab_index": i, "class": "ThunderRT6TextBox"}
              for i, k in enumerate(ALL_FIELD_KEYS)}
    # اسم الزبون الظاهر: بلا coord/tab_index (§11.2 معطّل) إلا إن طُلب تحديده
    fields["customer_name_display"] = (
        {"coord": None, "tab_index": 99, "class": "ThunderRT6TextBox"}
        if cndisplay_locatable else {"coord": None, "class": "ThunderRT6TextBox"}
    )
    screen.field_config.return_value = fields
    screen.button_config.return_value = {
        "store": {"title_re": "^تخزين$", "class": "ThunderRT6CommandButton"},
        "stop": {"title_re": "STOP|رجوع|إلغاء", "class": "ThunderRT6CommandButton"},
    }
    screen.check_unexpected_window.return_value = None
    screen.read_text.return_value = name
    return screen


@pytest.mark.asyncio
async def test_write_accepts_tab_index_fields_without_coord(tmp_path):
    # tab_index يكفي للتحديد ولو coord=null → لا يُرفض، يُخزَّن (§11.3 ملاحة Tab)
    screen = make_screen_tab()
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is True and result.needs_review is False
    screen.press_store.assert_called_once()


@pytest.mark.asyncio
async def test_write_skips_name_check_when_display_not_locatable(tmp_path):
    # customer_name_display بلا coord/tab_index → §11.2 معطّل: لا قراءة، لا رفض، يُخزَّن
    screen = make_screen_tab()
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg(customer_name="فداء شاكونه")), commit=True)
    assert result.ok is True
    screen.read_text.assert_not_called()            # لم يُقرأ اسم الزبون (تحقّق معطّل)
    screen.press_store.assert_called_once()


@pytest.mark.asyncio
async def test_write_reads_name_when_display_locatable(tmp_path):
    # customer_name_display محدَّد → يُقرأ الاسم ويُتحقّق: اسم مختلف تمامًا → مراجعة (§0)
    screen = make_screen_tab(cndisplay_locatable=True, name="عبدالله البعيد المختلف")
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg(customer_name="فداء شاكونه")), commit=True)
    assert result.ok is False and result.needs_review is True
    screen.read_text.assert_called()
    screen.press_store.assert_not_called()


@pytest.mark.asyncio
async def test_write_buy_leg_stores(tmp_path):
    screen = make_screen()
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_buy_leg()), commit=True)
    assert result.ok is True
    screen.press_store.assert_called_once()
    # لا قراءة اسم زبون في شاشة الشراء (لا customer_name_display)
    screen.read_text.assert_not_called()
