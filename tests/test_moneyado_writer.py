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
        customer_code="760",                  # كود المورد كحساب (طرفان بمورد «760 طه»)
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


def test_fill_non_critical_fields_swallow_errors():
    """«البلد» و«وسيلة الدفع» (الهاتف) غير حرجين: تعذّر إدخالهما لا يرمي (لا يُبطل الحفظ §11.1)؛
    الحقول الحرجة (الحساب) ترمي."""
    from core.writers.moneyado.fields import FieldOp
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0)
    scr._control = MagicMock(side_effect=RuntimeError("ElementNotEnabled"))
    scr.fill(FieldOp("country", "17", TYPE_KEYS), {})                       # لا يرمي
    scr.fill(FieldOp("payment_method", "01037354643", TYPE_KEYS, enter=True), {})  # لا يرمي
    with pytest.raises(RuntimeError):
        scr.fill(FieldOp("foreign_account", "85", TYPE_KEYS), {})           # حرج → يرمي


def test_fill_settle_wait_after_rate_divide(monkeypatch):
    """rate_divide خانة حاسبة: مهلة enter_wait إضافية بعد Enter كي يحسب البرنامج «المبلغ الصافي»."""
    from core.writers.moneyado.fields import FieldOp
    from core.writers.moneyado import screens as screens_mod
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0, enter_wait=5.0)
    scr._control = lambda cfg, method="": MagicMock()
    slept = []
    monkeypatch.setattr(screens_mod.time, "sleep", lambda s: slept.append(s))
    scr.fill(FieldOp("rate_divide", "5.72", TYPE_KEYS, enter=True), {})
    assert 5.0 in slept                        # طُبِّقت مهلة الحساب
    slept.clear()
    scr.fill(FieldOp("quantity", "20271", TYPE_KEYS, enter=True), {})       # خانة عادية
    assert 5.0 not in slept                    # لا مهلة حساب لغير rate_divide


def test_fill_extra_step_delay_before_store(monkeypatch):
    """الهاتف والملاحظات (آخر الحقول) → مهلة step_delay إضافية لتثبيت القيمة قبل «تخزين»."""
    from core.writers.moneyado.fields import FieldOp
    from core.writers.moneyado import screens as screens_mod
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0.3)
    scr._control = lambda cfg, method="": MagicMock()
    slept = []
    monkeypatch.setattr(screens_mod.time, "sleep", lambda s: slept.append(s))
    for key in ("payment_method", "notes"):
        slept.clear()
        scr.fill(FieldOp(key, "x", TYPE_KEYS, enter=True), {})
        assert slept.count(0.3) == 2, f"{key}: متوقّع مهلتَي step_delay (عادية + إضافية)"
    # خانة عادية → مهلة step_delay واحدة فقط
    slept.clear()
    scr.fill(FieldOp("customer", "760", TYPE_KEYS, enter=True), {})
    assert slept.count(0.3) == 1


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


def test_connect_app_rejects_multiple_instances():
    """تعدّد نسخ stock.exe → RuntimeError واضح (لا اتصال عشوائي §0)، بلا استدعاء connect."""
    from core.constants import OperationType
    from core.writers.moneyado import screens as screens_mod
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"sell_screen": {}, "buy_screen": {}})
    scr._list_stock_pids = MagicMock(return_value=[111, 222, 333])
    orig = screens_mod.Application
    screens_mod.Application = MagicMock()
    try:
        with pytest.raises(RuntimeError, match="التباس"):
            scr._connect_app(OperationType.BUY)
        screens_mod.Application.assert_not_called()      # لم نتصل بأي نسخة
    finally:
        screens_mod.Application = orig


def test_connect_app_errors_when_no_instance():
    """صفر نسخة → RuntimeError «شغّل MONEYADO»."""
    from core.constants import OperationType
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"sell_screen": {}, "buy_screen": {}})
    scr._list_stock_pids = MagicMock(return_value=[])
    with pytest.raises(RuntimeError, match="لا نسخة"):
        scr._connect_app(OperationType.BUY)


