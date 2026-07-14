"""
مرحلة أ — القيمة المالية أولوية مطلقة + لا خسارة صامتة (§3.5 §0).

المبدأ: أيّ رسالة بمرجع (A/X/SI) + قيمة مالية لا تُسقط أبدًا؛ أيّ انحراف في الهاتف/العملة
لا يُفقد الحوالة. تُختبَر الطبقات: قاعدة الآلاف/العشري، هاتف أيّ دولة، والتصعيد بلا خسارة.

لا مساس بمنطق matching/writer/cancellation/amendment (يُتحقَّق ضمن الحزمة الكاملة).
"""
from __future__ import annotations

from datetime import timedelta

from core.constants import SEED_SUPPLIERS, SEED_TREASURIES
from core.models import RawMessage, SupplierRecord, TreasuryRecord
from core.parsing import parse_message
from core.parsing.normalize import (
    classify_phone, expand_alf_amounts, parse_amount, phone_confidence,
)
from tests.golden_support import ADMIN, CENTRAL, NOW0, SENDER, build_pipeline

_T = [TreasuryRecord(**{"aliases": [], "active": True, **s}) for s in SEED_TREASURIES]
_S = [SupplierRecord(**{"aliases": [], "active": True, **s}) for s in SEED_SUPPLIERS]


# ═════════════════════ إصلاح ١: الآلاف مقابل العشري ═════════════════════
def test_dot_thousands_vs_decimal():
    assert parse_amount("6.200") == 6200      # 3 أرقام بعد النقطة → فاصل آلاف
    assert parse_amount("6.22") == 6.22        # 1-2 → عشري
    assert parse_amount("6.5") == 6.5
    assert parse_amount("45.000") == 45000
    assert parse_amount("1.234.567") == 1234567


def test_negative_becomes_abs_and_alf_expands():
    assert parse_amount("34.540-") == 34540    # سالب → abs()
    assert parse_amount("-35") == 35
    assert expand_alf_amounts("32 ألف")[0] == "32000"   # «ألف» → ×1000
    assert expand_alf_amounts("32 الف")[0] == "32000"


# ═════════════════════ إصلاح ٢: هاتف أيّ دولة (٤ طبقات) ═════════════════════
def test_phone_layers_confidence():
    assert phone_confidence("01062701804") == "high"                       # مصري (طبقة ١)
    assert phone_confidence("21690005505") == "high"                       # تونسي دوليّ (طبقة ١)
    assert phone_confidence("90278328453", "حول +90278328453") == "international"  # تركي (طبقة ٢)
    assert phone_confidence("01234567") == "uncertain"                     # 8 خانات بلا نمط (طبقة ٣)
    assert classify_phone("+90278328453") == "90278328453"                 # لا رفض (القائمة البيضاء أُلغيت)


def test_iban_16plus_not_captured_as_phone():
    """رقم 16+ خانة (IBAN/حساب) لا يُلتقَط كهاتف — حدود الكلمة تمنع بتره إلى 15 (م: X540)."""
    from core.parsing.normalize import extract_phone, extract_phone_candidates
    assert extract_phone("X540 حساب 7766000100009061 مبلغ 5000 ج م") is None
    assert extract_phone_candidates("X540 7766000100009061 5000") == []
    assert extract_phone("رقم 12345678901234567 مبلغ") is None            # 17 خانة أيضًا


def test_egyptian_010_extra_zero_is_high():
    """«010»+9 أرقام (12 خانة، صفر زائد) → مصريّ high بلا تعديل للرقم (م: X542)."""
    assert phone_confidence("010084257553") == "high"
    assert classify_phone("010084257553") == "010084257553"                # كما كُتب، بلا تعديل


def test_code_price_name_order_supplier_line():
    """سطر «كود سعر اسم» («769 6.06 طه») يُلتقَط في أزواج الرسالة الثانية (م: X539)."""
    from core.parsing import extract_code_name_price_lines
    assert extract_code_name_price_lines("769 6.06 طه", _S) == [("769", "طه", "6.06")]
    pairs = extract_code_name_price_lines("1300 عبدالله 5.82\n769 6.06 طه", _S)
    assert ("769", "طه", "6.06") in pairs and ("1300", "عبدالله", "5.82") in pairs
    # «كود مبلغ اسم» (مبلغ صحيح بلا كسر عشريّ) لا يُلتبَس سعرًا
    assert extract_code_name_price_lines("769 5000 محمد", _S) == []


# ═════════════════════ تكامل عبر الأنبوب (إصلاح ٣ + الطبقات) ═════════════════════
async def _drive(db, text, key="m1"):
    pipe = build_pipeline(db)
    raw = RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=SENDER,
                     text=text, received_at=NOW0)
    return await pipe._ingest(raw, NOW0 + timedelta(seconds=1))


async def _admin_texts(db):
    return [o.get("text") or "" for o in await db.outgoing.next_unsent(300)
            if o.get("chat_jid") == ADMIN and not o.get("reaction")]


async def test_dot_thousands_end_to_end(db):
    """«60.000 ج م» → 60000 (نقطة = آلاف) عبر المحلّل."""
    r = parse_message("A480\nفودافون\n01062701804\n60.000 ج م\nصافي", _T, _S)
    assert r.kind == "transfer" and r.leg.amount == 60000


async def test_negative_amount_recorded_and_alerts(db):
    """«34.540-» → 34540 + deviation_log(abs_negative) + تنبيه المالك — لا خسارة."""
    deal = await _drive(db, "A101\nفودافون\n01062701804\n34.540- ج م")
    assert deal is not None and deal.sell_leg.amount == 34540
    assert any(d["method"] == "abs_negative" for d in deal.sell_leg.deviation_log)
    assert any("سالب" in t for t in await _admin_texts(db))


async def test_international_phone_accepted_silently(db):
    """«+90278328453» (تركي) → يُقبَل كما كُتب، confidence=international، بلا تنبيه (طبقة ٢)."""
    deal = await _drive(db, "A102\n+90278328453\n600 ج م")
    assert deal is not None and deal.sell_leg.phone == "90278328453"
    assert deal.sell_leg.phone_confidence == "international"
    assert not any("غير مؤكَّد" in t for t in await _admin_texts(db))


async def test_uncertain_phone_accepted_with_alert(db):
    """«01234567» (8 خانات بلا كود) → يُقبَل + confidence=uncertain + تنبيه «راجع» (طبقة ٣)."""
    deal = await _drive(db, "A103\n01234567\n600 ج م")
    assert deal is not None and deal.sell_leg.phone == "01234567"
    assert deal.sell_leg.phone_confidence == "uncertain"
    assert any("غير مؤكَّد الدولة" in t for t in await _admin_texts(db))


async def test_ref_amount_no_phone_escalates_not_dropped(db):
    """مرجع + مبلغ بلا هاتف → الصفقة **تُسجَّل** (لا تُسقط) + تصعيد 🔴 خطر ماليّ (طبقة ٤)."""
    deal = await _drive(db, "A104\nفودافون\n600 ج م")
    assert deal is not None and deal.sell_leg.amount == 600 and deal.sell_leg.phone is None
    assert any("بلا رقم مستلم" in t and "600" in t for t in await _admin_texts(db))


async def test_ref_only_no_amount_is_noise_no_deal(db):
    """مرجع فقط بلا قيمة → هدرزة حقيقية: لا تُنشأ صفقة (ليست حوالة)."""
    deal = await _drive(db, "A105")
    assert deal is None
