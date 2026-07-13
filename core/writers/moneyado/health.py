"""
صحة نافذة MONEYADO (لوحة V2 م٥) — **قراءة فقط، لا يفتح فورمًا ولا يتفاعل مع النافذة**.

يميّز ثلاث حالات (رغبة المالك: «Python حيّ» ≠ «الأتمتة تستجيب فعلًا»):
- not_running   : لا عملية stock.exe.
- not_visible   : العملية حيّة لكن لا نافذة MONEYADO مرئية — قد تكون الأتمتة معلّقة بصمت.
- visible       : العملية حيّة والنافذة معروضة فعلًا.
(+ unknown_visibility إن غاب pywinauto، unavailable عند خطأ الفحص.)

**يعيد استخدام دوال `screens.py` بلا تعديلها**: `_pids_by_image_name` (على مستوى الوحدة، ctypes)،
و`Desktop`/`MoneyadoScreen._VISIBLE_WINDOW_CLASSES` لفلترة النوافذ المرئية (نفس منطق الكاتب، دون لمسه).
"""
from __future__ import annotations

from typing import Optional

from core.logging_setup import get_logger

log = get_logger(__name__)

_IMAGE = "stock.exe"


def _classify(running: list[int], visible: Optional[list[int]]) -> dict:
    """تصنيف نقيّ (قابل للاختبار بلا نظام) من قوائم PIDs المشغّلة/المرئية."""
    if not running:
        return {"state": "not_running", "running": False, "visible": False, "pid": None,
                "detail": "MONEYADO غير مشغّل — لا عملية stock.exe"}
    if visible is None:
        return {"state": "unknown_visibility", "running": True, "visible": None, "pid": running[0],
                "detail": "MONEYADO مشغّل؛ تعذّر فحص رؤية النافذة (pywinauto غير متاح)"}
    if not visible:
        return {"state": "not_visible", "running": True, "visible": False, "pid": running[0],
                "detail": "MONEYADO مشغّل لكن نافذته غير مرئية — قد تكون الأتمتة معلّقة بصمت"}
    return {"state": "visible", "running": True, "visible": True, "pid": visible[0],
            "instances": len(visible), "detail": "MONEYADO يعمل والنافذة مرئية"}


def _visible_pids(pids: list[int]) -> Optional[list[int]]:
    """يفلتر النوافذ المرئية لهذه الـ PIDs بنفس منطق screens (بلا تعديله). None إن تعذّر (لا pywinauto)."""
    try:
        from core.writers.moneyado import screens
    except Exception as exc:  # T5 — لا يُخفى السبب
        log.warning("تعذّر استيراد screens لفحص صحة MONEYADO: %s", exc)
        return None
    if not getattr(screens, "_PYWINAUTO_AVAILABLE", False) or screens.Desktop is None:
        return None
    wanted, visible = set(pids), []
    try:
        classes = screens.MoneyadoScreen._VISIBLE_WINDOW_CLASSES
        for win in screens.Desktop(backend="win32").windows():
            try:
                p = win.process_id()
                if p not in wanted or p in visible:
                    continue
                if win.class_name() not in classes:
                    continue
                if not win.is_visible() or win.is_minimized():
                    continue
                rect = win.rectangle()
                if rect.width() <= 0 or rect.height() <= 0:
                    continue
                visible.append(p)
            except Exception:                      # نافذة تلاشت/تعذّر فحصها — تخطٍّ آمن
                continue
    except Exception as exc:  # T5
        log.warning("تعذّر فحص رؤية نوافذ MONEYADO: %s", exc)
        return None
    return visible


def moneyado_health() -> dict:
    """صحة MONEYADO قراءة فقط — يميّز غير مشغّل/غير مرئي/مرئي. لا يفتح فورمًا ولا يتفاعل."""
    try:
        from core.writers.moneyado.screens import _pids_by_image_name
        running = _pids_by_image_name(_IMAGE)
    except Exception as exc:  # T5 — بيئة غير ويندوز/خطأ فحص
        log.warning("تعذّر فحص عمليات MONEYADO: %s", exc)
        return {"state": "unavailable", "running": None, "visible": None, "pid": None,
                "detail": f"تعذّر فحص MONEYADO: {exc}"}
    visible = _visible_pids(running) if running else []
    return _classify(running, visible)
