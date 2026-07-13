"""
اختبارات الصيغ الحقيقية (pr.md المصدر ٤). حالة لكل صيغة بالقيم المتوقّعة.
الحالات ذات الفجوات المتبقّية (خارج الإصلاحات 1-5) مُعلّمة xfail بسبب صريح.
"""
from __future__ import annotations

import pytest

from core.constants import Currency, OperationType, TreasuryType
from core.parsing import parse_message


async def _treas(db):
    return await db.treasuries.all_active()


# ── أ. صيغة عادية (بيع مفرد) — الفاصلة العربية في السعر (Fix 4) ───────────────
async def test_case_a_simple_sell(db):
    text = ("1208 فداء شاكونه 5،84\nبلس\nA5169\n010954227116\n"
            "1600 ج م\nفودافون\nبدون خصم")
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "A5169"
    assert leg.customer_code == "1208"
    assert leg.amount == 1600
    assert leg.currency == Currency.EGP
    assert leg.price_normalized == "5.84"      # «5،84» → «5.84» (Fix 4)
    assert leg.phone == "010954227116"
    assert leg.operation == OperationType.SELL
    assert leg.expects_pair is False


# ── ج. بيع فقط + خصم تونسي (A6604) ──────────────────────────────────────────
async def test_case_c_tnd_sell_only(db):
    text = ("A6604\nتونس/العاصمه\nالاسم: محمد عبدالرحيم\nالهاتف: 0925135252\n"
            "القيمه: 2000 دت\n526 بكر همالي 35.75\nوليد")
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    leg = res.leg
    assert leg.reference_number == "A6604"
    assert leg.customer_code == "526"
    assert leg.amount == 2000
    assert leg.currency == Currency.TND
    assert leg.price_normalized == "0.3575"    # تونسي 35.75 → 0.3575
    assert leg.operation == OperationType.SELL
    assert leg.expects_pair is False           # 🔴 لا انتظار (Fix 3)
    # #2 exact-match: «الاسم: محمد عبدالرحيم» لم يعد يُطابِق خزينة «محمد» خطأً →
    # تُحلّ الخزينة الصحيحة «وليد» (51) من سطرها المستقل (خزينة حقيقية، ليست مؤشّر خصم).
    assert leg.treasury is not None
    assert leg.treasury.code == "51"
    assert leg.treasury.type == TreasuryType.SELL_ONLY


# ── هـ. صيغة المرسل/المستلم (A5172) ─────────────────────────────────────────
async def test_case_e_sender_recipient(db):
    text = ("بلس\nA5172\nالمرسل : محمد دمياط\nالمستلم : 01044673940\n"
            "4.826 جنيه مصري\nفودافون كاش")
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.reference_number == "A5172"
    assert res.leg.amount == 4826             # 4.826 → فاصل آلاف يُشال
    assert res.leg.currency == Currency.EGP


# ── ي. «صافي» = بيع مفرد بلا عمولة ──────────────────────────────────────────
async def test_case_j_saafi_single(db):
    # صيغة أسطر نظيفة (بلا عملة ملتصقة) — «صافي» خزينة بيع/شراء لكن الحوالة مفردة
    text = "A5178\nفودافون\n01036326497\n45.000 جنيه مصر\nصافي"
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.amount == 45000
    assert res.leg.expects_pair is False


# ── المبلغ ملصق بالعملة بلا مسافة («مصر50000»/«50000م.ج») → يُلتقط في _fish_a_anchors ──
@pytest.mark.parametrize("glued", ["مصر50000", "50000مصر", "50000م.ج", "50000مج", "50,000مصر"])
async def test_glued_currency_amount(db, glued):
    # «مصر» وحدها (لا «مصري») لا يلتقطها detect_currency → fallback المبلغ الملصق بالعملة
    res = parse_message(f"A1\n{glued}\n01000000000", await _treas(db), [])
    assert res.leg.amount == 50000
    assert res.leg.currency == Currency.EGP


