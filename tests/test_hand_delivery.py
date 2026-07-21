"""تسليم يد (delivery_type=يد) + «واتساب»=فودافون + تنظيف «صافى» — قرار المالك 2026-07-21 (X1702).

المبدأ: حوالة «باليد» بلا خزينة بطبيعتها والكاتب لا يكتب بلا خزينة (foreign_account إلزاميّ) ⇒
تُلتقَط تفاصيلها وتُصعَّد **نظيفًا** للإدخال اليدويّ (⚠️ لا 🔴 «سعر غير موجود» المضلِّل)."""
from __future__ import annotations

from datetime import datetime, timezone

from core.constants import Mark, OperationType, Status
from core.parsing import parse_message
from core.parsing.classify import detect_hand_delivery
from core.parsing.normalize import normalize_payment

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)

X1702 = "X1702\nيرجي تسليم \nامال التركي \n00201055416129\nواتساب \nالقيمه \n58,500\nالقاهرة\nباليد"


# ═════════════════════════════════════════════════════════════════════════════
# ① واتساب = فودافون (المسار الفعّال: normalize_payment)
# ═════════════════════════════════════════════════════════════════════════════
def test_watsapp_is_vodafone():
    assert normalize_payment("واتساب") == "فودافون كاش"
    assert normalize_payment("واتس اب") == "فودافون كاش"
    assert normalize_payment("فودا") == "فودافون كاش"   # لم ننكسر القائمة القديمة


# ═════════════════════════════════════════════════════════════════════════════
# ② كشف تسليم يد — علامة صريحة، لا «تسليم» وحدها، لا سلاسل جزئية
# ═════════════════════════════════════════════════════════════════════════════
def test_detect_hand_delivery_fires_on_explicit_markers():
    assert detect_hand_delivery("58500 القاهرة باليد")
    assert detect_hand_delivery("تسليم يد")
    assert detect_hand_delivery("تسليم بيد")            # X1674
    assert detect_hand_delivery("تسليم بي اليد")        # X1675 (اليد)
    assert detect_hand_delivery("بالبيد")


def test_detect_hand_delivery_ignores_bare_deliver_and_substrings():
    # «تسليم» وحدها (تسليمٌ لمستلم) ليست تسليمًا يدويًّا
    assert not detect_hand_delivery("يرجى تسليم امال التركي 5000 فودافون")
    # «عبيد» اسمٌ يحوي «بيد» كسلسلة جزئية — مطابقة الكلمات الكاملة تمنع الالتباس
    assert not detect_hand_delivery("عبيد الله 5000 فودافون")
    assert not detect_hand_delivery("محمد 5000 فودافون")


# ═════════════════════════════════════════════════════════════════════════════
# ③ parse_message يثبّت delivery_type=يد على الطرف (مع رقم إشاري — X1702)
# ═════════════════════════════════════════════════════════════════════════════
def test_parse_sets_delivery_type_on_referenced_hand_delivery():
    res = parse_message(X1702, [], [], [])
    assert res.leg is not None
    assert res.leg.delivery_type == "يد"
    assert res.leg.reference_number == "X1702"
    assert res.leg.treasury is None                     # لا خزينة (تسليم يد)


def test_no_delivery_type_on_ordinary_transfer():
    res = parse_message("X1710\n633 حريز 6.02\n01000000000\n5000 ج م\nفودافون", [], [], [])
    assert res.leg is None or res.leg.delivery_type is None


# ═════════════════════════════════════════════════════════════════════════════
# ④ الأنبوب: تسليم يد بلا خزينة → صفقة موقوفة (ESCALATED) + ⚠️ + تصعيد نظيف، بلا كتابة
# ═════════════════════════════════════════════════════════════════════════════
def _pipe(db, writer):
    from core.bus import Bus
    from core.pipeline import Pipeline

    class _V:
        enabled = False
        async def verify_transaction(self, *a, **k): return (False, None)
        async def find_last_pending(self, *a, **k): return None

    return Pipeline(db, Bus(db, {"c@g.us", "a@g.us"}, "c@g.us", "a@g.us"), writer, _V(),
                    customer_room_jids=[], treasury_room_jids=[])


async def test_pipeline_hand_delivery_escalates_clean_no_write(db):
    from core.constants import Currency
    from core.models import BotControl, Deal, ParsedLeg

    class _W:
        name = "w"
        def __init__(self): self.calls = []
        async def write(self, job, *, commit):
            self.calls.append(commit)
            from core.models import WriteResult
            return WriteResult(ok=True)

    await db.control.set(BotControl(storage_enabled=True, auto_trust=True, state="running"), "t")
    writer = _W()
    pipe = _pipe(db, writer)
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="X1702",
                    customer_name="امال التركي", amount=58500.0, currency=Currency.EGP,
                    phone="00201055416129", delivery_type="يد")
    deal = Deal(deal_id="dhd1", status=Status.PARSED, sell_leg=leg,
                created_at=NOW, updated_at=NOW, chat_jid="c@g.us",
                source_message_keys=["c@g.us|K1|0|s@lid"])
    await db.deals.upsert(deal)
    out = await pipe.process_deal(deal, NOW)
    assert out.status == Status.ESCALATED
    assert out.mark == Mark.WARN
    assert "تسليم يد" in (out.hold_reason or "")
    assert writer.calls == []                            # 🔴 لم تُكتب في MONEYADO


async def test_pipeline_hand_delivery_with_treasury_not_blocked(db):
    """أمان: علامة يد مع خزينة محلولة → بوّابة اليد **لا** تُطلَق (تمضي كحوالة عاديّة)."""
    from core.constants import Currency, TreasuryType
    from core.models import BotControl, Deal, ParsedLeg, TreasuryRef

    class _W:
        name = "w"
        def __init__(self): self.calls = []
        async def write(self, job, *, commit):
            self.calls.append(commit)
            from core.models import WriteResult
            return WriteResult(ok=True)

    await db.control.set(BotControl(storage_enabled=True, auto_trust=True, state="running"), "t")
    writer = _W()
    pipe = _pipe(db, writer)
    leg = ParsedLeg(operation=OperationType.SELL, reference_number="X1711",
                    customer_code="633", customer_name="حريز", amount=5000.0,
                    price_raw="6.02", price_normalized="6.02", currency=Currency.EGP,
                    delivery_type="يد",
                    treasury=TreasuryRef(code="74", name="بلاس فون",
                                         type=TreasuryType.SELL_ONLY, currency=Currency.EGP))
    deal = Deal(deal_id="dhd2", status=Status.PARSED, sell_leg=leg,
                created_at=NOW, updated_at=NOW, chat_jid="c@g.us",
                source_message_keys=["c@g.us|K2|0|s@lid"])
    await db.deals.upsert(deal)
    out = await pipe.process_deal(deal, NOW)
    # لم تُصعَّد ببوّابة اليد (خزينتها محلولة) — إمّا كُتبت أو مضت لمسار الثقة/الكتابة العاديّ
    assert not (out.status == Status.ESCALATED and "تسليم يد" in (out.hold_reason or ""))
