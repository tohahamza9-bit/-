"""
عزل سجلّات الاختبار عن لوق الإنتاج.

اكتُشِف 2026-07-20 أثناء التحقيق في حادثة X1567: `artifacts/logs/bot.log` كان يحوي آثار
اختبارات (`job-1`، `d1`، `RuntimeError: MONEYADO غير مرئي`، تتبّعات mock) مختلطةً بسجلّات
إنتاج حقيقية، فاضطُرّ الفحص لطرح أدلّةٍ لأنّ مصدرها اختبارٌ لا الإنتاج.

هذه الاختبارات حارسٌ انحدار: تُثبت أنّ لا معالِج تسجيل يشير إلى مجلّد الإنتاج، وأنّ
أيّ سطرٍ يُكتَب أثناء السويت يهبط في المجلّد المؤقّت. سقوطُ أيّها = عودة التلوّث.
"""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from .conftest import _TEST_LOG_DIR

_PROD_LOG_DIR = Path(__file__).resolve().parent.parent / "artifacts" / "logs"


def _file_handlers() -> list[logging.FileHandler]:
    """معالِجات الملفّات الحقيقية على جذر التسجيل.

    يُستبعَد جهاز العدم (`\\\\.\\nul` على ويندوز / `/dev/null`): pytest نفسه يُركّب معالِجًا عليه
    (_FileHandler) — ليس وجهةً تُلوَّث، فلا يُحاكَم بقاعدة المجلّد.

    الفحص على **النصّ الخام** لا على `Path(...).name`: ويندوز يُفسّر `\\\\.\\nul` جذرَ UNC
    فيصير `.name` فارغًا، فيَنفُذ المعالِج من المرشِّح."""
    out = []
    for h in logging.getLogger().handlers:
        if not isinstance(h, logging.FileHandler):
            continue
        raw = (h.baseFilename or "").replace("/", "\\").rstrip("\\").lower()
        if raw.endswith("nul") or raw.endswith("null"):
            continue
        out.append(h)
    return out


def test_no_handler_points_at_production_log_dir():
    """لا معالِج ملفّات على جذر التسجيل يكتب في artifacts/logs."""
    offenders = [h.baseFilename for h in _file_handlers()
                 if _PROD_LOG_DIR.resolve() in Path(h.baseFilename).resolve().parents]
    assert not offenders, f"معالِجات تكتب في لوق الإنتاج: {offenders}"


def test_handlers_point_at_temp_dir():
    """المعالِجات موجودة فعلًا — وكلّها في المجلّد المؤقّت (لا تُعزَل بإطفاء التسجيل)."""
    handlers = _file_handlers()
    assert handlers, "لا معالِج ملفّات إطلاقًا — العزل يجب أن يُحوِّل لا أن يُطفئ"
    for h in handlers:
        assert _TEST_LOG_DIR.resolve() in Path(h.baseFilename).resolve().parents, h.baseFilename


def test_log_dir_env_overrides_settings():
    """`settings.log_dir` نفسه يشير للمجلّد المؤقّت — فحتى تهيئةٌ جديدة لا تعود للإنتاج."""
    from core.config import Settings

    assert Path(Settings().log_dir).resolve() == _TEST_LOG_DIR.resolve()


def test_emitted_line_lands_in_temp_log(caplog):
    """سطرٌ يُكتَب الآن يظهر في bot.log المؤقّت لا في نظيره الإنتاجيّ."""
    marker = "sentinel-عزل-السجلّات"
    logging.getLogger("core.test_isolation").warning(marker)
    for h in _file_handlers():
        h.flush()
    tmp_log = _TEST_LOG_DIR / "bot.log"
    assert tmp_log.exists() and marker in tmp_log.read_text(encoding="utf-8")

    prod_log = _PROD_LOG_DIR / "bot.log"
    if prod_log.exists():
        assert marker not in prod_log.read_text(encoding="utf-8", errors="replace")
