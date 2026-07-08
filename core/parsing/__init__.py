"""
وحدة الفهم (parsing) — MONEYADO Bot (§3، §4، §5، §6، §10).

الواجهة العامّة:
- parse_message(text, treasuries, suppliers) → ParseResult
- detect_control(text) → Optional[(action, value)]  (§10)

وحدة مشتركة بين الكاتبَين (§2.1) — تفهم الحوالة من بنيتها وقيمها لا من كلمات حرفية.
"""
from __future__ import annotations

from .classify import detect_control, is_out_of_scope
from .parser import parse_message

__all__ = ["parse_message", "detect_control", "is_out_of_scope"]
