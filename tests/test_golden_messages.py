"""
اختبار انحدار على البيانات الذهبية (tests/fixtures/messages_golden.json — مستخرَجة من تصدير
واتساب حقيقيّ، ~8.5 ألف حوالة). يقود **الأنبوب الحقيقي** على كل حالة ويقارن الحقول بالمتوقّع.

تحذير الملف نفسه: حقول `expected` مستخرَجة آلياً وبعضها خاطئ. لذا نُطبّق **قائمة استثناءات
موثّقة** (KNOWN_DIVERGENCES) لكل فرق ثبت — بالفرز اليدوي — أنه خطأ في البيانات أو أثر تصدير،
لا خطأ محلّل. ما تبقّى (الحقول غير المستثناة) يجب أن يطابق حتماً؛ أيّ انحدار مستقبليّ يكسر الاختبار.

الفئات (انظر السبب أمام كل مجموعة):
  EXPORT_SEQ   — كود تسلسليّ من التصدير (A1–A9) لا رقم إشاريّ حقيقيّ → الحالة كلها غير موثوقة.
  CHANNEL_DATA — «channel» في الملف = اسم خزينة/«صافي»/«كاش»؛ المحلّل يعطي القناة الصحيحة (فودافون/None).
  AMOUNT_DATA  — «amount/currency» في الملف None/مبتور خطأً؛ المحلّل يستخرج القيمة الصحيحة.
  BURAQ        — known_bad (قرار المالك ٢٠٢٦): «البراق» مورِّد صحيح في المحلّل (SEED_SUPPLIERS)
                 والملف غلط إذ يسمّيها خزينة → المحلّل يشتقّ خزينة «فودافون بالخصم». لا تعديل للمحلّل.
  FIFO_EDGE    — known_bad (قرار المالك ٢٠٢٦): رسالة ثانية بصيغة «كود اسم سعر» + سطر خزينة مستقلّ
                 لم تُدمَج (حافة نادرة؛ shape_04 يعمل ٥/٦). يُنتظَر مثال إنتاج حقيقيّ قبل الإصلاح.
  NET_EDIT     — «amount_net=None» في الملف بينما رسالة٢ صافٍ/تعديل تحمل صافيًا فعليًّا.
  TRAILING     — رسالة مفردة بذيل شبيه بالمورّد التقطه المحلّل؛ الملف تركه None (سلوك مبرَّر).
  PREFIX_EDIT  — رسالة٢ نسخة معدّلة («تم تعديل») بادئتها حرف عربيّ زائد — أثر تصدير في الدمج.
  BARE_AMOUNT  — الملف يترك الأرقام المجرّدة بلا عملة None سياسةً؛ المحلّل يستخرج الهاتف/المبلغ.
"""
from __future__ import annotations

import re

import pytest

from core import db as dbmod
from core.constants import SEED_SUPPLIERS, SEED_TREASURIES
from tests.golden_support import (
    adapt, build_pipeline, compare, ingest_case, iter_cases, seed_golden_treasuries,
)

_SINGLE_DIGIT_CODE = re.compile(r"^[A-Za-z]\d$")

