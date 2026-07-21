"""
تصنيف: نوع العملية §5 + تمييز الطرف §5.3 + كشف رسائل التحكّم §10.
"""
from __future__ import annotations

import re
from typing import Optional

from core.constants import (
    CANCEL_KEYWORDS,
    CONFIRM_KEYWORDS,
    EDIT_KEYWORDS,
    RERUN_KEYWORDS,
    EXPLICIT_BUY_KEYWORDS,
    EXPLICIT_SELL_KEYWORDS,
    HAND_DELIVERY_KEYWORDS,
    OUT_OF_SCOPE_PHRASES,
    OUT_OF_SCOPE_WORDS,
    SILENT_IGNORE_KEYWORDS,
    OperationType,
)

from .normalize import normalize_ar, parse_amount

# ── تقطيع نصّي إلى كلمات عربية/رقمية (لمطابقة الكلمات الكاملة) ────────────────
_WORD_SPLIT = re.compile(r"[^ء-ي0-9]+")
_CORRECT_KEYWORDS = ("تصحيح", "تصليح")


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_SPLIT.split(normalize_ar(text)) if t}


def _has_keyword(text: str, keywords: list[str]) -> bool:
    tokens = _tokens(text)
    return any(normalize_ar(k) in tokens for k in keywords)


def has_silent_keyword(text: str) -> bool:
    """§7.2: «صرف»/«قبض» → تجاهل صامت."""
    return _has_keyword(text, SILENT_IGNORE_KEYWORDS)


def is_out_of_scope(text: str) -> bool:
    """§0: تسليم يدوي/باليد → خارج نطاق البوت (يُصعَّد، لا يُدخَل).

    كلمات كاملة («تسليم»/«باليد») + عبارات كاملة («في يد»/«قاهره بيد») — لتفادي
    false positives مثل «بيد» وحدها. المطابقة بعد التطبيع (normalize_ar).
    """
    if not text or not text.strip():
        return False
    norm = normalize_ar(text)
    tokens = _tokens(text)
    if any(normalize_ar(w) in tokens for w in OUT_OF_SCOPE_WORDS):
        return True
    return any(normalize_ar(p) in norm for p in OUT_OF_SCOPE_PHRASES)


def detect_hand_delivery(text: str) -> bool:
    """تسليم يد (delivery_type=يد): علامة «باليد/بيد/تسليم يد» صريحة (كلمات كاملة) — تُميّز الحوالة
    كتسليمٍ يدويّ. أوسع من is_out_of_scope في أنها تُطبَّق **حتى مع وجود رقم إشاري** (X1702)، وأضيق
    في أنها **لا** تشمل «تسليم» وحدها (تسليمٌ لمستلم). النتيجة تُلتقَط على الطرف لا تُسقِط الحوالة."""
    return _has_keyword(text, HAND_DELIVERY_KEYWORDS)


def detect_explicit_operation(text: str) -> tuple[Optional[OperationType], bool]:
    """§5.1: كلمة «شراء»/«بيع» الصريحة تحكم وتتجاوز الافتراضي. يُرجع (op|None, explicit)."""
    tokens = _tokens(text)
    if any(normalize_ar(k) in tokens for k in EXPLICIT_BUY_KEYWORDS):
        return OperationType.BUY, True
    if any(normalize_ar(k) in tokens for k in EXPLICIT_SELL_KEYWORDS):
        return OperationType.SELL, True
    return None, False


def _extract_number(text: str) -> Optional[float]:
    """يستخرج قيمة رقمية من رسالة تحكّم («تعديل 9000»→9000) بقاعدة المبلغ (§3.5)."""
    m = re.search(r"\d[\d.,'،\s]*\d|\d", text)
    return parse_amount(m.group()) if m else None


def detect_control(text: str) -> Optional[tuple[str, Optional[float]]]:
    """يكشف الإلغاء/التعديل/التأكيد/التصحيح (§10) على مستوى النص.

    يُرجع (action, value) حيث action ∈ {'cancel','edit','confirm','correct'} أو None."""
    if not text or not text.strip():
        return None
    tokens = _tokens(text)
    if any(normalize_ar(k) in tokens for k in RERUN_KEYWORDS):
        return ("rerun", None)                # (بند 3) إعادة تشغيل يدويّة — قبل الإلغاء (كلمات متمايزة)
    if any(normalize_ar(k) in tokens for k in CANCEL_KEYWORDS):
        return ("cancel", None)
    if any(normalize_ar(k) in tokens for k in EDIT_KEYWORDS):
        return ("edit", _extract_number(text))
    if any(normalize_ar(k) in tokens for k in _CORRECT_KEYWORDS):
        return ("correct", _extract_number(text))
    if any(normalize_ar(k) in tokens for k in CONFIRM_KEYWORDS):
        return ("confirm", None)
    return None
