"""
اختبارات MoneyadoScreen._control — حارس تحديد الحقل بالإحداثي النسبي + الهامش (§11.3).

بلا pywinauto حقيقي: نحقن `_window`/`_form_rect` وهميّين، وكل «control» وهميّ يُرجع
مستطيلًا (rectangle) بموضع نسبيّ محدَّد. نختبر أنّ الهامش الافتراضي (15px) يلتقط حقلًا
انحرف 9px (الفورم تحرّك) مع بقاء الحماية من الحقول المتجاورة.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from core.writers.moneyado.screens import MoneyadoScreen

FORM_LEFT, FORM_TOP = 100, 200
TARGET = [50, 60]                      # الإحداثي القديم في الإعداد (coord)


def _rect(left, top):
    return SimpleNamespace(left=left, top=top)


def _fake_ctrl(rel_left, rel_top):
    """control وهميّ عند موضع نسبيّ (rel) عن الفورم — يُرجع rectangle مطلقًا."""
    c = MagicMock()
    c.rectangle.return_value = _rect(FORM_LEFT + rel_left, FORM_TOP + rel_top)
    c.wait.return_value = None
    return c


def _screen_with(controls) -> MoneyadoScreen:
    scr = MoneyadoScreen(config={})
    scr._form_rect = _rect(FORM_LEFT, FORM_TOP)
    win = MagicMock()
    win.descendants.return_value = controls
    scr._window = win
    return scr


def _cfg(**over) -> dict:
    base = {"coord": TARGET, "class": "ThunderRT6TextBox"}
    base.update(over)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# الهامش الافتراضي 15px يلتقط انحراف 9px (الفورم تحرّك +9)
# ─────────────────────────────────────────────────────────────────────────────
def test_default_tolerance_is_fifteen():
    assert MoneyadoScreen._DEFAULT_TOLERANCE == 15


def test_control_tolerance_catches_nine_pixel_drift():
    # الحقل الفعلي انحرف +9px في المحورين عن الإحداثي القديم
    drifted = _fake_ctrl(TARGET[0] + 9, TARGET[1] + 9)     # rel (59, 69)
    scr = _screen_with([drifted])
    ctrl = scr._control(_cfg())                             # بلا tolerance صريح → الافتراضي 15
    assert ctrl is drifted
    drifted.wait.assert_called_once()


def test_old_tolerance_eight_would_miss_nine_pixel_drift():
    # توثيق السبب: 8 (القديم) كان يفوّت انحراف 9px → رُفِع إلى 15.
    drifted = _fake_ctrl(TARGET[0] + 9, TARGET[1] + 9)
    scr = _screen_with([drifted])
    with pytest.raises(RuntimeError, match="غير موجود"):
        scr._control(_cfg(tolerance=8))


# ─────────────────────────────────────────────────────────────────────────────
# الحماية من الحقول المتجاورة تبقى: مجاور على بعد 40px لا يُلتقط (لا تطابق مزدوج)
# ─────────────────────────────────────────────────────────────────────────────
def test_control_fifteen_still_rejects_adjacent_field():
    drifted = _fake_ctrl(TARGET[0] + 9, TARGET[1] + 9)     # 9px → يُلتقط
    adjacent = _fake_ctrl(TARGET[0] + 40, TARGET[1])       # 40px → خارج الهامش
    scr = _screen_with([drifted, adjacent])
    ctrl = scr._control(_cfg())
    assert ctrl is drifted                                 # تطابق واحد فقط


def test_control_double_match_within_tolerance_raises():
    # حقلان كلاهما ضمن 15px من الهدف → تطابق مزدوج → استثناء صريح (§0 لا تخمين)
    a = _fake_ctrl(TARGET[0] + 9, TARGET[1] + 9)
    b = _fake_ctrl(TARGET[0] + 2, TARGET[1] + 2)
    scr = _screen_with([a, b])
    with pytest.raises(RuntimeError, match="تطابق مزدوج"):
        scr._control(_cfg())


# ═════════════════════════════════════════════════════════════════════════════
# الملاحة بترتيب Tab (§11.3) — مستقلّة عن DPI/موضع النافذة (البديل الجذري للإحداثي)
# ═════════════════════════════════════════════════════════════════════════════
def _screen_with_window(config=None):
    """شاشة بنافذة وهمية: get_focus يُرجع الحقل المركَّز؛ نراقب set_focus/type_keys."""
    scr = MoneyadoScreen(config=config if config is not None else {"tab_home_keys": "^{HOME}"})
    win = MagicMock()
    focused = MagicMock()
    focused.wait.return_value = None
    win.get_focus.return_value = focused
    scr._window = win                    # لا _form_rect: وضع Tab لا يحتاج مرجع الإحداثيات
    return scr, win, focused


def _typed(win):
    """سلسلة الضغطات المُرسَلة (الوسيط الأول لكل type_keys)."""
    return [c.args[0] for c in win.type_keys.call_args_list]


def test_control_tab_mode_navigates_and_returns_focused():
    scr, win, focused = _screen_with_window()
    # tab_index حاضر → يُستخدم Tab حتى لو حضر coord (وبلا _form_rect لن يعمل الإحداثي)
    ctrl = scr._control({"tab_index": 3, "coord": [999, 999], "class": "X"})
    assert ctrl is focused                       # الحقل المركَّز بعد الملاحة
    win.set_focus.assert_called_once()
    assert _typed(win) == ["^{HOME}", "{TAB 3}"]  # العودة لأول حقل ثم Tab×3
    focused.wait.assert_called_once()


def test_control_tab_index_zero_homes_without_tab():
    scr, win, _ = _screen_with_window()
    scr._control({"tab_index": 0})
    assert _typed(win) == ["^{HOME}"]            # 0 ضغطات Tab (أول حقل)


def test_control_tab_index_takes_precedence_over_coord():
    # coord حاضر لكن بلا _form_rect؛ لو لم يُستخدم Tab لرمى «مرجع الفورم غير مُهيّأ».
    scr, win, focused = _screen_with_window()
    assert scr._control({"tab_index": 1, "coord": [50, 60], "class": "X"}) is focused


def test_tab_home_keys_configurable_from_config():
    scr, win, _ = _screen_with_window(config={"tab_home_keys": "{HOME}"})
    scr._control({"tab_index": 2})
    assert _typed(win) == ["{HOME}", "{TAB 2}"]


def test_tab_home_keys_empty_skips_home():
    scr, win, _ = _screen_with_window(config={"tab_home_keys": ""})
    scr._control({"tab_index": 2})
    assert _typed(win) == ["{TAB 2}"]            # بلا مفتاح عودة → Tab فقط


def test_control_negative_tab_index_raises():
    scr, _, _ = _screen_with_window()
    with pytest.raises(RuntimeError, match="tab_index سالب"):
        scr._control({"tab_index": -1})


def test_tab_navigation_without_window_raises():
    scr = MoneyadoScreen(config={})
    with pytest.raises(RuntimeError, match="غير مُهيّأ"):
        scr._control({"tab_index": 0})


def test_coord_fallback_used_when_no_tab_index():
    # بلا tab_index → يعود لوضع الإحداثي (يحتاج _form_rect + مطابقة)
    drifted = _fake_ctrl(TARGET[0] + 9, TARGET[1] + 9)
    scr = _screen_with([drifted])                # يضبط _form_rect + descendants
    assert scr._control(_cfg()) is drifted       # لا tab_index → إحداثي
