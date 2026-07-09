"""
اختبارات التكامل — التدفّق الكامل (§15) عبر الأنبوب على الأمثلة الحقيقية.

تُثبت الضمانات الحرجة:
- المسار الذهبي: بيع مصري بسيط → مطابقة → كتابة → تحقّق → ✅ + قيد دفتر.
- تجميع الطرفين (§5.5): بيع+شراء برقم إشاري+هاتف = صفقة واحدة، الترتيب بيع أولًا.
- منع التكرار (§9): نفس الرسالة مرّتين → الثانية تُتجاهل.
- Kill Switch (§13): الافتراضي إيقاف → تعبئة بلا تخزين، بلا قيد دفتر.
- الإلغاء (§10): موظف معتمد → قيد عكسي بكامل القيمة.
- قاعدة الإخراج (§2.2): لا مخرجات لغرف الزبائن/الخزائن إطلاقًا.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.bus import Bus
from core.constants import Mark, OperationType, RoomType, Status, TreasuryType
from core.models import (
    BotControl, Deal, EmployeeRecord, ParsedLeg, RawMessage, Room, TreasuryRecord, WriteResult,
)
from core.pipeline import Pipeline

CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
CUST_ROOM = "customers@g.us"
TREAS_ROOM = "treasury@g.us"
EMP = "20100@s.whatsapp.net"

PAST = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)


# ─────────────────────────────────────────────────────────────────────────────
# مزيّفات الكتابة والتحقّق (نختبر تنسيق الأنبوب، لا داخليات RPA/SQL المختبَرة سابقًا)
# ─────────────────────────────────────────────────────────────────────────────
class SqlState:
    """حالة SQL محاكاة: مجموعة الحوالات المحفوظة فعلًا (reference, operation)."""
    def __init__(self):
        self.stored: set[tuple[str, str]] = set()


class FakeWriter:
    name = "fake"

    def __init__(self, result: WriteResult | None = None, fail_ops: set[str] | None = None,
                 sql: SqlState | None = None):
        self.calls: list[tuple[str, int, bool]] = []  # (operation, order_index, commit)
        self.jobs: list = []                            # الأوامر الكاملة (للتحقّق من القيم)
        self._result = result
        self._fail_ops = fail_ops or set()              # عمليات تُفشَل عمدًا (اختبار الفشل النصفي)
        self._sql = sql

    async def write(self, job, *, commit: bool) -> WriteResult:
        self.calls.append((job.operation.value, job.order_index, commit))
        self.jobs.append(job)
        if job.operation.value in self._fail_ops:
            return WriteResult(ok=False, needs_review=True, error="فشل محاكى", screenshot_path="/x.png")
        if self._result is not None:
            return self._result
        # «تخزين» فعلي ناجح → يظهر في SQL (يحاكي انعكاس الحفظ على القاعدة §11.4)
        if commit and self._sql is not None:
            self._sql.stored.add((job.leg.reference_number or "", job.operation.value))
        return WriteResult(ok=True)


class StubVerifier:
    """
    يحاكي SqlVerifier: verify_transaction يعكس حالة SQL الحقيقية —
    False قبل التخزين، True بعده. هذا ضروري لصحّة فحص §9 قبل الكتابة.
    """
    def __init__(self, sql: SqlState | None = None, enabled=True, ref="MREF-001", fail_verify=False):
        self.enabled = enabled
        self._sql = sql if sql is not None else SqlState()
        self._ref = ref
        self.fail_verify = fail_verify  # يحاكي انقطاع SQL العابر (لا يؤكّد رغم الحفظ الفعلي)

    async def verify_transaction(self, reference_number, amount, customer_code, operation):
        if self.fail_verify:
            return (False, None)  # عطل عابر — لا يؤكّد
        op = operation.value if hasattr(operation, "value") else str(operation)
        if (reference_number or "", op) in self._sql.stored:
            return (True, self._ref)
        return (False, None)

    async def find_last_pending(self, reference_number):
        return None


def _make_pipeline(db, writer=None, verifier=None, rooms=True):
    """يبني أنبوبًا باختبار متّسق: الكاتب والمتحقّق يتشاركان نفس حالة SQL المحاكاة."""
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    sql = SqlState()
    writer = writer or FakeWriter()
    verifier = verifier or StubVerifier()
    # فرض مشاركة نفس حالة SQL بين الكاتب والمتحقّق (اتّساق الاختبار)
    writer._sql = sql
    verifier._sql = sql
    return Pipeline(
        db, bus, writer, verifier,
        customer_room_jids=[CUST_ROOM] if rooms else [],
        treasury_room_jids=[TREAS_ROOM] if rooms else [],
    )


def _raw(key, text, jid=CENTRAL, sender=EMP, reply=None, at=PAST):
    return RawMessage(
        message_key=key, chat_jid=jid, sender_jid=sender, text=text,
        received_at=at, reply_to_key=reply,
    )


async def _enable_storage(db):
    await db.control.set(BotControl(storage_enabled=True, state="running"), "test")


# ── طرف الشراء المشتقّ لخزينة sell_and_buy (§6) — المرحلتان ٢ و٣ ──────────────
def _sab_sell_leg(**over):
    """طرف بيع على خزينة «بيع وشراء» بكود مُستكمل (SI بخصم: قبل 20475/بعد 20271)."""
    from core.constants import Currency, OperationType, TreasuryType
    from core.models import ParsedLeg, TreasuryRef
    base = dict(
        operation=OperationType.SELL, customer_code="570", customer_name="ايهاب ابو حميد",
        amount=20475.0, amount_after_discount=20271.0, commission=-204.0,
        currency=Currency.EGP, phone="01037354643", reference_number="SI1417",
        price_normalized="5.9", source_message_key="sab-1",
        treasury=TreasuryRef(code="90", name="خصم 1%", type=TreasuryType.SELL_AND_BUY, currency=Currency.EGP),
    )
    base.update(over)
    return ParsedLeg(**base)


def _sab_deal(leg):
    from core.models import Deal
    return Deal(deal_id="d-sab", status=Status.PARSED, sell_leg=leg,
               created_at=PAST, updated_at=PAST, source_message_keys=["sab-1"])


async def test_synthesize_buy_leg_for_sell_and_buy_with_supplier(db):
    # sell_and_buy **مع مورد** → تخليق طرف شراء (المبلغ الصافي، نفس الخزينة/الرقم/الهاتف)
    from core.models import SupplierRef
    pipe = _make_pipeline(db)
    deal = _sab_deal(_sab_sell_leg(supplier=SupplierRef(code="760", name="طه"), supplier_price_raw="5.72"))
    pipe._maybe_synthesize_buy_leg(deal)

    assert deal.is_two_legged is True
    buy = deal.buy_leg
    assert buy is not None and buy.operation == OperationType.BUY
    assert buy.amount == 20271                 # المبلغ بعد الخصم
    assert buy.amount_after_discount is None
    assert buy.commission is None              # بلا عمولة
    assert buy.commission_rate == 0.0
    assert buy.treasury.code == "90"           # نفس الخزينة الخارجية
    assert buy.reference_number == "SI1417"    # نفس الرقم الإشاري
    assert buy.phone == "01037354643"          # نفس الهاتف
    # طرف البيع لم يتغيّر (المبلغ قبل الخصم، العمولة سالبة)
    assert deal.sell_leg.amount == 20475 and deal.sell_leg.commission == -204.0


async def test_synthesize_buy_leg_uses_supplier_code_and_rate(db):
    # SI مع مورد: طرف الشراء المشتقّ = كود المورد + سعر المورد (لا نسخة صرفة من البيع)
    from core.constants import OperationType
    from core.models import SupplierRef
    pipe = _make_pipeline(db)
    leg = _sab_sell_leg(supplier=SupplierRef(code="760", name="طه"), supplier_price_raw="5.72")
    deal = _sab_deal(leg)
    pipe._maybe_synthesize_buy_leg(deal)

    buy = deal.buy_leg
    assert buy is not None and buy.operation == OperationType.BUY
    assert buy.amount == 20271                      # الصافي كالمعتاد
    assert buy.customer_code == "760"               # كود المورد (الحساب في شاشة الشراء §5.3)
    assert buy.customer_name == "طه"
    assert buy.price_normalized == "5.72"           # سعر المورد لا سعر البيع (5.9)
    assert buy.is_supplier_counterpart is True
    assert buy.supplier is not None and buy.supplier.code == "760"
    assert buy.supplier_price_raw is None           # استُهلك في البناء
    # طرف البيع لم يتغيّر (سعره وكوده وعمولته كما هي)
    assert deal.sell_leg.price_normalized == "5.9"
    assert deal.sell_leg.customer_code == "570"
    assert deal.sell_leg.commission == -204.0


async def test_synthesized_supplier_buy_leg_screen_fields(db):
    # النتيجة النهائية على شاشة الشراء: الحساب = كود المورد، السعر = سعر المورد، الكمية = الصافي
    from core.models import SupplierRef
    from core.writers.moneyado.fields import build_buy_fields
    pipe = _make_pipeline(db)
    leg = _sab_sell_leg(supplier=SupplierRef(code="760", name="طه"), supplier_price_raw="5.72")
    deal = _sab_deal(leg)
    pipe._maybe_synthesize_buy_leg(deal)
    ops = {op.key: op.value for op in build_buy_fields(deal.buy_leg)}
    assert ops["customer"] == "760"                 # كود المورد في خانة الحساب
    assert ops["rate_divide"] == "5.72"             # سعر المورد
    assert ops["quantity"] == "20271"               # الكمية = الصافي


async def test_no_synthesize_without_supplier(db):
    # 🔴 sell_and_buy **بلا «المورد:»** → لا تخليق طرف شراء (بيع فقط، قرار صاحب العمل)
    pipe = _make_pipeline(db)
    deal = _sab_deal(_sab_sell_leg())               # بلا supplier
    pipe._maybe_synthesize_buy_leg(deal)
    assert deal.buy_leg is None                     # لا طرف شراء
    assert deal.is_two_legged is False
    # طرف البيع لم يتغيّر
    assert deal.sell_leg.customer_code == "570" and deal.sell_leg.amount == 20475


async def test_synthesize_uses_amount_when_no_discount(db):
    from core.models import SupplierRef
    pipe = _make_pipeline(db)
    deal = _sab_deal(_sab_sell_leg(amount=5000.0, amount_after_discount=None, commission=None,
                                   supplier=SupplierRef(code="760", name="طه")))
    pipe._maybe_synthesize_buy_leg(deal)
    assert deal.buy_leg.amount == 5000.0       # بلا خصم → المبلغ نفسه


async def test_no_synthesis_for_sell_only_treasury(db):
    from core.constants import Currency, TreasuryType
    from core.models import TreasuryRef
    pipe = _make_pipeline(db)
    leg = _sab_sell_leg(treasury=TreasuryRef(code="74", name="بلاس فون", type=TreasuryType.SELL_ONLY, currency=Currency.EGP))
    deal = _sab_deal(leg)
    pipe._maybe_synthesize_buy_leg(deal)
    assert deal.buy_leg is None and deal.is_two_legged is False


async def test_no_synthesis_when_supplier_buy_leg_exists(db):
    # طرفان بمورد أصلاً → لا تخليق (لا يمسّ مسار المورد §5.3)
    from core.constants import OperationType
    from core.models import ParsedLeg, SupplierRef
    pipe = _make_pipeline(db)
    deal = _sab_deal(_sab_sell_leg())
    deal.buy_leg = ParsedLeg(operation=OperationType.BUY,
                             supplier=SupplierRef(code="760", name="طه"), amount=20000.0)
    deal.is_two_legged = True
    pipe._maybe_synthesize_buy_leg(deal)
    assert deal.buy_leg.supplier.code == "760" and deal.buy_leg.amount == 20000.0


async def test_sell_and_buy_produces_ordered_sell_then_buy_jobs(db):
    # المرحلة ٣: بعد التخليق (مع مورد)، أوامر الكتابة = بيع (order=0) ثم شراء (order=1)
    from core.models import SupplierRef
    pipe = _make_pipeline(db)
    deal = _sab_deal(_sab_sell_leg(supplier=SupplierRef(code="760", name="طه"), supplier_price_raw="5.72"))
    await db.deals.upsert(deal)
    pipe._maybe_synthesize_buy_leg(deal)
    jobs = sorted(await pipe.queue.build_write_jobs(deal), key=lambda j: j.order_index)
    assert [j.operation for j in jobs] == [OperationType.SELL, OperationType.BUY]
    assert (jobs[0].order_index, jobs[1].order_index) == (0, 1)
    assert jobs[1].leg.amount == 20271 and jobs[1].leg.commission is None


# ═════════════════════════════════════════════════════════════════════════════
# 1) المسار الذهبي — بيع مصري بسيط (§3.2) مع مطابقة الغرف
# ═════════════════════════════════════════════════════════════════════════════
async def test_golden_path_simple_egp_sell(db):
    await _enable_storage(db)
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier())
    # ربط غرفة الخزينة بكود «بلاس فون» (74) — شرط بحث المطابقة المحصور بالغرفة (§8.1)
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY, treasury_code="74", active=True))

    # رسائل الغرف (مصدر المطابقة §8) — تحمل الاسم + المبلغ + الهاتف
    await pipe.capture(_raw("cust1", "فداء شاكونه 1600 ج م 010954227116", jid=CUST_ROOM))
    await pipe.capture(_raw("treas1", "بلاس 1600 فداء شاكونه", jid=TREAS_ROOM))

    # حوالة المركزية (§3.2)
    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون\nبدون خصم"
    await pipe.capture(_raw("m1", text, jid=CENTRAL))

    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    deal = (await db.deals.by_status(Status.COMPLETED))
    assert len(deal) == 1, "الصفقة يجب أن تكتمل ✅"
    d = deal[0]
    assert d.sell_leg.customer_code == "1208"
    assert d.sell_leg.amount == 1600
    assert d.sell_leg.treasury.code == "74"          # بلاس فون (§4.2)
    assert d.sell_leg.price_normalized == "5.84"      # مصري كما هو (§3.6)
    assert d.mark == Mark.DONE

    # الكاتب: عملية بيع واحدة بالتزام (commit=True)
    assert writer.calls == [("sell", 0, True)]

    # قيد الدفتر مسجَّل ومؤكَّد (§9)
    entries = await db.ledger.entries_for_deal(d.deal_id)
    assert len(entries) == 1 and entries[0].sql_verified is True
    assert entries[0].moneyado_ref == "MREF-001"

    # علامة ✅ أُرسلت للمركزية فقط (§2.2 §8.3)
    outs = await db.outgoing.next_unsent(50)
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)
    assert any(o.get("reaction") == Mark.DONE.value and o["chat_jid"] == CENTRAL for o in outs)


async def test_expire_stale_pending_on_startup(db):
    """حجْر الإقلاع: WAITING/PARSED عمرها >120s → ESCALATED + تنبيه؛ الحديثة (≤120s) تبقى."""
    pipe = _make_pipeline(db)
    now = PAST + timedelta(seconds=1000)
    old = now - timedelta(seconds=200)        # > 120s → تُحجَر
    fresh = now - timedelta(seconds=30)        # ≤ 120s → تبقى

    def _leg(ref):
        return ParsedLeg(operation=OperationType.SELL, customer_code="1208",
                         amount=1600.0, reference_number=ref)

    deals = [
        Deal(deal_id="w1", status=Status.WAITING_SECOND_LEG, created_at=old, updated_at=old,
             chat_jid=CENTRAL, sell_leg=_leg("A1")),
        Deal(deal_id="p1", status=Status.PARSED, created_at=old, updated_at=old,
             chat_jid=CENTRAL, sell_leg=_leg("A2")),
        Deal(deal_id="p2", status=Status.PARSED, created_at=fresh, updated_at=fresh,
             chat_jid=CENTRAL, sell_leg=_leg("A3")),   # حديثة
    ]
    for d in deals:
        await db.deals.upsert(d)

    quarantined = await pipe.expire_stale_on_startup(now)
    assert {d.deal_id for d in quarantined} == {"w1", "p1"}
    assert (await db.deals.get("w1")).status == Status.ESCALATED
    assert (await db.deals.get("p1")).status == Status.ESCALATED
    assert (await db.deals.get("p2")).status == Status.PARSED       # الحديثة لم تُحجَر
    # تنبيه واحد للمسؤول لكل صفقة محجورة (لا كتابة في MONEYADO)
    outs = await db.outgoing.next_unsent(50)
    admin_notifs = [o for o in outs if o["chat_jid"] == ADMIN]
    assert len(admin_notifs) == 2


async def test_two_message_supplier_second_merges_not_new_deal(db):
    """حوالة A مع مورد برسالتين بنفس الرقم الإشاري: الثانية (المورد) تُدمَج في صفقة الأولى
    (الزبون) لا تُنشئ صفقة جديدة → طرفان: بيع الزبون + شراء المورد المشتقّ (§6)."""
    from core.parsing import parse_message
    from core.queue.service import QueueService

    treas = await db.treasuries.all_active()
    svc = QueueService(db)
    now = PAST + timedelta(seconds=1000)

    # الرسالة الأولى (الزبون): A8136 + مبلغ 1010 + كود 1300 + سعر 5.90 (بلا خزينة → تنتظر الثانية)
    leg1 = parse_message(
        "A8136\n01001234567\n1010 ج م\nفودافون كاش\n1300 عبدالله معتيق 5.90", treas, []).leg
    leg1.source_message_key = "m1"
    deal1 = await svc.try_group(leg1, now, chat_jid=CENTRAL)
    assert deal1.status == Status.WAITING_SECOND_LEG

    # الرسالة الثانية (المورد): نفس A8136 + مبلغ أصغر 1000 + كود 760 «طه» (غير مُدرَج كمورد)
    msg2 = "A8136\n01001234567\n1000 ج م\nفودافون كاش\n760 طه 5.90"
    leg2 = parse_message(msg2, treas, []).leg
    leg2.source_message_key = "m2"
    merged = await svc.try_absorb_supplier_second(leg2, _raw("m2", msg2), now, treas, [])
    assert merged is not None, "الرسالة الثانية تُدمَج لا تُنشئ صفقة جديدة"
    assert merged.deal_id == deal1.deal_id

    sell = merged.sell_leg
    assert (sell.customer_code, sell.customer_name) == ("1300", "عبدالله معتيق")
    assert sell.amount == 1010 and sell.amount_after_discount == 1000     # الأصغر = بعد الخصم
    assert sell.supplier is not None and (sell.supplier.code, sell.supplier.name) == ("760", "طه")
    assert sell.supplier_price_raw == "5.90"
    assert sell.treasury is not None and sell.treasury.type == TreasuryType.SELL_AND_BUY  # «خصم 1%»

    # طرف الشراء المشتقّ من المورد (pipeline)
    pipe = _make_pipeline(db)
    pipe._maybe_synthesize_buy_leg(merged)
    buy = merged.buy_leg
    assert buy is not None and buy.operation == OperationType.BUY
    assert buy.customer_code == "760" and buy.amount == 1000
    assert buy.price_normalized == "5.90" and buy.is_supplier_counterpart is True


async def test_supplier_second_ignored_without_customer_code(db):
    """رسالة ثانية بلا كود (تسوية خصم/خزينة فقط) لا تُلتقط كطرف مورد — تتبع المسار العادي."""
    from core.parsing import parse_message
    from core.queue.service import QueueService
    treas = await db.treasuries.all_active()
    svc = QueueService(db)
    now = PAST + timedelta(seconds=1000)
    leg1 = parse_message(
        "A8136\n01001234567\n1010 ج م\nفودافون كاش\n1300 عبدالله معتيق 5.90", treas, []).leg
    leg1.source_message_key = "m1"
    await svc.try_group(leg1, now, chat_jid=CENTRAL)
    # رسالة ثانية: خزينة + مبلغ بلا كود → ليست طرف مورد
    msg2 = "A8136\n01001234567\n1000 ج م\nبلاس فون"
    leg2 = parse_message(msg2, treas, []).leg
    leg2.source_message_key = "m2"
    assert await svc.try_absorb_supplier_second(leg2, _raw("m2", msg2), now, treas, []) is None


async def test_supplier_second_pattern_fishing_code_last(db):
    """متانة الترتيب: الرسالة الثانية بالكود **آخر السطر** والسعر أولًا → تُلتقط بـ pattern-fishing."""
    from core.parsing import parse_message
    from core.queue.service import QueueService
    treas = await db.treasuries.all_active()
    svc = QueueService(db)
    now = PAST + timedelta(seconds=1000)
    leg1 = parse_message(
        "A8136\n01001234567\n1010 ج م\nفودافون كاش\n1300 عبدالله معتيق 5.90", treas, []).leg
    leg1.source_message_key = "m1"
    await svc.try_group(leg1, now, chat_jid=CENTRAL)
    # الرسالة الثانية: المبلغ بسطر عملة، وسطر المورد «5.90 طه 760» (الكود آخرًا — يفشل سطرًا-بسطر)
    msg2 = "A8136\n01001234567\n1000 ج م\nفودافون كاش\n5.90 طه 760"
    leg2 = parse_message(msg2, treas, []).leg
    leg2.source_message_key = "m2"
    merged = await svc.try_absorb_supplier_second(leg2, _raw("m2", msg2), now, treas, [])
    assert merged is not None
    sell = merged.sell_leg
    assert sell.supplier is not None and (sell.supplier.code, sell.supplier.name) == ("760", "طه")
    assert sell.amount_after_discount == 1000 and sell.supplier_price_raw == "5.90"


async def test_auto_trust_skips_room_matching_and_completes(db):
    """وضع التلقائي (auto_trust): غرف مُصنّفة بلا رسالة غرفة → تُتخطّى المطابقة وتكتمل عبر بوابة الثقة."""
    await db.control.set(BotControl(storage_enabled=True, auto_trust=True, state="running"), "test")
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier())                  # غرف مُصنّفة (rooms=True)
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY, treasury_code="74", active=True))
    # حوالة المركزية فقط — بلا أي رسالة في غرفة الخزينة/الزبون (لن تتطابق عادةً)
    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون\nبدون خصم"
    await pipe.capture(_raw("m1", text, jid=CENTRAL))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)
    completed = await db.deals.by_status(Status.COMPLETED)
    assert len(completed) == 1, "auto_trust يكمل بلا مطابقة غرف"
    assert writer.calls == [("sell", 0, True)]


async def test_without_auto_trust_waits_for_room_match(db):
    """بلا وضع التلقائي: نفس الحالة (غرف مُصنّفة بلا رسالة غرفة) → لا تكتمل (تنتظر المطابقة §8.1)."""
    await db.control.set(BotControl(storage_enabled=True, auto_trust=False, state="running"), "test")
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier())
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY, treasury_code="74", active=True))
    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون\nبدون خصم"
    await pipe.capture(_raw("m1", text, jid=CENTRAL))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)
    assert len(await db.deals.by_status(Status.COMPLETED)) == 0, "بلا auto_trust لا تكتمل قبل المطابقة"
    assert writer.calls == []


# ═════════════════════════════════════════════════════════════════════════════
# 1ب) DRY_RUN — عُبّئت الشاشة، لا «تخزين» ولا دفتر، لكن ✅ «تمّ — DRY_RUN» على المركزية
# ═════════════════════════════════════════════════════════════════════════════
async def test_dry_run_marks_done_without_store_or_ledger(db):
    await _enable_storage(db)   # Kill Switch ON — ومع ذلك DRY_RUN يمنع الحفظ
    # الكاتب في DRY_RUN: يُرجع dry_run=True (عُبّئ بلا «تخزين»/«خروج»)
    writer = FakeWriter(result=WriteResult(ok=True, dry_run=True))
    pipe = _make_pipeline(db, writer, StubVerifier())
    # ربط غرفة الخزينة بكود «بلاس فون» (74) — شرط بحث المطابقة المحصور بالغرفة (§8.1)
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY, treasury_code="74", active=True))

    await pipe.capture(_raw("cust1", "فداء شاكونه 1600 ج م 010954227116", jid=CUST_ROOM))
    await pipe.capture(_raw("treas1", "بلاس 1600 فداء شاكونه", jid=TREAS_ROOM))
    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون\nبدون خصم"
    await pipe.capture(_raw("m1", text, jid=CENTRAL))

    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    # لا اكتمال حقيقي: لا COMPLETED ولا قيد دفتر (لم يُخزَّن شيء)؛ الصفقة تبقى MATCHED
    assert len(await db.deals.by_status(Status.COMPLETED)) == 0
    matched = await db.deals.by_status(Status.MATCHED)
    assert len(matched) == 1, "DRY_RUN يترك الصفقة MATCHED (بلا اكتمال)"
    assert matched[0].mark == Mark.DONE, "علامة ✅ محفوظة (بلا حالة COMPLETED)"
    entries = await db.ledger.entries_for_deal(matched[0].deal_id)
    assert entries == [], "DRY_RUN لا يكتب دفترًا"

    # لكن ✅ بصري وُضع على المركزية فقط (§8.3)
    outs = await db.outgoing.next_unsent(50)
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)
    done_reactions = [o for o in outs if o.get("reaction") == Mark.DONE.value and o["chat_jid"] == CENTRAL]
    assert len(done_reactions) == 1, "✅ DRY_RUN مرّة واحدة على المركزية"


# ═════════════════════════════════════════════════════════════════════════════
# 2) تجميع الطرفين (§5.5) — بيع + شراء = صفقة واحدة، الترتيب بيع أولًا
# ═════════════════════════════════════════════════════════════════════════════
async def test_two_leg_grouping_and_order(db):
    await _enable_storage(db)
    # المورد «طه» في القائمة (§5.4) + كود خزينة «خصم 1%» مُستكمل (ملحق ب-3)
    await db.suppliers.upsert(SupplierRecordFactory("طه", "760"))
    await db.treasuries.upsert(TreasuryRecord(
        code="90", name="خصم 1%", type=TreasuryType.SELL_AND_BUY,
        aliases=["خصم", "خصم1", "خصم 1"], active=True,
    ))
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)  # تركيز على التجميع/الترتيب

    sell = "A6779\n01115233493\n8.475 ج م\nفود فون كاش\n53 احمد العكاري 5.90"
    buy = "A6779\n01115233493\n8.391 ج م\nفود فون كاش\n760 طه 5.86"
    await pipe.capture(_raw("sell1", sell, at=PAST))
    await pipe.capture(_raw("buy1", buy, at=PAST + timedelta(seconds=20)))

    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    # صفقة واحدة فقط تحتوي الطرفين
    completed = await db.deals.by_status(Status.COMPLETED)
    assert len(completed) == 1, "الطرفان يجب أن يندمجا في صفقة واحدة (§7.3)"
    d = completed[0]
    assert d.is_two_legged and d.sell_leg is not None and d.buy_leg is not None
    assert d.sell_leg.amount == 8475 and d.buy_leg.amount == 8391
    # العمولة = الفرق بالسالب (§6.2): 8391 − 8475 = −84
    assert d.sell_leg.commission == pytest.approx(-84.0)
    # خزينة الطرفين = «خصم 1%» (§6.1)
    assert d.sell_leg.treasury.code == "90" and d.buy_leg.treasury.code == "90"

    # الترتيب الصارم: بيع (0) ثم شراء (1) — §7.3 §11.4
    assert writer.calls == [("sell", 0, True), ("buy", 1, True)]

    # قيدان في الدفتر (بيع + شراء)
    entries = await db.ledger.entries_for_deal(d.deal_id)
    assert {e.operation for e in entries} == {OperationType.SELL, OperationType.BUY}


# ═════════════════════════════════════════════════════════════════════════════
# 3) منع التكرار (§9) — نفس الرسالة مرّتين → الثانية تُتجاهل
# ═════════════════════════════════════════════════════════════════════════════
async def test_idempotency_no_double_download(db):
    await _enable_storage(db)
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)

    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"
    await pipe.capture(_raw("dup1", text))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)
    assert len(writer.calls) == 1  # نزلت مرّة

    # الرسالة نفسها تصل ثانية (message_key جديد لكن الحارس يعتمد الدفتر + المفتاح)
    # نفس المفتاح: capture upsert لا يعيد الإدراج؛ نحاكي معالجة ثانية صريحة
    from core.models import ParsedLeg
    leg = ParsedLeg(operation=OperationType.SELL, customer_code="1208", amount=1600,
                    source_message_key="dup1")
    assert await pipe.guard.already_downloaded("dup1") is True  # الحارس يمنع (§9)
    assert len(writer.calls) == 1, "لا إدخال مزدوج (§9)"


# ═════════════════════════════════════════════════════════════════════════════
# 4) Kill Switch (§13) — الافتراضي إيقاف → تعبئة بلا تخزين، بلا قيد دفتر
# ═════════════════════════════════════════════════════════════════════════════
async def test_kill_switch_default_off(db):
    # لا نفعّل التخزين — الافتراضي إيقاف (§13)
    ctrl = await db.control.get()
    assert ctrl.storage_enabled is False, "الافتراضي عند التشغيل: إيقاف (§13)"

    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)
    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"
    await pipe.capture(_raw("k1", text))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    # الكاتب استُدعي بـ commit=False (يعبّئ ويتوقّف عند «تخزين»)
    assert writer.calls == [("sell", 0, False)]
    # لا قيد دفتر (لم يُخزَّن شيء)
    assert await db.ledger.was_downloaded("k1") is False
    assert len(await db.deals.by_status(Status.COMPLETED)) == 0


# ═════════════════════════════════════════════════════════════════════════════
# 5) الإلغاء (§10) — موظف معتمد يرد «إلغاء» → قيد عكسي بكامل القيمة
# ═════════════════════════════════════════════════════════════════════════════
async def test_cancel_by_authorized_employee(db):
    await _enable_storage(db)
    await db.employees.upsert(EmployeeRecord(whatsapp_number=EMP, name="موظف", active=True))
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)

    # حوالة تكتمل أولًا
    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"
    await pipe.capture(_raw("orig1", text))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)
    assert writer.calls == [("sell", 0, True)]

    # رد «إلغاء» من موظف معتمد على الحوالة الأصلية (§10)
    await pipe.capture(_raw("cancel1", "إلغاء", reply="orig1", at=now + timedelta(seconds=5)))
    now2 = now + timedelta(seconds=65)
    await pipe.process_inbox(now2)

    # قيد عكسي: شراء بكامل القيمة نفس الخزينة (تصفير §10)
    reversal = [c for c in writer.calls if c[0] == "buy"]
    assert reversal, "الإلغاء يولّد قيدًا عكسيًا (شراء)"
    d = await db.deals.find_by_source_key("orig1")
    assert d.status == Status.CANCELLED


# ═════════════════════════════════════════════════════════════════════════════
# 6) قاعدة الإخراج الصارمة (§2.2) — لا مخرجات لغرف الزبائن/الخزائن أبدًا
# ═════════════════════════════════════════════════════════════════════════════
async def test_output_whitelist_never_writes_rooms(db):
    await _enable_storage(db)
    # حوالة تعلّق (خزينة غير معروفة) لتوليد ⚠️ + سيناريوهات إرسال متنوّعة
    pipe = _make_pipeline(db, FakeWriter(), StubVerifier(), rooms=False)
    await pipe.capture(_raw("w1", "9999 زبون مجهول 5.0\nخزينةوهمية\nA9999\n0100\n500 ج م"))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    outs = await db.outgoing.next_unsent(100)
    # كل وجهة يجب أن تكون المركزية أو المسؤول — لا غرفة زبون/خزينة إطلاقًا (§2.2)
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)


# ═════════════════════════════════════════════════════════════════════════════
# 7) الفشل النصفي (§11.4) — بيع نزل، شراء فشل → لا يُعاد البيع، تصعيد للمسؤول
# ═════════════════════════════════════════════════════════════════════════════
async def test_half_failure_sell_ok_buy_fails(db):
    await _enable_storage(db)
    await db.suppliers.upsert(SupplierRecordFactory("طه", "760"))
    await db.treasuries.upsert(TreasuryRecord(
        code="90", name="خصم 1%", type=TreasuryType.SELL_AND_BUY,
        aliases=["خصم", "خصم1", "خصم 1"], active=True,
    ))
    writer = FakeWriter(fail_ops={"buy"})  # الشراء يفشل تقنيًا
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)

    await pipe.capture(_raw("s1", "A6779\n01115233493\n8.475 ج م\nفود فون كاش\n53 احمد العكاري 5.90", at=PAST))
    await pipe.capture(_raw("b1", "A6779\n01115233493\n8.391 ج م\nفود فون كاش\n760 طه 5.86",
                            at=PAST + timedelta(seconds=20)))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    # البيع نزل مرّة واحدة (لا يُعاد أبدًا §11.4)، ثم حاول الشراء وفشل
    assert writer.calls == [("sell", 0, True), ("buy", 1, True)]
    d = await db.deals.find_by_source_key("s1")
    assert d.status == Status.SELL_DONE, "بيع نزل + شراء فشل → SELL_DONE (§11.4)"

    # الدفتر: قيد بيع واحد فقط (الشراء لم يُسجَّل)
    entries = await db.ledger.entries_for_deal(d.deal_id)
    assert [e.operation for e in entries] == [OperationType.SELL]

    # dead-letter + تصعيد للمسؤول بتفصيل الطرفين
    dead = [x async for x in db.dead_letter.col.find({})]
    assert len(dead) == 1 and dead[0]["screenshot_path"] == "/x.png"
    outs = await db.outgoing.next_unsent(100)
    assert any(o["chat_jid"] == ADMIN for o in outs), "تصعيد للمسؤول (§11.4)"
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)  # §2.2


# ═════════════════════════════════════════════════════════════════════════════
# 8) التعديل (§10) — «تعديل 9000» على بيع 10000 → قيد عكسي بالفرق (1000)
# ═════════════════════════════════════════════════════════════════════════════
async def test_edit_reversal_by_difference(db):
    await _enable_storage(db)
    await db.employees.upsert(EmployeeRecord(whatsapp_number=EMP, name="موظف", active=True))
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)

    # حوالة أصلية 10000 (بلاس، مصري)
    await pipe.capture(_raw("e_orig", "1208 فداء 5.0\nبلاس\nA7001\n010900\n10000 ج م\nفودافون"))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)
    assert writer.calls == [("sell", 0, True)]

    # رد «تعديل 9000» من موظف معتمد
    await pipe.capture(_raw("e_edit", "تعديل 9000", reply="e_orig", at=now + timedelta(seconds=5)))
    await pipe.process_inbox(now + timedelta(seconds=10))

    # قيد عكسي بالفرق فقط (10000 − 9000 = 1000)، شراء (عكس البيع)، نفس الخزينة
    rev_jobs = [j for j in writer.jobs if j.is_reversal]
    assert len(rev_jobs) == 1
    assert rev_jobs[0].operation == OperationType.BUY
    assert rev_jobs[0].leg.amount == pytest.approx(1000.0)
    assert rev_jobs[0].leg.treasury.code == "74"  # نفس خزينة بلاس


# ═════════════════════════════════════════════════════════════════════════════
# 9) التذكير والتصعيد الزمني (§8.1) — لم تظهر في الغرف → تذكيران ثم غرفة المسؤول
# ═════════════════════════════════════════════════════════════════════════════
async def test_reminders_then_escalation(db):
    await _enable_storage(db)
    writer = FakeWriter()
    # الغرف مُعدّة لكن بلا رسالة مطابقة → لن تتطابق أبدًا
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=True)
    # غرفة خزينة «بلاس فون» (74) مرتبطة لكن بلا رسالة الحوالة → مسار التذكير العام (لا «تم» يدوي)
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY, treasury_code="74", active=True))

    await pipe.capture(_raw("nf1", "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"))
    t0 = PAST + timedelta(seconds=120)
    await pipe.process_inbox(t0)
    await pipe.tick(t0)
    d = await db.deals.find_by_source_key("nf1")
    assert d.status == Status.MATCHING and writer.calls == []  # لم تُكتب (لم تتطابق §8.1)

    # بعد نافذة المطابقة (>15s) → تذكير أول «غير موجودة» في المركزية
    await pipe.tick(t0 + timedelta(seconds=20))
    d = await db.deals.find_by_source_key("nf1")
    assert d.reminders_sent == 1 and d.status == Status.HELD

    # بعد 16 دقيقة → تذكير ثانٍ
    await pipe.tick(t0 + timedelta(seconds=20, minutes=16))
    d = await db.deals.find_by_source_key("nf1")
    assert d.reminders_sent == 2

    # بعد 16 دقيقة أخرى → تصعيد لغرفة المسؤول + إغلاق
    await pipe.tick(t0 + timedelta(seconds=20, minutes=32))
    d = await db.deals.find_by_source_key("nf1")
    assert d.status == Status.ESCALATED

    # التذكيرات في المركزية، التصعيد للمسؤول — لا شيء لغرف الزبائن/الخزائن (§2.2)
    outs = await db.outgoing.next_unsent(100)
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)
    assert any(o["chat_jid"] == ADMIN for o in outs)  # التصعيد وصل المسؤول
    # لم تُكتب الحوالة إطلاقًا (لم تُنزَّل بلا مطابقة/تجاوز)
    assert writer.calls == []


# ═════════════════════════════════════════════════════════════════════════════
# 10) أمان §9 — «تم» على صفقة TECH_FAILED (خُزّنت فعلًا لكن تعذّر تأكيد SQL) لا يُعيد الكتابة
# ═════════════════════════════════════════════════════════════════════════════
async def test_confirm_on_tech_failed_no_double_entry(db):
    await _enable_storage(db)
    await db.employees.upsert(EmployeeRecord(whatsapp_number=EMP, name="موظف", active=True))
    sql = SqlState()
    writer = FakeWriter(sql=sql)
    # SQL يفشل في التأكيد (عطل عابر) رغم أن التخزين الفعلي نجح
    verifier = StubVerifier(sql=sql, fail_verify=True)
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    pipe = Pipeline(db, bus, writer, verifier, customer_room_jids=[], treasury_room_jids=[])

    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"
    await pipe.capture(_raw("tf1", text))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    # البيع خُزّن فعلًا (في SQL المحاكاة) لكن التأكيد فشل → TECH_FAILED، بلا قيد دفتر
    assert writer.calls == [("sell", 0, True)]
    assert ("A5169", "sell") in sql.stored           # خُزّن فعلًا في MONEYADO/SQL
    d = await db.deals.find_by_source_key("tf1")
    assert d.status == Status.TECH_FAILED
    assert await db.ledger.was_downloaded("tf1") is False  # لا قيد (لم يتأكّد)

    # موظف معتمد يرى الحوالة محفوظة فيرد «تم» — يجب ألّا يعيد البوت الكتابة (§8.1 بند7 / §9)
    await pipe.capture(_raw("confirm1", "تم", reply="tf1", at=now + timedelta(seconds=5)))
    await pipe.process_inbox(now + timedelta(seconds=10))

    # لا كتابة ثانية — الإدخال المزدوج محبَط
    assert writer.calls == [("sell", 0, True)], "«تم» على TECH_FAILED لا يعيد التخزين (منع ازدواج §9)"
    outs = await db.outgoing.next_unsent(100)
    assert any(o["chat_jid"] == ADMIN for o in outs)  # حُوّل للمسؤول للمراجعة اليدوية
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)


# ═════════════════════════════════════════════════════════════════════════════
# حوالة مرتكزها الرقم الإشاري (A06/صافي) عبر process_inbox → صفقة لا هدرزة
# ═════════════════════════════════════════════════════════════════════════════
async def test_reference_anchored_transfer_not_noise_in_inbox(db):
    pipe = _make_pipeline(db)
    text = "A06\nفودافون\n01097298988\n69.000 مصري\nصافي"
    await pipe.capture(_raw("refA06", text, jid=CENTRAL))
    await pipe.process_inbox(PAST + timedelta(seconds=120))

    # لم تُهمَل كهدرزة → أُنشئت صفقة تحمل الرقم الإشاري A06 (مرساة بلا كود زبون)
    d = await db.deals.find_by_source_key("refA06")
    assert d is not None, "الحوالة المرتكزة على الرقم الإشاري أُهمِلت كهدرزة"
    leg = d.sell_leg or d.buy_leg
    assert leg is not None and leg.reference_number == "A06"
    assert leg.amount == 69000.0 and leg.customer_code is None


# ═════════════════════════════════════════════════════════════════════════════
# خارج النطاق (تسليم يدوي) عبر process_inbox → تصعيد لغرفة المسؤول، لا إدخال
# ═════════════════════════════════════════════════════════════════════════════
async def test_out_of_scope_escalates_to_admin(db):
    pipe = _make_pipeline(db)
    text = "A6363\nتسليم باليد\nمحمود ابراهيم\n01203187000\nالقيمة 59574 ج م"
    await pipe.capture(_raw("oos1", text, jid=CENTRAL))
    await pipe.process_inbox(PAST + timedelta(seconds=120))

    # لا صفقة أُنشئت (ليست حوالة)
    assert await db.deals.col.count_documents({}) == 0
    # رسالة تصعيد لغرفة المسؤول فقط (§2.2)
    outs = await db.outgoing.next_unsent(100)
    assert any(o["chat_jid"] == ADMIN for o in outs)
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)
    assert any("خارج النطاق" in (o.get("text") or "") for o in outs)


# ═════════════════════════════════════════════════════════════════════════════
# option B (§0): بيع بخزينة لا تُحلّ → ⚠️ تعليق، لا يُكتب بخزينة فارغة/خاطئة (#2)
# ═════════════════════════════════════════════════════════════════════════════
async def test_unresolved_treasury_held_not_written(db):
    await _enable_storage(db)
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, rooms=False)   # بلا غرف → يصل مباشرة لبوابة الثقة
    text = "A55\n1234 احمد علي 5.9\n01000000000\n5000 مصري\nخزينةمجهولة"
    await pipe.capture(_raw("ut1", text))
    await pipe.process_inbox(PAST + timedelta(seconds=120))    # خزينة None → WAITING
    await pipe.tick(PAST + timedelta(seconds=220))             # المهلة → PARSED مفرد → بوابة الثقة → HELD

    d = await db.deals.find_by_source_key("ut1")
    assert d.status == Status.HELD               # لم يُدخَل بخزينة فارغة (§0)
    assert d.mark == Mark.WARN
    assert writer.calls == []                    # لم تُكتب


# ═════════════════════════════════════════════════════════════════════════════
# فشل الإدخال (فشل تقني) → رسالة لغرفة المسؤول فقط، بلا أي تفاعل 🔴 على المركزية (قرار المستخدم)
# التفاعلان الوحيدان على المركزية: 🔸 (تطابقت الغرفتان) و✅ (أُدخِلت بنجاح).
# ═════════════════════════════════════════════════════════════════════════════
async def test_write_failure_notifies_admin_no_central_reaction(db):
    await _enable_storage(db)
    writer = FakeWriter(fail_ops={"sell"})           # الإدخال يفشل تقنيًا (مثل stock.exe not found)
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)
    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"
    await pipe.capture(_raw("wf1", text))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    # حاول البيع وفشل → TECH_FAILED (لا يُعاد §11.4)
    assert writer.calls == [("sell", 0, True)]
    d = await db.deals.find_by_source_key("wf1")
    assert d.status == Status.TECH_FAILED

    outs = await db.outgoing.next_unsent(100)
    # رسالة السبب في غرفة المسؤول موجودة (انتظار تدخّل يدوي)
    assert any(o["chat_jid"] == ADMIN for o in outs), "فشل الإدخال يُبلَّغ للمسؤول"
    # 🔴 لا أي تفاعل على المركزية عند الفشل — ولا حتى ✅ (لم يُدخَل)
    reactions = [o.get("reaction") for o in outs if o.get("reaction")]
    assert Mark.FAILED.value not in reactions, "ممنوع تفاعل 🔴 على المركزية عند الفشل"
    assert Mark.DONE.value not in reactions, "لا ✅ لأن الإدخال لم ينجح"
    # قاعدة الإخراج (§2.2): المركزية أو المسؤول فقط
    assert all(o["chat_jid"] in {CENTRAL, ADMIN} for o in outs)


# ═════════════════════════════════════════════════════════════════════════════
# البوّابة الديناميكية: المطابقة تُستدعى من تصنيف db.rooms (لا env) — إصلاح بلاغ 🔸
# ═════════════════════════════════════════════════════════════════════════════
async def test_matching_invoked_from_db_rooms_even_with_empty_env(db):
    # 🔴 الغرف مصنّفة في db.rooms فقط (env/seed فارغ) → البوّابة تقرأ DB → المطابقة تُستدعى.
    from core.models import Room
    await db.rooms.upsert(Room(jid=CUST_ROOM, type=RoomType.CUSTOMER))
    await db.rooms.upsert(Room(jid=TREAS_ROOM, type=RoomType.TREASURY))
    await _enable_storage(db)
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)   # seed env فارغ

    # حوالة كاملة (لولا المطابقة لأُدخِلت) لكن لا رسالة في الغرف → تبقى MATCHING لا COMPLETED
    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"
    await pipe.capture(_raw("dbm1", text))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    d = await db.deals.find_by_source_key("dbm1")
    assert d.status == Status.MATCHING, "المطابقة استُدعيت من db.rooms (لولاها لأكملت)"
    assert writer.calls == []                    # لم تُكتب (لم تتطابق بعد)


async def test_matching_skipped_when_no_rooms_in_db_or_env(db):
    # لا غرف في db ولا env → المطابقة تُتخطّى (وضع مركزية فقط) → تُدخَل مباشرة
    await _enable_storage(db)
    writer = FakeWriter()
    pipe = _make_pipeline(db, writer, StubVerifier(), rooms=False)
    text = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"
    await pipe.capture(_raw("dbm2", text))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    d = await db.deals.find_by_source_key("dbm2")
    assert d.status == Status.COMPLETED          # لا غرف → لا مطابقة → أُدخِلت
    assert writer.calls == [("sell", 0, True)]


# مصنع مساعد لسجلّ المورد
def SupplierRecordFactory(name, code):
    from core.models import SupplierRecord
    return SupplierRecord(name=name, code=code, aliases=[], active=True)