def test_connect_app_single_instance_connects_and_pins():
    """بلا PID مثبّت + نسخة واحدة → اتصال بها ويُحدَّث self._pid منها."""
    from core.constants import OperationType
    from core.writers.moneyado import screens as screens_mod
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"sell_screen": {}, "buy_screen": {}})
    scr._list_stock_pids = MagicMock(return_value=[4242])
    orig = screens_mod.Application
    app_ctor = MagicMock()
    screens_mod.Application = app_ctor
    try:
        scr._connect_app(OperationType.BUY)
        assert app_ctor.return_value.connect.call_args.kwargs["process"] == 4242
        assert scr._pid == 4242                          # ثُبِّت على المكتشَفة
    finally:
        screens_mod.Application = orig


def test_connect_app_pinned_pid_used_when_alive():
    """MONEYADO_PID مثبّت **وحيّ** → يُستخدم (يفضّ الالتباس حتى مع وجود نسخة أخرى)."""
    from core.constants import OperationType
    from core.writers.moneyado import screens as screens_mod
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"sell_screen": {}, "buy_screen": {}}, pid=9999)
    scr._list_stock_pids = MagicMock(return_value=[9999, 111])   # 9999 حيّة (مع نسخة أخرى)
    orig = screens_mod.Application
    app_ctor = MagicMock()
    screens_mod.Application = app_ctor
    try:
        scr._connect_app(OperationType.SELL)
        assert app_ctor.return_value.connect.call_args.kwargs["process"] == 9999
    finally:
        screens_mod.Application = orig


def test_connect_app_pinned_pid_dead_falls_back_to_single():
    """MONEYADO_PID مثبّت لكنه مات → بحث تلقائي؛ نسخة واحدة عاملة → تُستخدم ويُحدَّث self._pid."""
    from core.constants import OperationType
    from core.writers.moneyado import screens as screens_mod
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"sell_screen": {}, "buy_screen": {}}, pid=9999)
    scr._list_stock_pids = MagicMock(return_value=[5555])        # 9999 ماتت، الحيّة 5555
    orig = screens_mod.Application
    app_ctor = MagicMock()
    screens_mod.Application = app_ctor
    try:
        scr._connect_app(OperationType.BUY)
        assert app_ctor.return_value.connect.call_args.kwargs["process"] == 5555
        assert scr._pid == 5555                          # حُدِّث تلقائيًا من المكتشَفة
    finally:
        screens_mod.Application = orig


def test_connect_app_pinned_pid_dead_multiple_raises():
    """MONEYADO_PID مثبّت لكنه مات + أكثر من نسخة عاملة → RuntimeError «حدّد MONEYADO_PID»."""
    from core.constants import OperationType
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"sell_screen": {}, "buy_screen": {}}, pid=9999)
    scr._list_stock_pids = MagicMock(return_value=[111, 222])    # 9999 ماتت، نسختان
    with pytest.raises(RuntimeError, match="التباس"):
        scr._connect_app(OperationType.BUY)


def test_confirm_store_on_main_presses_enter_on_top_window():
    """confirm_store_on_main يضغط Enter على النافذة الرئيسية للتطبيق (top_window) لا على الفورم."""
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0)
    app = MagicMock()
    top = MagicMock()
    app.top_window.return_value = top
    scr._app = app
    scr.is_main_screen = MagicMock(return_value=True)   # الشاشة أُغلقت بعد Enter
    scr.confirm_store_on_main()
    top.type_keys.assert_called_once()
    assert top.type_keys.call_args.args[0] == "{ENTER}"


def test_confirm_store_on_main_retries_until_screen_closed():
    """يعيد Enter حتى تُغلَق شاشة العملية (is_main_screen) قبل المتابعة (§11.3)."""
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0)
    app, top = MagicMock(), MagicMock()
    app.top_window.return_value = top
    scr._app = app
    scr.is_main_screen = MagicMock(side_effect=[False, True])   # ما زالت مفتوحة ثم أُغلقت
    scr.confirm_store_on_main()
    assert top.type_keys.call_count == 2                        # ضُغط Enter مرّتين حتى الإغلاق


def test_bind_form_pins_concrete_window_and_recaptures_form_rect():
    """_bind_form يثبّت self._window على كائن wait() الملموس ويعيد التقاط form_rect منه (لا spec كسول)."""
    from core.constants import OperationType
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"buy_screen": {"form_class": "ThunderRT6FormDC"}}, connect_wait=0)
    app, spec, concrete = MagicMock(), MagicMock(), MagicMock()
    concrete.rectangle.return_value = "BUY_RECT"
    concrete.class_name.return_value = "ThunderRT6FormDC"   # الفورم الصحيح
    app.window.return_value = spec
    spec.wait.return_value = concrete                 # wait() يُرجع الكائن الملموس
    scr._app = app
    scr._bind_form(OperationType.BUY)
    assert scr._window is concrete                    # مثبّت على الملموس لا الـ WindowSpecification
    assert scr._form_rect == "BUY_RECT"               # form_rect من الفورم المربوط الحالي
    spec.rectangle.assert_not_called()                # لم يُلتقط rect من الـ spec الكسول


