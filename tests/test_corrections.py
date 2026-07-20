"""
قاموس التصحيحات الحيّ — استبدالٌ نصّيّ يسري فورًا (يغلب الثابت) + CRUD اللوحة (مدير).

طلب المالك: جدول corrections، Parser يراجعه أوّلًا (dynamic wins)، حيّ بلا إعادة تشغيل،
زرّ حفظ في الانتباه، صفحة مدير (عرض/بحث/حذف/إضافة). مثال: «فودا»→«فودافون» (channel).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.corrections import apply_corrections, build_correction_map
from core.models import CorrectionRecord

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


def _c(wrong, correct, ft="channel", active=True):
    return CorrectionRecord(field_type=ft, wrong_text=wrong, correct_value=correct, active=active)


# ═════════════════════════════════════════════════════════════════════════════
# الدالّة النقيّة apply_corrections
# ═════════════════════════════════════════════════════════════════════════════
def test_example_channel_correction():
    """المثال الحرفيّ: «فودا» → «فودافون»."""
    out, fired = apply_corrections("فودا", [_c("فودا", "فودافون")])
    assert out == "فودافون" and fired == ["فودا"]


def test_does_not_break_longer_word():
    """«فودافون» يبقى كما هو — الاستبدال على رمزٍ كامل لا سلسلةٍ جزئيّة."""
    assert apply_corrections("فودافون", [_c("فودا", "فودافون")])[0] == "فودافون"


def test_glued_to_price_is_split():
    """«طه6.07» → «طه بريك 6.07» — البادئة العربيّة تُفصَل عن السعر فيُصحَّح الرمز وحده."""
    out, fired = apply_corrections("طه6.07", [_c("طه", "طه بريك", "supplier")])
    assert out == "طه بريك 6.07" and fired == ["طه"]


def test_inside_full_message():
    """يُطبَّق داخل رسالة متعدّدة الأسطر بلا مساس بالباقي."""
    msg = "X1609\n01044669692\nالقيمة 11000 جنيه مصري\nفودا\n\n108 رحيم سبها6.08"
    out, _ = apply_corrections(msg, [_c("فودا", "فودافون")])
    assert "\nفودافون\n" in out and "01044669692" in out


def test_ambiguous_wrong_text_is_dropped():
    """رمزٌ خاطئٌ بقيمتين مختلفتين → التباسٌ يُسقَط كلاهما (لا تخمين §0)."""
    assert build_correction_map([_c("فودا", "فودافون"), _c("فودا", "انستا")]) == {}


def test_inactive_is_ignored():
    assert apply_corrections("فودا", [_c("فودا", "فودافون", active=False)]) == ("فودا", [])


def test_empty_inputs():
    assert apply_corrections("فودا", []) == ("فودا", [])
    assert apply_corrections("", [_c("فودا", "فودافون")]) == ("", [])


def test_normalized_match_ignores_diacritics():
    """المطابقة على المطبَّع العربيّ — تلتقط الرمز رغم اختلاف التشكيل/الألف."""
    out, fired = apply_corrections("فودآ", [_c("فودا", "فودافون")])
    assert out == "فودافون" and fired  # «فودآ» تُطبَّع «فودا»


# ═════════════════════════════════════════════════════════════════════════════
# حيّ عبر المُفكِّك (dynamic wins، بلا إعادة تشغيل)
# ═════════════════════════════════════════════════════════════════════════════
async def test_parse_message_uses_correction_to_resolve_treasury(db):
    """تصحيحٌ يحوّل رمزًا غير محلول إلى alias خزينة موجودة → تُحلّ الخزينة."""
    from core.parsing import parse_message
    tre = await db.treasuries.all_active()
    sup = await db.suppliers.all_active()
    msg = "X9\n0917133939\nامين\nصفاقس\n3795دت\nمحموذ"      # «محموذ» خطأ إملاء «محمود»
    r0 = parse_message(msg, tre, sup)
    assert r0.leg is None or r0.leg.treasury is None       # بلا تصحيح لا تُحلّ
    corr = [_c("محموذ", "محمود", "treasury")]
    r1 = parse_message(msg, tre, sup, corr)
    assert r1.leg is not None and r1.leg.treasury is not None
    assert "محمود" in r1.leg.treasury.name


async def test_correction_repo_roundtrip_and_delete(db):
    """المستودع: upsert (حيّ في all_active) → list_all → delete."""
    await db.corrections.upsert(_c("فودا", "فودافون"))
    active = await db.corrections.all_active()
    assert any(c.wrong_text == "فودا" and c.correct_value == "فودافون" for c in active)
    # upsert ثانٍ يعدّل بلا ازدواج (نفس المفتاح المطبَّع)
    await db.corrections.upsert(_c("فودا", "فودافون كاش"))
    active = await db.corrections.all_active()
    assert len([c for c in active if c.wrong_text == "فودا"]) == 1
    assert (await db.corrections.delete("فودا")) == 1
    assert not any(c.wrong_text == "فودا" for c in await db.corrections.all_active())


async def test_repo_bump_usage(db):
    await db.corrections.upsert(_c("فودا", "فودافون"))
    await db.corrections.bump_usage(["فودا"])
    rows = await db.corrections.list_all()
    assert next(r for r in rows if r["wrong_text"] == "فودا")["times_used"] == 1


async def test_repo_search(db):
    await db.corrections.upsert(_c("فودا", "فودافون", "channel"))
    await db.corrections.upsert(_c("محموذ", "محمود", "treasury"))
    assert len(await db.corrections.list_all("فود")) == 1
    assert len(await db.corrections.list_all()) == 2


async def test_pipeline_bumps_usage_end_to_end(db):
    """عبر _ingest: التصحيح يُطبَّق وتُزاد times_used فعليًّا."""
    from core.bus import Bus
    from core.models import RawMessage, WriteResult
    from core.pipeline import Pipeline

    class _W:
        name = "w"
        async def write(self, job, *, commit):
            return WriteResult(ok=True)

    class _V:
        enabled = False
        async def verify_transaction(self, *a, **k):
            return (False, None)
        async def find_last_pending(self, *a, **k):
            return None

    C, A = "central@g.us", "admin@g.us"
    pipe = Pipeline(db, Bus(db, {C, A}, C, A), _W(), _V(),
                    customer_room_jids=[], treasury_room_jids=[])
    await db.corrections.upsert(_c("فودا", "فودافون"))
    raw = RawMessage(message_key="c1", chat_jid=C, sender_jid="s@lid",
                     text="X1\n01044669692\n1000 جنيه\nفودا", received_at=NOW)
    await pipe._ingest(raw, NOW)
    rows = await db.corrections.list_all()
    assert next(r for r in rows if r["wrong_text"] == "فودا")["times_used"] >= 1


# ═════════════════════════════════════════════════════════════════════════════
# استخراج أزواج «خطأ→صحيح» من deviation_log لزرّ الحفظ التلقائيّ (الانتباه)
# ═════════════════════════════════════════════════════════════════════════════
def test_ai_correction_pairs_extracts_clean_pairs():
    from dashboard.transfers import _ai_correction_pairs
    deal = {"sell_leg": {"deviation_log": [
        {"field": "supplier", "raw_value": "طهه", "extracted_value": "طه", "method": "ai"},
        {"field": "ai_first", "raw_value": "حوالة بلا خزينة محلولة", "extracted_value": "…", "method": "ai_first"},
        {"field": "treasury", "raw_value": "محموذ", "extracted_value": "محمود صفاقس", "method": "ai"},
    ]}}
    pairs = _ai_correction_pairs(deal)
    assert {"wrong_text": "طهه", "correct_value": "طه", "field_type": "supplier"} in pairs
    assert {"wrong_text": "محموذ", "correct_value": "محمود صفاقس", "field_type": "treasury"} in pairs
    # ai_first (raw_value = سبب لا رمز) مُستبعَد
    assert not any(p["wrong_text"].startswith("حوالة") for p in pairs)


def test_ai_correction_pairs_skips_sentences_and_dupes():
    from dashboard.transfers import _ai_correction_pairs
    deal = {"sell_leg": {"deviation_log": [
        {"field": "treasury", "raw_value": "جربه ميدون طويلة", "extracted_value": "صالح", "method": "ai"},
        {"field": "supplier", "raw_value": "طه", "extracted_value": "طه", "method": "ai"},  # لا فرق
    ]}, "buy_leg": {"deviation_log": [
        {"field": "supplier", "raw_value": "براق", "extracted_value": "البراق", "method": "ai"},
        {"field": "supplier", "raw_value": "براق", "extracted_value": "البراق", "method": "ai"},  # مكرّر
    ]}}
    pairs = _ai_correction_pairs(deal)
    assert pairs == [{"wrong_text": "براق", "correct_value": "البراق", "field_type": "supplier"}]
