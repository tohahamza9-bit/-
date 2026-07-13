"""
اختبارات سياسة التفاعلات (قاعدة المالك) + جذر A292 + تنبيه «حوالة محتملة فشل استخراجها».

القاعدة: ✅ فقط عند تأكيد SQL الفعليّ (اليقين ١٠٠٪)؛ وإلا 🟡 «بانتظار التأكيد».
noise يحمل مرجعًا (Axxxx) → تنبيه المالك (is_alert) لا سقوط صامت.
"""
from __future__ import annotations

from datetime import timedelta

from core.constants import SEED_SUPPLIERS, SEED_TREASURIES, Mark, Status
from core.models import SupplierRecord, TreasuryRecord

from tests.test_integration import (  # يعيد استخدام تجهيزات التكامل
    ADMIN, CENTRAL, PAST, FakeWriter, StubVerifier, _enable_storage, _make_pipeline, _raw,
)

_A_TRANSFER = "1208 فداء شاكونه 5.84\nبلاس\nA5169\n010954227116\n1600 ج م\nفودافون"


# ── جذر A292: رمز العملة الملتصق «دمصرى/دمصري» يُفكَّك كحوالة ─────────────────
def test_glued_egp_currency_damasry_parses_as_transfer():
    from core.parsing.parser import parse_message
    tre = [TreasuryRecord(**t) for t in SEED_TREASURIES]
    sup = [SupplierRecord(**s) for s in SEED_SUPPLIERS]
    for tok in ("دمصرى", "دمصري"):
        r = parse_message(f"A292 / عاطف / 01210509822 / 24900 {tok} / محفظه اورنج", tre, sup)
        assert r.kind == "transfer", f"«{tok}» يجب أن تُفكَّك كحوالة"
        assert r.leg.amount == 24900 and r.leg.reference_number == "A292"


# ── قاعدة ✅: عند تأكيد SQL فقط ──────────────────────────────────────────────
async def test_completion_sql_disabled_marks_pending_not_done(db):
    await _enable_storage(db)                        # التخزين مُفعّل → commit=True
    writer = FakeWriter()                            # ok=True (بلا dry_run)
    verifier = StubVerifier(enabled=False)           # SQL معطّل → لا يقين
    pipe = _make_pipeline(db, writer, verifier, rooms=False)
    await pipe.capture(_raw("m1", _A_TRANSFER))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    done = await db.deals.by_status(Status.COMPLETED)
    assert len(done) == 1, "الحالة COMPLETED (قرار الحالة كما هو)"
    assert done[0].mark == Mark.MATCHED, "🟡 بانتظار تأكيد SQL — لا ✅"
    reacts = [o.get("reaction") for o in await db.outgoing.next_unsent(50) if o.get("reaction")]
    assert Mark.MATCHED.value in reacts, "🟡 على المركزية"
    assert Mark.DONE.value not in reacts, "لا ✅ بلا تأكيد SQL"


async def test_completion_sql_enabled_marks_done(db):
    await _enable_storage(db)
    writer = FakeWriter()
    verifier = StubVerifier(enabled=True)            # SQL مُفعّل → يؤكّد بعد الحفظ
    pipe = _make_pipeline(db, writer, verifier, rooms=False)
    await pipe.capture(_raw("m1", _A_TRANSFER))
    now = PAST + timedelta(seconds=120)
    await pipe.process_inbox(now)
    await pipe.tick(now)

    done = await db.deals.by_status(Status.COMPLETED)
    assert len(done) == 1 and done[0].mark == Mark.DONE, "✅ عند تأكيد SQL"
    reacts = [o.get("reaction") for o in await db.outgoing.next_unsent(50) if o.get("reaction")]
    assert Mark.DONE.value in reacts, "✅ على المركزية بعد التأكيد"


# ── تنبيه «حوالة محتملة فشل استخراجها» (noise يحمل مرجعًا) ────────────────────
async def test_noise_with_reference_alerts_owner(db):
    pipe = _make_pipeline(db, FakeWriter(), StubVerifier(), rooms=False)
    # مرجع A888 لكن رمز عملة مجهول «دكك» → فشل استخراج المبلغ → noise (لا سقوط صامت)
    await pipe.capture(_raw("m1", "A888 / عاطف اسكندر / 01210509822 / 24900 دكك / محفظه اورنج"))
    await pipe.process_inbox(PAST + timedelta(seconds=120))

    outs = await db.outgoing.next_unsent(50)
    admin_alerts = [o for o in outs if o["chat_jid"] == ADMIN and o.get("is_alert")]
    assert admin_alerts, "تنبيه فوريّ للمالك عند حوالة محتملة فشل استخراجها"
    assert any("A888" in (o.get("text") or "") for o in admin_alerts), "التنبيه يذكر المرجع"


async def test_pure_noise_without_reference_stays_silent(db):
    pipe = _make_pipeline(db, FakeWriter(), StubVerifier(), rooms=False)
    await pipe.capture(_raw("m1", "صباح الخير يا شباب"))   # ضجيج حقيقي بلا مرجع
    await pipe.process_inbox(PAST + timedelta(seconds=120))
    outs = await db.outgoing.next_unsent(50)
    assert not any(o["chat_jid"] == ADMIN for o in outs), "الهدرزة الحقيقية تبقى صامتة (لا تنبيه)"