def test_bind_form_rejects_wrong_window_class():
    """الكائن المربوط ليس ThunderRT6FormDC → RuntimeError «فورم خاطئ» (§0)."""
    from core.constants import OperationType
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"buy_screen": {"form_class": "ThunderRT6FormDC"}}, connect_wait=0)
    app, spec, wrong = MagicMock(), MagicMock(), MagicMock()
    wrong.class_name.return_value = "SomeOtherDialog"
    app.window.return_value = spec
    spec.wait.return_value = wrong
    scr._app = app
    with pytest.raises(RuntimeError, match="فورم خاطئ"):
        scr._bind_form(OperationType.BUY)


def test_button_matches_title_among_descendants():
    """_button يعدّد أزرار الصنف ويطابق العنوان بالـ regex يدويًا (descendants تتجاهل title_re)."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({})
    win = MagicMock()
    store, back, other = _fake_button("تخزين"), _fake_button("رجوع"), _fake_button("ايصال صرف")
    win.descendants.return_value = [other, back, store]     # كما لو تجاهلت title_re
    scr._window = win
    btn = scr._button({"title_re": "^تخزين$", "class": "ThunderRT6CommandButton"})
    assert btn is store                                     # طُوبق «تخزين» بالضبط
    assert win.descendants.call_args.kwargs["class_name"] == "ThunderRT6CommandButton"


def test_button_missing_raises():
    """لا زر يطابق العنوان → RuntimeError صريح (لا child_window على wrapper)."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({})
    win = MagicMock()
    win.descendants.return_value = [_fake_button("رجوع")]
    scr._window = win
    with pytest.raises(RuntimeError, match="زر غير موجود"):
        scr._button({"title_re": "^تخزين$", "class": "ThunderRT6CommandButton"})


def test_confirm_store_on_main_swallows_errors_after_store():
    """فشل Enter على النافذة الرئيسية لا يرمي (الحفظ تمّ فعلاً) — best-effort مسجَّل (T5)."""
    from core.writers.moneyado.screens import MoneyadoScreen
    scr = MoneyadoScreen({}, step_delay=0)
    app = MagicMock()
    app.top_window.side_effect = RuntimeError("no top window")
    scr._app = app
    scr.is_main_screen = MagicMock(return_value=True)   # نعتبرها أُغلقت (نتفادى حلقة طويلة)
    scr.confirm_store_on_main()   # لا يرمي


# ── فتح شاشة العملية من القائمة الرئيسية (§11.3) ─────────────────────────────
def _visible_form(visible=True) -> MagicMock:
    w = MagicMock()
    w.is_visible.return_value = visible
    return w


def test_is_main_screen_true_when_no_form_open():
    """القائمة الرئيسية = لا فورم ThunderRT6FormDC مرئي مفتوح."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0)
    app = MagicMock()
    app.windows.return_value = []                     # لا فورم مفتوح
    scr._app = app
    assert scr.is_main_screen() is True
    # فورم مرئي مفتوح → ليست القائمة الرئيسية
    app.windows.return_value = [_visible_form(True)]
    assert scr.is_main_screen() is False
    # فورم موجود لكنه غير مرئي → ما زلنا على القائمة الرئيسية
    app.windows.return_value = [_visible_form(False)]
    assert scr.is_main_screen() is True


def _fake_button(text: str) -> MagicMock:
    b = MagicMock()
    b.window_text.return_value = text
    return b


def test_click_button_matches_by_text_among_enumerated_buttons():
    """click_button يعدّد الأزرار ويطابق بالنص (بعد تطبيع خفيف) ثم يضغط الزر الصحيح."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0)
    app, top = MagicMock(), MagicMock()
    sell_btn, buy_btn = _fake_button("بيع عملة "), _fake_button("شراء عملة")  # مسافة زائدة مقصودة
    app.top_window.return_value = top
    top.descendants.return_value = [buy_btn, sell_btn]
    scr._app = app
    scr.click_button("بيع عملة")
    sell_btn.click.assert_called_once()             # طُوبق رغم المسافة الزائدة (تطبيع)
    buy_btn.click.assert_not_called()