# ── قائمة الاستثناءات الموثّقة: case_id → الحقول التي قيمتها في الملف خطأ/أثر (تُتجاوَز) ──────
# (الحالات ذات الكود أحادي الرقم تُستثنى ديناميكياً بالكامل — ليست هنا.)
KNOWN_DIVERGENCES: dict[str, set[str]] = {
    # CHANNEL_DATA
    "shape_02_msg2_rate_treasury#5": {"channel"},          # الملف: «بلاس» (خزينة) بدل القناة
    "shape_05_msg2_treasury_only#1": {"channel"},          # القناة داخل «ارجو تحويل …» (ضجيج طلب)
    "shape_05_msg2_treasury_only#5": {"channel"},          # الملف: «صافي» (مؤشّر خصم) لا قناة
    "arabic_comma_rate#3": {"channel"},                    # الملف: None؛ «فدفون» قناة صحيحة
    "arabic_indic_digits#0": {"channel"},                  # الملف: «بلاس» (خزينة)
    "misspelled_channel#1": {"channel"},                   # الملف: «بلاس» (خزينة)
    "misspelled_channel#2": {"channel"},                   # الملف: «بلاس» (خزينة)
    "amount_midsentence#0": {"channel"},                   # الملف: «بلاس» (خزينة)
    # AMOUNT_DATA (+ CHANNEL_DATA حيث ذُكر)
    "comma_thousands#0": {"amount_gross"},                 # «3,000»→3000 صحيح؛ الملف None
    "comma_thousands#1": {"amount_net", "currency"},       # «ج٠م»→EGP وصافٍ حقيقيّ؛ الملف None
    "misspelled_channel#3": {"amount_gross", "currency", "channel"},   # «20000ج»→EGP؛ الملف None/خزينة
    "amount_midsentence#2": {"amount_gross"},              # «6،070»→6070 (فاصلة آلاف)؛ الملف 70
    "rate_stuck_to_name#0": {"amount_net", "currency"},    # «ج٠م»→EGP وصافٍ؛ الملف None
    # BURAQ (known_bad: «البراق» مورّد صحيح؛ الملف يسمّيها خزينة خطأً — قرار المالك)
    "shape_05_msg2_treasury_only#0": {"treasury"},
    "shape_06_msg2_two_rates#1": {"treasury"},
    "shape_06_msg2_two_rates#2": {"treasury"},
    "shape_06_msg2_two_rates#3": {"amount_gross", "treasury"},   # amount: «49,915»→49915 صحيح
    "comma_thousands#2": {"amount_gross", "treasury"},
    "arabic_comma_rate#2": {"treasury"},
    # FIFO_EDGE (known_bad: رسالة ثانية لم تُدمَج — حافة نادرة؛ ننتظر مثال إنتاج قبل الإصلاح)
    "shape_04_msg2_rate_only#2": {"supplier_code", "supplier_name", "rate"},
    "amount_midsentence#1": {"supplier_code", "supplier_name", "rate"},
    "shape_06_msg2_two_rates#4": {"supplier_code", "supplier_name", "rate", "treasury"},
    "shape_06_msg2_two_rates#5": {"supplier_code", "supplier_name", "rate", "treasury"},
    "arabic_indic_digits#3": {"supplier_code", "supplier_name", "rate", "treasury"},
    "amount_midsentence#3": {"channel", "treasury"},       # channel/treasury=البراق داخل جملة طلب
    # NET_EDIT
    "arabic_comma_rate#1": {"amount_net"},
    "arabic_indic_digits#1": {"amount_net"},
    # TRAILING (رسالة مفردة بذيل مورّد التقطه المحلّل)
    "shape_01_single_all_fields#2": {"amount_gross", "supplier_code", "supplier_name", "rate"},
    "shape_01_single_all_fields#5": {"supplier_code", "supplier_name"},
    # PREFIX_EDIT (رسالة٢ = نسخة معدّلة ببادئة عربية — أثر تصدير في الدمج)
    "prefixed_code#0": {"amount_gross", "currency", "supplier_code", "supplier_name", "rate", "treasury"},
    "prefixed_code#1": {"amount_gross", "currency", "supplier_code", "supplier_name", "rate", "treasury"},
    "prefixed_code#2": {"channel", "supplier_code", "supplier_name", "rate", "treasury"},
    "prefixed_code#3": {"amount_gross", "amount_net", "supplier_code", "supplier_name", "rate", "treasury"},
    # BARE_AMOUNT (الملف يترك المجرّد بلا عملة None سياسةً)
    "bare_amount_no_currency#0": {"recipient", "amount_gross", "currency", "treasury"},
    "bare_amount_no_currency#1": {"amount_gross", "currency", "supplier_code", "supplier_name", "rate", "treasury"},
    "bare_amount_no_currency#2": {"recipient", "amount_gross", "currency", "treasury"},
    "bare_amount_no_currency#3": {"amount_gross", "currency", "supplier_code", "supplier_name", "rate", "treasury"},
}

_CASES = list(iter_cases())


