# -*- coding: utf-8 -*-
"""حارس تطابق العملة (P1) — الثغرة الماليّة «الحالة E».

الجذر: لم يكن في الشيفرة كلّها أيّ تدقيق يقارن عملة الحوالة بعملة خزينتها. العملة تُستنتَج
**من** الخزينة في ثلاثة مواضع (`parser.py:1152`، `_ai_rescue`، `_apply_fragment`) ولا تُقارَن
**بها** قطّ — فخزينةٌ تونسيّة كانت تلتصق بحوالةٍ مصريّة وتُكتَب، فيذهب المال إلى حسابٍ بعملةٍ
خاطئة بلا أيّ مانع.

الحارس في `Pipeline._trust_gate` (نقطة الاختناق الواحدة قبل `build_write_jobs`) — لا في
المفكِّك: الخزينة تبقى مربوطةً وظاهرةً للمراجع، وتُمنَع **الكتابة** وحدها. لذلك لا تتأثّر
الذخيرة الذهبية ولا حرّاس الربط 2-A.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.constants import Currency, Mark, OperationType, Status, TreasuryType
from core.models import Deal, ParsedLeg, TreasuryRef
from core.pipeline import Pipeline

_NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)

_TND = TreasuryRef(code="58", name="عمر العاصمة", type=TreasuryType.SELL_ONLY,
                   currency=Currency.TND)
_EGP = TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY,
                   currency=Currency.EGP)
_NOCUR = TreasuryRef(code="99", name="خزينة بلا عملة", type=TreasuryType.SELL_ONLY,
                     currency=None)


def _leg(currency, treasury, **over) -> ParsedLeg:
    base = dict(operation=OperationType.SELL, reference_number="X9001", amount=1000.0,
                price_raw="6.0", price_normalized="6.0", customer_code="390",
                customer_name="العربي", currency=currency, treasury=treasury)
    base.update(over)
    return ParsedLeg(**base)


def _deal(leg: ParsedLeg) -> Deal:
    return Deal(deal_id="d-cur", chat_jid="c@g.us", sell_leg=leg, status=Status.MATCHING,
                created_at=_NOW, updated_at=_NOW, first_received_at=_NOW)


def _gate(deal: Deal):
    """بوّابة الثقة وحدها — دالّة نقيّة لا تحتاج قاعدة ولا أنبوبًا حيًّا."""
    return Pipeline._trust_gate(Pipeline.__new__(Pipeline), deal)


# ═══════════════════════════════════════════════════════════════════════════
# (أ) صفقة EGP + خزينة TND مذكورة بالاسم → تُعلَّق، لا تُكتب
# ═══════════════════════════════════════════════════════════════════════════
def test_egp_deal_with_tnd_treasury_is_blocked():
    """الحالة E حرفيًّا: حوالة مصريّة على خزينة «عمر العاصمة» (58، TND)."""
    ok, reason = _gate(_deal(_leg(Currency.EGP, _TND)))
    assert ok is False, "خزينة بعملة مخالفة مرّت — المال يذهب لحساب بعملة خاطئة"
    assert "عملة الخزينة" in reason and "عملة الحوالة" in reason
    assert "TND" in reason and "EGP" in reason
    assert "عمر العاصمة" in reason, "السبب لا يسمّي الخزينة — المراجع لن يعرف ماذا يصحّح"


def test_tnd_deal_with_egp_treasury_is_blocked():
    """الاتّجاه المعاكس محروسٌ أيضًا (لا حراسة في اتّجاه واحد)."""
    ok, reason = _gate(_deal(_leg(Currency.TND, _EGP)))
    assert ok is False
    assert "بلاس فون" in reason


def test_currency_conflict_predicate_matches_gate():
    """المُسنَد المستعمَل في فرع التصعيد هو نفسه المستعمَل في البوّابة (لا انحراف بينهما)."""
    assert Pipeline._currency_conflict(_leg(Currency.EGP, _TND)) is True
    assert Pipeline._currency_conflict(_leg(Currency.TND, _TND)) is False


# ═══════════════════════════════════════════════════════════════════════════
# (ب) عملة مطابقة → تمرّ عاديًّا
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("currency,treasury", [(Currency.TND, _TND), (Currency.EGP, _EGP)])
def test_matching_currency_passes(currency, treasury):
    ok, reason = _gate(_deal(_leg(currency, treasury)))
    assert ok is True and reason is None


# ═══════════════════════════════════════════════════════════════════════════
# (ج) عملة غير معروفة → السلوك القديم بلا تغيير (لا نقسو على الغامض §0)
# ═══════════════════════════════════════════════════════════════════════════
def test_unknown_treasury_currency_keeps_old_behaviour():
    ok, reason = _gate(_deal(_leg(Currency.EGP, _NOCUR)))
    assert ok is True and reason is None


def test_unknown_leg_currency_keeps_old_behaviour():
    ok, reason = _gate(_deal(_leg(None, _TND)))
    assert ok is True and reason is None


def test_no_treasury_still_reports_its_own_reason():
    """لا يبتلع الحارسُ الجديد سببَ «لا خزينة محلولة» القائم (ترتيب الفحوص محفوظ)."""
    ok, reason = _gate(_deal(_leg(Currency.EGP, None)))
    assert ok is False and reason == "لا خزينة محلولة — تعذّر تحديد الحساب"


# ═══════════════════════════════════════════════════════════════════════════
# (د) عكسيّ — كل الخزائن الفعّالة بعملتها الصحيحة: صفر تعليق
# ═══════════════════════════════════════════════════════════════════════════
def _seeded_treasuries():
    from core.constants import SEED_TREASURIES
    return [t for t in SEED_TREASURIES if t.get("currency")]


@pytest.mark.parametrize("seed", _seeded_treasuries(),
                         ids=[t["name"] for t in _seeded_treasuries()])
def test_no_false_hold_for_any_seeded_treasury(seed):
    """كل خزينة مبذورة على حوالةٍ بعملتها الصحيحة → تمرّ. أيّ تعليقٍ هنا إطلاقٌ كاذب."""
    tref = TreasuryRef(code=seed["code"] or "X", name=seed["name"],
                       type=TreasuryType(seed["type"]), currency=Currency(seed["currency"]))
    ok, reason = _gate(_deal(_leg(Currency(seed["currency"]), tref)))
    assert ok is True, f"إطلاق كاذب على «{seed['name']}»: {reason}"


# ═══════════════════════════════════════════════════════════════════════════
# (هـ) العملة المستنتَجة من الخزينة لا يمكن أن تتعارض معها (بالبناء)
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("treasury", [_TND, _EGP])
def test_currency_inferred_from_treasury_never_conflicts(treasury):
    """`parser.py` يُسنِد `currency = trec.currency` عند غيابها ويسم المصدر — فالتساوي بالبناء."""
    leg = _leg(treasury.currency, treasury, currency_confidence="inferred_from_treasury")
    assert Pipeline._currency_conflict(leg) is False
    assert _gate(_deal(leg))[0] is True


# ═══════════════════════════════════════════════════════════════════════════
# الطرف الثاني (شراء) محروسٌ كذلك — البوّابة تمرّ على الطرفين
# ═══════════════════════════════════════════════════════════════════════════
def test_buy_leg_conflict_is_caught():
    deal = _deal(_leg(Currency.TND, _TND))
    deal.buy_leg = _leg(Currency.EGP, _TND, operation=OperationType.BUY)
    deal.is_two_legged = True
    ok, reason = _gate(deal)
    assert ok is False and "عملة الخزينة" in reason


# ═══════════════════════════════════════════════════════════════════════════
# تكامل: تُعلَّق ⚠️ وتُبلَّغ للمسؤول ولا تصل الكاتب
# ═══════════════════════════════════════════════════════════════════════════
async def test_currency_conflict_holds_warns_and_notifies_admin(db):
    """السلوك المطلوب (الخيار أ): HELD + ⚠️ + إبلاغ المسؤول، وصفر كتابة — عبر الأنبوب كاملًا.

    النصّ يُنتج عملةً **صريحة** EGP («5000 مصري») مع خزينة «عمر» = 58 التونسيّة (TND).
    """
    from tests.test_integration import (ADMIN, PAST, FakeWriter, _enable_storage,  # type: ignore
                                        _make_pipeline, _raw)

    await _enable_storage(db)
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, rooms=False)      # بلا غرف → مباشرةً لبوّابة الثقة
    text = "A56\n1234 احمد علي 5.9\n01000000000\n5000 مصري\nعمر"
    await pipe.capture(_raw("cur1", text))
    await pipe.process_inbox(PAST + timedelta(seconds=120))
    await pipe.tick(PAST + timedelta(seconds=220))      # المهلة → PARSED مفرد → بوّابة الثقة

    d = await db.deals.find_by_source_key("cur1")
    assert d.sell_leg.currency == Currency.EGP and d.sell_leg.treasury.code == "58", \
        "التجهيزة نفسها لم تُنتج التعارض المقصود"
    assert d.status == Status.HELD, "تعارض العملة يجب أن يُعلَّق لا أن يمرّ"
    assert d.mark == Mark.WARN
    assert writer.calls == [], "كُتبت حوالة بعملة مخالفة لخزينتها"
    outs = await db.outgoing.next_unsent(100)
    admin = [o for o in outs if o["chat_jid"] == ADMIN and "عملة الخزينة" in (o.get("text") or "")]
    assert admin, "لم يُبلَّغ المسؤول — تعليقٌ صامتٌ يضيع في الطابور"