def test_click_button_missing_lists_available_buttons():
    """زر غير موجود → RuntimeError صريح يسرد الأزرار المتاحة (لا timeout غامض §11.3)."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0)
    app, top = MagicMock(), MagicMock()
    app.top_window.return_value = top
    top.descendants.return_value = [_fake_button("بيع"), _fake_button("شراء")]   # النص الفعلي مختصر
    scr._app = app
    with pytest.raises(RuntimeError, match="غير موجود"):
        scr.click_button("بيع عملة")
    # رسالة الخطأ تسرد المتاح للمعايرة
    try:
        scr.click_button("بيع عملة")
    except RuntimeError as exc:
        assert "بيع" in str(exc) and "شراء" in str(exc)


def test_click_button_uses_configured_button_class():
    """صنف زر القائمة الرئيسية قابل للضبط عبر config["main_menu"]["button_class"]."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"main_menu": {"button_class": "CustomBtn"}}, step_delay=0)
    app, top = MagicMock(), MagicMock()
    app.top_window.return_value = top
    top.descendants.return_value = [_fake_button("بيع عملة")]
    scr._app = app
    scr.click_button("بيع عملة")
    assert top.descendants.call_args.kwargs["class_name"] == "CustomBtn"


def test_open_sell_screen_checks_main_clicks_then_binds():
    """open_sell_screen: يتصل → يتأكّد القائمة → يضغط «بيع عملة» بالنص → يربط الفورم — بالترتيب."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0)
    calls = []
    scr._connect_app = MagicMock(side_effect=lambda op: calls.append("connect"))
    scr._form_open_and_visible = MagicMock(return_value=False)   # لا فورم مفتوح مسبقًا
    scr.is_main_screen = MagicMock(side_effect=lambda: calls.append("is_main") or True)
    scr.click_button = MagicMock(side_effect=lambda label: calls.append(f"click:{label}"))
    scr._wait_form_open = MagicMock(return_value=True)           # الفورم ظهر بعد الضغطة الأولى
    scr._bind_form = MagicMock(side_effect=lambda op, **kw: calls.append("bind"))
    scr.open_sell_screen()
    assert calls == ["connect", "is_main", "click:بيع عملة", "bind"]


def test_open_screen_uses_already_open_form_directly():
    """فورم العملية مفتوح ومرئي مسبقًا → يُربَط مباشرة بلا ضغط زر ولا فحص القائمة (§11.3)."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0)
    scr._connect_app = MagicMock()
    scr._form_open_and_visible = MagicMock(return_value=True)    # الفورم مفتوح مسبقًا
    scr.is_main_screen = MagicMock()
    scr.click_button = MagicMock()
    scr._bind_form = MagicMock()
    scr.open_sell_screen()
    scr._bind_form.assert_called_once()             # رُبط الفورم مباشرة
    scr.click_button.assert_not_called()            # بلا ضغط زر
    scr.is_main_screen.assert_not_called()          # بلا فحص القائمة (قصّرنا مباشرة)


def test_open_buy_screen_uses_buy_label():
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0)
    scr._connect_app = MagicMock()
    scr._form_open_and_visible = MagicMock(return_value=False)
    scr.is_main_screen = MagicMock(return_value=True)
    scr.click_button = MagicMock()
    scr._wait_form_open = MagicMock(return_value=True)
    scr._bind_form = MagicMock()
    scr.open_buy_screen()
    scr.click_button.assert_called_once_with("شراء عملة")


def test_form_open_and_visible_matches_operation_marker():
    """يُميّز فورم البيع (ايصال قبض) من الشراء (ايصال صرف) — لا يُعامَل بيع متبقٍّ كشاشة شراء."""
    from core.constants import OperationType
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"sell_screen": {}, "buy_screen": {}})
    app = MagicMock()
    scr._app = app

    sell_form = MagicMock()
    sell_form.is_visible.return_value = True
    sell_form.descendants.return_value = [_fake_button("ايصال قبض"), _fake_button("تخزين"), _fake_button("رجوع")]
    app.windows.return_value = [sell_form]
    assert scr._form_open_and_visible(OperationType.SELL) is True    # فيه «ايصال قبض»
    assert scr._form_open_and_visible(OperationType.BUY) is False    # لا «ايصال صرف»

    buy_form = MagicMock()
    buy_form.is_visible.return_value = True
    buy_form.descendants.return_value = [_fake_button("ايصال صرف"), _fake_button("تخزين"), _fake_button("رجوع")]
    app.windows.return_value = [buy_form]
    assert scr._form_open_and_visible(OperationType.BUY) is True
    assert scr._form_open_and_visible(OperationType.SELL) is False


