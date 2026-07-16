"""
ربط الرسالة الثانية — تصميم حتميّ بطبقتين (§7.3): المرجع أولاً، ثم FIFO لكل مُرسِل.

يستبدل آلية القرب/التجاور بالكامل: تحت دفعة سريعة من نفس المُرسِل (العمل الطبيعي للشركة)
كل زوج يُربَط بصاحبه الصحيح 100% بلا تصعيد/تنبيه — الدقة بالتصميم لا بالمراجعة اللاحقة.
"""
from __future__ import annotations

import contextlib
import io
from datetime import datetime, timedelta, timezone

from core.bus import Bus
from core.constants import Status
from core.models import RawMessage, WriteResult
from core.pipeline import Pipeline

NOW = datetime(2026, 7, 12, 19, 41, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
S1 = "sender1@lid"
S2 = "sender2@lid"


class _W:
    name = "w"

    async def write(self, job, *, commit):
        return WriteResult(ok=True)


class _V:
    enabled = False

    async def verify_transaction(self, *a, **k):
        return (False, None)

    async def find_last_pending(self, *a, **k):
        return None


def _pipeline(db):
    return Pipeline(db, Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN), _W(), _V(),
                    customer_room_jids=[], treasury_room_jids=[])


async def _run_burst(pipe, msgs, *, base=NOW, gap=0.3, now_offset=6):
    """يلتقط رسائل الدفعة (نفس التوقيت المتقارب) ثم يعالجها دورةً واحدة (كدفعة الإنتاج)."""
    for i, (text, key, sender) in enumerate(msgs):
        await pipe.capture(RawMessage(message_key=key, chat_jid=CENTRAL, sender_jid=sender,
                                      text=text, received_at=base + timedelta(seconds=i * gap)))
    with contextlib.redirect_stderr(io.StringIO()):
        await pipe.process_inbox(base + timedelta(seconds=now_offset))


async def _deal(db, ref):
    return await db.deals.find_by_grouping_key.__self__.col.find_one(
        {"sell_leg.reference_number": ref})  # مباشرة (waiting أو مكتملة)


async def _deals_by_ref(db):
    out = {}
    async for d in db.deals.col.find({}):
        r = (d.get("sell_leg") or {}).get("reference_number")
        if r:
            out.setdefault(r, []).append(d)
    return out


# ═════════════════════════════════════════════════════════════════════════════
# الطبقة ١ — مرجع صريح يتجاوز القُرب تماماً
# ═════════════════════════════════════════════════════════════════════════════
async def test_layer1_ref_beats_closer_different_ref_fragment(db):
    """رسالة ثانية بمرجع صريح A9021 → تُربَط بصفقة A9021، حتى لو صفقة أخرى (A9099) أحدث/أقرب."""
    pipe = _pipeline(db)
    await _run_burst(pipe, [
        # A9021 (خصم شكل جديد) msg1 — الأقدم
        ("A9021\nفودافون\n01039081887\n5700ج مصري\n1214 مكتب المدينه 5.98", "a21", S1),
        # حوالة أحدث A9099 (أقرب زمنيًا) تنتظر أيضًا — طُعم للقرب
        ("A9099\n0100000000\n9999 مصري\nصافي", "a99", S1),
        # msg2 لـA9021 بمرجعها الصريح → يجب أن تتجاوز A9099 الأقرب وتربط A9021
        ("A9021\nفودافون\n01039081887\n5.643 مصري\nبلاس فون", "a21b", S1),
    ])
    by = await _deals_by_ref(db)
    a21 = by["A9021"][0]["sell_leg"]
    assert a21["customer_code"] == "1214" and a21["commission"] == -57.0   # رُبط + خصم صح
    # A9099 لم تُلوَّث (بقيت بلا خزينة A9021)
    a99 = by["A9099"][0]["sell_leg"]
    assert (a99.get("treasury") or {}).get("name") != "بلاس فون" or a99.get("customer_code") is None


