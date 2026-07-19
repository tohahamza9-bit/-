"""
السياق الحيّ لطبقة الفهم الذكي — «قاموس حيّ + سياق المُرسِل».

المبدأ: لا قوائم ثابتة ولا cache. كل نداء يقرأ الكيانات من DB **لحظته** ويضمّ تاريخ المُرسِل.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.ai_context import build_sender_context
from core.ai_understanding import (AiProposal, build_prompt, validate_line_parse,
                                   validate_postal)
from core.constants import Currency, OperationType, Status, TreasuryType
from core.models import Deal, DetectionConfig, ParsedLeg, SupplierRecord, TreasuryRecord, TreasuryRef

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
SENDER = "emp1@lid"


class _RecBus:
    central_jid, admin_jid = CENTRAL, "admin@g.us"

    def __init__(self):
        self.admin_msgs: list[str] = []

    async def notify_admin(self, text, reply_to_key=None, forward_key=None):
        self.admin_msgs.append(text)

    async def reply_central(self, text, reply_to_key=None, *, is_alert=False): ...


class _SpyAi:
    """يلتقط الـprompt الفعليّ المُرسَل — لنتحقّق ممّا رآه النموذج."""

    def __init__(self, data=None):
        self.data = data or {}
        self.prompts: list[str] = []
        self.contexts: list[object] = []

    async def propose(self, text, treasuries, suppliers, known_shapes=None, sender_context=None):
        self.prompts.append(build_prompt(text, treasuries, suppliers, known_shapes, sender_context))
        self.contexts.append(sender_context)
        return AiProposal(data=self.data, model="fake/model", latency_ms=1)


def _pipe(db, bus, ai):
    from core.pipeline import Pipeline

    class _W:
        name = "w"
        async def write(self, job, *, commit): ...
    return Pipeline(db, bus, _W(), None, customer_room_jids=[], treasury_room_jids=[],
                    ai_client=ai)


def _raw(text="X9\nمرجع بلا تفكيك", key="m1", sender=SENDER):
    from core.models import RawMessage
    return RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=sender,
                      text=text, received_at=NOW)


async def _enable_ai(db):
    await db.detection.set(DetectionConfig(ai_enabled=True, ai_model="fake/model",
                                           ai_confidence_threshold=0.9))


async def _completed_deal(db, i, *, treasury="فودافون بالخصم", tcode="85",
                          price="6.04", phone="01000000000", sender=SENDER, si=False,
                          created=None):
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=f"R{i}",
                    customer_code="1", customer_name="زبون", amount=1000.0,
                    currency=Currency.EGP, price_normalized=price, phone=phone,
                    sender_jid=sender, is_si_format=si,
                    treasury=TreasuryRef(code=tcode, name=treasury,
                                         type=TreasuryType.SELL_ONLY, currency=Currency.EGP))
    await db.deals.upsert(Deal(deal_id=f"d{i}", status=Status.COMPLETED, sell_leg=leg,
                               created_at=created or (NOW - timedelta(hours=i)),
                               updated_at=NOW, chat_jid=CENTRAL))


# ═══════════════════════════════════════════════════════════════════════════
# (1) لا cache: كيان يُضاف الآن يظهر في النداء التالي
# ═══════════════════════════════════════════════════════════════════════════
async def test_treasury_added_now_appears_in_next_call(db):
    """🔴 خزينة تُضاف ثم يقع نداء → النموذج يراها فورًا (لا قائمة ثابتة، لا إبطال cache)."""
    await _enable_ai(db)
    ai = _SpyAi()
    pipe = _pipe(db, _RecBus(), ai)
    await pipe._ai_rescue_parse(_raw(), await db.treasuries.all_active(), [])
    assert "خزينة وليدة" not in ai.prompts[-1]

    await db.treasuries.upsert(TreasuryRecord(name="خزينة وليدة", code="999",
                                              type=TreasuryType.SELL_ONLY))
    await pipe._ai_rescue_parse(_raw(key="m2"), await db.treasuries.all_active(), [])
    assert "خزينة وليدة" in ai.prompts[-1], "الكيان الجديد لم يصل للنموذج"


async def test_supplier_added_seconds_ago_is_known(db):
    """مورد أُضيف قبل لحظات → يظهر في القوائم المُرسَلة."""
    await _enable_ai(db)
    ai = _SpyAi()
    await db.suppliers.upsert(SupplierRecord(name="مورد جديد جدًّا", code="777"))
    await _pipe(db, _RecBus(), ai)._ai_rescue_parse(
        _raw(), [], await db.suppliers.all_active())
    assert "مورد جديد جدًّا" in ai.prompts[-1]


# ═══════════════════════════════════════════════════════════════════════════
# (2) تاريخ المُرسِل يُبنى من DB ويُحقن في الـprompt
# ═══════════════════════════════════════════════════════════════════════════
async def test_sender_history_is_built_from_db(db):
    for i in range(1, 5):
        await _completed_deal(db, i)
    ctx = await build_sender_context(db, SENDER, NOW)
    assert ctx.deals_seen == 4
    assert ctx.treasuries and ctx.treasuries[0][0] == "فودافون بالخصم"
    assert "6.04" in ctx.prices
    assert not ctx.is_new_sender()


async def test_sender_history_reaches_the_prompt(db):
    await _enable_ai(db)
    for i in range(1, 4):
        await _completed_deal(db, i)
    ai = _SpyAi()
    await _pipe(db, _RecBus(), ai)._ai_rescue_parse(_raw(), [], [])
    assert "تاريخ المُرسِل" in ai.prompts[-1]
    assert "فودافون بالخصم" in ai.prompts[-1]


async def test_new_sender_context_says_so(db):
    """مُرسِل بلا تاريخ → السياق يصرّح بذلك (فلا يُرجّح النموذج أنماطًا)."""
    ctx = await build_sender_context(db, "brand-new@lid", NOW)
    assert ctx.deals_seen == 0 and ctx.is_new_sender()
    assert "مُرسِل جديد" in ctx.as_prompt_block()


async def test_pair_stats_last_7_days(db):
    """إحصاء (المُرسِل × الخزينة) خلال ٧ أيام: عدد ومتوسط سعر."""
    for i in range(1, 4):
        await _completed_deal(db, i, price="6.00")
    await _completed_deal(db, 9, price="9.99", created=NOW - timedelta(days=30))  # خارج النافذة
    ctx = await build_sender_context(db, SENDER, NOW)
    stats = {p["treasury"]: p for p in ctx.pair_stats}
    assert stats["فودافون بالخصم"]["count"] == 3
    assert stats["فودافون بالخصم"]["avg_price"].startswith("6")


async def test_no_phone_ratio_detects_postal_sender(db):
    """مُرسِل أغلب حوالاته بلا رقم → نسبة البريد عالية (أساس ترجيح نمط البريد)."""
    for i in range(1, 5):
        await _completed_deal(db, i, phone="")
    ctx = await build_sender_context(db, SENDER, NOW)
    assert ctx.no_phone_ratio == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# (3) السطر الملتصق: «1160عبد السلام زكري6.04»
# ═══════════════════════════════════════════════════════════════════════════
_GLUED = "1160عبد السلام زكري6.04"


def _line_prop(raw=_GLUED, code="1160", name="عبد السلام زكري", price="6.04", conf=0.97):
    return AiProposal(data={"line_parse": [{"raw": raw, "code": code, "name": name,
                                            "price": price, "confidence": conf}]},
                      model="m")


def test_glued_line_is_decomposed():
    """🔴 المطلوب: {كود 1160، اسم «عبد السلام زكري»، سعر 6.04}."""
    v = validate_line_parse(_line_prop(), f"X1\n{_GLUED}", 0.9)
    assert len(v) == 1
    assert (v[0].code, v[0].name, v[0].price) == ("1160", "عبد السلام زكري", "6.04")


def test_glued_line_rejects_invented_part():
    """جزءٌ ليس من السطر نفسه (اسم مخترع) → يُرفض التفكيك بالكامل."""
    assert validate_line_parse(_line_prop(name="اسم من عند النموذج"), f"X1\n{_GLUED}", 0.9) == []


def test_glued_line_rejects_absent_raw():
    """سطر خام غير موجود في الرسالة → يُرفض."""
    assert validate_line_parse(_line_prop(), "X1\nسطر آخر تمامًا", 0.9) == []


def test_glued_line_rejects_low_confidence():
    assert validate_line_parse(_line_prop(conf=0.5), f"X1\n{_GLUED}", 0.9) == []


def test_glued_line_requires_registered_entity_when_asked():
    """عند تمرير الكيانات: كود/اسم غير مسجَّل → يُرفض."""
    sups = [SupplierRecord(name="عبد السلام زكري", code="1160")]
    assert len(validate_line_parse(_line_prop(), f"X1\n{_GLUED}", 0.9, entities=sups)) == 1
    other = [SupplierRecord(name="شخص آخر", code="1")]
    assert validate_line_parse(_line_prop(), f"X1\n{_GLUED}", 0.9, entities=other) == []


# ═══════════════════════════════════════════════════════════════════════════
# (4) حوالة البريد (بلا رقم مستلم)
# ═══════════════════════════════════════════════════════════════════════════
def _postal_prop(is_postal=True, conf=0.95):
    return AiProposal(data={"is_postal": is_postal, "postal_confidence": conf}, model="m")


def test_postal_confirmed_above_threshold():
    assert validate_postal(_postal_prop(), 0.9) is True


def test_postal_undecided_below_threshold():
    """ثقة دون العتبة → لا حسم (None) ⇒ تصعيد بسؤال، لا افتراض."""
    assert validate_postal(_postal_prop(conf=0.6), 0.9) is None


async def test_postal_sender_completes_with_alert(db):
    """مُرسِل تاريخه بريد + النموذج واثق → تنبيه «بريد بلا رقم» (بلا اختراع هاتف)."""
    await _enable_ai(db)
    for i in range(1, 5):
        await _completed_deal(db, i, phone="")
    bus, ai = _RecBus(), _SpyAi({"is_postal": True, "postal_confidence": 0.96})
    await _pipe(db, bus, ai)._ai_rescue_parse(_raw(), [], [])
    assert any("بريد بلا رقم مستلم" in m for m in bus.admin_msgs), bus.admin_msgs


async def test_postal_from_new_sender_escalates_with_question(db):
    """مُرسِل جديد بلا تاريخ → لا يُفترَض البريد: تصعيد بسؤال «هل هذه حوالة بريد؟»."""
    await _enable_ai(db)
    bus, ai = _RecBus(), _SpyAi({"is_postal": True, "postal_confidence": 0.96})
    await _pipe(db, bus, ai)._ai_rescue_parse(_raw(sender="fresh@lid"), [], [])
    assert any("هل هذه حوالة بريد؟" in m for m in bus.admin_msgs), bus.admin_msgs
