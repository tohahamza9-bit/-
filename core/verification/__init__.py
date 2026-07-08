"""
التحقّق بعد الحفظ + استرجاع الحالة (§11.4 §12 §9).

- SqlVerifier: قراءة فقط من SQL Server (pyodbc) للتأكد أن الحوالة نزلت.
- recover_pending: بعد الإطفاء المفاجئ، يفحص ما كان قيد الإدخال ويمنع الازدواج.
"""
from .recovery import recover_pending
from .sql_verifier import SqlVerifier

__all__ = ["SqlVerifier", "recover_pending"]