# ═════════════════════════════════════════════════════════════════════════════
# الطبقة ٢ — FIFO: عدة حوالات بلا مرجع ثم رسائل ثانية بنفس الترتيب
# ═════════════════════════════════════════════════════════════════════════════
async def test_layer2_fifo_three_pairs_same_order(db):
    """3 حوالات ناقصة بلا مرجع صريح بالثانية، ثم 3 رسائل ثانية بنفس الترتيب → كلٌّ لصاحبها (FIFO)."""
    pipe = _pipeline(db)
    await _run_burst(pipe, [
        ("A801\n01000000001\n1001 مصري\nصافي", "p1", S1),
        ("A802\n01000000002\n1002 مصري\nصافي", "p2", S1),
        ("A803\n01000000003\n1003 مصري\nصافي", "p3", S1),
        ("501 زبون واحد 5.91\nبلاس فون", "s1", S1),   # → A801 (الأقدم)
        ("502 زبون اثنين 5.92\nبلاس فون", "s2", S1),  # → A802
        ("503 زبون ثلاثة 5.93\nبلاس فون", "s3", S1),  # → A803
    ])
    by = await _deals_by_ref(db)
    assert by["A801"][0]["sell_leg"]["customer_code"] == "501"
    assert by["A802"][0]["sell_leg"]["customer_code"] == "502"
    assert by["A803"][0]["sell_leg"]["customer_code"] == "503"


# ═════════════════════════════════════════════════════════════════════════════
# الحالات الحقيقية — A9017/A9018 (رسالة ثانية متطابقة) + A9015 (خصم شكل جديد)
# ═════════════════════════════════════════════════════════════════════════════
async def test_real_duplicate_second_a9017_a9018(db):
    """A9017 وA9018 (ناقصتان) بثانية **متطابقة نصًّا** «1277 شركة القن / بلاس فون» → كلٌّ يربط بصاحبه."""
    pipe = _pipeline(db)
    await _run_burst(pipe, [
        ("A9017\n1465ج.م\nفودافون كاش\n01055387621\nصافي", "r17", S1),
        ("1277 شركة القن 5.92\nبلاس فون", "r17b", S1),
        ("A9018\n586ج.م\nفودافون كاش\n01008791770\nصافي", "r18", S1),
        ("1277 شركة القن 5.92\nبلاس فون", "r18b", S1),
    ])
    by = await _deals_by_ref(db)
    for ref, amt in (("A9017", 1465.0), ("A9018", 586.0)):
        sl = by[ref][0]["sell_leg"]
        assert sl["customer_code"] == "1277" and sl["amount"] == amt
        assert (sl.get("treasury") or {}).get("name") == "بلاس فون"


async def test_real_discount_new_format_a9015(db):
    """A9015 (الشكل الجديد: المرجع في الرسالتين) → الخصم يُحتسَب (29720−29423=297)، لا يُسحَب fragment خاطئ."""
    pipe = _pipeline(db)
    await _run_burst(pipe, [
        ("A9015\n01127173416 فودافون 29720 جنيه مصري\n1284 مروان الشاوش 5.98", "r15", S1),
        ("A9015\n01127173416 فودافون 29.423 جنيه مصري\nبلاس", "r15b", S1),
    ])
    by = await _deals_by_ref(db)
    assert len(by["A9015"]) == 1                                  # صفقة واحدة (لا انفصال)
    sl = by["A9015"][0]["sell_leg"]
    assert sl["customer_code"] == "1284" and sl["amount"] == 29720.0
    assert sl["commission"] == -297.0                            # الخصم صح
    assert (sl.get("treasury") or {}).get("name") == "بلاس فون"


