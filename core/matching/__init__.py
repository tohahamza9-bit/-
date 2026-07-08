"""
المطابقة وبوابة الثقة والعلامات والتصعيد (§8, §11.2) — الوكيل A3.

- fuzzy:   مطابقة عربية متسامحة (دوال نقيّة، بلا حالة).
- verify:  بوابة الثقة + مطابقة المبلغ + تحقّق اسم الزبون (§8.2, §11.2).
- service: MatchingService — مطابقة الغرف + التذكير/التصعيد + وضع العلامات (§8.1).
"""
from __future__ import annotations

from .fuzzy import names_match, normalize_ar
from .service import MatchingService
from .verify import amount_matches, trust_gate, verify_customer_name

__all__ = [
    "normalize_ar",
    "names_match",
    "amount_matches",
    "trust_gate",
    "verify_customer_name",
    "MatchingService",
]
