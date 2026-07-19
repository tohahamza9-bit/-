"""
الجزء 1 — 🔴 **إلغاء** الحل التلقائي بالتشابه (قرار المالك 2026-07-19) + الجزء 2 — dedup بالمحتوى.

الجزء 1: مسار التشابه (resolve_bold) معطَّل بالكامل (bold_resolve_enabled=False): اسمٌ لا يُحلّ
صارمًا → يبقى unresolved فيُصعَّد بالرسالة الإلزامية — لا تخمين ولا alias متعلَّم. يبقى شغّالاً:
الحل الصارم + aliases اليدوية + التطبيع الحتميّ + طبقة القروب (test_room_match).
دوالّ resolve_bold نفسها تبقى مختبَرةً (أداة محفوظة غير موصولة) لتوثيق ما كانت تفعله.
الجزء 2: نفس المرجع+نفس الجوهر = تكرار (تجاهل+ريأكشن)؛ نفس المرجع+جوهر مختلف = صفقة جديدة+تنبيه.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from core.constants import OperationType, Status, TreasuryType
from core.models import Deal, ParsedLeg, RawMessage, TreasuryRecord, TreasuryRef
from core.parsing.resolve import resolve_bold

NOW = datetime(2026, 7, 18, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
S1 = "sender1@lid"

_TRS = [
    TreasuryRecord(name="أبو يوسف جديد", code="77", type=TreasuryType.SELL_ONLY),
    TreasuryRecord(name="محمود صفاقس", code="78", type=TreasuryType.SELL_ONLY),
    TreasuryRecord(name="بلاس فون", code="74", type=TreasuryType.SELL_ONLY),
]


# ── resolve_bold — الحل الجريء والخطوط الحمراء ────────────────────────────────────────
def test_resolve_bold_misspelled_treasury():
    """«ابو بوسف»/«محموذ» (إملاء خاطئ) → أقرب خزينة مسجّلة، score ≥ 70."""
    r, s = resolve_bold("ابو بوسف", _TRS, 70)
    assert r is not None and r.name == "أبو يوسف جديد" and s >= 70
    r2, s2 = resolve_bold("محموذ", _TRS, 70)
    assert r2 is not None and r2.name == "محمود صفاقس" and s2 >= 70


def test_resolve_bold_generic_word_alone_no_match():
    """(الخطّ الأحمر ب) كلمة عامّة وحدها («شركة») لا تصنع مطابقة أبدًا."""
    assert resolve_bold("شركة", _TRS, 70) == (None, 0)
    assert resolve_bold("مكتب", _TRS, 70) == (None, 0)


def test_resolve_bold_no_candidate_above_threshold():
    """اسم لا يشبه أيّ خزينة مسجّلة → (None, 0) → يُصعَّد لاحقًا (لا تخمين تحت العتبة)."""
    r, s = resolve_bold("زينكوترون", _TRS, 70)
    assert r is None and s == 0


def test_resolve_bold_customers_never_candidates():
    """(الخطّ الأحمر أ) يُستدعى على الخزائن فقط؛ اسم عميل غير مسجَّل كخزينة → لا يُحلّ خزينةً."""
    assert resolve_bold("محمد علي التاجر", _TRS, 70)[0] is None


# ── _auto_resolve_treasury — التنزيل + التنبيه + التعلّم ──────────────────────────────
class _RecBus:
    def __init__(self):
        self.central_jid = CENTRAL
        self.admin_jid = ADMIN
        self.admin_msgs: list[str] = []
        self.central_msgs: list = []
        self.reactions: list = []

    async def notify_admin(self, text, reply_to_key=None, forward_key=None):
        self.admin_msgs.append(text)

    async def reply_central(self, text, reply_to_key=None, *, is_alert=False):
        self.central_msgs.append(text)

    async def mark_central(self, key, emoji):
        self.reactions.append((key, emoji))

    async def flush_reactions(self):
        pass


def _pipe(db, bus):
    from core.pipeline import Pipeline

    class _W:
        name = "w"
        async def write(self, job, *, commit): ...
    return Pipeline(db, bus, _W(), None, customer_room_jids=[], treasury_room_jids=[])


def _leg(unresolved=None, ref="X1", amount=5000.0, phone="01011", code="100"):
    return ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code=code,
                     customer_name="زبون", amount=amount, phone=phone, price_normalized="6.0",
                     unresolved_treasury=unresolved, sender_jid=S1)


async def test_similarity_path_disabled_name_escalates(db):
    """🔴 (قرار المالك 2026-07-19) «ابو بوسف» يشبه «أبو يوسف جديد» — ومع ذلك **لا يُحلّ**:
    المسار ملغى (bold_resolve_enabled=False)، فيبقى unresolved بلا تنبيه حلّ ولا alias متعلَّم → تصعيد."""
    await db.treasuries.seed_if_empty([{"name": t.name, "code": t.code, "type": t.type.value} for t in _TRS])
    bus = _RecBus()
    pipe = _pipe(db, bus)
    assert pipe._bold_resolve_enabled is False                                  # المفتاح مطفأ افتراضًا
    leg = _leg(unresolved="ابو بوسف")
    raw = RawMessage(message_key="k", chat_jid=CENTRAL, sender_jid=S1, text="...", received_at=NOW)
    await pipe._auto_resolve_treasury(leg, raw, await db.treasuries.all_active())
    assert leg.treasury is None and leg.unresolved_treasury == "ابو بوسف"       # لم يُخمَّن
    assert not any(d.get("method") == "similarity" for d in leg.deviation_log)
    assert not bus.admin_msgs                                                   # لا تنبيه حلّ
    # ولا alias متعلَّم: الإملاء الخاطئ يظلّ غير محلول صارمًا
    from core.parsing.resolve import resolve_treasury
    assert resolve_treasury("ابو بوسف", await db.treasuries.all_active()) is None


async def test_similarity_path_disabled_supplier_escalates(db):
    """نظير الخزينة للموردين: اسم مورّد مشابه لا يُحلّ تلقائيًّا — يبقى unresolved → تصعيد."""
    await db.suppliers.seed_if_missing([{"name": "شركة النور", "code": "12"}])
    bus = _RecBus()
    pipe = _pipe(db, bus)
    leg = _leg()
    leg.unresolved_supplier = "شركه النوور"
    raw = RawMessage(message_key="k", chat_jid=CENTRAL, sender_jid=S1, text="...", received_at=NOW)
    await pipe._auto_resolve_supplier(leg, raw, await db.suppliers.all_active())
    assert leg.supplier is None and leg.unresolved_supplier == "شركه النوور"
    assert not bus.admin_msgs


async def test_manual_alias_still_resolves_strictly(db):
    """ما يبقى شغّالاً: alias **يدويّ** مسجَّل يُحلّ صارمًا (ليس تخمينًا)."""
    await db.treasuries.seed_if_empty([{"name": t.name, "code": t.code, "type": t.type.value} for t in _TRS])
    await db.treasuries.add_alias("أبو يوسف جديد", "ابو يوسف")
    from core.parsing.resolve import resolve_treasury
    assert resolve_treasury("ابو يوسف", await db.treasuries.all_active()).name == "أبو يوسف جديد"


def test_deterministic_normalization_still_works():
    """ما يبقى شغّالاً: التطبيع الحتميّ (همزات/ياءات/ة-ه/مسافات) — حتميّ لا تخمين."""
    from core.parsing.resolve import resolve_treasury
    assert resolve_treasury("ابو يوسف جديد", _TRS).name == "أبو يوسف جديد"   # همزة الوصل
    assert resolve_treasury("  محمود   صفاقس  ", _TRS).name == "محمود صفاقس"  # مسافات


async def test_auto_resolve_no_candidate_escalates_not_resolved(db):
    """لا مرشّح ≥ العتبة → لا حلّ (يبقى unresolved، بلا تنبيه حلّ) → يُصعَّد لاحقًا."""
    await db.treasuries.seed_if_empty([{"name": t.name, "code": t.code, "type": t.type.value} for t in _TRS])
    bus = _RecBus()
    pipe = _pipe(db, bus)
    leg = _leg(unresolved="زينكوترون")
    raw = RawMessage(message_key="k", chat_jid=CENTRAL, sender_jid=S1, text="...", received_at=NOW)
    await pipe._auto_resolve_treasury(leg, raw, await db.treasuries.all_active())
    assert leg.treasury is None and leg.unresolved_treasury == "زينكوترون"      # لم يُحلّ
    assert not bus.admin_msgs                                                   # لا تنبيه حلّ


# ── dedup بالمحتوى (الجزء 2) ──────────────────────────────────────────────────────────
def _deal(ref, phone, amount, code, status=Status.COMPLETED, did="d1"):
    leg = ParsedLeg(operation=OperationType.SELL, reference_number=ref, customer_code=code,
                    customer_name="زبون", amount=amount, phone=phone, price_normalized="6.0",
                    treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY),
                    sender_jid=S1)
    return Deal(deal_id=did, status=status, sell_leg=leg, created_at=NOW, updated_at=NOW, chat_jid=CENTRAL)


async def test_reference_reuse_duplicate_same_content(db):
    """(X1243) نفس المرجع + نفس الجوهر (هاتف/مبلغ/كود) — مهما طال الوقت → 'duplicate'."""
    bus = _RecBus()
    pipe = _pipe(db, bus)
    await db.deals.upsert(_deal("X1243", "01024383998", 3000.0, "1208"))
    incoming = ParsedLeg(operation=OperationType.SELL, reference_number="X1243", customer_code="1208",
                         amount=3000.0, phone="01024383998", sender_jid=S1)
    assert await pipe._reference_reuse_action(incoming, NOW) == "duplicate"


async def test_reference_reuse_reused_different_content(db):
    """(X1242) نفس المرجع + جوهر مختلف → 'reused' (صفقة جديدة + تنبيه، لا تجاهل)."""
    bus = _RecBus()
    pipe = _pipe(db, bus)
    await db.deals.upsert(_deal("X1242", "01000000001", 500.0, "100"))
    incoming = ParsedLeg(operation=OperationType.SELL, reference_number="X1242", customer_code="826",
                         amount=40000.0, phone="01099999999", sender_jid=S1)
    assert await pipe._reference_reuse_action(incoming, NOW) == "reused"


async def test_reference_reuse_new_when_no_prior(db):
    """لا صفقة سابقة بالمرجع → 'new'."""
    pipe = _pipe(db, _RecBus())
    incoming = ParsedLeg(operation=OperationType.SELL, reference_number="X9999", amount=100.0)
    assert await pipe._reference_reuse_action(incoming, NOW) == "new"


async def test_reference_reuse_ignores_waiting_and_cancelled(db):
    """صفقة WAITING (رسالة ثانية شرعيّة) أو CANCELLED (أُلغيت) لا تُحسَب سابقةً → 'new'."""
    pipe = _pipe(db, _RecBus())
    await db.deals.upsert(_deal("X1", "01011", 100.0, "1", status=Status.WAITING_SECOND_LEG, did="w"))
    await db.deals.upsert(_deal("X1", "01011", 100.0, "1", status=Status.CANCELLED, did="c"))
    incoming = ParsedLeg(operation=OperationType.SELL, reference_number="X1", customer_code="1",
                         amount=100.0, phone="01011")
    assert await pipe._reference_reuse_action(incoming, NOW) == "new"