def test_close_stray_forms_clicks_back_until_gone():
    """يغلق الفورم المتبقّي بـ«رجوع» حتى يختفي (يعود للقائمة قبل فتح العملية التالية)."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0)
    app = MagicMock()
    form, back = MagicMock(), _fake_button("رجوع")
    form.is_visible.return_value = True
    form.descendants.return_value = [back, _fake_button("تخزين")]
    app.windows.side_effect = [[form], []]   # بعد النقر: لا فورم
    scr._app = app
    scr._close_stray_forms()
    back.click.assert_called_once()


def test_open_buy_closes_stray_sell_form_then_clicks():
    """فورم بيع متبقٍّ عند فتح الشراء → يُغلق (رجوع) ثم يُضغط «شراء عملة» (لا يُعاد استخدام البيع)."""
    from core.constants import OperationType
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0)
    scr._connect_app = MagicMock()
    scr._form_open_and_visible = MagicMock(return_value=False)   # لا فورم شراء مطابق
    # القائمة محجوبة أولًا (فورم بيع متبقٍّ) ثم تُصبح متاحة بعد الإغلاق
    scr.is_main_screen = MagicMock(side_effect=[False, True])
    scr._close_stray_forms = MagicMock()
    scr.click_button = MagicMock()
    scr._wait_form_open = MagicMock(return_value=True)
    scr._bind_form = MagicMock()
    scr.open_buy_screen()
    scr._close_stray_forms.assert_called_once()          # أُغلق فورم البيع المتبقّي
    scr.click_button.assert_called_once_with("شراء عملة")  # ثم ضُغط «شراء عملة»
    scr._bind_form.assert_called_once()


def test_open_screen_raises_when_not_main():
    """لا نفتح فورمًا فوق فورم مفتوح (§0): is_main_screen=False → استثناء صريح، بلا ضغط زر."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0)
    scr._connect_app = MagicMock()
    scr._form_open_and_visible = MagicMock(return_value=False)
    scr.is_main_screen = MagicMock(return_value=False)   # يبقى محجوبًا حتى بعد محاولة الإغلاق
    scr._close_stray_forms = MagicMock()
    scr.click_button = MagicMock()
    scr._bind_form = MagicMock()
    with pytest.raises(RuntimeError, match="القائمة الرئيسية"):
        scr.open_sell_screen()
    scr._close_stray_forms.assert_called_once()          # حاول إغلاق الفورم المتبقّي أولًا
    scr.click_button.assert_not_called()
    scr._bind_form.assert_not_called()


def test_open_screen_uses_configured_labels():
    """نصوص الأزرار قابلة للضبط عبر config["main_menu"] (اختلاف النص على الجهاز §11.3)."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({"main_menu": {"sell_button": "بيع"}}, step_delay=0)
    scr._connect_app = MagicMock()
    scr._form_open_and_visible = MagicMock(return_value=False)
    scr.is_main_screen = MagicMock(return_value=True)
    scr.click_button = MagicMock()
    scr._wait_form_open = MagicMock(return_value=True)
    scr._bind_form = MagicMock()
    scr.open_sell_screen()
    scr.click_button.assert_called_once_with("بيع")


def test_open_screen_retries_when_click_swallowed():
    """نقر VB6 مبتلَع (الفورم لم يفتح) → إعادة الضغط حتى يظهر، ثم يربط (§11.3)."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0, open_click_retries=3)
    scr._connect_app = MagicMock()
    scr._form_open_and_visible = MagicMock(return_value=False)   # لا فورم مفتوح في رأس اللفّة
    scr.is_main_screen = MagicMock(return_value=True)
    scr.click_button = MagicMock()
    scr._wait_form_open = MagicMock(side_effect=[False, True])   # الضغطة الأولى ابتُلعت، الثانية نجحت
    scr._bind_form = MagicMock()
    scr.open_sell_screen()
    assert scr.click_button.call_count == 2         # أُعيد الضغط مرّة
    scr._bind_form.assert_called_once()             # ثم رُبط الفورم


