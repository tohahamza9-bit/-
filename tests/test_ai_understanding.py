"""
طبقة الفهم الذكي (OpenRouter) — إنقاذ أخير قبل التصعيد.

القاعدة: النموذج يقترح، والكود يتحقّق حتميًّا. كيان غير مسجَّل، أو رقم غير موجود في النصّ، أو
ثقة دون العتبة، أو فشل نداء — كلّها تُصعَّد كما لو أن الطبقة غير موجودة. لا نداء إطلاقًا وهي مطفأة.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.ai_understanding import AiProposal, validate_proposal
from core.constants import OperationType, Status, TreasuryType
from core.models import Deal, DetectionConfig, ParsedLeg, SupplierRecord, TreasuryRecord

NOW = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
MSG_KEY = "ai-1"
# نصّ رسالة حقيقيّ الشكل: خزينة مكتوبة بخطأ إملائيّ («اوسـ» بدل «قروب اوس») فيفشل الحلّ الصارم.
TEXT = "X9001\n149 زبون تجريبي\nالمبلغ 1000\nالسعر 6.0\nخزينة: قروب اوسي\n01012345678"


class _RecBus:
    def __init__(self):
        self.central_jid = CENTRAL
        self.admin_jid = ADMIN
        self.admin_msgs: list[str] = []

    async def notify_admin(self, text, reply_to_key=None, forward_key=None):
        self.admin_msgs.append(text)

    async def reply_central(self, text, reply_to_key=None, *, is_alert=False):
        pass


class _FakeAi:
    """عميل نموذج وهميّ — يُرجِع اقتراحًا مُعدًّا سلفًا، أو None لمحاكاة فشل النداء/المهلة."""

    def __init__(self, data, *, fail=False, model="fake/model"):
        self.data, self.fail, self.model = data, fail, model
        self.calls = 0

    async def propose(self, text, treasuries, suppliers, known_shapes=None,
                      sender_context=None):
        self.calls += 1
        if self.fail:
            return None
        return AiProposal(data=self.data, model=self.model, latency_ms=42)


def _conf(**over) -> dict:
    base = {"operation": 1.0, "amount": 1.0, "currency": 1.0,
            "treasury": 1.0, "supplier": 1.0, "price": 1.0, "customer": 1.0}
    base.update(over)
    return base


def _proposal(**over) -> dict:
    d = {"operation": "بيع", "customer_code": "149", "customer_name": "زبون تجريبي",
         "amount": 1000, "currency": "TND", "treasury": "قروب اوس", "price": 6.0,
         "phone": "01012345678", "reference_number": "X9001", "confidence": _conf()}
    d.update(over)
    return d


async def _seed(db, *, ai_enabled=True, threshold=0.9):
    await db.treasuries.upsert(
        TreasuryRecord(name="قروب اوس", code="82", type=TreasuryType.SELL_ONLY))
    await db.suppliers.upsert(SupplierRecord(name="طه", code="S1"))
    cfg = DetectionConfig(ai_enabled=ai_enabled, ai_model="fake/model",
                          ai_confidence_threshold=threshold)
    await db.detection.set(cfg)
    await db.raw.col.insert_one({
        "message_key": MSG_KEY, "chat_jid": CENTRAL, "text": TEXT,
        "received_at": NOW.replace(tzinfo=None), "processed": True})


def _deal() -> Deal:
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="X9001", customer_code="149",
                    customer_name="زبون تجريبي", amount=1000.0, phone="01012345678",
                    price_normalized="6.0", treasury=None, unresolved_treasury="قروب اوسي",
                    sender_jid="emp@lid", source_message_key=MSG_KEY)
    return Deal(deal_id="d-ai", status=Status.PARSED, sell_leg=leg, created_at=NOW,
                updated_at=NOW, first_received_at=NOW, chat_jid=CENTRAL,
                source_message_keys=[MSG_KEY])


def _pipe(db, bus, ai_client):
    from core.pipeline import Pipeline

    class _W:
        name = "w"
        async def write(self, job, *, commit): ...
    return Pipeline(db, bus, _W(), None, customer_room_jids=[], treasury_room_jids=[],
                    ai_client=ai_client)


# ═══════════════════════════════════════════════════════════════════════════
# المسار السعيد: فهم مُتحقَّق منه → إكمال + تنبيه + resolved_by=ai
# ═══════════════════════════════════════════════════════════════════════════
async def test_verified_understanding_completes_and_alerts(db):
    """اقتراح صحيح لكيان مسجَّل بأرقام موجودة وثقة عالية → تُحلّ الخزينة + تنبيه «فُهمت بالذكاء»."""
    await _seed(db)
    bus, ai = _RecBus(), _FakeAi(_proposal())
    deal = _deal()
    assert await _pipe(db, bus, ai)._ai_rescue(deal, NOW) is True
    assert deal.sell_leg.treasury is not None and deal.sell_leg.treasury.code == "82"
    assert deal.sell_leg.unresolved_treasury is None
    assert any("فُهمت بالذكاء الاصطناعي" in m for m in bus.admin_msgs)
    assert any(dv.get("method") == "ai" and dv.get("field") == "treasury"
               for dv in deal.sell_leg.deviation_log)


async def test_resolved_by_ai_surfaces_as_badge(db):
    """الحوالة المُنقَذة تُعرَض في اللوحة بشارة «ذكاء» (resolved_by=ai)."""
    from dashboard.transfers import _resolved_by
    await _seed(db)
    deal = _deal()
    await _pipe(db, _RecBus(), _FakeAi(_proposal()))._ai_rescue(deal, NOW)
    assert _resolved_by(deal.sell_leg.model_dump()) == "ai"


# ═══════════════════════════════════════════════════════════════════════════
# الخطوط الحمراء: كل إخفاق ⇒ تصعيد (False) ولا تُلمَس الخزينة
# ═══════════════════════════════════════════════════════════════════════════
async def test_unregistered_entity_escalates(db):
    """خزينة يقترحها النموذج لكنها غير مسجَّلة → لا تُعتمَد (تصعيد) + اقتراحها يُرسَل للاستئناس."""
    await _seed(db)
    bus, ai = _RecBus(), _FakeAi(_proposal(treasury="خزينة وهمية لا وجود لها"))
    deal = _deal()
    assert await _pipe(db, bus, ai)._ai_rescue(deal, NOW) is False
    assert deal.sell_leg.treasury is None
    assert any("غير مسجَّلة" in m for m in bus.admin_msgs)


async def test_low_confidence_escalates(db):
    """ثقة حقل جوهريّ دون العتبة → تصعيد، مع إرفاق اقتراح النموذج كمساعدة للمسؤول."""
    await _seed(db)
    bus, ai = _RecBus(), _FakeAi(_proposal(confidence=_conf(treasury=0.62)))
    deal = _deal()
    assert await _pipe(db, bus, ai)._ai_rescue(deal, NOW) is False
    assert deal.sell_leg.treasury is None
    assert any("اقتراح النموذج" in m for m in bus.admin_msgs)


async def test_invented_number_escalates(db):
    """رقم غير موجود في نصّ الرسالة (مبلغ مخترع) → تصعيد ولا يُسجَّل شيء."""
    await _seed(db)
    bus, ai = _RecBus(), _FakeAi(_proposal(amount=987654))
    deal = _deal()
    assert await _pipe(db, bus, ai)._ai_rescue(deal, NOW) is False
    assert deal.sell_leg.treasury is None


async def test_call_failure_escalates(db):
    """فشل/مهلة النداء → تصعيد عاديّ كأن الطبقة غير موجودة (ممنوع أن تقف حوالة على API)."""
    await _seed(db)
    bus, ai = _RecBus(), _FakeAi(None, fail=True)
    deal = _deal()
    assert await _pipe(db, bus, ai)._ai_rescue(deal, NOW) is False
    assert deal.sell_leg.treasury is None
    assert bus.admin_msgs == []          # لا ضجيج عند فشل البنية التحتية


async def test_disabled_layer_never_calls_model(db):
    """الطبقة مطفأة → لا نداء إطلاقًا (لا كلفة، لا زمن) والسلوك مطابق لما قبلها."""
    await _seed(db, ai_enabled=False)
    ai = _FakeAi(_proposal())
    deal = _deal()
    assert await _pipe(db, _RecBus(), ai)._ai_rescue(deal, NOW) is False
    assert ai.calls == 0


async def test_resolved_treasury_is_never_overwritten(db):
    """خزينة حسمها الفهم الحتميّ → الطبقة لا تُستدعى ولا تلمسها (الكود الحتميّ يغلب دائمًا)."""
    from core.models import TreasuryRef
    await _seed(db)
    ai = _FakeAi(_proposal(treasury="قروب اوس"))
    deal = _deal()
    deal.sell_leg.treasury = TreasuryRef(code="99", name="خزينة صريحة",
                                         type=TreasuryType.SELL_ONLY)
    assert await _pipe(db, _RecBus(), ai)._ai_rescue(deal, NOW) is False
    assert deal.sell_leg.treasury.code == "99"
    assert ai.calls == 0


# ═══════════════════════════════════════════════════════════════════════════
# حالة XI1321 الحقيقيّة: تصحيح إملاء المورد ثم **إعادة التفكيك الحتميّ**
# ═══════════════════════════════════════════════════════════════════════════
# المورد المسجَّل فعلًا في الإنتاج: الاسم «عز الدين » بكود 300 وإملاءات بديلة.
_EZZ = SupplierRecord(name="عز الدين ", code="300",
                      aliases=["عز الدين لعجيلي", "عزالدين العجيلي"])
# الرسالة الثانية كما وردت حرفيًّا (لاحظ «عد الدين» — سقط حرف الزاي).
XI_SECOND = "591نبيل البقار 34.5\n\n\nعد الدين العجيلي 34.5"
XI_FIRST = "XI1321\n\n55292852\nالمهدي\n320د.ت\n\nجربه/ميدون"
XI_K1, XI_K2 = "xi-1", "xi-2"


def _xi_correction_proposal() -> dict:
    """ما يُفترَض أن يعيده النموذج: تصحيح الإملاء فقط — بلا أيّ قيمة ماليّة."""
    return {
        "operation": "بيع", "customer_code": "591", "customer_name": "نبيل البقار",
        "amount": None, "currency": None, "treasury": None, "supplier": "عز الدين ",
        "price": 34.5, "reference_number": "XI1321",
        "corrections": [{"raw": "عد الدين العجيلي", "entity_type": "supplier",
                         "official": "عز الدين ", "confidence": 0.96}],
        "confidence": _conf(treasury=0.0),
    }


async def _seed_xi(db, *, ai_enabled=True):
    from core.constants import Currency
    await db.suppliers.upsert(_EZZ)
    await db.treasuries.upsert(
        TreasuryRecord(name="فودافون بالخصم", code="85", type=TreasuryType.SELL_AND_BUY))
    await db.detection.set(DetectionConfig(ai_enabled=ai_enabled, ai_model="fake/model",
                                           ai_confidence_threshold=0.9))
    for k, t in ((XI_K1, XI_FIRST), (XI_K2, XI_SECOND)):
        await db.raw.col.insert_one({"message_key": k, "chat_jid": CENTRAL, "text": t,
                                     "received_at": NOW.replace(tzinfo=None), "processed": True})
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="XI1321",
                    customer_code="591", customer_name="نبيل البقار", amount=320.0,
                    currency=Currency.TND, price_raw="34.5", price_normalized="34.5",
                    treasury=None, phone="55292852", sender_jid="emp@lid",
                    source_message_key=XI_K1)
    d = Deal(deal_id="d-xi", status=Status.PARSED, sell_leg=leg, created_at=NOW, updated_at=NOW,
             first_received_at=NOW, chat_jid=CENTRAL, source_message_keys=[XI_K1, XI_K2])
    await db.deals.upsert(d)
    return d


async def test_xi1321_supplier_typo_is_rescued_by_replay(db):
    """XI1321: «عد الدين العجيلي» ← المورد المسجَّل «عز الدين» ⇒ إعادة التفكيك تبني طرف الشراء
    وتشتقّ الخزينة، فتنزل الحوالة بدل التصعيد — والزبون 591 نبيل البقار يبقى بدوره."""
    deal = await _seed_xi(db)
    bus, ai = _RecBus(), _FakeAi(_xi_correction_proposal())
    assert await _pipe(db, bus, ai)._ai_rescue(deal, NOW) is True
    lg = deal.sell_leg or deal.buy_leg
    assert lg.treasury is not None, "الخزينة لم تُشتقّ بعد إعادة التفكيك"
    assert deal.sell_leg.customer_code == "591"
    assert deal.sell_leg.customer_name == "نبيل البقار"
    assert any("فُهمت بالذكاء الاصطناعي" in m and "عد الدين العجيلي" in m for m in bus.admin_msgs)
    assert any(dv.get("method") == "ai" for dv in lg.deviation_log)


async def test_correction_never_persisted_as_alias(db):
    """🔴 التصحيح مؤقّت: لا يُكتب إملاءً دائمًا في قاعدة البيانات (لا تعلّم صامت)."""
    deal = await _seed_xi(db)
    await _pipe(db, _RecBus(), _FakeAi(_xi_correction_proposal()))._ai_rescue(deal, NOW)
    rec = [s for s in await db.suppliers.all_active() if s.code == "300"][0]
    assert "عد الدين العجيلي" not in rec.aliases


async def test_model_may_not_assign_treasury_role(db):
    """🔴 الخطر الحيّ الملاحَظ في XI1321: النموذج اقترح خزينة «صالح جربة تونس» من سطر الموقع
    «جربه/ميدون» بثقة 1.0. القاعدة: بلا رمز خزينة موسوم حتميًّا (unresolved_treasury) لا تُقبَل
    خزينةٌ من النموذج إطلاقًا — النموذج يصحّح الهجاء ولا يُسنِد الأدوار."""
    deal = await _seed_xi(db)
    await db.treasuries.upsert(
        TreasuryRecord(name="صالح جربة تونس", code="29", type=TreasuryType.SELL_ONLY))
    prop = _xi_correction_proposal()
    prop["corrections"] = []                       # لا تصحيح مورد ⇒ لا مسار إعادة
    prop["treasury"], prop["treasury_code"] = "صالح جربة تونس", "29"
    prop["confidence"] = _conf()
    assert deal.sell_leg.unresolved_treasury is None
    assert await _pipe(db, _RecBus(), _FakeAi(prop))._ai_rescue(deal, NOW) is False
    assert deal.sell_leg.treasury is None, "أُسنِدت خزينة من النموذج بلا رمز موسوم — خطر ماليّ"


async def test_correction_of_unregistered_target_escalates(db):
    """تصحيح نحو اسم غير مسجَّل → يُرفض ولا تُعاد التفكيك (تصعيد)."""
    deal = await _seed_xi(db)
    prop = _xi_correction_proposal()
    prop["corrections"][0]["official"] = "مورد وهميّ غير مسجَّل"
    assert await _pipe(db, _RecBus(), _FakeAi(prop))._ai_rescue(deal, NOW) is False
    assert (deal.sell_leg or deal.buy_leg).treasury is None


async def test_correction_of_absent_token_escalates(db):
    """النموذج «صحّح» نصًّا لا وجود له في الرسالة → يُرفض (منع اختراع الكلمات)."""
    deal = await _seed_xi(db)
    prop = _xi_correction_proposal()
    prop["corrections"][0]["raw"] = "اسم لم يرد في الرسالة إطلاقًا"
    assert await _pipe(db, _RecBus(), _FakeAi(prop))._ai_rescue(deal, NOW) is False
    assert (deal.sell_leg or deal.buy_leg).treasury is None


async def test_low_confidence_correction_escalates(db):
    """ثقة التصحيح دون العتبة → يُرفض التصحيح (تصعيد)."""
    deal = await _seed_xi(db)
    prop = _xi_correction_proposal()
    prop["corrections"][0]["confidence"] = 0.71
    assert await _pipe(db, _RecBus(), _FakeAi(prop))._ai_rescue(deal, NOW) is False
    assert (deal.sell_leg or deal.buy_leg).treasury is None


# ═══════════════════════════════════════════════════════════════════════════
# وحدة التحقّق الحتميّ — مباشرةً
# ═══════════════════════════════════════════════════════════════════════════
def _recs():
    return ([TreasuryRecord(name="قروب اوس", code="82", type=TreasuryType.SELL_ONLY)],
            [SupplierRecord(name="طه", code="S1")])


def test_validator_accepts_alias_and_code():
    """المطابقة تقبل الكود والاسم الرسميّ — وترفض ما عداهما (لا تشابه)."""
    trs, sup = _recs()
    p = AiProposal(data=_proposal(treasury=None, treasury_code="82"), model="m")
    assert validate_proposal(p, TEXT, trs, sup).ok is True
    p2 = AiProposal(data=_proposal(treasury="قروب او"), model="m")   # بادئة قريبة — تُرفض
    v2 = validate_proposal(p2, TEXT, trs, sup)
    assert v2.ok is False and "غير مسجَّلة" in v2.reason


def test_validator_rejects_unknown_currency():
    trs, sup = _recs()
    p = AiProposal(data=_proposal(currency="XYZ"), model="m")
    v = validate_proposal(p, TEXT, trs, sup)
    assert v.ok is False and "عملة" in v.reason


def test_validator_requires_treasury_field():
    """النموذج لم يقترح خزينةً أصلًا → لا فائدة من الإنقاذ (تصعيد)."""
    trs, sup = _recs()
    p = AiProposal(data=_proposal(treasury=None, treasury_code=None), model="m")
    v = validate_proposal(p, TEXT, trs, sup)
    assert v.ok is False and "treasury" in v.reason


def test_number_in_text_accepts_alf_expansion():
    """توسّع «ألف» الموثَّق (§3.5): «32 ألف» في النصّ يقبل المبلغ 32000 — لا يُعدّ اختراعًا."""
    from core.ai_understanding import _num_in_text
    assert _num_in_text(32000, "المبلغ 32 الف جنيه") is True
    assert _num_in_text(32000, "المبلغ 500 جنيه") is False
    assert _num_in_text(1000, "القيمة 1,000 دت") is True
    assert _num_in_text(6.0, "السعر 6.0") is True
    # 🔴 مطابقة رقميّة تامّة لا جزئيّة: مبلغٌ مقترح 100 والنصّ فيه 1000 ⇒ مرفوض (خطر ماليّ).
    assert _num_in_text(100, "المبلغ 1000") is False
    assert _num_in_text(5.7, "السعر 5.72") is False


def test_number_in_text_million_and_word_amounts():
    """(1أ+1ب، 2026-07-23) توحيد expand + «مليون» + الأعداد المكتوبة بالحروف — اشتقاقٌ لا اختراع."""
    from core.ai_understanding import _num_in_text
    # (1أ) «مليون» — كانت مرفوضةً لغيابها عن قائمة كلمات التوسّع.
    assert _num_in_text(2_000_000, "مبلغ 2 مليون") is True
    assert _num_in_text(1_500_000, "قيمة 1.5 مليون جنيه") is True   # رأس عشريّ
    assert _num_in_text(1_000_000, "مليون جنيه") is True            # مُعامِل قائم بذاته
    assert _num_in_text(2_000_000, "مليونين") is True               # مثنّى
    # (1ب) رؤوسٌ مكتوبةٌ بالحروف.
    assert _num_in_text(50_000, "خمسين الف") is True
    assert _num_in_text(30_000, "ثلاثين الف جنيه") is True
    assert _num_in_text(25_000, "خمسه وعشرين الف") is True          # واو عطف ملتصقة
    assert _num_in_text(150_000, "مئه وخمسين الف") is True
    assert _num_in_text(200_000, "مئتين الف") is True
    assert _num_in_text(2_000, "الفين") is True
    # 🔴 الاختراع يبقى مرفوضًا: رقمٌ لا أصل له، أو مشتقٌّ ماليًّا (1ج غير مُفعَّلة).
    assert _num_in_text(99_999, "حوالة 50000") is False
    assert _num_in_text(31_000, "مبلغ 30 الف") is False
    assert _num_in_text(28_420, "28.136مصري وسعر 6.03") is False    # اشتقاق ماليّ مرفوض عمدًا
    assert _num_in_text(5_000, "لا رقم هنا") is False


# ═══════════════════════════════════════════════════════════════════════════
# توسعة X1325: الرسالة الأولى الفاشلة تفكيكًا — تصحيح كلمة العملة ثم إعادة تفكيك
# ═══════════════════════════════════════════════════════════════════════════
X1325_TEXT = "X1325\n٠١٠٢٩٢٨١٦٢٠\n3,600 جني م\n  صافي"


def _currency_fix_proposal(raw="جني", official="جنيه", conf=0.98) -> dict:
    return {"corrections": [{"raw": raw, "entity_type": "currency",
                             "official": official, "confidence": conf}],
            "confidence": _conf()}


def _raw_msg(text=X1325_TEXT, key="x1325-1"):
    from core.models import RawMessage
    return RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid="emp@lid",
                      text=text, received_at=NOW)


async def test_x1325_currency_typo_rescued(db):
    """🔴 «3,600 جني م» فشل تفكيكها ⇒ تصعيد. التصحيح «جني»←«جنيه» يُعيد التفكيك فيظهر 3600."""
    await _seed(db)
    bus, ai = _RecBus(), _FakeAi(_currency_fix_proposal())
    res = await _pipe(db, bus, ai)._ai_rescue_parse(_raw_msg(), [], [])
    assert res is not None and res.kind == "transfer"
    assert res.leg.amount == 3600.0
    assert any("فُهمت بالذكاء الاصطناعي" in m for m in bus.admin_msgs)


async def test_currency_fix_outside_closed_list_rejected(db):
    """بديل خارج قائمة العملات المغلقة → يُرفض (النموذج لا يخترع كلمات)."""
    await _seed(db)
    ai = _FakeAi(_currency_fix_proposal(official="عملة غريبة"))
    assert await _pipe(db, _RecBus(), ai)._ai_rescue_parse(_raw_msg(), [], []) is None


async def test_currency_fix_must_extend_raw_token(db):
    """التصحيح إضافيّ لا حذفيّ: «3,600»←«جنيه» مرفوض (ليس امتدادًا للخام) — يمنع ابتلاع رقم."""
    await _seed(db)
    ai = _FakeAi(_currency_fix_proposal(raw="3,600", official="جنيه"))
    assert await _pipe(db, _RecBus(), ai)._ai_rescue_parse(_raw_msg(), [], []) is None


async def test_parse_rescue_disabled_when_layer_off(db):
    """الطبقة مطفأة → لا نداء ولا إنقاذ تفكيك."""
    await _seed(db, ai_enabled=False)
    ai = _FakeAi(_currency_fix_proposal())
    assert await _pipe(db, _RecBus(), ai)._ai_rescue_parse(_raw_msg(), [], []) is None
    assert ai.calls == 0


async def test_parse_rescue_call_failure_escalates(db):
    """فشل النداء → None (تصعيد عاديّ)."""
    await _seed(db)
    ai = _FakeAi(None, fail=True)
    assert await _pipe(db, _RecBus(), ai)._ai_rescue_parse(_raw_msg(), [], []) is None
