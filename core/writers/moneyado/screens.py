"""
غلاف التعامل الفعلي مع شاشة MONEYADO عبر pywinauto (§11.3) — «كيف نكتب فعليًا».

مفصول عن منطق «أي حقل بأي قيمة» (fields.py) ليُحقن mock في الاختبار.
pywinauto قد لا يكون مثبّتًا (بيئة اختبار بلا برنامج MONEYADO) → الاستيراد محروس (try/except)
والصنف الفعلي لا يُشغَّل إلا على الجهاز الحقيقي. الاختبارات تحقن ScreenController وهميًّا.
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from ...config import load_json_config
from ...constants import OperationType
from ...logging_setup import get_logger
from .fields import ENTER_ONLY, SELECT, FieldOp

log = get_logger(__name__)

# تطبيع نصّ زرّ القائمة الرئيسية للمطابقة: يشيل التشكيل ويضغط المسافات ويشذّبها (نص الزر
# الفعلي قد يحمل مسافة زائدة/تشكيلًا مختلفًا عن الإعداد §11.3).
_BTN_TASHKEEL = re.compile(r"[ً-ْٰـ]")


def _norm_btn(s: str) -> str:
    return re.sub(r"\s+", " ", _BTN_TASHKEEL.sub("", s or "")).strip()


def _actionable(ctrl) -> bool:
    """هل عنصر (wrapper) مرئي وفعّال للنقر؟ (يتسامح مع wrappers لا تدعم الفحص)."""
    try:
        return bool(ctrl.is_visible() and ctrl.is_enabled())
    except Exception:
        return True


def _pids_by_image_name(image_name: str) -> list[int]:
    """قائمة PIDs لكل العمليات التي اسم صورتها image_name (عبر ToolHelp32 — بلا psutil).

    يُستخدم لعدّ نسخ stock.exe: أكثر من نسخة = التباس اتصال (§0) → رفض بدل اتصال عشوائي.
    """
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]
    k32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32)]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(-1).value:
        return []
    pids: list[int] = []
    try:
        entry = PROCESSENTRY32()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32)
        target = image_name.lower()
        ok = k32.Process32First(snap, ctypes.byref(entry))
        while ok:
            exe = entry.szExeFile.decode("mbcs", "ignore").lower()
            if exe == target:
                pids.append(int(entry.th32ProcessID))
            ok = k32.Process32Next(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return pids

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
    # مميّز فورم العملية: عنوان ThunderRT6FormDC فارغ على الجهاز فلا يميّز بيع/شراء (مؤكَّد حيًّا)؛
    # نميّز بزرّ خاصّ بكل عملية — البيع فيه «ايصال قبض»، الشراء فيه «ايصال صرف». قابل للضبط عبر
    # config[screen]["form_marker"] إن اختلف على الجهاز.
    _DEFAULT_FORM_MARKER = {
        OperationType.SELL: "ايصال قبض",
        OperationType.BUY: "ايصال صرف",
    }
    # خانات يحسب البرنامج قيمةً تابعة بعد Enter عليها (المبلغ الصافي بعد السعر في شاشة الشراء
    # §11.3) → مهلة إضافية (moneyado_enter_wait) بعد Enter قبل الحقل التالي كي يتمّ الحساب.
    _SETTLE_AFTER_ENTER = {"rate_divide"}
    # الخانات الأخيرة قبل «تخزين» (الهاتف/الملاحظات) → مهلة step_delay إضافية لتثبيت القيمة قبل
    # الضغط على «تخزين» في شاشتَي البيع والشراء (§11.3).
    _EXTRA_SETTLE_BEFORE_STORE = {"payment_method", "notes"}

    def __init__(
        self,
        config: dict,
        *,
        timeout: float = 20.0,
        connect_wait: float = 1.0,
        step_delay: float = 0.3,
        open_timeout: float = 30.0,
        open_retry_wait: float = 6.0,
        open_click_retries: int = 3,
        enter_wait: float = 3.0,
        pid: Optional[int] = None,
    ) -> None:
        self._config = config
        self._timeout = timeout
        # PID نسخة stock.exe المثبّتة (اختياري): يُتصل به حصريًا فيتفادى التباس تعدّد النسخ (§0).
        self._pid = pid
        # مهل الجهاز البطيء (§11.3): بعد تثبيت النافذة، وبعد تعبئة كل حقل — قابلة للضبط من .env.
        self._connect_wait = connect_wait
        self._step_delay = step_delay
        # مهلة انتظار حساب البرنامج بعد Enter على خانة حاسبة (المبلغ الصافي §11.3) = MONEYADO_ENTER_WAIT.
        self._enter_wait = enter_wait
        # ربط فورم العملية بعد ظهوره (§11.3).
        self._open_timeout = open_timeout
        # فتح الشاشة من القائمة: انتظار ظهور الفورم بعد كل ضغطة، وعدد إعادات الضغط إن ابتُلع
        # النقر (نقر زر VB6 يُبتلَع أحيانًا فلا يفتح الفورم — أُثبت على الجهاز الحيّ §11.3).
        self._open_retry_wait = open_retry_wait
        self._open_click_retries = max(1, int(open_click_retries))
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
            open_timeout=getattr(settings, "moneyado_open_timeout", 30.0),
            open_retry_wait=getattr(settings, "moneyado_open_retry_wait", 6.0),
            open_click_retries=getattr(settings, "moneyado_open_click_retries", 3),
            enter_wait=getattr(settings, "moneyado_enter_wait", 3.0),
            pid=getattr(settings, "moneyado_pid", None),
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

    # أصناف نوافذ MONEYADO التي تدلّ على نسخة **معروضة أمام المستخدم**: النافذة الرئيسية
    # (ThunderRT6Form) أو فورم العملية (ThunderRT6FormDC). 🔴 لا ThunderRT6Main: نافذة VB6
    # مخفية صفرية الحجم (0×0) وWS_VISIBLE **دائمًا** لكل نسخة — فلا تُميّز المعروضة من الخلفية
    # (أُثبت حيًّا: نسختان كلتاهما is_visible=True بينما النافذة الحقيقية مُصغّرة/صفرية).
    _VISIBLE_WINDOW_CLASSES = {"ThunderRT6Form", "ThunderRT6FormDC"}

    def _list_stock_pids(self, process_name: str) -> list[int]:
        """PIDs العمليات باسم process_name (يُعزل للاختبار)."""
        return _pids_by_image_name(process_name)

    def _visible_stock_pids(self, process_name: str, pids: list[int]) -> list[int]:
        """من بين pids، مَن له نافذة MONEYADO **معروضة فعلاً** — بترتيب الظهور بلا تكرار.
        «معروضة» = صنفها Form/FormDC، WS_VISIBLE، **غير مُصغّرة**، و**حجمها > 0** (لا 0×0)؛
        فتُستبعَد النسخ الخلفية/المُصغّرة/صفرية الحجم. يُعزل للاختبار (يلمس pywinauto)."""
        wanted = set(pids)
        visible: list[int] = []
        for win in Desktop(backend="win32").windows():
            try:
                p = win.process_id()
                if p not in wanted or p in visible:
                    continue
                if win.class_name() not in self._VISIBLE_WINDOW_CLASSES:
                    continue
                if not win.is_visible() or win.is_minimized():
                    continue
                rect = win.rectangle()
                if rect.width() <= 0 or rect.height() <= 0:   # 0×0 = غير معروضة فعلاً
                    continue
                visible.append(p)
            except Exception:                 # نافذة تلاشت/تعذّر فحصها — تخطٍّ آمن
                continue
        return visible

    def _connect_app(self, operation: OperationType, *, pid: Optional[int] = None) -> None:
        """يتصل بعملية MONEYADO (بلا عنوان — VB6) ويهيّئ self._app. لا يربط فورمًا بعد.

        🔴 يتصل **فقط** بنسخة stock.exe نافذتها مرئية (Main/Form/FormDC) — لا يقبل نسخة خلفية/شبح
        أبدًا (قرار المستخدم §0):
          1) لا نسخة stock.exe عاملة → «MONEYADO غير مشغّل».
          2) عاملة لكن لا نافذة مرئية → «MONEYADO غير مرئي — افتحه أولًا».
          3) نسخة مرئية → تُستخدم ويُحدَّث self._pid. PID مثبّت (MONEYADO_PID) يُفضَّل إن كان ضمن
             المرئية؛ وإلا أوّل نسخة مرئية (أكثر من مرئية = نادر → الأولى).
        """
        if not _PYWINAUTO_AVAILABLE:
            raise RuntimeError("pywinauto غير متاح — لا يمكن الاتصال بشاشة MONEYADO.")
        screen = self._screen(operation)
        process_name = screen.get("process_name", self._DEFAULT_PROCESS)
        pids = self._list_stock_pids(process_name)
        if not pids:
            raise RuntimeError(f"MONEYADO غير مشغّل (لا نسخة {process_name}) — شغّله أولًا.")

        visible = self._visible_stock_pids(process_name, pids)
        if not visible:
            raise RuntimeError("MONEYADO غير مرئي — افتحه أولًا (لا نافذة MONEYADO مرئية).")

        target_pid = pid if pid is not None else self._pid
        if target_pid is not None and target_pid in visible:
            chosen = target_pid                    # المثبّت مرئي → يُفضَّل (يفضّ الالتباس)
        else:
            chosen = visible[0]                    # أوّل نسخة مرئية
            if len(visible) > 1:
                log.warning("أكثر من نسخة MONEYADO مرئية (%s) — استُخدمت الأولى %s (§0).",
                            sorted(visible), chosen)
        self._connect_pid(chosen)                  # يُحدَّث self._pid

    def _connect_pid(self, pid: int) -> None:
        """يتصل بنسخة MONEYADO المعطاة ويثبّت self._pid عليها."""
        self._app = Application(backend="win32").connect(timeout=self._timeout, process=pid)
        self._pid = pid

    def _bind_form(self, operation: OperationType, *, timeout: Optional[float] = None) -> None:
        """يلتقط فورم العملية (بيع/شراء) المفتوح وينتظر جاهزيته + مرجع الإحداثيات + حارس الشاشة.

        `timeout`: مهلة انتظار ظهور الفورم — None تعني self._timeout (فورم مفتوح مسبقًا)؛ يمرّر
        مسار الفتح self._open_timeout (انتظار ظهوره بعد ضغط الزر §11.3).
        """
        if self._app is None:
            raise RuntimeError("الاتصال بالتطبيق غير مُهيّأ (_connect_app لم يُستدعَ).")
        screen = self._screen(operation)
        form_class = screen.get("form_class", self._DEFAULT_FORM_CLASS)
        wait_timeout = self._timeout if timeout is None else timeout

        # التقط الفورم النشط من صنفه (بلا عنوان) وانتظر جاهزيته — لا sleep ثابت (§11.3).
        # 🔴 نثبّت self._window على الكائن الملموس الذي يُرجعه wait() (لا WindowSpecification كسول):
        #    وإلا تُحلّ .rectangle() (المرجع) و.descendants() (الحقول) كلٌّ على حدة، فقد تُشيران
        #    إلى فورمَين مختلفين حين يوجد أكثر من ThunderRT6FormDC (فورم بيع متبقٍّ + شراء) →
        #    فيصير form_rect من فورم والحقول من آخر، فيفشل تحديد الحقل («غير موجود»).
        self._window = self._app.window(class_name=form_class).wait(
            "ready visible enabled", timeout=wait_timeout
        )
        # تحقّق دفاعي (§0): الكائن المربوط فورم العملية الصحيح (ThunderRT6FormDC) لا نافذة أخرى.
        # (wait() يُرجع DialogWrapper يلفّ الفورم — نتحقّق بالصنف لا بوجود child_window، فالـ
        #  wrappers لا تملك child_window أصلًا؛ الأزرار تُحدَّد عبر descendants في _button.)
        actual_class = self._window.class_name()
        if actual_class != form_class:
            raise RuntimeError(
                f"فورم خاطئ مربوط لعملية {operation.value}: {actual_class!r} بدل {form_class!r} (§0)."
            )

        # 🔴 لا نحرّك النافذة (move_window كان يُغلق MONEYADO على الجهاز): الإحداثيات نسبية
        # للفورم (rel = rect - form_rect) فتعمل عند أي موضع للنافذة — لا حاجة لتثبيت الموضع (§11.3).
        time.sleep(self._connect_wait)  # مهلة استقرار للجهاز البطيء قبل التقاط المرجع/التفاعل

        # مرجع الإحداثيات النسبية: يُعاد التقاطه **في كل ربط** (بيع أو شراء) من الفورم المربوط
        # الحالي — لا يُعاد استخدام rect فورم سابق (يشمل مسار «فورم مفتوح مسبقًا → استخدامه»).
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

    def _main_menu_buttons(self) -> list:
        """كل أزرار القائمة الرئيسية (صنفها) مع نصوصها — للتشخيص واختيار الزر بالنص (§11.3)."""
        btn_class = self._main_menu_cfg().get("button_class", self._DEFAULT_MAIN_BUTTON_CLASS)
        main = self._app.top_window()
        return [(b, (b.window_text() or "")) for b in main.descendants(class_name=btn_class)]

    def click_button(self, label: str) -> None:
        """يضغط زرًّا في القائمة الرئيسية بنصّه (label) — أكثر استقرارًا من الإحداثي (§11.3).

        يعدّد أزرار القائمة أولًا ويطبع نصوصها (تشخيص: النص الفعلي قد يخالف الإعداد)، ثم يطابق
        بالنص (تام بعد تطبيع خفيف). زر غير موجود → خطأ صريح يسرد الأزرار المتاحة، لا timeout غامض.
        """
        if not _PYWINAUTO_AVAILABLE:
            raise RuntimeError("pywinauto غير متاح — تعذّر الضغط على زر القائمة الرئيسية.")
        if self._app is None:
            raise RuntimeError("الاتصال بالتطبيق غير مُهيّأ (connect/_connect_app لم يُستدعَ).")

        buttons = self._main_menu_buttons()
        available = [t for _, t in buttons]
        # 🔍 تشخيص (§11.3): اطبع كل أزرار القائمة الرئيسية المتاحة قبل الضغط.
        log.debug("أزرار القائمة الرئيسية المتاحة (%d): %r", len(available), available)

        target = _norm_btn(label)
        candidates = [w for w, t in buttons if _norm_btn(t) == target]
        if not candidates:
            raise RuntimeError(
                f"زر «{label}» غير موجود في القائمة الرئيسية. الأزرار المتاحة: {available} — "
                f'اضبط النص الصحيح في config["main_menu"] (§11.3).'
            )
        # فضّل الزر المرئي القابل للنقر (قد يوجد زر بنفس النص لكنه مخفي/غير فعّال §11.3).
        match = next((w for w in candidates if _actionable(w)), candidates[0])
        # 🔴 wrappers من descendants لا تدعم .wait() (كحقول VB6 النصية) — لا نستدعيها؛
        #    الزر مُعدَّد وموجود، والنقر المباشر (BM_CLICK) هو ما نجح على الجهاز الحيّ.
        match.click()
        time.sleep(self._step_delay)  # مهلة استقرار حتى يفتح الفورم (جهاز بطيء §11.3)

    def _form_marker(self, operation: OperationType) -> str:
        """نصّ الزرّ المميّز لفورم العملية (بيع «ايصال قبض» / شراء «ايصال صرف»)."""
        return self._screen(operation).get("form_marker", self._DEFAULT_FORM_MARKER[operation])

    def _form_open_and_visible(self, operation: OperationType) -> bool:
        """هل فورم العملية **المطلوبة** (بيع/شراء) مفتوح ومرئي مسبقًا؟

        🔴 عنوان ThunderRT6FormDC فارغ على الجهاز فلا يميّز بيع/شراء (مؤكَّد حيًّا)؛ نميّز بزرّ
        خاصّ بكل عملية (بيع «ايصال قبض» / شراء «ايصال صرف»). هكذا فورم بيع متبقٍّ **لا** يُعامَل
        كأنه شاشة شراء (فيُتخطّى ضغط «شراء عملة»)، والعكس — منعًا لربط الشاشة الخطأ (§0).
        """
        form_class = self._screen(operation).get("form_class", self._DEFAULT_FORM_CLASS)
        btn_class = self._main_menu_cfg().get("button_class", self._DEFAULT_MAIN_BUTTON_CLASS)
        marker = _norm_btn(self._form_marker(operation))
        for w in self._app.windows(class_name=form_class):
            try:
                if not w.is_visible():
                    continue
                texts = {_norm_btn(b.window_text() or "")
                         for b in w.descendants(class_name=btn_class)}
                if marker in texts:
                    return True
            except Exception:  # نافذة أُغلقت أثناء الفحص — تجاهل
                continue
        return False

    def _close_stray_forms(self) -> None:
        """يغلق أي فورم ThunderRT6FormDC مرئي (رجوع) للعودة للقائمة الرئيسية (§11.3).

        يُستدعى قبل فتح عملية جديدة حين يتبقّى فورم عملية أخرى (مثل فورم بيع بعد تخزينه فيبقى
        مرئيًا): «رجوع» آمن هنا لأن الطرف السابق خُزّن فعلًا قبل هذه المرحلة — فلا يحجب فورم متبقٍّ
        فتح الشاشة التالية («شراء عملة») ولا يُعاد استخدامه خطأً.
        """
        form_class = self._DEFAULT_FORM_CLASS
        btn_class = self._main_menu_cfg().get("button_class", self._DEFAULT_MAIN_BUTTON_CLASS)
        for _ in range(self._open_click_retries + 1):
            forms = [w for w in self._app.windows(class_name=form_class) if w.is_visible()]
            if not forms:
                return
            for w in forms:
                back = next((b for b in w.descendants(class_name=btn_class)
                             if _norm_btn(b.window_text() or "") == "رجوع" and _actionable(b)), None)
                if back is not None:
                    try:
                        back.click()
                    except Exception as exc:
                        log.warning("تعذّر إغلاق فورم متبقٍّ (رجوع): %s", exc)
            time.sleep(self._step_delay)

    def _wait_form_open(self, operation: OperationType) -> bool:
        """ينتظر ظهور فورم العملية حتى `_open_retry_wait` (سبر كل 0.25s). True إن ظهر."""
        steps = max(1, int(self._open_retry_wait / 0.25))
        for _ in range(steps):
            if self._form_open_and_visible(operation):
                return True
            time.sleep(0.25)
        return self._form_open_and_visible(operation)

    def _open_screen(self, operation: OperationType, label: str) -> None:
        """يفتح فورم العملية من القائمة الرئيسية: اتصال بالتطبيق → (فورم مفتوح مسبقًا؟ استخدمه) →
        تأكّد القائمة → ضغط الزر بالنص مع إعادة إن ابتُلع النقر → ربط الفورم (§11.3)."""
        self._connect_app(operation)

        # فورم العملية **نفسها** مفتوح ومرئي مسبقًا (استئناف) → اربطه مباشرة بلا ضغط.
        if self._form_open_and_visible(operation):
            log.info("فورم «%s» مفتوح ومطابق للعملية → استخدامه مباشرة (بلا ضغط §11.3).", label)
            self._bind_form(operation, timeout=self._open_timeout)
            return

        # فورم عملية **أخرى** متبقٍّ (مثل فورم بيع بعد تخزينه يبقى مرئيًا) يحجب القائمة → أغلقه
        # للعودة للقائمة قبل ضغط زر العملية المطلوبة (يمنع تخطّي الضغط أو ربط الشاشة الخطأ §0).
        if not self.is_main_screen():
            log.info("فورم عملية أخرى متبقٍّ → إغلاقه (رجوع) قبل فتح «%s» (§11.3).", label)
            self._close_stray_forms()
            if not self.is_main_screen():      # ما زال محجوبًا رغم الإغلاق → خطأ صريح
                raise RuntimeError(
                    f"الشاشة الحالية ليست القائمة الرئيسية — لا نفتح «{label}» فوق فورم مفتوح (§0)."
                )

        # 🔴 نقر زر VB6 يُبتلَع أحيانًا فلا يفتح الفورم (أُثبت على الجهاز الحيّ §11.3): بدل انتظار
        #    المهلة كاملة ثم الفشل، نعيد الضغط حتى يظهر الفورم — نعيد فقط إن لم يظهر أي فورم (لا
        #    نفتح شاشتين §0). فحص «مفتوح مسبقًا» في رأس اللفّة يحمي من سباق فتح متأخّر.
        for attempt in range(1, self._open_click_retries + 1):
            if self._form_open_and_visible(operation):
                break
            log.info("فتح «%s» من القائمة بالنص (محاولة %d/%d §11.3).",
                     label, attempt, self._open_click_retries)
            self.click_button(label)
            if self._wait_form_open(operation):
                break
            log.warning("شاشة «%s» لم تفتح بعد الضغط (محاولة %d/%d) — نقر VB6 مبتلَع، إعادة (§11.3).",
                        label, attempt, self._open_click_retries)
        else:
            raise RuntimeError(
                f"شاشة «{label}» لم تفتح بعد {self._open_click_retries} محاولات ضغط "
                f"(نقر VB6 مبتلَع/تعذّر الفتح §11.3)."
            )

        self._bind_form(operation, timeout=self._open_timeout)  # الفورم ظاهر الآن → ربط فوري

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
    # حقول غير حرجة (§11.1): تعذّر إدخالها لا يُفشِل العملية — تُترك فارغة وتُسجَّل. «البلد» (كود
    # الدفع) و«وسيلة الدفع» (الهاتف) اختياريان: زر «تخزين» يُفعَّل بدونهما، فلا يُبطلان الحفظ.
    _NON_CRITICAL_FIELDS = {"country", "payment_method"}

    def fill(self, field_op: FieldOp, field_cfg: dict) -> None:
        log.debug("fill: %s = %r enter=%s enter_only=%s",
                  field_op.key, field_op.value, field_op.enter, field_op.method == ENTER_ONLY)
        try:
            self._fill(field_op, field_cfg)
        except Exception as exc:
            if field_op.key in self._NON_CRITICAL_FIELDS:
                log.warning("تعذّر إدخال %s «%s» (%s) — تُركت فارغة (غير حرج §11.1).",
                            field_op.key, field_op.value, exc)
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
            # خانة حاسبة (السعر → المبلغ الصافي §11.3): مهلة إضافية بعد Enter كي يُتمّ البرنامج
            # الحساب قبل الانتقال للحقل التالي (وإلا يبقى «المبلغ الصافي» غير محسوب).
            if field_op.key in self._SETTLE_AFTER_ENTER:
                time.sleep(self._enter_wait)
        time.sleep(self._step_delay)  # مهلة استقرار بعد كل حقل (جهاز بطيء §11.3)
        # الحقول الأخيرة (الهاتف/الملاحظات) → مهلة step_delay إضافية لتثبيت القيمة قبل «تخزين».
        if field_op.key in self._EXTRA_SETTLE_BEFORE_STORE:
            time.sleep(self._step_delay)

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
        """يحدّد زرًّا على الفورم بعنوانه (title_re) + صنفه.

        🔴 self._window كائن ملموس (DialogWrapper) لا يدعم child_window؛ و descendants على
        backend=win32 **تتجاهل title_re** (تُرجع كل أزرار الصنف). لذا نعدّد بالصنف ونطابق النصّ
        بالـ regex يدويًا (نظير click_button)، ونفضّل الزر المرئي الفعّال (§0/§11.3).
        """
        if self._window is None:
            raise RuntimeError("الاتصال بالشاشة غير مُهيّأ (connect لم يُستدعَ).")
        title_re = cfg["title_re"]
        btn_class = cfg.get("class") or self._DEFAULT_MAIN_BUTTON_CLASS  # صنف VB6 (أضمن من control_type)
        pattern = re.compile(title_re)
        matches = [b for b in self._window.descendants(class_name=btn_class)
                   if pattern.search(b.window_text() or "")]
        btn = next((b for b in matches if _actionable(b)), matches[0] if matches else None)
        if btn is None:
            raise RuntimeError(
                f"زر غير موجود على الفورم: title_re={title_re!r} class={btn_class!r} (§11.3)."
            )
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
        """Enter على النافذة الرئيسية للتطبيق (top_window) لإغلاق رسالة التأكيد بعد «تخزين»
        والعودة للقائمة الرئيسية، **مع التحقّق فعليًا من إغلاق شاشة العملية** قبل المتابعة (§11.3).

        🔴 لا نضغط داخل مربع البيع/الشراء (self._window) بل على النافذة النشطة الأعلى للتطبيق.
        نعيد الضغط حتى يُغلَق فورم العملية (is_main_screen) أو نستنفد المحاولات — كي لا تبدأ
        عملية الكتابة التالية وشاشة السابقة ما زالت مفتوحة. best-effort: تعذّر الضغط لا يُبطل
        حفظًا تمّ فعلاً — يُسجَّل ويُكمل (T5 لا نبتلع بصمت).
        """
        if self._app is None:
            raise RuntimeError("الاتصال بالتطبيق غير مُهيّأ (connect لم يُستدعَ).")
        for _ in range(self._open_click_retries + 1):
            try:
                self._app.top_window().type_keys("{ENTER}", set_foreground=True)
            except Exception as exc:
                log.warning("تعذّر Enter على النافذة الرئيسية بعد «تخزين» (%s) — الحفظ تمّ، متابعة.", exc)
            if self.is_main_screen():          # لا فورم عملية مفتوح → عُدنا للقائمة الرئيسية
                return
            time.sleep(self._step_delay)
        log.warning("لم تُغلَق شاشة العملية بعد «تخزين» رغم Enter المتكرّر — متابعة (الحفظ تمّ).")

    def press_stop(self, operation: OperationType) -> None:
        btn = self._button(self.button_config(operation)["stop"])
        btn.click()
