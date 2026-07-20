"""AI-first للحالات الغامضة (§1-§7، قرار المالك 2026-07-20).

يغطّي: كاشف الغموض، الرسالتان معًا، تصحيح الإملاء، البريد، تعدّد المعلّقات،
استقلال الرسائل الأولى المتزامنة (X1327+X1328)، وfail-open عند فشل API.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from core.ai_understanding import AiProposal, join_pair, validate_link_choice
from core.ambiguity import detect, detect_post, detect_pre
from core.constants import OperationType, TreasuryType
from core.models import (
    Currency, DetectionConfig, ParsedLeg, ParseResult, RawMessage, SupplierRecord,
    TreasuryRecord,
)
from core.parsing.parser import parse_message

NOW = datetime(2026, 7, 20, 12, 0, 0)
CENTRAL = "central@g.us"
EMP = "employee@s.whatsapp.net"


# ═════════════════════════════════════════════════════════════════════════════
# أدوات
# ═════════════════════════════════════════════════════════════════════════════
def _raw(key, text, *, sender=EMP, at=NOW, jid=CENTRAL):
    return RawMessage(message_key=key, chat_jid=jid, sender_jid=sender, text=text,
                      received_at=at)


class _FakeAi:
    """نموذج وهميّ — يُرجِع اقتراحًا مُعدًّا سلفًا، أو None لمحاكاة فشل/مهلة API."""

    def __init__(self, data=None, *, fail=False, model="fake/model"):
        self.data, self.fail, self.model = data or {}, fail, model
        self.calls: list[dict] = []

    async def propose(self, text, treasuries, suppliers, known_shapes=None,
                      sender_context=None, pair=False):
        self.calls.append({"text": text, "pair": pair})
        if self.fail:
            return None
        return AiProposal(data=self.data, model=self.model, latency_ms=7)


class _RecBus:
    central_jid = CENTRAL

    def __init__(self):
        self.admin_msgs: list[str] = []
        self.central_msgs: list[str] = []

    async def notify_admin(self, text, reply_to_key=None, forward_key=None):
        self.admin_msgs.append(text)

    async def reply_central(self, text, reply_to_key, *, is_alert=False):
        self.central_msgs.append(text)

    async def mark_central(self, message_key, emoji):
        pass


def _pipe(db, bus, ai_client):
    from core.pipeline import Pipeline

    class _W:
        name = "w"

        async def write(self, job, *, commit):
            ...

    return Pipeline(db, bus, _W(), None, customer_room_jids=[], treasury_room_jids=[],
                    ai_client=ai_client)


async def _enable(db, **over):
    cfg = DetectionConfig(ai_enabled=True, ai_first_enabled=True, ai_model="fake/model",
                          ai_confidence_threshold=0.9, **over)
    await db.detection.set(cfg)
    return cfg


def _tre(name="عزالدين", code="82"):
    return TreasuryRecord(code=code, name=name, type=TreasuryType.SELL_ONLY, aliases=[], active=True)


def _sup(name="البراق", code="7"):
    return SupplierRecord(code=code, name=name, aliases=[], active=True)


# ═════════════════════════════════════════════════════════════════════════════
# (١) الكاشف — نظيفة 100% لا تستدعي API
# ═════════════════════════════════════════════════════════════════════════════
def test_clean_message_is_not_ambiguous(db_lists=None):
    """رسالة نظيفة بثقة عالية → لا غموض ⇒ لا نداء API إطلاقًا."""
    leg = ParsedLeg(operation=OperationType.SELL, amount=1000.0, currency=Currency.TND, customer_code="793",
                    customer_name="حميد", reference_number="X1258")
    res = ParseResult(kind="transfer", leg=leg, confidence=0.95)
    v = detect("X1258\n793 حميد\n1000 دت", res, [_tre()], [_sup()], min_confidence=0.7)
    assert v.ambiguous is False
    assert v.reasons == []


def test_glued_currency_is_clean_not_ambiguous():
    """«6150ج.م» التصاقٌ طبيعيّ للعملة — يجب ألّا يُصنَّف غموضًا (وإلا 92% كاذبة)."""
    assert detect_pre("A9152\n01225646621\n6150ج.م", [_tre()], [_sup()]) == []


def test_nonstandard_currency_word_flags():
    """«جنى» تحريف لـ«جنيه» وdetect_currency يفشل عليها → غموض."""
    reasons = detect_pre("X1300\n5000 جنى", [_tre()], [_sup()])
    assert any("عملة غير معياريّة" in r for r in reasons)


def test_recipient_name_colliding_with_treasury_flags():
    """اسم المستلم يطابق خزينةً مسجَّلة → أخطر التصادمات ⇒ غموض."""
    leg = ParsedLeg(operation=OperationType.SELL, amount=100.0, customer_name="عزالدين", reference_number="X1")
    reasons = detect_pre("X1\nعزالدين\n100", [_tre("عزالدين")], [_sup()], leg=leg)
    assert any("يطابق خزينةً" in r for r in reasons)


def test_low_parse_confidence_flags():
    leg = ParsedLeg(operation=OperationType.SELL, amount=100.0, reference_number="X1")
    res = ParseResult(kind="transfer", leg=leg, confidence=0.42)
    assert any("ثقة تفكيك منخفضة" in r for r in detect_post(res, min_confidence=0.7))


def test_unresolved_treasury_flags():
    leg = ParsedLeg(operation=OperationType.SELL, amount=100.0, unresolved_treasury="عد الدين")
    res = ParseResult(kind="transfer", leg=leg, confidence=0.95)
    assert any("خزينة لم تُحلّ" in r for r in detect_post(res))


# ═════════════════════════════════════════════════════════════════════════════
# (٢) الرسالتان معًا
# ═════════════════════════════════════════════════════════════════════════════
def test_join_pair_marks_boundary():
    j = join_pair(["أولى", "ثانية"])
    assert "أولى" in j and "ثانية" in j and "الرسالة الثانية" in j


def test_join_pair_skips_empty():
    assert join_pair(["أولى", "  "]) == "أولى"


async def test_pair_partner_ignores_independent_first_message(db):
    """X1327+X1328: رسالتان أوليان متزامنتان — ليست إحداهما تكملةً للأخرى (§2)."""
    await _enable(db)
    p = _pipe(db, _RecBus(), _FakeAi())
    tre, sup = await db.treasuries.all_active(), await db.suppliers.all_active()
    m1 = _raw("k1", "X1327\n01234567890\n1000 ج.م\nانستا باي", at=NOW)
    m2 = _raw("k2", "X1328\n01234567891\n2000 ج.م\nانستا باي", at=NOW + timedelta(seconds=1))
    assert p._pair_partner(m1, [m1, m2], tre, sup) is None


async def test_pair_partner_accepts_completion_fragment(db):
    """تكملة بلا مرجع خاصّ بها → تُضمّ للأولى."""
    await _enable(db)
    p = _pipe(db, _RecBus(), _FakeAi())
    tre, sup = await db.treasuries.all_active(), await db.suppliers.all_active()
    m1 = _raw("k1", "X1327\n01234567890\n1000 ج.م", at=NOW)
    m2 = _raw("k2", "بلس", at=NOW + timedelta(seconds=2))
    assert p._pair_partner(m1, [m1, m2], tre, sup) is m2


# ═════════════════════════════════════════════════════════════════════════════
# (٥) تعدّد المعلّقات — عتبات القرار
# ═════════════════════════════════════════════════════════════════════════════
def test_link_choice_auto_above_095():
    prop = AiProposal(data={"link_index": 1, "link_confidence": 0.97}, model="m")
    c = validate_link_choice(prop, 2)
    assert c.action == "auto" and c.index == 1


def test_link_choice_ask_between_080_and_095():
    prop = AiProposal(data={"link_index": 0, "link_confidence": 0.88}, model="m")
    assert validate_link_choice(prop, 2).action == "ask"


def test_link_choice_falls_back_to_fifo_below_080():
    prop = AiProposal(data={"link_index": 0, "link_confidence": 0.5}, model="m")
    assert validate_link_choice(prop, 2).action == "fifo"


def test_link_choice_rejects_out_of_range_index():
    """فهرس خارج المدى ⇒ FIFO لا تخمين — حتى لو ادّعى النموذج ثقةً كاملة."""
    prop = AiProposal(data={"link_index": 9, "link_confidence": 0.99}, model="m")
    assert validate_link_choice(prop, 2).action == "fifo"


# ═════════════════════════════════════════════════════════════════════════════
# (٦) الضمانات — fail-open
# ═════════════════════════════════════════════════════════════════════════════
async def test_api_failure_falls_back_to_deterministic(db):
    """فشل/مهلة API → المسار الحتميّ يمضي كما هو، بلا استثناء ولا توقّف (§6)."""
    await _enable(db)
    bus, ai = _RecBus(), _FakeAi(fail=True)
    p = _pipe(db, bus, ai)
    tre, sup = await db.treasuries.all_active(), await db.suppliers.all_active()
    raw = _raw("k1", "X1300\n5000 جنى")
    res = parse_message(raw.text, tre, sup)
    from core.ambiguity import detect as _d
    out = await p._ai_first(raw, res, _d(raw.text, res, tre, sup), tre, sup, None, NOW)
    assert out is None
    assert ai.calls, "كان يجب محاولة النداء"


async def test_ai_first_disabled_makes_no_call(db):
    """مطفأة افتراضيًّا: ai_first_enabled=False ⇒ لا نداء API إطلاقًا."""
    await db.detection.set(DetectionConfig(ai_enabled=True, ai_first_enabled=False))
    ai = _FakeAi({"link_index": 0})
    p = _pipe(db, _RecBus(), ai)
    raw = _raw("k1", "X1300\n5000 جنى")
    assert await p._ai_link_choice(raw, [], [], [], NOW) is None
    assert ai.calls == []


# ═════════════════════════════════════════════════════════════════════════════
# حارس الدور — حادثة XI1321
# ═════════════════════════════════════════════════════════════════════════════
def test_role_guard_rejects_city_token_jerba():
    """XI1321 حرفيًّا: «جربه» من سطر الموقع لا تصلح خزينةً مهما بلغت الثقة."""
    from core.ambiguity import treasury_role_conflict
    assert treasury_role_conflict("جربه", "A9151\nسلم الى سليمان\nجربه/ميدون\n1000دت") \
        == "موقع/مدينة"


def test_role_guard_rejects_region_token():
    from core.ambiguity import treasury_role_conflict
    assert treasury_role_conflict("تونس", "X1258\nتونس /بريد\nNizar\n470 تونسي") == "موقع/مدينة"


def test_role_guard_rejects_customer_name_token():
    """الرمز نفسه لا يكون زبونًا وخزينةً معًا."""
    from core.ambiguity import treasury_role_conflict
    leg = ParsedLeg(operation=OperationType.SELL, amount=100.0, customer_name="عزالدين")
    assert treasury_role_conflict("عزالدين", "X1\nعزالدين\n100", leg) == "اسم المستلم"


def test_role_guard_rejects_delivery_line_token():
    from core.ambiguity import treasury_role_conflict
    assert treasury_role_conflict(
        "ميدون", "A9151\nسلم الى ميدون\n1000دت") == "سطر موقع/تسليم"


@pytest.mark.parametrize("name", [
    "صالح جربة تونس", "عصام سوسة", "فتحي جربة", "محمود صفاقس", "وليد تونس العاصمة",
])
def test_role_guard_allows_treasury_names_containing_cities(name):
    """9 من 14 خزينة تحمل اسم مدينة **جزءًا من هويّتها** للتمييز. شرط «أيّ جزء موقع»
    كان يرفضها كلّها حين يكتبها الموظّف كاملةً — تعطيلٌ لا حماية. الشرط «كلّها موقع»."""
    from core.ambiguity import treasury_role_conflict
    assert treasury_role_conflict(name, name) is None


@pytest.mark.parametrize("tok", ["جربه", "جربة", "تونس", "مصر", "العاصمة", "تونس العاصمة"])
def test_role_guard_rejects_pure_location_tokens(tok):
    """موقعٌ خالص لا يصلح خزينةً — بما فيه المعرَّف بـ«ال» (التطبيع يُسقطها)."""
    from core.ambiguity import treasury_role_conflict
    assert treasury_role_conflict(tok, tok) == "موقع/مدينة"


def test_role_guard_location_hint_needs_word_boundary():
    """«حي» داخل «فتحي» ليست دلالة موقع — مطابقةٌ عرَضيّة على جزء كلمة."""
    from core.ambiguity import treasury_role_conflict
    assert treasury_role_conflict("فتحي جربة", "X1\nفتحي جربة\n100") is None


def test_role_guard_allows_legitimate_treasury_token():
    """خزينة حقيقيّة في سطرها الخاصّ → لا تعارض ⇒ تُقبل."""
    from core.ambiguity import treasury_role_conflict
    assert treasury_role_conflict("البراق", "X1300\n01234567890\n5000 ج.م\nالبراق") is None


async def test_xi1321_treasury_from_location_line_is_rejected_end_to_end(db):
    """المسار كاملًا: النموذج يقترح خزينةً **مسجَّلة** بثقة 1.0 مشتقّة من سطر الموقع
    «جربه/ميدون» — تجتاز التحقّق الحتميّ (كيان مسجَّل + لا رقم مخترع) ومع ذلك
    يجب أن يرفضها حارس الدور، فلا يُطبَّق شيء ويُصعَّد."""
    await _enable(db)
    tre_list = await db.treasuries.all_active()
    jerba = next((t for t in tre_list if "جرب" in t.name), None)
    if jerba is None:                      # لا خزينة جربة في البذرة → أنشئها
        jerba = TreasuryRecord(code="29", name="صالح جربة تونس",
                               type=TreasuryType.SELL_ONLY, aliases=[], active=True)
        tre_list = list(tre_list) + [jerba]
    bus = _RecBus()
    ai = _FakeAi({
        "treasury": jerba.name,
        "corrections": [{"raw": "جربه", "entity_type": "treasury",
                         "official": jerba.name, "confidence": 1.0}],
        "confidence": {"treasury": 1.0},
    })
    p = _pipe(db, bus, ai)
    sup = await db.suppliers.all_active()
    text = "A9151\nسلم الى\nسليمان\nجربه/ميدون\n1000دت\n00218921999133"
    raw = _raw("k-xi1321", text)
    res = parse_message(text, tre_list, sup)
    from core.ambiguity import detect as _d
    out = await p._ai_first(raw, res, _d(text, res, tre_list, sup), tre_list, sup, None, NOW)
    assert out is None, "خزينة من سطر الموقع كان يجب ألّا تُطبَّق"
    assert any("حارس الدور" in m for m in bus.admin_msgs), bus.admin_msgs


async def test_rejected_proposal_escalates_with_suggestion(db):
    """اقتراح لم يجتز التحقّق ⇒ لا يُطبَّق، ويُصعَّد مرفقًا به (§3و)."""
    await _enable(db)
    bus = _RecBus()
    # خزينة غير مسجَّلة + ثقة عالية ⇒ يجب أن يرفضها المدقّق الحتميّ
    ai = _FakeAi({"treasury": "خزينة وهمية غير مسجلة",
                  "confidence": {"treasury": 0.99}})
    p = _pipe(db, bus, ai)
    tre, sup = await db.treasuries.all_active(), await db.suppliers.all_active()
    raw = _raw("k1", "X1300\n5000 جنى")
    res = parse_message(raw.text, tre, sup)
    from core.ambiguity import detect as _d
    out = await p._ai_first(raw, res, _d(raw.text, res, tre, sup), tre, sup, None, NOW)
    assert out is None
    assert any("لم يجتز التحقّق" in m for m in bus.admin_msgs)
