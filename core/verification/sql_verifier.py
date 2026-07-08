"""
التحقّق من SQL Server — قراءة فقط (§11.4) 🔴.

> بعد كل «تخزين»: قراءة من SQL Server (مستخدم قراءة فقط — ليس sa) للتأكد أن الحوالة
> نزلت (المبلغ + الزبون + النوع) → عندها فقط ✅ وتحديث الدفتر بمرجع MONEYADO.

قواعد صارمة:
- **قراءة فقط:** لا يُبنى أو يُنفَّذ أي استعلام INSERT/UPDATE/DELETE إطلاقًا — يُتحقَّق
  من نصّ كل استعلام عند التهيئة (يفشل بوضوح إن خالف — T5).
- **القاعدة الحيّة يُمنع الاختبار عليها** (§11.4): الـ DSN يُقرأ من env ويجب أن يشير
  إلى نسخة SQL تجريبية معزولة مسترجَعة بـ REST_ADO — لا إلى MONEYADO.mdf الحيّة.
- pyodbc متزامن → يُنفَّذ عبر asyncio.to_thread حتى لا يحجب حلقة الأحداث.
- إن `enabled=False` (النسخة التجريبية لم تُجهَّز بعد) → نتيجة محايدة آمنة مع تحذير،
  ولا يدّعي التحقّق (القاعدة الذهبية §0: لو شكّيت لا تُنزّل/لا تعلّم ✅).

ملاحظة تشغيل: pyodbc قد لا يكون مثبّتًا على بيئة التطوير — الاستيراد محروس.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Callable, Optional

from ..constants import OperationType
from ..logging_setup import get_logger

log = get_logger(__name__)

# ── استيراد محروس (pyodbc غير مثبّت في بيئة التطوير/الاختبار) ─────────────────
try:  # pragma: no cover - يعتمد على بيئة التشغيل
    import pyodbc  # type: ignore
except ImportError:  # لا نبتلع بصمت (T5) — نسجّل السبب
    pyodbc = None  # type: ignore
    log.warning(
        "pyodbc غير مثبّت — التحقّق من SQL Server معطّل فعليًا حتى تثبيته على الجهاز الفرعي (§16 م0)."
    )


# الكلمات الممنوعة في أي استعلام (قراءة فقط §11.4) — أي منها يُبطل التهيئة.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE|ALTER|MERGE|EXEC|EXECUTE|CREATE|GRANT|REVOKE|SET)\b",
    re.IGNORECASE,
)


def _assert_read_only(sql: str, key: str) -> None:
    """يفشل بوضوح إن كان الاستعلام ليس SELECT محضًا (§11.4 قراءة فقط)."""
    stripped = sql.lstrip()
    if not re.match(r"(?is)^\s*(SELECT|WITH)\b", stripped):
        raise ValueError(
            f"استعلام غير مسموح '{key}': يجب أن يبدأ بـ SELECT (قراءة فقط §11.4) — {sql!r}"
        )
    match = _FORBIDDEN.search(sql)
    if match:
        raise ValueError(
            f"استعلام يحوي كلمة كتابة ممنوعة '{match.group(1)}' في '{key}' "
            f"(مستخدم قراءة فقط §11.4) — {sql!r}"
        )


def _default_connect(dsn: str):  # pragma: no cover - يتطلّب SQL Server حقيقيًا
    """اتصال pyodbc فعلي بوضع قراءة فقط (طبقة أمان إضافية فوق صلاحيات المستخدم)."""
    if pyodbc is None:
        raise RuntimeError("pyodbc غير مثبّت — تعذّر الاتصال بـ SQL Server.")
    return pyodbc.connect(dsn, readonly=True)


def _as_str(value: Any) -> Optional[str]:
    return str(value) if value is not None else None


class SqlVerifier:
    """
    يقرأ الاستعلامات من إعداد `sql_queries.json` (المُحمَّل مسبقًا كـ dict) ويستعلم
    SQL Server قراءةً فقط. `connect` قابل للحقن لتسهيل الاختبار (mock connection).
    """

    def __init__(
        self,
        dsn: str,
        queries_config: dict,
        enabled: bool,
        *,
        connect: Optional[Callable[[str], Any]] = None,
    ) -> None:
        self.dsn = dsn
        self.enabled = enabled
        self._config = queries_config or {}
        self._connect = connect or _default_connect
        # نحتاج pyodbc فقط مع الاتصال الافتراضي؛ الحقن في الاختبار لا يتطلّبه.
        self._needs_pyodbc = connect is None

        tables = self._config.get("tables", {})
        columns = self._config.get("columns", {})
        # خريطة تعبئة {placeholders} في قوالب الاستعلامات (جداول + أعمدة).
        self._render_map = {**tables, **columns}
        # خريطة اختيارية: قيمة نوع العملية في SQL (ملحق ب-2 — تُستكمل وقت التنفيذ).
        # الافتراضي = قيمة الـ enum ('sell'/'buy') إن لم تُحدَّد.
        self._operation_codes: dict = self._config.get("operation_codes", {})

        # نبني نصوص الاستعلامات النهائية ونتحقّق أنها قراءة فقط (يفشل بوضوح إن خالف).
        self._sql: dict[str, str] = {}
        for key, template in self._config.get("queries", {}).items():
            rendered = template.format(**self._render_map)
            _assert_read_only(rendered, key)
            self._sql[key] = rendered

        if self.enabled and not self._sql:
            raise ValueError(
                "sql_enabled=True لكن لا توجد استعلامات في sql_queries.json — "
                "استكمِل المعلّق التقني (ملحق ب-2) قبل التشغيل."
            )
        if not self.enabled:
            log.warning(
                "SqlVerifier مُهيّأ بـ enabled=False — لن يُجرى تحقّق فعلي (النسخة التجريبية §11.4 لم تُجهَّز)."
            )

    # ── تنفيذ متزامن (يُغلَّف بـ to_thread) ──────────────────────────────────
    def _run_select(self, query_key: str, params: tuple) -> Optional[tuple]:
        """يفتح اتصالًا، ينفّذ SELECT واحدًا، يُرجع الصف الأول (أو None). قراءة فقط."""
        sql = self._sql[query_key]
        conn = self._connect(self.dsn)
        try:
            cursor = conn.cursor()
            cursor.execute(sql, params)
            return cursor.fetchone()
        finally:
            close = getattr(conn, "close", None)
            if callable(close):
                close()

    def _operation_value(self, operation: OperationType) -> Any:
        """يحوّل نوع العملية إلى القيمة المخزَّنة في عمود OperationType (قابل للإعداد)."""
        raw = operation.value if isinstance(operation, OperationType) else str(operation)
        return self._operation_codes.get(raw, raw)

    # ── الواجهة العامّة (async) ──────────────────────────────────────────────
    async def verify_transaction(
        self,
        reference_number: Optional[str],
        amount: float,
        customer_code: Optional[str],
        operation: OperationType,
    ) -> tuple[bool, Optional[str]]:
        """
        يستعلم SQL للتأكد أن الحوالة نزلت (المبلغ + الزبون + النوع) — §11.4.
        يُرجع (verified, moneyado_ref). عند التعذّر/الشك → (False, None) آمنة محايدة.
        """
        if not self.enabled:
            log.warning(
                "التحقّق من SQL معطّل (sql_enabled=False) — لا ندّعي التحقّق للحوالة %s؛ نتيجة محايدة آمنة (§0).",
                reference_number,
            )
            return (False, None)
        if self._needs_pyodbc and pyodbc is None:
            log.error(
                "sql_enabled=True لكن pyodbc غير مثبّت — تعذّر التحقّق من الحوالة %s (لن نعلّم ✅).",
                reference_number,
            )
            return (False, None)

        params = (reference_number, amount, customer_code, self._operation_value(operation))
        try:
            row = await asyncio.to_thread(self._run_select, "verify_transaction", params)
        except Exception:  # لا silent catch (T5) — خطأ في بوت مالي يُسجَّل كاملًا
            log.exception("فشل استعلام التحقّق من SQL للحوالة %s", reference_number)
            return (False, None)

        if row is None:
            log.info(
                "SQL: الحوالة %s (مبلغ=%s زبون=%s نوع=%s) غير موجودة — لم تُؤكَّد بعد.",
                reference_number, amount, customer_code, operation,
            )
            return (False, None)

        moneyado_ref = _as_str(row[0])
        log.info("SQL ✅ الحوالة %s مؤكّدة — مرجع MONEYADO=%s", reference_number, moneyado_ref)
        return (True, moneyado_ref)

    async def find_last_pending(self, reference_number: Optional[str]) -> Optional[dict]:
        """
        فحص ما قبل إعادة المحاولة بعد التعطّل (§12 §9): هل حُفظت آخر حوالة كانت قيد الإدخال؟
        يُرجع dict {ref, refnum, amount} إن وُجدت، وإلا None.
        """
        if not self.enabled:
            log.warning(
                "find_last_pending: SQL معطّل — لا يمكن الجزم بحفظ الحوالة %s (نتيجة محايدة).",
                reference_number,
            )
            return None
        if self._needs_pyodbc and pyodbc is None:
            log.error("find_last_pending: pyodbc غير مثبّت — تعذّر فحص الحوالة %s.", reference_number)
            return None

        try:
            row = await asyncio.to_thread(self._run_select, "last_pending", (reference_number,))
        except Exception:  # T5
            log.exception("فشل استعلام find_last_pending للحوالة %s", reference_number)
            return None

        if row is None:
            return None
        return {"ref": _as_str(row[0]), "refnum": _as_str(row[1]), "amount": row[2]}
