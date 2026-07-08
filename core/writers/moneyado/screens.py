"""
غلاف التعامل الفعلي مع شاشة MONEYADO عبر pywinauto (§11.3) — «كيف نكتب فعليًا».

مفصول عن منطق «أي حقل بأي قيمة» (fields.py) ليُحقن mock في الاختبار.
pywinauto قد لا يكون مثبّتًا (بيئة اختبار بلا برنامج MONEYADO) → الاستيراد محروس (try/except)
والصنف الفعلي لا يُشغَّل إلا على الجهاز الحقيقي. الاختبارات تحقن ScreenController وهميًّا.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ...config import load_json_config
from ...constants import OperationType
from ...logging_setup import get_logger
from .fields import SELECT, FieldOp

log = get_logger(__name__)

# ── استيراد محروس (§11.3): البيئة الاختبارية بلا pywinauto/برنامج MONEYADO ──
try:
    from pywinauto import Application, Desktop  # type: ignore

    _PYWINAUTO_AVAILABLE = True
except ImportError as _exc:  # لا نُخفي السبب (T5) — نسجّله ونعطّل التشغيل الحيّ فقط
    Application = None  # type: ignore
    Desktop = None  # type: ignore
    _PYWINAUTO_AVAILABLE = False
    log.warning("pywinauto غير مثبّت — MoneyadoScreen غير قابل للتشغيل الحيّ: %s", _exc)


_SCREEN_KEY = {
    OperationType.SELL: "sell_screen",
    OperationType.BUY: "buy_screen",
}


class ScreenController(ABC):
    """عقد التعامل مع الشاشة — يُحقن الحقيقي (MoneyadoScreen) أو mock في الاختبار."""

    @abstractmethod
    def field_config(self, operation: OperationType) -> dict:
        """خريطة الحقول {مفتاح: {coord, class, ...}} من ملف moneyado_fields.json."""

    @abstractmethod
    def button_config(self, operation: OperationType) -> dict:
        """إعداد الأزرار المسموحة (store/stop) — لا أزرار طباعة/معاينة (§2.3)."""

    @abstractmethod
    def connect(self, operation: OperationType) -> None:
        """يتصل بنافذة «بيع/شراء عملة» وينتظر جاهزيتها (wait لا sleep §11.3)."""

    @abstractmethod
    def is_main_screen(self) -> bool:
        """هل الشاشة الحالية هي القائمة الرئيسية؟ (لا فورم بيع/شراء ThunderRT6FormDC مفتوح §11.3)."""

    @abstractmethod
    def click_button(self, label: str) -> None:
        """يضغط زرًّا في القائمة الرئيسية بنصّه (label) — أكثر استقرارًا من الإحداثيات (§11.3)."""

    @abstractmethod
    def open_sell_screen(self) -> None:
        """يفتح شاشة «بيع عملة» من القائمة الرئيسية (نص الزر) وينتظر ظهور الفورم (§11.3)."""

    @abstractmethod
    def open_buy_screen(self) -> None:
        """يفتح شاشة «شراء عملة» من القائمة الرئيسية (نص الزر) وينتظر ظهور الفورم (§11.3)."""

    @abstractmethod
    def check_unexpected_window(self) -> Optional[str]:
        """يُرجع عنوان أي نافذة طارئة (رصيد/خطأ/معاينة §11.3) أو None."""

    @abstractmethod
    def fill(self, field_op: FieldOp, field_cfg: dict) -> None:
        """يكتب خانة واحدة (type_keys/select حسب الطريقة) — الحقل يُحدَّد بالإحداثي (coord/class)."""

    @abstractmethod
    def press_enter_on_active(self) -> None:
        """يضغط Enter على العنصر النشط حاليًا (حقل enter_only يحسبه البرنامج §11.1) — بلا إحداثي."""

    @abstractmethod
    def read_text(self, field_cfg: dict) -> str:
        """يقرأ نص خانة (اسم الزبون الظاهر §11.2) — الحقل يُحدَّد بالإحداثي (coord/class)."""

    @abstractmethod
    def screenshot(self, path: str) -> None:
        """يحفظ لقطة شاشة للنافذة (dead-letter §11.3)."""

    @abstractmethod
    def press_store(self, operation: OperationType) -> None:
        """يضغط «تخزين» — الزر الوحيد المسموح للحفظ (§2.3)."""

    @abstractmethod
    def confirm_store_on_main(self) -> None:
        """بعد «تخزين»: Enter على النافذة الرئيسية (خارج مربع البيع/الشراء) لإغلاق رسالة
        التأكيد/التنبيه والعودة للقائمة الرئيسية قبل العملية التالية (§11.3)."""

    @abstractmethod
    def press_stop(self, operation: OperationType) -> None:
        """يضغط «STOP/رجوع» — صمّام الأمان: يلغي بلا حفظ (§2.3)."""


class MoneyadoScreen(ScreenController):
    """التنفيذ الفعلي عبر pywinauto backend=win32 (تطبيق VB6/Delphi قديم §11.3).

    لا يُنشأ في الاختبار (يُحقن mock)؛ يُشغَّل على الجهاز الفرعي فقط.
    """

    # المرساة الافتراضية حين لا يذكرها الإعداد (فحص الجهاز الفعلي §11.3)
    _DEFAULT_PROCESS = "stock.exe"
    _DEFAULT_FORM_CLASS = "ThunderRT6FormDC"
    # هامش تطابق الإحداثي بالبكسل. رُفِع 8→15: الإحداثيات الجديدة تختلف عن القديمة بـ +9px
    # منتظمة (الفورم تحرّك قليلًا)؛ 15 يغطّي الفرق بأمان مع إبقاء الحماية من الحقول المتجاورة.
    _DEFAULT_TOLERANCE = 15
    # 🔴 الملاحة المفضّلة: ترتيب Tab (§11.3) — مستقلّ عن DPI/موضع النافذة (بديل الإحداثي الهشّ).
    # نعود لأول حقل ثم نضغط Tab بعدد tab_index. مفتاح «العودة لأول حقل» قابل للضبط على الجهاز
    # عبر "tab_home_keys" في الإعداد (VB6: قد لا يعمل Ctrl+Home داخل حقل نصّي؛ يُعاير بحوالة تجريبية).
    _DEFAULT_TAB_HOME_KEYS = "^{HOME}"
    # أزرار القائمة الرئيسية (نص عربي) — نفتح شاشة العملية بالنص لا بالإحداثي (§11.3، أكثر
    # استقرارًا). قابلة للضبط عبر config["main_menu"] إن اختلف النص/الصنف على الجهاز.
    _DEFAULT_SELL_BUTTON = "بيع عملة"
    _DEFAULT_BUY_BUTTON = "شراء عملة"
    _DEFAULT_MAIN_BUTTON_CLASS = "ThunderRT6CommandButton"

    def __init__(
        self,
        config: dict,
        *,
        timeout: float = 20.0,
        connect_wait: float = 1.0,
        step_delay: float = 0.3,
    ) -> None:
        self._config = config
        self._timeout = timeout
        # مهل الجهاز البطيء (§11.3): بعد تثبيت النافذة، وبعد تعبئة كل حقل — قابلة للضبط من .env.
        self._connect_wait = connect_wait
        self._step_delay = step_delay
        self._app = None
        self._window = None
        self._form_rect = None  # يُلتقط مرة في connect؛ مرجع الإحداثيات النسبية
        self._unexpected_titles: list[str] = config.get("unexpected_window_titles", [])

    @classmethod
    def from_settings(cls, settings) -> "MoneyadoScreen":
        """يحمّل ملف الحقول (coord/class) من الإعدادات (§ config.moneyado_fields_file)."""
        config = load_json_config(settings.moneyado_fields_file)
        return cls(
            config,
            connect_wait=getattr(settings, "moneyado_connect_wait", 1.0),
            step_delay=getattr(settings, "moneyado_step_delay", 0.3),
        )

    # ── إعداد الحقول والأزرار ────────────────────────────────────────────────
    def _screen(self, operation: OperationType) -> dict:
        return self._config[_SCREEN_KEY[operation]]

    def field_config(self, operation: OperationType) -> dict:
        return self._screen(operation).get("fields", {})

    def button_config(self, operation: OperationType) -> dict:
        return self._screen(operation).get("buttons", {})

    def _forbidden(self, operation: OperationType) -> list[str]:
        return self._screen(operation).get("forbidden_buttons", [])

    # ── الاتصال بلا عنوان: بالعملية + صنف الفورم (§11.3) ─────────────────────────
    def connect(self, operation: OperationType, *, pid: Optional[int] = None) -> None:
        """يتصل بالتطبيق ثم يربط فورم العملية المفتوح (يفترض الفورم مفتوحًا مسبقًا).

        لفتح الفورم من القائمة الرئيسية بنفس البوت استخدم open_sell_screen/open_buy_screen.
        """
        self._connect_app(operation, pid=pid)
        self._bind_form(operation)

    def _connect_app(self, operation: OperationType, *, pid: Optional[int] = None) -> None:
        """يتصل بعملية MONEYADO (بلا عنوان — VB6) ويهيّئ self._app. لا يربط فورمًا بعد."""
        if not _PYWINAUTO_AVAILABLE:
            raise RuntimeError("pywinauto غير متاح — لا يمكن الاتصال بشاشة MONEYADO.")
        screen = self._screen(operation)
        # نوافذ MONEYADO (VB6/ThunderRT6FormDC) بلا عنوان → نتصل بالعملية لا بالعنوان.
        process_name = screen.get("process_name", self._DEFAULT_PROCESS)
        # لو تعدّدت النسخ، مرِّر PID صريحًا؛ وإلا pywinauto يتصل بأحدث عملية بالاسم.
        connect_kwargs = {"process": pid} if pid is not None else {"path": process_name}
        self._app = Application(backend="win32").connect(timeout=self._timeout, **connect_kwargs)

    def _bind_form(self, operation: OperationType) -> None:
        """يلتقط فورم العملية (بيع/شراء) المفتوح وينتظر جاهزيته + مرجع الإحداثيات + حارس الشاشة."""
        if self._app is None:
            raise RuntimeError("الاتصال بالتطبيق غير مُهيّأ (_connect_app لم يُستدعَ).")
        screen = self._screen(operation)
        form_class = screen.get("form_class", self._DEFAULT_FORM_CLASS)

        # التقط الفورم النشط من صنفه (بلا عنوان) وانتظر جاهزيته — لا sleep ثابت (§11.3).
        self._window = self._app.window(class_name=form_class)
        self._window.wait("ready visible enabled", timeout=self._timeout)

        # 🔴 لا نحرّك النافذة (move_window كان يُغلق MONEYADO على الجهاز): الإحداثيات نسبية
        # للفورم (rel = rect - form_rect) فتعمل عند أي موضع للنافذة — لا حاجة لتثبيت الموضع (§11.3).
        time.sleep(self._connect_wait)  # مهلة استقرار للجهاز البطيء قبل التقاط المرجع/التفاعل

        # مرجع الإحداثيات النسبية: يُلتقط مرة واحدة بعد الجاهزية.
        self._form_rect = self._window.rectangle()

        # حارس الشاشة الصحيحة (§0): الفورمان من نفس الصنف وبلا عنوان → نميّز ببصمة العدد.
        expected = screen.get("expected_field_count")
        if expected is not None:
            actual = len(self._window.descendants(class_name="ThunderRT6TextBox"))
            # هامش معقول حول العدد المتوقّع (التخطيط ثابت لكن الإحصاء قد يشمل عناصر مساعدة).
            if abs(actual - int(expected)) > max(int(expected) // 4, 5):
                raise RuntimeError(
                    f"شاشة غير متوقّعة لعملية {operation.value}: "
                    f"عدد حقول النص {actual} خارج المدى المتوقّع (~{expected}) — "
                    "قد تكون الشاشة الخطأ (§0)."
                )

    # ── فتح شاشة العملية من القائمة الرئيسية (بالنص §11.3) ───────────────────────
    def _main_menu_cfg(self) -> dict:
        return self._config.get("main_menu", {})

    def is_main_screen(self) -> bool:
        """القائمة الرئيسية = لا فورم بيع/شراء (ThunderRT6FormDC) مفتوح ومرئي (§11.3).

        يتطلّب اتصالًا سابقًا بالتطبيق (self._app). يُستدعى داخل open_sell/open_buy_screen.
        """
        if not _PYWINAUTO_AVAILABLE:
            raise RuntimeError("pywinauto غير متاح — تعذّر فحص القائمة الرئيسية.")
        if self._app is None:
            raise RuntimeError("الاتصال بالتطبيق غير مُهيّأ (connect/_connect_app لم يُستدعَ).")
        form_class = self._main_menu_cfg().get("form_class", self._DEFAULT_FORM_CLASS)
        open_forms = [w for w in self._app.windows(class_name=form_class) if w.is_visible()]
        return not open_forms

    def click_button(self, label: str) -> None:
        """يضغط زرًّا في القائمة الرئيسية بنصّه (label) — أكثر استقرارًا من الإحداثي (§11.3)."""
        if not _PYWINAUTO_AVAILABLE:
            raise RuntimeError("pywinauto غير متاح — تعذّر الضغط على زر القائمة الرئيسية.")
        if self._app is None:
            raise RuntimeError("الاتصال بالتطبيق غير مُهيّأ (connect/_connect_app لم يُستدعَ).")
        btn_class = self._main_menu_cfg().get("button_class", self._DEFAULT_MAIN_BUTTON_CLASS)
        main = self._app.top_window()
        btn = main.child_window(title=label, class_name=btn_class)
        btn.wait("ready visible enabled", timeout=self._timeout)
        btn.click()
        time.sleep(self._step_delay)  # مهلة استقرار حتى يفتح الفورم (جهاز بطيء §11.3)

    def _open_screen(self, operation: OperationType, label: str) -> None:
        """يفتح فورم العملية من القائمة الرئيسية: اتصال بالتطبيق → تأكّد القائمة → ضغط الزر
        بالنص → انتظار ظهور الفورم وربطه (§11.3). لا نفتح فورمًا فوق فورم (§0)."""
        self._connect_app(operation)
        if not self.is_main_screen():
            raise RuntimeError(
                f"الشاشة الحالية ليست القائمة الرئيسية — لا نفتح «{label}» فوق فورم مفتوح (§0)."
            )
        log.info("فتح شاشة «%s» من القائمة الرئيسية بالنص (§11.3).", label)
        self.click_button(label)
        self._bind_form(operation)

    def open_sell_screen(self) -> None:
        label = self._main_menu_cfg().get("sell_button", self._DEFAULT_SELL_BUTTON)
        self._open_screen(OperationType.SELL, label)

    def open_buy_screen(self) -> None:
        label = self._main_menu_cfg().get("buy_button", self._DEFAULT_BUY_BUTTON)
        self._open_screen(OperationType.BUY, label)

    def _tab_to(self, tab_index: int):
        """
        يُحدّد الحقل بترتيب Tab (§11.3) — مستقلّ عن DPI/موضع النافذة:
          يركّز الفورم → يعود لأول حقل (tab_home_keys) → Tab بعدد tab_index → يُرجع الحقل المركَّز.

        الملاحة مطلقة (نعود لأول حقل في كل مرة) فلا تتراكم أخطاء التزامن بعد Enter/الأزرار.
        """
        if self._window is None:
            raise RuntimeError("الاتصال بالشاشة غير مُهيّأ (connect لم يُستدعَ).")
        idx = int(tab_index)
        if idx < 0:
            raise RuntimeError(f"tab_index سالب ({idx}) — غير صالح (§11.3).")
        home_keys = self._config.get("tab_home_keys", self._DEFAULT_TAB_HOME_KEYS)
        self._window.set_focus()
        if home_keys:
            self._window.type_keys(home_keys, set_foreground=True)          # العودة لأول حقل
        if idx:
            self._window.type_keys("{TAB " + str(idx) + "}", set_foreground=True)  # Tab×tab_index
        ctrl = self._window.get_focus()
        if ctrl is None:
            raise RuntimeError(f"تعذّر تحديد الحقل المركَّز بعد Tab×{idx} (§11.3).")
        ctrl.wait("ready visible enabled", timeout=self._timeout)
        return ctrl

    def _control(self, field_cfg: dict, method: str = ""):
        """يُحدّد الحقل: بترتيب Tab (tab_index) إن وُجد — مستقلّ عن DPI — وإلّا بالإحداثي النسبي.

        حارس التفرّد (§0، وضع الإحداثي): تطابق مزدوج أو انعدام تطابق → استثناء صريح، لا تخمين.
        """
        if self._window is None:
            raise RuntimeError("الاتصال بالشاشة غير مُهيّأ (connect لم يُستدعَ).")

        # (1) الوضع المفضّل: ترتيب Tab (لا يتأثّر بـ DPI/موضع النافذة §11.3).
        tab_index = field_cfg.get("tab_index")
        if tab_index is not None:
            return self._tab_to(tab_index)

        # (2) fallback: الإحداثي النسبي للفورم (يحتاج مرجع الفورم من connect).
        if self._form_rect is None:
            raise RuntimeError("مرجع الفورم غير مُهيّأ (connect لم يُستدعَ).")
        coord = field_cfg.get("coord")
        if coord is None:
            raise RuntimeError("لا ترتيب Tab ولا إحداثي (tab_index/coord=null) — البوت لا يخمّن (§11.1).")
        target_left, target_top = coord
        field_class = field_cfg.get("class")
        tolerance = field_cfg.get("tolerance", self._DEFAULT_TOLERANCE)

        matches = []
        for ctrl in self._window.descendants(class_name=field_class):
            rect = ctrl.rectangle()
            rel_left = rect.left - self._form_rect.left
            rel_top = rect.top - self._form_rect.top
            if abs(rel_left - target_left) <= tolerance and abs(rel_top - target_top) <= tolerance:
                matches.append((ctrl, rel_left, rel_top))

        if len(matches) > 1:
            coords = ", ".join(f"({l},{t})" for _, l, t in matches)
            raise RuntimeError(
                f"تطابق مزدوج عند الإحداثي ({target_left},{target_top}) "
                f"صنف {field_class}: {coords} — لا نخمّن أيّهما (§0)."
            )
        if not matches:
            raise RuntimeError(
                f"الحقل غير موجود عند الإحداثي ({target_left},{target_top}) "
                f"صنف {field_class} (هامش {tolerance}px)."
            )

        ctrl = matches[0][0]
        try:
            ctrl.wait("ready visible enabled", timeout=self._timeout)
        except AttributeError:
            # حقول VB6 النصية (EditWrapper) لا تدعم .wait() — نتجاوز بصمت
            pass
        return ctrl

    # ── التعبئة ──────────────────────────────────────────────────────────────
    def fill(self, field_op: FieldOp, field_cfg: dict) -> None:
        try:
            self._fill(field_op, field_cfg)
        except Exception as exc:
            # 🔴 البلد «غير حرج» (§11.1): تعذّر تحديد القائمة أو اختيار العنصر لا يُفشِل البيع —
            #    يُسجَّل ويُتخطّى (تبقى الخانة فارغة). باقي الحقول حرجة → يُعاد رفع الخطأ.
            if field_op.key == "country":
                log.warning("تعذّر إدخال البلد «%s» (%s) — تُركت فارغة (غير حرج §11.1).",
                            field_op.value, exc)
                return
            raise

    def press_enter_on_active(self) -> None:
        """يضغط Enter على العنصر النشط حاليًا عبر نافذة الفورم (بلا تحديد بإحداثي §11.3).

        لحقل يحسبه البرنامج (المبلغ المخصوم §11.1): بعد Enter العمولة يكون التركيز عليه
        تلقائيًا؛ فـEnter هنا يؤكّد القيمة المحسوبة وينتقل إلى الحقل التالي (يفعّل «البلد»).
        🔴 لا كتابة ولا مسح — كي لا نُفسد القيمة التي يحسبها البرنامج (§0).
        """
        if self._window is None:
            raise RuntimeError("الاتصال بالشاشة غير مُهيّأ (connect لم يُستدعَ).")
        # نفس آلية Enter المستخدمة للحقول الأخرى، لكن على الحقل النشط بلا تحديد بإحداثي.
        self._window.type_keys("{ENTER}", set_foreground=True)
        time.sleep(self._step_delay)  # مهلة استقرار (جهاز بطيء §11.3)

    def _fill(self, field_op: FieldOp, field_cfg: dict) -> None:
        ctrl = self._control(field_cfg, field_op.method)
        if field_op.method == SELECT:
            # قائمة منسدلة (نوع العملة/البلد) — اختيار مباشر بالنص كما بالقائمة. النص يطابق
            # عناصر القائمة تمامًا («جنيه مصري»=فهرس 2، «دينار تونسي»=فهرس 4 — مؤكَّد بفحص
            # item_texts الحيّ) فالاختيار بالاسم صحيح (لا بالرقم الهشّ الذي يُزحزحه أي إدراج).
            ctrl.select(field_op.value)
            # 🔴 select() يرسل CB_SETCURSEL: يحدّث العرض لكنه لا يُطلق حدث SelChange في VB6،
            #    فالحقول التابعة للعملة (المبلغ المحلي) قد لا تُحسب. Enter بعده يُطلق الحدث.
            #    حارس (T5 لا نبتلع بصمت): فشل الإطلاق يُسجَّل ولا يُبطل الاختيار الذي تمّ (§0).
            try:
                ctrl.type_keys("{ENTER}", set_foreground=True)
            except Exception as exc:
                log.warning(
                    "تعذّر إطلاق SelChange بـ Enter بعد اختيار «%s» (%s) — الاختيار قائم.",
                    field_op.value, exc,
                )
            time.sleep(self._step_delay)  # مهلة استقرار بعد كل حقل (جهاز بطيء §11.3)
            return
        # type_keys وليس set_text (§11.3): يحاكي كتابة حقيقية فيُفعّل البحث التلقائي
        ctrl.set_focus()
        ctrl.type_keys("^a{BACKSPACE}", set_foreground=True)  # مسح المحتوى القديم بأمان
        if field_op.value:
            ctrl.type_keys(field_op.value, with_spaces=True, set_foreground=True)
        if field_op.enter:
            ctrl.type_keys("{ENTER}", set_foreground=True)
        time.sleep(self._step_delay)  # مهلة استقرار بعد كل حقل (جهاز بطيء §11.3)

    def read_text(self, field_cfg: dict) -> str:
        ctrl = self._control(field_cfg, "read")
        return (ctrl.window_text() or "").strip()

    # ── النافذة الطارئة (§11.3) ──────────────────────────────────────────────
    def check_unexpected_window(self) -> Optional[str]:
        if not _PYWINAUTO_AVAILABLE:
            return None
        try:
            for win in Desktop(backend="win32").windows():
                title = win.window_text() or ""
                if not title:
                    continue
                for bad in self._unexpected_titles:
                    if bad in title:
                        return title
        except Exception as exc:  # لا نُخفيه (T5) — نسجّله ونكمل بأمان
            log.warning("فحص النافذة الطارئة أخفق: %s", exc)
        return None

    def screenshot(self, path: str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if self._window is not None:
            self._window.capture_as_image().save(str(p))

    # ── الأزرار المسموحة فقط (§2.3) ──────────────────────────────────────────
    def _button(self, cfg: dict):
        # الأزرار لها عناوين ثابتة («تخزين»/«رجوع») → تبقى المطابقة بالعنوان (§11.3).
        if self._window is None:
            raise RuntimeError("الاتصال بالشاشة غير مُهيّأ (connect لم يُستدعَ).")
        title_re = cfg["title_re"]
        btn_class = cfg.get("class")
        # صنف VB6 (ThunderRT6CommandButton) أضمن من control_type على backend=win32.
        if btn_class:
            btn = self._window.child_window(title_re=title_re, class_name=btn_class)
        else:
            btn = self._window.child_window(title_re=title_re, control_type="Button")
        btn.wait("ready visible enabled", timeout=self._timeout)
        return btn

    def press_store(self, operation: OperationType) -> None:
        btn = self._button(self.button_config(operation)["store"])
        text = btn.window_text() or ""
        # حارس إضافي: لا نضغط أبدًا زرًّا ممنوعًا (طباعة/معاينة/إيصال §2.3)
        for forbidden in self._forbidden(operation):
            if forbidden and forbidden in text:
                raise RuntimeError(f"رفض ضغط زر ممنوع '{text}' (§2.3)")
        btn.click()

    def confirm_store_on_main(self) -> None:
        """Enter على النافذة الرئيسية للتطبيق (top_window) لإغلاق رسالة التأكيد/التنبيه التي
        يعرضها البرنامج بعد «تخزين» والعودة للقائمة الرئيسية (§11.3).

        🔴 لا نضغط داخل مربع البيع/الشراء (self._window) بل على النافذة النشطة الأعلى للتطبيق:
        رسالة التأكيد/القائمة الرئيسية تكون فوق الفورم بعد الحفظ. best-effort: تعذّر الضغط
        لا يُبطل حفظًا تمّ فعلاً — يُسجَّل ويُكمل (T5 لا نبتلع بصمت).
        """
        if self._app is None:
            raise RuntimeError("الاتصال بالتطبيق غير مُهيّأ (connect لم يُستدعَ).")
        try:
            self._app.top_window().type_keys("{ENTER}", set_foreground=True)
        except Exception as exc:
            log.warning("تعذّر Enter على النافذة الرئيسية بعد «تخزين» (%s) — الحفظ تمّ، متابعة.", exc)

    def press_stop(self, operation: OperationType) -> None:
        btn = self._button(self.button_config(operation)["stop"])
        btn.click()