def test_open_screen_raises_after_retries_exhausted():
    """كل الضغطات ابتُلعت → RuntimeError صريح بعد استنفاد المحاولات، بلا ربط فورم (§11.3)."""
    from core.writers.moneyado.screens import _PYWINAUTO_AVAILABLE, MoneyadoScreen
    if not _PYWINAUTO_AVAILABLE:
        pytest.skip("pywinauto غير متاح")
    scr = MoneyadoScreen({}, step_delay=0, open_click_retries=3)
    scr._connect_app = MagicMock()
    scr._form_open_and_visible = MagicMock(return_value=False)
    scr.is_main_screen = MagicMock(return_value=True)
    scr.click_button = MagicMock()
    scr._wait_form_open = MagicMock(return_value=False)          # لا يظهر الفورم أبدًا
    scr._bind_form = MagicMock()
    with pytest.raises(RuntimeError, match="لم تفتح بعد 3 محاولات"):
        scr.open_sell_screen()
    assert scr.click_button.call_count == 3         # حاول 3 مرّات
    scr._bind_form.assert_not_called()              # لم يُربَط فورم


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
    # الترتيب الدقيق لشاشة الشراء: الحساب، (رقم المعاملة+التاريخ AO)، الرقم الإشاري، العملة،
    # الكمية قبل السعر، ×و/، (المبلغ الصافي AO)، العمولة، (المسلّم AO)، الزبون، الهاتف.
    assert keys(ops) == [
        "foreign_account", "transaction_number", "date_field", "reference_number",
        "currency_type", "quantity", "rate_multiply", "rate_divide", "net_amount",
        "commission_rate", "commission", "amount_delivered", "customer", "payment_method",
    ]
    # حقول AO يحسبها/يملؤها البرنامج → ENTER_ONLY (Enter فقط، بلا كتابة)
    for ao in ("transaction_number", "date_field", "net_amount", "amount_delivered"):
        assert by_key(ops, ao).method == ENTER_ONLY, ao
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
    """نسبة العمولة "0"(+Enter) ثم العمولة "0" عند None(+Enter) ثم «المبلغ المسلّم» enter_only."""
    ops = build_buy_fields(make_buy_leg())               # make_buy_leg بلا عمولة → "0"
    cr = by_key(ops, "commission_rate")
    assert (cr.value, cr.enter) == ("0", True)            # "0" لا فارغة، + Enter
    comm = by_key(ops, "commission")
    assert (comm.value, comm.enter) == ("0", True)        # بلا عمولة → "0" (لا فارغة) + Enter
    ad = by_key(ops, "amount_delivered")
    assert (ad.method, ad.value) == (ENTER_ONLY, "")      # يحسبه البرنامج، Enter فقط
    k = keys(ops)
    assert k.index("commission_rate") < k.index("commission") < k.index("amount_delivered") < k.index("customer")


def test_buy_rate_divide_tnd_safety_normalizes_gt_one():
    """سراح أمان تونسي: TND + سعر > 1 (وصل غير مطبَّع) → ×0.01 (35→0.35)."""
    ops = build_buy_fields(make_buy_leg(currency=Currency.TND, price_normalized="35"))
    assert by_key(ops, "rate_divide").value == "0.35"
    ops2 = build_buy_fields(make_buy_leg(currency=Currency.TND, price_normalized="35.75"))
    assert by_key(ops2, "rate_divide").value == "0.3575"


def test_buy_rate_divide_tnd_idempotent_when_already_normalized():
    """TND + سعر ≤ 1 (مطبَّع مسبقًا 0.35) → يبقى كما هو (لا تطبيع مزدوج)."""
    ops = build_buy_fields(make_buy_leg(currency=Currency.TND, price_normalized="0.35"))
    assert by_key(ops, "rate_divide").value == "0.35"


def test_buy_rate_divide_egp_untouched():
    """EGP: السعر كما هو ولو > 1 (5.72 لا يُقسم) — السراح للتونسي وحده."""
    ops = build_buy_fields(make_buy_leg(currency=Currency.EGP, price_normalized="5.72"))
    assert by_key(ops, "rate_divide").value == "5.72"


