"""
المرحلة ب — استنتاج العملة من السياق (Context Intelligence).

أولويّة الحسم: ١ كلمة عملة صريحة تفوز دائمًا؛ ٢ استنتاج من currency الخزينة (DB ديناميكيّ)
→ confidence=inferred_from_treasury + deviation_log؛ ٣ استنتاج من الهاتف (مصري→EGP/تونسي→TND)
→ inferred_from_phone؛ ٤ غياب كامل → currency=None + تنبيه.

لا مساس بمنطق matching/writer/cancellation/amendment.
"""
from __future__ import annotations

from datetime import timedelta

from core.constants import Currency, OperationType, SEED_SUPPLIERS, SEED_TREASURIES
from core.models import ParsedLeg, RawMessage, SupplierRecord, TreasuryRecord
from core.parsing import parse_message
from tests.golden_support import ADMIN, CENTRAL, NOW0, SENDER, build_pipeline

_T = [TreasuryRecord(**{"aliases": [], "active": True, **s}) for s in SEED_TREASURIES]
_S = [SupplierRecord(**{"aliases": [], "active": True, **s}) for s in SEED_SUPPLIERS]


def _cur_dev(leg):
    return [d for d in (leg.deviation_log or []) if d.get("field") == "currency"]


# ═════════════════════ أولويّة ٢: استنتاج من الخزينة ═════════════════════
def test_currency_inferred_from_treasury():
    """خزينة معروفة (بلاس فون=EGP) + مبلغ بلا كلمة عملة → EGP باستنتاج + deviation_log."""
    r = parse_message("A5169\n1300 عبدالله معتيق 5.90\nبلاس\n01062701804\n1600", _T, _S)
    assert r.kind == "transfer"
    assert r.leg.currency == Currency.EGP
    assert r.leg.currency_confidence == "inferred_from_treasury"
    dev = _cur_dev(r.leg)
    assert dev and dev[0]["method"] == "inferred_from_treasury" and dev[0]["extracted_value"] == "EGP"


# ═════════════════════ أولويّة ١: الصريحة تفوز على الخزينة ═════════════════════
def test_explicit_currency_wins_over_treasury():
    """كلمة عملة صريحة (د.ت) تفوز حتى مع خزينة مصرية → TND + confidence=explicit، بلا deviation."""
    r = parse_message("A5169\n1300 عبدالله معتيق 5.90\nبلاس\n01062701804\n1600 د ت", _T, _S)
    assert r.leg.currency == Currency.TND
    assert r.leg.currency_confidence == "explicit"
    assert _cur_dev(r.leg) == []


# ═════════════════════ أولويّة ٣: استنتاج من الهاتف ═════════════════════
def test_currency_inferred_from_phone_tnd():
    """هاتف تونسي (216…) بلا عملة/خزينة → TND باستنتاج من الهاتف."""
    r = parse_message("A17\nلمياء\n21690005505\n295", _T, _S)
    assert r.leg.currency == Currency.TND and r.leg.currency_confidence == "inferred_from_phone"


def test_currency_inferred_from_phone_egp():
    """هاتف مصري (01…) بلا عملة/خزينة → EGP باستنتاج من الهاتف."""
    r = parse_message("A18\nفودافون\n01062701804\n600", _T, _S)
    assert r.leg.currency == Currency.EGP and r.leg.currency_confidence == "inferred_from_phone"


# ═════════════════════ أولويّة ٤: غياب كامل → None ═════════════════════
def test_currency_none_when_no_signal():
    """بلا عملة صريحة ولا خزينة ولا هاتف مُستنتَج (ليبي) → currency=None + confidence=None."""
    r = parse_message("A19\nفودافون\n0912345678\n600", _T, _S)
    assert r.leg.currency is None and r.leg.currency_confidence is None


async def test_priority4_missing_currency_alerts(db):
    """أولويّة ٤: مبلغ موجود بلا عملة مؤكَّدة → تنبيه للمراجعة (best-effort، لا يمسّ الربط/الكتابة)."""
    pipe = build_pipeline(db)
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="A20",
                    amount=600.0, currency=None, phone="0912345678")
    raw = RawMessage(message_key="p4", chat_jid=CENTRAL, sender_jid=SENDER,
                     text="A20", received_at=NOW0)
    await pipe._phase_a_alerts(leg, raw)
    texts = [o.get("text") or "" for o in await db.outgoing.next_unsent(300)
             if o.get("chat_jid") == ADMIN and not o.get("reaction")]
    assert any("بلا عملة مؤكَّدة" in t for t in texts)
