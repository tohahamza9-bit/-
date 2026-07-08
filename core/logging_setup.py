"""
التسجيل (T5: لا silent catches — كل خطأ يُسجَّل؛ في بوت مالي الخطأ الصامت = أموال).
سجلّ مزدوج: ملف دوّار على القرص + الكونسول. الأخطاء المالية تذهب لسجلّ منفصل حرج.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

_CONFIGURED = False


def setup_logging(log_dir: str, level: int = logging.INFO) -> None:
    """يُهيّئ التسجيل مرّة واحدة. يُستدعى عند بدء التطبيق."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # الكونسول
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # ملف عام دوّار (كل شيء)
    general = logging.handlers.RotatingFileHandler(
        log_path / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=10, encoding="utf-8"
    )
    general.setFormatter(fmt)
    root.addHandler(general)

    # سجلّ حرج منفصل (ERROR فأعلى) — للمراجعة المالية السريعة
    critical = logging.handlers.RotatingFileHandler(
        log_path / "critical.log", maxBytes=10 * 1024 * 1024, backupCount=20, encoding="utf-8"
    )
    critical.setLevel(logging.ERROR)
    critical.setFormatter(fmt)
    root.addHandler(critical)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """يُرجع مسجّلًا باسم الوحدة. استخدمه في كل وحدة: `log = get_logger(__name__)`."""
    return logging.getLogger(name)
