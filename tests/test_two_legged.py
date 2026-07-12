"""
اختبارات منطق الطرفين (§5.5 §7.3) بعد الإصلاح (Fix 3):
- أُلغيت قاعدة «sell_and_buy ⇒ طرفان» (expects_pair=False).
- «صافي» في الرسالة الأولى = ملاحظة لا خزينة (§7.3، بلاغ A11–A16): فالرسالة الأولى دائمًا
  خزينتها None وتنتظر الرسالة الثانية (الخزينة+الكود). لا خزينة تُحلّ من الرسالة الأولى أبدًا.
- الطرفان = وصول رسالة ثانية بمورد بنفس الـref/الهاتف → دمج، الخصم = الفرق سالبًا.
- انتهاء المهلة بلا شراء → إدخال كطرف واحد (لا تصعيد).
المصدر: pr.md المصدر ٢ + الصورة ١٠ (A7239).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.constants import OperationType, Status
from core.models import SupplierRecord
from core.parsing import parse_message
from core.queue.commission import compute_commission
from core.queue.service import QueueService

NOW = datetime(2026, 7, 6, 12, 0, 0, tzinfo=timezone.utc)


async def _treas(db):
    return await db.treasuries.all_active()


async def _suppliers(db):
    await db.suppliers.upsert(SupplierRecord(code="760", name="طه", aliases=["طه"]))
    return await db.suppliers.all_active()


# ── «صافي» مؤشّر خصم (ملاحظة) لا خزينة، ولا يجعل الحوالة طرفين ─────────────────
async def test_saafi_single_sell_not_pair_in_parser(db):
    text = "A08\nفودافون\n01097298988\n69000 مصري\nصافي"
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    # 🔴 «صافي» مؤشّر خصم → ملاحظة لا خزينة (بلاغ A11–A16)
    assert res.leg.treasury is None and "صافي" in (res.leg.notes or "")
    assert res.leg.operation == OperationType.SELL
    assert res.leg.expects_pair is False          # لا ينتظر بسبب نوع الخزينة


async def test_saafi_first_message_waits_for_second_message(db):
    # 🔴 بلاغ إنتاج A11–A16: بيع «صافي» رسالةً أولى — «صافي» ملاحظة لا خزينة، فالخزينة None
    # والهوية غائبة → ينتظر الرسالة الثانية (كان يُحلّ «صافي» كخزينة ويُعلَّق «كود ناقص»).
    svc = QueueService(db)
    res = parse_message("A08\nفودافون\n01097298988\n69000 مصري\nصافي", await _treas(db), [])
    leg = res.leg
    assert leg.treasury is None and "صافي" in (leg.notes or "")   # صافي ملاحظة لا خزينة
    leg.source_message_key = "m-saafi"
    deal = await svc.try_group(leg, NOW)
    assert deal.status == Status.WAITING_SECOND_LEG
    assert deal.is_two_legged is False


# ── الطرفان: بيع ثم شراء بمورد بنفس الـref → دمج + خصم سالب ──────────────────
async def test_two_legged_a6779_merge_and_discount(db):
    svc = QueueService(db)
    treas, sup = await _treas(db), await _suppliers(db)

    sell = parse_message("A6779\n01115233493\n8.475 ج م\n53 احمد العكاري 5.90", treas, sup).leg
    sell.source_message_key = "sell6779"
    buy = parse_message("A6779\n01115233493\n8.391 ج م\n760 طه 5.86", treas, sup).leg
    buy.source_message_key = "buy6779"

    d1 = await svc.try_group(sell, NOW)
    assert d1.status == Status.WAITING_SECOND_LEG    # البيع ينتظر (خزينته ضمنية = None)

    d2 = await svc.try_group(buy, NOW + timedelta(seconds=30))
    assert d2.deal_id == d1.deal_id                  # نفس الصفقة (نفس ref+هاتف)
    assert d2.is_two_legged is True
    assert d2.sell_leg is not None and d2.buy_leg is not None
    assert d2.sell_leg.operation == OperationType.SELL   # الترتيب: بيع أولًا
    assert d2.buy_leg.operation == OperationType.BUY
    # الخصم = مبلغ الشراء − مبلغ البيع = 8391 − 8475 = −84 (سالب في العمولة §6.2)
    assert compute_commission(d2.sell_leg, d2.buy_leg) == -84.0


async def test_two_legged_a7239_discount_minus_1000(db):
    # الصورة ١٠ (الأهم): بيع 100000 + شراء 99000 → الخصم −1000
    sell = parse_message("A7239\n01064074568\n100000 مصري\n562 بوجناح 5.84",
                         await _treas(db), await _suppliers(db)).leg
    buy = parse_message("A7239\n01064074568\n99.000 مصري\n760 طه 5.80",
                        await _treas(db), await _suppliers(db)).leg
    assert sell.amount == 100000 and buy.amount == 99000
    assert compute_commission(sell, buy) == -1000.0


# ── السيناريو التونسي الحقيقي: رسالتان، الثانية بلا رقم إشاري (بلاغ A7804) ────
# رسالة١ تحمل كل بيانات الترويسة (ref/مدينة/اسم/هاتف/قيمة) لكن بلا هوية زبون → تنتظر.
# رسالة٢ «570 ايهاب … / وليد العاصمة» بلا ref/هاتف → رد مكمّل يُربَط بالقرب الزمني/الغرفة.
MSG1_TND = ("A7804\nتونس/العاصمة\nالاسم: محمد عبدالرحيم\n"
            "الهاتف: 0925135252\nالقيمة: 1000 د.ت")
MSG2_TND = "570 ايهاب ابو حميد 35\nوليد العاصمة"


async def test_tnd_first_message_recipient_not_incomplete(db):
    """رسالة١ تونسية: اسم المستلم المنفرد (بلا كود) → recipient_name، فليست ناقصة الهوية
    (is_incomplete=False) ولا تنبيه «أكمل البيانات» المبكر — تنتظر الرسالة الثانية طبيعيًّا."""
    from core.constants import Currency
    from core.queue.service import is_incomplete_first_message
    leg = parse_message(MSG1_TND, await _treas(db), []).leg
    assert leg.reference_number == "A7804"
    assert leg.phone == "0925135252"            # «الهاتف:» رغم البادئة
    assert leg.amount == 1000                   # «القيمة: 1000 د.ت»
    assert leg.currency == Currency.TND
    assert leg.recipient_name == "محمد عبدالرحيم"   # (ج) سطر «الاسم:» يُلتقط اسمَ مستلم
    assert leg.country == "العاصمة"
    assert leg.customer_code is None            # لا كود زبون بعد → تنتظر الرسالة الثانية
    # 🔴 recipient_name حاضر → ليست ناقصة الهوية (تغيّر مقصود: الصيغة التونسية باسم مستلم)
    assert is_incomplete_first_message(leg) is False


async def test_tnd_two_messages_link_and_complete(db):
    """رسالة١ (تنتظر) + رسالة٢ (وليد العاصمة) → تُربطان وتكتمل الصفقة صح."""
    from core.constants import Currency
    from core.queue.service import is_completion_fragment
    svc = QueueService(db)
    treas = await _treas(db)
    JID = "tnd@g.us"

    leg1 = parse_message(MSG1_TND, treas, []).leg
    leg1.source_message_key = "m1"
    deal1 = await svc.try_group(leg1, NOW, chat_jid=JID)
    assert deal1.status == Status.WAITING_SECOND_LEG

    res2 = parse_message(MSG2_TND, treas, [])
    assert res2.kind == "noise"                 # بلا مبلغ → ليست حوالة مستقلّة
    leg2 = res2.leg
    # (أ/ب) «وليد العاصمة» تُحلّ خزينة code 51 عبر تجريد لاحقة المدينة
    assert leg2.treasury is not None and leg2.treasury.code == "51"
    assert is_completion_fragment(leg2) is True

    deal2 = await svc.absorb_fragment(leg2, JID, "m2", NOW + timedelta(seconds=30))
    assert deal2 is not None
    assert deal2.deal_id == deal1.deal_id       # رُبطت بنفس الصفقة المعلّقة
    assert deal2.status == Status.PARSED         # اكتملت → ستُعالَج
    assert deal2.sell_leg.treasury.code == "51"
    assert deal2.sell_leg.customer_code == "570"
    assert deal2.sell_leg.customer_name == "ايهاب ابو حميد"
    assert deal2.sell_leg.amount == 1000         # المبلغ من الرسالة الأولى
    assert deal2.sell_leg.currency == Currency.TND


# ── الصيغة التونسية الجديدة: اسم مستلم منفرد بلا كود (A8990) ─────────────────
# رسالة١: «A8990 / هاتف / اسم مستلم / مدينة / مبلغ د.ت» — اسم منفرد = recipient_name، بلا كود.
# رسالة٢: «1208 فداء شاكونه 35.5 / محمود» — كود + سعر + خزينة (نفس منطق الرسالة الثانية).
MSG1_A8990 = "A8990\n0918300701\nفايزه\nصفاقس\n351 د.ت"
MSG1_A8990_WA = "A8990\n0918300701 واتساب\nفايزه\nصفاقس\n351 د.ت"  # «واتساب» على سطر الهاتف
MSG2_A8990 = "1208 فداء شاكونه 35.5 / محمود"


async def test_a8990_first_message_recipient_not_incomplete(db):
    """A8990: المبلغ/العملة/الهاتف/اسم المستلم تُلتقط، وليست ناقصة (recipient حاضر) → لا تنبيه مبكر."""
    from core.constants import Currency
    from core.queue.service import is_incomplete_first_message
    leg = parse_message(MSG1_A8990, await _treas(db), []).leg
    assert leg.reference_number == "A8990"
    assert leg.phone == "0918300701"
    assert leg.amount == 351.0 and leg.currency == Currency.TND
    assert leg.recipient_name == "فايزه"          # اسم المستلم المنفرد (بلا كود)
    assert leg.customer_code is None              # لا كود زبون → تنتظر الثانية
    assert is_incomplete_first_message(leg) is False


async def test_a8990_whatsapp_on_phone_line_still_parses(db):
    """«0918300701 واتساب»: الهاتف يُلتقط و«واتساب» تُتجاهَل بلا إرباك — وليست ناقصة."""
    from core.constants import Currency
    from core.queue.service import is_incomplete_first_message
    leg = parse_message(MSG1_A8990_WA, await _treas(db), []).leg
    assert leg.phone == "0918300701"
    assert leg.recipient_name == "فايزه"
    assert leg.amount == 351.0 and leg.currency == Currency.TND
    assert is_incomplete_first_message(leg) is False


async def test_a8990_waits_second_then_completes(db):
    """A8990 (تنتظر) + «1208 فداء شاكونه 35.5 / محمود» → تُربطان وتكتمل (كود+خزينة من الثانية)."""
    from core.constants import Currency
    from core.queue.service import is_completion_fragment
    svc = QueueService(db)
    treas = await _treas(db)
    JID = "tnd@g.us"

    leg1 = parse_message(MSG1_A8990, treas, []).leg
    leg1.source_message_key = "a1"
    deal1 = await svc.try_group(leg1, NOW, chat_jid=JID)
    assert deal1.status == Status.WAITING_SECOND_LEG          # تنتظر الرسالة الثانية طبيعيًّا

    leg2 = parse_message(MSG2_A8990, treas, []).leg
    assert is_completion_fragment(leg2) is True               # كود+سعر+خزينة بلا مبلغ → مكمّلة
    deal2 = await svc.absorb_fragment(leg2, JID, "a2", NOW + timedelta(seconds=30))
    assert deal2 is not None and deal2.deal_id == deal1.deal_id
    assert deal2.status == Status.PARSED                      # اكتملت
    assert deal2.sell_leg.customer_code == "1208"             # الكود من الثانية
    assert deal2.sell_leg.treasury is not None and "محمود" in deal2.sell_leg.treasury.name
    assert deal2.sell_leg.amount == 351.0 and deal2.sell_leg.currency == Currency.TND  # المبلغ من الأولى


# ── انتهاء المهلة بلا شراء → طرف واحد (لا تصعيد) ─────────────────────────────
async def test_timeout_finalizes_single_leg_not_escalate(db):
    svc = QueueService(db)
    sell = parse_message("A6779\n01115233493\n8.475 ج م\n53 احمد العكاري 5.90",
                         await _treas(db), await _suppliers(db)).leg
    sell.source_message_key = "solo6779"
    await svc.try_group(sell, NOW)
    finalized = await svc.sweep_waiting(NOW + timedelta(seconds=91))
    assert len(finalized) == 1
    assert finalized[0].status == Status.PARSED       # طرف واحد، لا ESCALATED
    assert finalized[0].is_two_legged is False
