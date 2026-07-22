"""
حارس اتّساق قاموس التصحيحات (بلاغ SI5225/SI5227): قاعدةٌ غير مُتَّسِقة (wrong_text رمزٌ داخل
correct_value، مثل «بلاس»→«بلاس فون») كانت تُضاعِف الاسم حين يُطبَّق فوق ناتجه («بلاس فون» →
«بلاس فون فون») فتفشل مطابقة الخزينة 74 وتُصعَّد الحوالة «لا خزينة».

الإصلاح طبقتان: (١) apply_corrections لا يُطبّق إن كان الصواب حاضرًا سلفًا عند الموضع (حارس موضعيّ)؛
(٢) is_non_idempotent_correction ترفض تعلّم/حفظ مثل هذه القاعدة أصلًا.
"""
from __future__ import annotations

from core.corrections import apply_corrections, is_non_idempotent_correction
from core.models import CorrectionRecord


def _c(w, cv, ft="treasury"):
    return CorrectionRecord(field_type=ft, wrong_text=w, correct_value=cv)


BLAS = _c("بلاس", "بلاس فون")
VODA = _c("فودافون", "فودافون بالخصم")
TYPO = _c("فدفون", "فودافون بالخصم")     # خطأٌ إملائيّ حقيقيّ — ليس رمزًا داخل الصواب


# ═══ الحارس الموضعيّ في apply_corrections ═══
def test_no_double_when_correct_already_present():
    """«بلاس فون» + قاعدة «بلاس→بلاس فون» → يبقى «بلاس فون» (لا «بلاس فون فون»)."""
    assert apply_corrections("الخزينة: بلاس فون", [BLAS])[0] == "الخزينة: بلاس فون"
    assert apply_corrections("بلاس فون", [BLAS])[0] == "بلاس فون"


def test_bare_token_still_corrected():
    """«بلاس» وحدها (بلا «فون») ما زالت تُصحَّح — الحارس لا يُعطّل الفائدة الأصليّة."""
    assert apply_corrections("الخزينة: بلاس", [BLAS])[0] == "الخزينة: بلاس فون"
    assert apply_corrections("بلاس", [BLAS])[0] == "بلاس فون"


def test_vodafone_expansion_not_doubled():
    """«فودافون بالخصم» + قاعدة «فودافون→فودافون بالخصم» → لا تضاعف."""
    assert apply_corrections("فودافون بالخصم", [VODA])[0] == "فودافون بالخصم"


def test_real_typo_correction_preserved():
    """قاعدة تصحيحٍ حقيقيّة (فدفون→فودافون بالخصم) تعمل — الحارس لا يمسّها."""
    assert apply_corrections("فدفون", [TYPO])[0] == "فودافون بالخصم"


def test_si_message_treasury_resolves_after_guard():
    """رسالة SI بـ«الخزينة: بلاس فون» + القاعدة الفاسدة نشطة → النصّ يبقى «بلاس فون» (يُحلّ 74)."""
    txt = "رقم العملية: SI5225\nنوع التحويل: فودافون كاش\nالخزينة: بلاس فون"
    out = apply_corrections(txt, [BLAS, VODA, TYPO])[0]
    assert "بلاس فون فون" not in out
    assert "الخزينة: بلاس فون" in out


# ═══ حارس التعلّم is_non_idempotent_correction ═══
def test_detect_non_idempotent_rules():
    assert is_non_idempotent_correction("بلاس", "بلاس فون") is True
    assert is_non_idempotent_correction("فودافون", "فودافون بالخصم") is True


def test_legit_rules_not_flagged():
    assert is_non_idempotent_correction("فدفون", "فودافون بالخصم") is False    # إملائيّ ≠ رمز داخل الصواب
    assert is_non_idempotent_correction("طه", "عبد الله") is False
    assert is_non_idempotent_correction("بلاس فون", "بلاس فون") is False        # متطابق