def test_buy_fields_every_typed_field_has_enter():
    """كل خانة تُكتب فيها قيمة في شاشة الشراء بـ enter=True؛ حقول AO = ENTER_ONLY."""
    ao = {"transaction_number", "date_field", "net_amount", "amount_delivered"}
    ops = build_buy_fields(make_buy_leg(payment_method="فودافون كاش", recipient_name="محمد"))
    for op in ops:
        if op.key in ao:
            assert op.method == ENTER_ONLY, op.key
        else:
            assert op.enter is True, f"{op.key} بلا enter=True"
    # تأكيد شمول الخانات التي كانت ناقصة
    assert {"reference_number", "rate_multiply", "quantity", "notes"} <= set(keys(ops))


def test_buy_fields_enter_on_currency_country_payment():
    """currency_type والبلد (كود الدفع) ووسيلة الدفع (الهاتف) بـ Enter — بلاه تبقى الخانات فارغة."""
    ops = build_buy_fields(make_buy_leg(payment_method="فودافون كاش"))
    assert by_key(ops, "currency_type").enter is True     # (١) العملة
    assert by_key(ops, "country").enter is True           # (٥) كود وسيلة الدفع (17)
    pm = by_key(ops, "payment_method")
    assert (pm.value, pm.enter) == ("01115233493", True)  # (٤) رقم الهاتف + Enter


def test_buy_fields_commission_value_when_present():
    """لو للطرف عمولة (مسار الطرفين بمورد) تُكتب قيمتها لا فارغة."""
    comm = by_key(build_buy_fields(make_buy_leg(commission=-12.0)), "commission")
    assert comm.value == "-12"


def test_buy_fields_customer_skipped_when_no_customer_code():
    """بلا customer_code (مشتقّ sell_and_buy بلا مورد) → خانة الزبون تُتخطّى (لا FieldOp)."""
    leg = make_buy_leg(customer_code=None, supplier=None)
    assert "customer" not in keys(build_buy_fields(leg))


def test_buy_fields_customer_written_from_customer_code():
    """مورد حقيقي (customer_code مضبوط) → خانة الزبون = كود المورد + Enter."""
    cust = by_key(build_buy_fields(make_buy_leg()), "customer")   # customer_code = 760
    assert (cust.value, cust.enter) == ("760", True)


def test_buy_screen_customer_field_locatable_in_real_config():
    """إعداد buy_screen الحقيقي: خانة customer لها إحداثي (لا null) — فلا يرفض الكاتب طرف المورد."""
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    cfg = json.loads((root / "config" / "moneyado_fields.json").read_text(encoding="utf-8"))
    customer = cfg["buy_screen"]["fields"]["customer"]
    assert customer.get("coord") is not None or customer.get("tab_index") is not None


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
async def test_write_store_sequence_order(tmp_path):
    """الترتيب المؤكَّد: press_store → check_unexpected_window → confirm_store_on_main
    (الفحص قبل التأكيد الأعمى؛ الرجوع للقائمة بعد الفحص، ثم POST_STORE_WAIT قبل العملية التالية §11.3)."""
    screen = make_screen()
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    await writer.write(make_job(make_sell_leg()), commit=True)
    names = [c[0] for c in screen.method_calls]
    i_store = names.index("press_store")
    i_confirm = names.index("confirm_store_on_main")
    assert i_store < i_confirm                              # التخزين ثم الرجوع للقائمة
    # فحص النافذة الطارئة يقع **بين** التخزين والتأكيد (لا تأكيد أعمى قبل الفحص)
    assert "check_unexpected_window" in names[i_store:i_confirm]


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
async def test_write_sell_opens_sell_screen_from_main(tmp_path):
    # البيع يفتح «بيع عملة» من القائمة الرئيسية (لا الشراء)
    screen = make_screen()
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_sell_leg()), commit=True)
    assert result.ok is True
    screen.open_sell_screen.assert_called_once()
    screen.open_buy_screen.assert_not_called()


@pytest.mark.asyncio
async def test_write_buy_opens_buy_screen_from_main(tmp_path):
    # الشراء يفتح «شراء عملة» من القائمة الرئيسية (لا البيع)
    screen = make_screen(name=None)
    writer = make_writer(screen, dry_run=False, tmp_path=tmp_path)
    result = await writer.write(make_job(make_buy_leg()), commit=True)
    assert result.ok is True
    screen.open_buy_screen.assert_called_once()
    screen.open_sell_screen.assert_not_called()


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
    screen.open_sell_screen.assert_not_called()     # ولا نفتح الشاشة قبل استكمال الحقول
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