async def _golden_db():
    """قاعدة وهمية مبذورة بالخزائن (SEED + خزائن الملف) والموردين — كقاعدة الإنتاج."""
    from mongomock_motor import AsyncMongoMockClient

    class _Mock(dbmod.Database):
        async def connect(self):
            self._client = AsyncMongoMockClient()
            self.mdb = self._client[self._db_name]
            self.raw = dbmod.RawMessageRepo(self.mdb, "raw_messages", "message_key")
            self.rooms = dbmod.RoomRepo(self.mdb, "rooms", "jid")
            self.deals = dbmod.DealRepo(self.mdb, "deals", "deal_id")
            self.ledger = dbmod.LedgerRepo(self.mdb, "ledger", "entry_id")
            self.outbox = dbmod.OutboxRepo(self.mdb, "outbox", "job_id")
            self.outgoing = dbmod.OutgoingRepo(self.mdb, "outgoing", "_id")
            self.treasuries = dbmod.TreasuryListRepo(self.mdb, "treasuries", "name")
            self.suppliers = dbmod.SupplierListRepo(self.mdb, "suppliers", "name")
            self.payment_channels = dbmod.PaymentChannelListRepo(self.mdb, "payment_channels", "name")
            self.employees = dbmod.EmployeeListRepo(self.mdb, "employees", "whatsapp_number")
            self.control = dbmod.ControlRepo(self.mdb, "bot_control", "_key")
            self.dead_letter = dbmod.DeadLetterRepo(self.mdb, "dead_letter", "_id")
            self.pending_replies = dbmod.PendingReplyRepo(self.mdb, "pending_replies", "message_key")
            self.unknown_terms = dbmod.UnknownTermRepo(self.mdb, "unknown_terms", "term")
            self.sender_slots = dbmod.SenderSlotRepo(self.mdb, "sender_slots", "slot_key")
            self.reconciliation = dbmod.ReconciliationRepo(self.mdb, "reconciliation_reports", "deal_id")

    d = _Mock("mongodb://mock", "moneyado_golden")
    await d.connect()
    await d.treasuries.seed_if_empty(SEED_TREASURIES)
    await d.suppliers.seed_if_missing(SEED_SUPPLIERS)
    await seed_golden_treasuries(d)
    return d


@pytest.mark.parametrize("cid,messages,expected", _CASES, ids=[c[0] for c in _CASES])
async def test_golden_case(cid, messages, expected):
    if _SINGLE_DIGIT_CODE.match(str(expected.get("code") or "")):
        pytest.skip("EXPORT_SEQ: كود تسلسليّ من التصدير (A1–A9) لا رقم إشاريّ حقيقيّ")
    db = await _golden_db()
    try:
        pipe = build_pipeline(db)
        got = adapt(await ingest_case(pipe, messages))
        diffs = compare(expected, got, skip=KNOWN_DIVERGENCES.get(cid, set()))
        assert not diffs, f"{cid}: فروق غير مستثناة: " + "; ".join(
            f"{f}: expected={e!r} got={g!r}" for f, e, g in diffs)
    finally:
        await db.close()


# ── تثبيت سلوكيات المحلّل الصحيحة حيث بيانات الملف خاطئة (توثيق «المحلّل مُحِقّ») ──────────
async def test_arabic_thousands_comma_not_decimal():
    """«6،070» فاصلة عربية = فاصل آلاف → 6070 (الملف يقول 70 خطأً)."""
    db = await _golden_db()
    try:
        got = adapt(await ingest_case(build_pipeline(db),
                    ["A56\n01123325384\nتسليم 6،070 ج.م\nفودافون", "393 تنيمه 6.18\nبلاس"]))
        assert got["amount_gross"] == 6070
    finally:
        await db.close()


async def test_comma_thousands_extracted():
    """«49,915»→49915 و«3,000»→3000 (الملف يتركهما None خطأً)."""
    db = await _golden_db()
    try:
        pipe = build_pipeline(db)
        g1 = adapt(await ingest_case(pipe,
                   ["A79\n01276720726\nباسم محمد عوض\n 49,915 حنيه مصري \n\nانستاباي",
                    "826 شركة ارسال 6.08\nالبراق 6.11"]))
        assert g1["amount_gross"] == 49915
    finally:
        await db.close()


def test_prefixed_reference_recovered():
    """رقم إشاريّ ببادئة عربية زائدة «اA3002» → يُستخرَج A3002 ولا تُصنَّف الرسالة noise (بلاغ prefixed_code)."""
    from core.constants import SEED_SUPPLIERS as _S, SEED_TREASURIES as _T
    from core.models import SupplierRecord, TreasuryRecord
    from core.parsing import parse_message
    T = [TreasuryRecord(**{"aliases": [], "active": True, **s}) for s in _T]
    S = [SupplierRecord(**{"aliases": [], "active": True, **s}) for s in _S]
    r = parse_message("اA3002 \nفودافون\n01032344779\nمبلغ 2040  مصرى", T, S)
    assert r.kind == "transfer" and r.leg.reference_number == "A3002"
    # لا مطابقة زائدة: كلمة عربية عاديّة تبقى بلا رقم إشاريّ
    assert parse_message("مرحبا كيف حالك اليوم", T, S).kind != "transfer"
