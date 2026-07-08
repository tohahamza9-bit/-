"""
الحارس (منع التكرار §9) والإلغاء/التعديل/التصحيح (§10).

- `idempotency.Guard`: مصدر الحقيقة = الدفتر الداخلي + message key — لا الاسم+المبلغ ولا الرقم الإشاري وحده.
- `corrections`: قيود إلحاقية فقط (Append-only) — عكس البيع=شراء، عكس الشراء=بيع.
"""
from .corrections import build_reversal, is_out_of_active_window
from .idempotency import Guard

__all__ = ["Guard", "build_reversal", "is_out_of_active_window"]