# ── عملة ومبلغ على سطرين منفصلين («مصر» ثم «50000») → يُلتقطان في _fish_a_anchors ──
@pytest.mark.parametrize("text, amount", [
    ("A8154\n01029051735\nمصر\n50000\n562 بوجناح 5.96", 50000),   # «مصر» + «50000» سطران
    ("A8154\n01029051735\nمصر\n49.500\nبلاس فون", 49500),         # «49.500» بنقطة = 49500 لا 49.5
    ("A9\n01029051735\nمصري\n50000\n562 بوجناح 5.96", 50000),     # «مصري» سطر مستقلّ → EGP
])
async def test_standalone_currency_and_amount_lines(db, text, amount):
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.amount == amount
    assert res.leg.currency == Currency.EGP


# ── عملة مستقلّة + سطرٌ يخلط الهاتف بالمبلغ متجاورين → يُعزَل الهاتف بنمطه فيُؤخَذ المبلغ الصحيح ──
# قبل التحسين: «01029051735 50000» يُدمَج في _bare_amount رقمًا عملاقًا؛ بعده: الهاتف (11/01) يُعزَل.
async def test_phone_and_amount_on_same_line_takes_amount(db):
    res = parse_message("A310\nمصر\n01029051735 50000\n562 بوجناح 5.96", await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.phone == "01029051735"        # الهاتف كامل (لم يبتلع خانات المبلغ)
    assert res.leg.amount == 50000               # المبلغ نظيف (لم يُدمَج برقم الهاتف)
    assert res.leg.currency == Currency.EGP


# ── تحسينات VodafoneBot: bidi + عزل الهاتف عن المبلغ + مرشّحو المبلغ (§3.5) ──────
async def test_bidi_markers_stripped(db):
    from core.parsing.normalize import parse_amount, repair_bidi_digits
    assert repair_bidi_digits("‏01012‎") == "01012"       # RLM/LRM يُزالان
    assert parse_amount("‏5000‎ مصري") == 5000
    leg = parse_message("A300\n‏01011112222‎\n3000 مصري", await _treas(db), []).leg
    assert leg.phone == "01011112222" and leg.amount == 3000        # لا انعكاس/تشويه


@pytest.mark.parametrize("text, phone, amount", [
    ("A301\n01012345678 5000 مصري", "01012345678", 5000),          # هاتف + مبلغ نفس السطر
    ("A302\nالقيمة: 01098765432 15150 جنيه مصري", "01098765432", 15150),
])
async def test_strip_phones_for_amount(db, text, phone, amount):
    leg = parse_message(text, await _treas(db), []).leg
    assert leg.phone == phone                                       # الهاتف كامل (لم يُدمَج بالمبلغ)
    assert leg.amount == amount                                     # المبلغ نظيف (بلا خانات الهاتف)


def test_parse_amount_candidates_takes_largest():
    from core.parsing.normalize import parse_amount
    assert parse_amount("5000 مصري") == 5000                       # مرشّح واحد → مباشرة
    assert parse_amount("5 000") == 5000                            # فاصل آلاف بمسافة = مرشّح واحد
    assert parse_amount("5000 مصري 603") == 5000                   # مرشّحان → الأكبر (+تحذير ملتبس)


# ── الهاتف: رفض الليبي، قبول التونسي 8-خانات والمصري 11، وعزله عن المبلغ (§3.4) ──
@pytest.mark.parametrize("text, expected_phone", [
    ("A200\n+218 91-2192050\n3000 دت", "218912192050"),  # ليبي (+218) → يُقبَل كما كُتب (جولة ٤)
    ("A201\n92192050\nمصر\n50000", "92192050"),         # تونسي 8-خانات (يبدأ 9)
    ("A202\n01029051735\nمصر\n50000", "01029051735"),   # مصري 11
    ("A203\n01064074568 مصر 100000", "01064074568"),    # الهاتف يُعزَل عن المبلغ
])
async def test_phone_classification(db, text, expected_phone):
    res = parse_message(text, await _treas(db), [])
    assert res.leg.phone == expected_phone


# ── الرسالة الثانية التونسية: سعر ملصق بالاسم + عدد صحيح سعرًا (لا كودًا) ─────────
@pytest.mark.parametrize("text, code, name, price_raw", [
    ("939 سامر قريش35.5", "939", "سامر قريش", "35.5"),   # السعر ملصق بالاسم → يُفصَل
    ("محمد عاشور 35\nفتحي", None, "محمد عاشور", "35"),    # «35» بعد الاسم + تونسي → سعر لا كود
    ("53 احمد العكاري 5.90\nوليد", "53", "احمد العكاري", "5.90"),  # الكود قبل الاسم يبقى كودًا
    ("526 بكر همالي\nوليد", "526", "بكر همالي", None),    # كود 3-خانات قبل الاسم يبقى كودًا
])
async def test_tunisian_second_message_price_vs_code(db, text, code, name, price_raw):
    from core.parsing.parser import parse_completion_fragment
    f = parse_completion_fragment(text, await _treas(db), [])
    assert f.customer_code == code
    assert f.customer_name == name
    assert f.price_raw == price_raw


# ── الاستقرار: رقم إشاري + هاتف → جاهزة فورًا (0s) حتى بلا مبلغ+عملة مُلتصقين ─────
def test_ref_plus_phone_is_immediately_stable():
    from core.queue.stabilization import looks_like_complete_transfer as C
    assert C("A8154\n01029051735\nمصر\n50000") is True    # ref + هاتف → فورية
    assert C("A201\n92192050\nمصر\n50000") is True          # ref + هاتف تونسي → فورية
    assert C("مصر\n50000") is False                          # بلا رقم إشاري → ليست فورية


# ── الأرقام العربية-الهندية + هاتف مصري دوليّ + عملة «دم» (§3.4 §3.5) ──────────
async def test_arabic_indic_digits_and_intl_phone(db):
    # «٠١٠…» → 01093871382، «مبلغ 15150» label، «فودافون كاش» وسيلة، ref+هاتف
    r = parse_message("A8031\nحول لي\n٠١٠٩٣٨٧١٣٨٢\nمبلغ 15150 جنيه مصري\nفودافون كاش",
                      await _treas(db), [])
    assert r.kind == "transfer"
    assert r.leg.reference_number == "A8031"
    assert r.leg.phone == "01093871382"          # أرقام عربية-هندية → لاتينية
    assert r.leg.amount == 15150 and r.leg.currency == Currency.EGP


@pytest.mark.parametrize("raw, expected", [
    ("٠١٠٩٣٨٧١٣٨٢", "01093871382"),   # عربية-هندية
    ("+20 100 745 3278", "201007453278"),  # مصري دوليّ (+20) → كما كُتب بلا تحويل (جولة ٤)
])
def test_phone_normalization(raw, expected):
    from core.parsing.normalize import extract_phone
    assert extract_phone(raw) == expected


async def test_daal_meem_is_egp(db):
    # «دم» (درهم/دينار مصري) → EGP: «16650دم»
    leg = parse_message("A700\n01000000000\n16650دم", await _treas(db), []).leg
    assert leg.amount == 16650 and leg.currency == Currency.EGP


async def test_attachment_kilobyte_is_noise(db):
    # «127 كيلوبايت» (صورة مرفقة) لا تُقرأ زبونًا (code 127)
    leg = parse_message("A701\n01000000000\n127 كيلوبايت\n5000 ج م", await _treas(db), []).leg
    assert leg.customer_code is None


# ── الرسالة الثانية: كود ملصق ببداية الاسم + كود بنقطة ختامية (§3.2) ──────────
@pytest.mark.parametrize("text, code, name, price", [
    ("728معتصم شعافي 35.5\nعمر", "728", "معتصم شعافي", "35.5"),   # كود ملصق «728معتصم»
    ("الوروار 1298. 35.5\nعمر", "1298", "الوروار", "35.5"),        # كود بنقطة ختامية «1298.»
])
async def test_second_message_glued_code_and_trailing_dot(db, text, code, name, price):
    from core.parsing.parser import parse_completion_fragment
    f = parse_completion_fragment(text, await _treas(db), [])
    assert (f.customer_code, f.customer_name, f.price_raw) == (code, name, price)


# ── الرسالة الثانية التونسية: كود رقميّ أولًا + سعر صحيح <100 لاحقًا → السعر لا كودٌ ثانٍ (§3.6) ──
# قبل الإصلاح: «35» يُصنَّف كودًا ثانيًا (CODE_RE) فيُسقَط والسعر=None؛ بعده: يُلتقط سعرًا فيُطبَّع 0.35.
# «فتحي» خزينة تونسية (كود 80، alias) → تضبط العملة TND وتُحذَف من الاسم.
@pytest.mark.parametrize("text, code, name", [
    ("986 سند التركي 35\nفتحي", "986", "سند التركي"),
    ("1054 الحارف مستقبل 35\nفتحي", "1054", "الحارف مستقبل"),
])
async def test_second_message_tnd_integer_price_after_code(db, text, code, name):
    from core.parsing.parser import parse_completion_fragment
    f = parse_completion_fragment(text, await _treas(db), [])
    assert f.customer_code == code
    assert f.customer_name == name
    assert f.currency == Currency.TND
    assert f.price_raw == "35"
    assert f.price_normalized == "0.35"       # سعر تونسي صحيح 35 → 0.35 (§3.6)


async def test_two_leg_glued_code_pairs(db):
    # بيع+شراء بكود ملصق «145خماج» → زوجان صالحان لـ Path A (§7.3)
    from core.parsing.parser import extract_code_name_price_lines
    pairs = extract_code_name_price_lines("145خماج 5.84\n760 طه 5.9")
    assert pairs == [("145", "خماج", "5.84"), ("760", "طه", "5.9")]


async def test_supplier_albarraq_seeded_makes_two_leg(db):
    # مورد «البراق» (كود 1280) مسجّل → «258احمد بوسالم 5.78 / البراق 5.84» يصير زوجين (بيع+شراء)
    from core.constants import SEED_SUPPLIERS
    from core.parsing.parser import extract_code_name_price_lines
    await db.suppliers.seed_if_missing(SEED_SUPPLIERS)
    sp = await db.suppliers.all_active()
    pairs = extract_code_name_price_lines("258احمد بوسالم 5.78\nالبراق 5.84", sp)
    assert pairs == [("258", "احمد بوسالم", "5.78"), ("1280", "البراق", "5.84")]


async def test_omar_alias_resolves_treasury_58(db):
    # «عمر» alias لخزينة تونس (عمر العاصمة) كود 58 — مؤكَّد في seed
    from core.parsing.parser import parse_completion_fragment
    f = parse_completion_fragment("الصافنات 35.5\nعمر", await _treas(db), [])
    assert f.treasury is not None and f.treasury.code == "58"


# ── #1 عملة ملتصقة بالرقم «541ج» → المبلغ يُلتقط (كان xfail) ──────────────────
async def test_case_j_glued_currency_slash(db):
    text = "بلس / A5183 / فودافون / 01094589619 / 541ج / صافي"
    res = parse_message(text, await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.amount == 541           # «541ج» → 541 (Fix #1)
    assert res.leg.currency == Currency.EGP


# ── فجوة متبقّية خارج الثغرات الأربع: شراء صريح بلا مبلغ/كود/ref ──────────────
@pytest.mark.xfail(reason="خارج الأربع: شراء صريح «شراء مؤمن عريبي 6.30» بلا مبلغ/كود/ref → noise",
                   strict=False)
async def test_case_w_explicit_buy(db):
    from core.models import SupplierRecord
    await db.suppliers.upsert(SupplierRecord(code="900", name="مؤمن عريبي", aliases=["مؤمن عريبي"]))
    res = parse_message("شراء مؤمن عريبي 6.30\nبلاس فون",
                        await _treas(db), await db.suppliers.all_active())
    assert res.kind == "transfer"
    assert res.leg.operation == OperationType.BUY


# ── الرسالة الأولى: سطر عربيّ منفرد بلا أرقام = اسم المستلم (§11.1) ────────────
async def test_first_message_bare_arabic_line_is_recipient(db):
    """«خيرية» في سطر منفرد بعد الرقم الإشاري → recipient_name، بلا ابتلاع الدفع/المبلغ/الهاتف."""
    from core.constants import SEED_SUPPLIERS
    await db.suppliers.seed_if_missing(SEED_SUPPLIERS)
    sp = await db.suppliers.all_active()
    msg = "A8676\nارجو تنفيذ\nخيرية\n+20 115 2936804\nالقيمة: 14.288 ج م\nانستا باي"
    leg = parse_message(msg, await _treas(db), sp).leg
    assert leg.recipient_name == "خيرية"          # السطر العربيّ المنفرد التُقِط اسمَ مستلم
    assert leg.payment_method == "إنستا باي"       # «انستا باي» بقيت دفعًا (لم تُبتلَع كاسم مستلم)
    assert leg.phone == "201152936804"        # مصري دوليّ (+20) → كما كُتب بلا تحويل (جولة ٤)
    assert leg.amount == 14288
    assert leg.currency == Currency.EGP


# ── الرسالة الثانية: «اسم كود سعر» (الكود في الوسط §3.2) ──────────────────────
def test_name_code_price_pattern_only():
    """«احمد بوزويص 876 5.88» (اسم + كود وسط + سعر) → زوج زبون؛ و«كود أولاً» لا يتأثّر."""
    from core.parsing.parser import extract_code_name_price_lines
    assert extract_code_name_price_lines("احمد بوزويص 876 5.88") == [("876", "احمد بوزويص", "5.88")]
    assert extract_code_name_price_lines("1300 عبدالله معتيق 5.90") == [("1300", "عبدالله معتيق", "5.90")]


async def test_second_message_name_code_price_plus_supplier_two_pairs(db):
    """«احمد بوزويص 876 5.88 / البراق» → زوجان (زبون بكود وسط + مورد) فيعمل ربط Path A (§7.3)."""
    from core.constants import SEED_SUPPLIERS
    from core.parsing.parser import extract_code_name_price_lines
    await db.suppliers.seed_if_missing(SEED_SUPPLIERS)
    sp = await db.suppliers.all_active()
    pairs = extract_code_name_price_lines("احمد بوزويص 876 5.88\nالبراق", sp)
    assert pairs == [("876", "احمد بوزويص", "5.88"), ("1280", "البراق", None)]


# ── استنتاج EGP من الهاتف المصري (01…) + الرقم المجرّد مبلغًا (§3.4) ───────────
def test_is_egyptian_phone():
    from core.parsing.parser import _is_egyptian_phone
    assert _is_egyptian_phone("01000204661") is True
    assert _is_egyptian_phone("0925135252") is False    # 10 خانات
    assert _is_egyptian_phone("92512345") is False       # تونسي 8
    assert _is_egyptian_phone(None) is False


async def test_egyptian_phone_infers_egp_and_bare_amount(db):
    """هاتف مصري (01…) بلا «ج.م» → EGP افتراضيًّا + رقم مجرّد يُقبَل مبلغًا («2051»→2051)."""
    res = parse_message("A8859\n01000204661\n2051\nفودافون", await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.amount == 2051
    assert res.leg.currency == Currency.EGP
    assert res.leg.phone == "01000204661"


async def test_egyptian_phone_keeps_explicit_currency_and_amount(db):
    """عملة صريحة «ج م» لا يُلغيها الاستنتاج؛ المبلغ من رمز العملة لا الرقم المجرّد."""
    res = parse_message("A200\n01012345678\n3950 ج م\nصافي", await _treas(db), [])
    assert res.leg.amount == 3950 and res.leg.currency == Currency.EGP


async def test_non_egyptian_phone_no_egp_inference(db):
    """هاتف غير مصري لا يُستنتَج منه EGP (العملة تبقى None)؛ لكن الرقم المجرّد يُلتقَط مبلغًا
    (فيكس د: شكل حوالة صحيح — مرجع + هاتف — يكفي لالتقاط الرقم المجرّد الوحيد)."""
    res = parse_message("A100\n0925135252\n2000\nصافي", await _treas(db), [])
    assert res.leg.currency is None
    assert res.leg.amount == 2000        # فيكس د: رقم مجرّد + مرجع + هاتف → مبلغ


# ── تقوية الجولة ٥: استقلالية الاستخراج عن الموضع (فيكس أ/ب/ج/د) ──────────────
async def test_explicit_labels_on_separate_segments(db):
    """فيكس أ: «الاسم/الرقم/القيمة» على مقاطع مستقلّة → تحدّد الحقل التالي، والتسمية لا تصير اسمًا."""
    res = parse_message(
        "A100\nالقيمة\n5000 ج م\nالاسم\nكرم\nالرقم\n01007285143\nفودافون كاش",
        await _treas(db), [])
    assert res.leg.amount == 5000
    assert res.leg.recipient_name == "كرم"          # لا «الاسم» (كلمة التسمية)
    assert res.leg.phone == "01007285143"
    assert res.leg.payment_method == "فودافون كاش"


async def test_labeled_bare_value_amount(db):
    """فيكس ب: رقم مجرّد بعد تسمية «القيمة» (بلا كلمة عملة) → يُقبَل مبلغًا."""
    res = parse_message("A103\nالاسم\nكرم\n01007285143\nالقيمة\n2900", await _treas(db), [])
    assert res.leg.amount == 2900 and res.leg.recipient_name == "كرم"


async def test_two_phone_candidates_keeps_both(db):
    """فيكس ج: رقمان محتملان بالضبط → يُحفَظ كلاهما (phone + phone_alt) مرتبطين بالحوالة."""
    res = parse_message("A104\nكرم\n27348472 او 0923134302\n5000 ج م", await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.phone == "27348472"
    assert res.leg.phone_alt == "0923134302"


async def test_three_phone_candidates_keeps_strongest_only(db):
    """فيكس ج: ثلاثة أرقام فأكثر → يكفي الأقوى ترجيحًا (بلا احتفاظ بالباقي)."""
    res = parse_message(
        "A106\nكرم\n27348472 او 0923134302 او 21694385651\n5000 ج م", await _treas(db), [])
    assert res.leg.phone == "27348472"
    assert res.leg.phone_alt is None


async def test_bare_amount_no_currency_with_ref_and_phone(db):
    """فيكس د: مبلغ مجرّد بلا كلمة عملة + كود A + هاتف واضح → يُستخرَج المبلغ."""
    res = parse_message("A107\n01007285143\nكرم\n10000", await _treas(db), [])
    assert res.kind == "transfer"
    assert res.leg.amount == 10000
    assert res.leg.phone == "01007285143"


async def test_bare_amount_multiple_candidates_escalate(db):
    """فيكس د: رقمان مجرّدان مرشّحان للمبلغ بلا حسم → ambiguous_amount (تصعيد لا تخمين)."""
    res = parse_message("A109\n01007285143\nكرم\n10000\n7500", await _treas(db), [])
    assert res.leg.amount is None
    assert set(res.leg.ambiguous_amount or []) == {10000, 7500}