async def test_wrong_ref_fragment_not_pulled(db):
    """A9015 (بهوية، بلا خزينة) لا تسحب fragment عديم‑مرجع «3 محمد حمعه» يخصّ حوالة أخرى — تنتظر msg2."""
    pipe = _pipeline(db)
    # الرسالة الثانية عديمة‑المرجع «3 محمد حمعه» تصل قبل msg2 لـA9015 (طُعم)
    await _run_burst(pipe, [
        ("A9015\n01127173416 فودافون 29720 جنيه مصري\n1284 مروان الشاوش 5.98", "w15", S1),
        ("3 محمد حمعه 5.92\nبلاس", "wbait", S1),                  # fragment خاطئ (لحوالة أخرى)
        ("A9015\n01127173416 فودافون 29.423 جنيه مصري\nبلاس", "w15b", S1),
    ])
    by = await _deals_by_ref(db)
    sl = by["A9015"][0]["sell_leg"]
    assert sl["customer_code"] == "1284" and sl["commission"] == -297.0   # لم يُلوَّث، الخصم صح


# ═════════════════════════════════════════════════════════════════════════════
# اختبار الحمل — 15 زوج من نفس المُرسِل خلال ثوانٍ → 100% ربط صحيح
# ═════════════════════════════════════════════════════════════════════════════
async def test_load_15_pairs_same_sender_all_correct(db):
    """محاكاة 30 رسالة (15 زوج) من نفس المُرسِل: 15 حوالة ناقصة متتالية ثم 15 رسالة ثانية بنفس
    الترتيب → FIFO يربط كل زوج بصاحبه 100% بلا تصادم/تصعيد (دفعة الشركة الطبيعية)."""
    pipe = _pipeline(db)
    msgs = []
    for i in range(15):
        ref = f"A7{200 + i}"
        msgs.append((f"{ref}\n0100000{i:04d}\n{2000 + i} مصري\nصافي", f"f{i}", S1))
    for i in range(15):                                          # الثواني بنفس الترتيب
        code = str(600 + i)
        msgs.append((f"{code} زبون{i} 5.9{i % 10}\nبلاس فون", f"s{i}", S1))
    await _run_burst(pipe, msgs, gap=0.15, now_offset=8)

    by = await _deals_by_ref(db)
    for i in range(15):
        ref = f"A7{200 + i}"
        assert ref in by, f"صفقة {ref} مفقودة"
        assert len(by[ref]) == 1, f"{ref} انفصلت إلى {len(by[ref])} صفقة"
        sl = by[ref][0]["sell_leg"]
        assert sl["customer_code"] == str(600 + i), \
            f"{ref} رُبط بكود {sl['customer_code']} بدل {600 + i} (تصادم FIFO!)"
        assert (sl.get("treasury") or {}).get("name") == "بلاس فون"
    # لا تصعيد للمسؤول (الدقة بالتصميم، بلا تنبيه تحت الضغط)
    outs = await db.outgoing.next_unsent(200)
    assert not [o for o in outs if o["chat_jid"] == ADMIN], "لا يجوز تصعيد تحت الدفعة"


# ═════════════════════════════════════════════════════════════════════════════
# فصل الربط عن التحقّق — بعد ربط صحيح، فحص المعقولية (خزينة/كود) يبقى كما هو
# ═════════════════════════════════════════════════════════════════════════════
async def test_linking_separate_from_validation_missing_identity(db):
    """رُبط صح بالـFIFO (اكتملت الخزينة «بلس») لكن الهوية غائبة → التحقّق يحجب كالمعتاد (⚠️/تعليق)،
    بلا تغيير في منطق فحص المعقولية — الربط منفصل عن التحقّق."""
    from core.models import BotControl
    await db.control.set(BotControl(storage_enabled=True, state="running"), "test")
    pipe = _pipeline(db)
    # حوالة ناقصة الهوية + رسالة ثانية «بلس» (خزينة فقط، بلا كود) → تُربَط الخزينة، لكن الهوية تبقى ناقصة
    await _run_burst(pipe, [
        ("A9500\nفودافون\n01000000009\n5000 مصري", "v1", S1),   # بلا كود/اسم → ناقصة
        ("بلس", "v1b", S1),                                       # خزينة فقط (بلاس فون)
    ])
    with contextlib.redirect_stderr(io.StringIO()):
        await pipe.tick(NOW + timedelta(seconds=220))
    by = await _deals_by_ref(db)
    d = by["A9500"][0]
    sl = d["sell_leg"]
    assert (sl.get("treasury") or {}).get("name") == "بلاس فون"   # الربط تمّ (الخزينة من الثانية)
    assert sl.get("customer_code") is None                        # الهوية ما زالت ناقصة
    assert d["status"] in ("held", "escalated")                  # التحقّق حجبها (كود مفقود) — كالمعتاد


