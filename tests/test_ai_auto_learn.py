"""
التعلّم التلقائي من نجاحات الذكاء (قرار المالك).

المبدأ: نجاحٌ **مؤكَّد** للذكاء (ثقة ≥ 0.9 + تحقّقٌ حتميّ + كتابةٌ في MONEYADO) → تُستخرَج
أزواج «خطأ→صحيح» تلقائيًّا وتُحفَظ في القاموس بـcreated_by=«ai_auto»، فتسري فورًا على التالي.

الضمانات: تعلّمٌ من النجاحات فقط · اليدويّ يكسب دائمًا · لا تكرار · قابلٌ للحذف من /corrections.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from core.constants import OperationType, Status
from core.db import utcnow
from core.models import CorrectionRecord, Deal, ParsedLeg
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _Fix:
    """يحاكي VerifiedCorrection (raw/official/entity_type/confidence)."""
    raw: str
    official: str
    entity_type: str
    confidence: float
    record: object = None


def _mgr(**over):
    base = {"field_type": "supplier", "wrong_text": "x", "correct_value": "y"}
    base.update(over)
    return CorrectionRecord(**base)


# ═════════════════════════════════════════════════════════════════════════════
# learn_if_absent — اليدويّ يكسب، لا تكرار
# ═════════════════════════════════════════════════════════════════════════════
async def test_learn_if_absent_inserts_when_new(db):
    ok = await db.corrections.learn_if_absent(
        CorrectionRecord(field_type="supplier", wrong_text="فكهاتي", correct_value="فكهاني",
                         created_by="ai_auto", confidence=0.95))
    assert ok is True
    rows = await db.corrections.list_all()
    r = next(x for x in rows if x["wrong_text"] == "فكهاتي")
    assert r["created_by"] == "ai_auto" and r["correct_value"] == "فكهاني"


async def test_learn_if_absent_skips_duplicate(db):
    await db.corrections.learn_if_absent(_mgr(wrong_text="فكهاتي", correct_value="فكهاني",
                                              created_by="ai_auto"))
    ok = await db.corrections.learn_if_absent(_mgr(wrong_text="فكهاتي", correct_value="آخر",
                                                   created_by="ai_auto"))
    assert ok is False                                          # موجود → لا تكرار
    r = next(x for x in await db.corrections.list_all() if x["wrong_text"] == "فكهاتي")
    assert r["correct_value"] == "فكهاني"                       # لم يُدهَس


async def test_manual_correction_always_wins(db):
    """تصحيحٌ يدويّ موجود → التعلّم التلقائي لا يدهسه (اليدويّ يكسب دائمًا)."""
    await db.corrections.upsert(_mgr(wrong_text="راحنلا", correct_value="النحار",
                                     field_type="treasury", created_by="manager"))
    ok = await db.corrections.learn_if_absent(_mgr(wrong_text="راحنلا", correct_value="شيء آخر",
                                                   field_type="treasury", created_by="ai_auto"))
    assert ok is False
    r = next(x for x in await db.corrections.list_all() if x["wrong_text"] == "راحنلا")
    assert r["correct_value"] == "النحار" and r["created_by"] == "manager"


# ═════════════════════════════════════════════════════════════════════════════
# _record_learnable_corrections — يثبّت الأزواج على الطرف (deviation_log)
# ═════════════════════════════════════════════════════════════════════════════
def test_record_learnable_only_entity_types():
    leg = ParsedLeg(operation=OperationType.SELL)
    Pipeline._record_learnable_corrections(leg, [
        _Fix("فكهاتي", "فكهاني", "supplier", 0.95),
        _Fix("راحنلا", "النحار", "treasury", 0.92),
        _Fix("شيء", "آخر", "currency", 0.99),        # نوعٌ غير مدعوم → يُتجاهَل
        _Fix("طه", "طه", "supplier", 0.99),           # لا فرق → يُتجاهَل
    ], "fake/model")
    learn = [d for d in leg.deviation_log if d.get("field") == "ai_learn"]
    assert len(learn) == 2
    assert {d["wrong_text"] for d in learn} == {"فكهاتي", "راحنلا"}
    assert all(d["confidence"] >= 0.9 for d in learn)


# ═════════════════════════════════════════════════════════════════════════════
# _learn_from_ai_success — يحفظ بعد النجاح، يحترم عتبة الثقة
# ═════════════════════════════════════════════════════════════════════════════
def _pipe(db):
    from core.bus import Bus

    class _W:
        name = "w"
        async def write(self, job, *, commit):
            from core.models import WriteResult
            return WriteResult(ok=True)

    class _V:
        enabled = False
        async def verify_transaction(self, *a, **k):
            return (False, None)
        async def find_last_pending(self, *a, **k):
            return None

    return Pipeline(db, Bus(db, {"c@g.us", "a@g.us"}, "c@g.us", "a@g.us"), _W(), _V(),
                    customer_room_jids=[], treasury_room_jids=[])


def _deal_with_learn(entries):
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="X9", amount=1000.0,
                    deviation_log=list(entries))
    return Deal(deal_id="d1", status=Status.COMPLETED, sell_leg=leg,
                created_at=NOW, updated_at=NOW, chat_jid="c@g.us")


async def test_learn_from_success_saves_ai_auto(db):
    pipe = _pipe(db)
    deal = _deal_with_learn([
        {"field": "ai_learn", "field_type": "supplier", "wrong_text": "فكهاتي",
         "correct_value": "فكهاني", "confidence": 0.95, "model": "m"},
        {"field": "ai_first", "raw_value": "غامضة"},          # غير قابل للتعلّم
    ])
    await pipe._learn_from_ai_success(deal)
    active = await db.corrections.all_active()
    got = [c for c in active if c.wrong_text == "فكهاتي"]
    assert got and got[0].created_by == "ai_auto" and got[0].confidence == 0.95


async def test_learn_from_success_respects_confidence_gate(db):
    pipe = _pipe(db)
    deal = _deal_with_learn([
        {"field": "ai_learn", "field_type": "supplier", "wrong_text": "ضعيف",
         "correct_value": "قويّ", "confidence": 0.85},         # < 0.9 → لا يُحفَظ
    ])
    await pipe._learn_from_ai_success(deal)
    assert not any(c.wrong_text == "ضعيف" for c in await db.corrections.all_active())


async def test_learned_correction_applies_live_to_next_message(db):
    """التصحيح المُتعلَّم يسري فورًا على الرسالة التالية (نفس all_active) — dynamic wins."""
    from core.corrections import apply_corrections
    from core.parsing import parse_completion_fragment
    from core.models import SupplierRecord
    await db.suppliers.col.insert_one(SupplierRecord(
        code="1255", name="النحار", aliases=["نجار"], active=True).model_dump())
    pipe = _pipe(db)
    deal = _deal_with_learn([
        {"field": "ai_learn", "field_type": "supplier", "wrong_text": "راحنلا",
         "correct_value": "النحار", "confidence": 0.93},
    ])
    await pipe._learn_from_ai_success(deal)
    corr = await db.corrections.all_active()
    # ① الاستبدال حيٌّ فورًا (dynamic wins)
    out, fired = apply_corrections("راحنلا 5.90", corr)
    assert out == "النحار 5.90" and fired == ["راحنلا"]
    # ② وتُحلّ المورّد في مسار الرسالة الثانية بعد التصحيح
    tre, sup = await db.treasuries.all_active(), await db.suppliers.all_active()
    frag = parse_completion_fragment("راحنلا 5.90", tre, sup, corr)
    assert frag.supplier is not None and frag.supplier.code == "1255"


# ═════════════════════════════════════════════════════════════════════════════
# تكامل: صفقةٌ تكتمل بكتابةٍ فعليّة (commit) → التعلّم يجري في فرع الإكمال
# ═════════════════════════════════════════════════════════════════════════════
async def test_learning_triggers_on_committed_write(db):
    """صفقةٌ بتصحيح ذكاءٍ مثبَّت (ai_learn) تُكتَب فعليًّا في MONEYADO (storage_enabled + auto_trust)
    → التصحيح يُحفَظ ai_auto من فرع الإكمال (لا من dry_run/الإيقاف)."""
    from core.constants import Currency, Mark, TreasuryType
    from core.models import BotControl, TreasuryRef
    await db.control.set(BotControl(storage_enabled=True, auto_trust=True, state="running"), "t")
    pipe = _pipe(db)
    leg = ParsedLeg(
        operation=OperationType.SELL, reference_number="X77", customer_code="633",
        customer_name="حريز", amount=5000.0, price_raw="6.02", price_normalized="6.02",
        currency=Currency.EGP,
        treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY,
                             currency=Currency.EGP),
        deviation_log=[{"field": "ai_learn", "field_type": "supplier",
                        "wrong_text": "حريزز", "correct_value": "حريز", "confidence": 0.94}])
    deal = Deal(deal_id="dc1", status=Status.PARSED, sell_leg=leg, created_at=NOW, updated_at=NOW,
                chat_jid="c@g.us", source_message_keys=["mk1"])
    await db.deals.upsert(deal)
    await pipe.process_deal(deal, NOW)
    got = [c for c in await db.corrections.all_active() if c.wrong_text == "حريزز"]
    assert got and got[0].created_by == "ai_auto", "لم يتعلّم من الكتابة الناجحة"


async def test_no_learning_when_storage_off(db):
    """التخزين موقوف (Kill Switch) → لا كتابة فعليّة → **لا تعلّم** (نجاحٌ غير مؤكَّد)."""
    from core.constants import Currency, TreasuryType
    from core.models import BotControl, TreasuryRef
    await db.control.set(BotControl(storage_enabled=False, auto_trust=True, state="running"), "t")
    pipe = _pipe(db)
    leg = ParsedLeg(
        operation=OperationType.SELL, reference_number="X78", customer_code="633",
        customer_name="حريز", amount=5000.0, price_raw="6.02", price_normalized="6.02",
        currency=Currency.EGP,
        treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY,
                             currency=Currency.EGP),
        deviation_log=[{"field": "ai_learn", "field_type": "supplier",
                        "wrong_text": "ززز", "correct_value": "حريز", "confidence": 0.94}])
    deal = Deal(deal_id="dc2", status=Status.PARSED, sell_leg=leg, created_at=NOW, updated_at=NOW,
                chat_jid="c@g.us", source_message_keys=["mk2"])
    await db.deals.upsert(deal)
    await pipe.process_deal(deal, NOW)
    assert not any(c.wrong_text == "ززز" for c in await db.corrections.all_active())