# ═════════════════════════════════════════════════════════════════════════════
# خطأ إملاء الخزينة في الرسالة الثانية — لا إسقاط صامت ولا انزياح FIFO (حادثة X850–X853)
# ═════════════════════════════════════════════════════════════════════════════
async def test_misspelled_treasury_second_escalates_not_dropped(db):
    """رسالة ثانية بهوية زبون واضحة + خزينة بخطأ إملائيّ («خزينةمجهوله» لا تُطابِق أيّ خزينة §0):
    سابقًا تُسقَط صامتةً فتبقى الأولى معلّقة. الآن: تُربَط الهوية بالأولى، وتبقى الخزينة غير محلولة
    (تُصعَّد لاحقًا)، ويُلتقَط الرمز المجهول في unknown_terms."""
    pipe = _pipeline(db)
    # now_offset=95 > STABILIZE_MAX: الرسالة الثانية (خزينة مجهولة → ليست ردّ إكمال) تستقرّ بالعمر
    # فتُعالَج في الدفعة (في الإنتاج تُعالِجها نبضة لاحقة بعد الاستقرار). نختبر منطق الربط لا التوقيت.
    await _run_burst(pipe, [
        ("X900\n\n01000000001\n1000 ج م", "x900", S1),      # ناقصة: بلا كود/اسم/خزينة
        ("11 زبون واحد 6.04\nخزينةمجهوله", "x900b", S1),     # هوية + خزينة بخطأ إملائيّ
    ], now_offset=95)
    by = await _deals_by_ref(db)
    sl = by["X900"][0]["sell_leg"]
    assert sl["customer_code"] == "11"                        # الهوية رُبطت (لا إسقاط صامت)
    assert (sl.get("treasury") or None) is None               # الخزينة غير محلولة → تصعيد لاحق
    terms = [t["term"] async for t in db.unknown_terms.col.find({})]
    assert "خزينةمجهوله" in terms                             # الرمز الغريب التُقِط للوحة


async def test_typo_treasury_does_not_shift_fifo(db):
    """الإصلاح الجوهريّ (X850–X853): خطأُ إملاءِ خزينةٍ في ثانية X900 لا يُزيح الطابور —
    X900 يأخذ زبونه وX901 يأخذ زبونه (بلا off-by-one). بلا الإصلاح كانت X900 تسرق زبون X901."""
    pipe = _pipeline(db)
    await _run_burst(pipe, [
        ("X900\n\n01000000001\n1000 ج م", "x900", S1),
        ("11 زبون واحد 6.04\nخزينةمجهوله", "x900b", S1),      # خزينة بخطأ إملائيّ (كانت تُسقِط X900)
        ("X901\n\n01000000002\n2000 ج م", "x901", S1),
        ("22 زبون اثنين 6.04\nبلاس", "x901b", S1),            # خزينة صحيحة (بلاس فون)
    ])
    by = await _deals_by_ref(db)
    # كلٌّ بزبونه — لا انزياح. بلا الإصلاح: X900 (معلّقة لأن ثانيتها سقطت) تبتلع «22» فتصير X900=22.
    assert by["X900"][0]["sell_leg"]["customer_code"] == "11"
    assert by["X901"][0]["sell_leg"]["customer_code"] == "22"
