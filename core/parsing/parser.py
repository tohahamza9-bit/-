"""
كشف الصيغة A/SI وتفكيكها إلى ParsedLeg (§3.1–§3.3، §5، §6).
- A: سطور متتابعة بلا عناوين، كل سطر حوالة يبدأ بكود الزبون.
- SI: حقول مُعنونة صريحة (`رقم العملية:`، `الخزينة:` ...).
"""
from __future__ import annotations

import re
from typing import Optional

from core.constants import Currency, OperationType
from core.logging_setup import get_logger
from core.models import (
    ParsedLeg,
    ParseResult,
    SupplierRecord,
    SupplierRef,
    TreasuryRecord,
    TreasuryRef,
)

from .classify import (
    detect_control,
    detect_explicit_operation,
    has_silent_keyword,
    is_out_of_scope,
)
from .normalize import (
    classify_phone,
    detect_currency,
    extract_phone,
    is_phone_like,
    normalize_ar,
    normalize_digits,
    normalize_payment,
    normalize_price,
    parse_amount,
)
from .resolve import resolve_supplier, resolve_treasury

log = get_logger(__name__)

# ── أنماط ────────────────────────────────────────────────────────────────────
_REFERENCE_RE = re.compile(r"^[A-Za-z]{1,4}\d{2,}$")
# رمز رقمي (سعر): يقبل الفاصلة اللاتينية «.» «,» والعربية «،» (مثل 5،84)
_NUMERIC_TOKEN_RE = re.compile(r"^\d+(?:[.,،]\d+)?$")
_ARABIC_RE = re.compile(r"[ء-ي]")

# فاصل العنوان/القيمة في السطور المعنونة: «:» أو «=» («القيمة = 25000» = «القيمة: 25000»)
_LABEL_SEP_RE = re.compile(r"[:=]")

# وسوم المبلغ المُعنون في حوالة A: «القيمة =»/«المبلغ :»/«القيمة:» → قيمة رقمية (§3.5)
_AMOUNT_LABELS = ("القيمه", "المبلغ")

# كلمة عملة على **سطر مستقلّ** (§3.4): «مصر»/«مصري»→EGP، «تونس»/«تونسي»→TND. تُلتقط عملةً حين
# تُفصَل عن المبلغ على سطرين («مصر» ثم «50000»)، إذ «مصر» وحدها لا يلتقطها detect_currency.
_STANDALONE_CURRENCY = {
    "مصر": Currency.EGP, "مصري": Currency.EGP,
    "تونس": Currency.TND, "تونسي": Currency.TND,
}
# سطر رقميّ مجرّد (بلا حرف عربيّ): مبلغ محتمل — «50000»/«49.500». الفواصل «.،,» فواصل آلاف (§3.5).
_BARE_NUMBER_RE = re.compile(r"^\d[\d.,،'\s]*$")

# سطر تعليمات بشري (طلب لا بيانات): «ارجو تحويل…» / «برجاء…» → يُتجاهَل (§7.2).
# مطابقة بالكلمة الكاملة المطبَّعة (لا بادئة) كي لا تُطابَق أسماء مثل «رجائي/مرجان».
_REQUEST_NOISE_WORDS = {
    normalize_ar(w)
    for w in ("ارجو", "أرجو", "نرجو", "يرجى", "برجاء", "الرجاء", "بالرجاء", "حول")
}
# ضجيج مرفقات/تعليمات: صورة مرفقة («127 كيلوبايت»)، تُتجاهَل فلا تُقرأ كزبون (§7.2).
_ATTACHMENT_NOISE = ("كيلوبايت", "ميجابايت", "بايت")

# ── الأماكن/البلدان (§11.1 خانة البلد) ───────────────────────────────────────
_CITIES = {
    "العاصمه": "العاصمة", "سوسه": "سوسة", "جربه": "جربة", "صفاقس": "صفاقس",
    "صفاقص": "صفاقس", "حمامات": "حمامات", "نابل": "نابل", "بنزرت": "بنزرت",
    "القيروان": "القيروان", "المنستير": "المنستير", "قابس": "قابس",
    "مدنين": "مدنين", "توزر": "توزر",
}
_COUNTRIES = {"تونس": "تونس", "مصر": "مصر", "ليبيا": "ليبيا"}

# ── عناوين الصيغة SI (§3.3) ──────────────────────────────────────────────────
_SI_LABELS = [
    "رقم العمليه", "رقم المستلم", "اسم الزبون", "القيمه قبل", "القيمه بعد",
    "السعر", "نوع التحويل", "الخزينه", "القيمه", "المورد",
]


def _has_reference(text: str) -> bool:
    """§0: هل يحمل النصّ رقمًا إشاريًا (Axxxx/SIxxxx) كرمز مستقلّ؟ — مرساة معاملة قاطعة."""
    return any(
        _REFERENCE_RE.match(tok)
        for tok in re.split(r"[\s/]+", (text or "").strip())
        if tok
    )


# ═════════════════════════════════════════════════════════════════════════════
# الواجهة العامّة
# ═════════════════════════════════════════════════════════════════════════════
def parse_message(
    text: str,
    treasuries: list[TreasuryRecord],
    suppliers: list[SupplierRecord],
) -> ParseResult:
    """يفكّك رسالة المركزية إلى ParseResult (§3، §5، §7.2، §10)."""
    if not text or not text.strip():
        return ParseResult(kind="noise", reason="رسالة فارغة", confidence=1.0)
    text = normalize_digits(text)          # أرقام عربية-هندية → لاتينية قبل أي استخراج (§3.5)

    # خارج النطاق (§0): تسليم يدوي/باليد → يُصعَّد لا يُدخَل.
    # 🔴 قرار المستخدم: وجود رقم إشاري (Axxxx) مرساة معاملة قاطعة → حوالة (pattern-fishing)
    # وتتقدّم على out_of_scope. فحص التسليم اليدوي يقتصر على الرسائل **بلا** رقم إشاري.
    if not _has_reference(text) and is_out_of_scope(text):
        return ParseResult(
            kind="out_of_scope",
            reason="تسليم يدوي/باليد — خارج نطاق البوت (§0)", confidence=1.0,
        )

    leg, conf = _parse_transfer(text, treasuries, suppliers)
    # حوالة واضحة = مبلغ + مرساة هوية (كود زبون أو رقم إشاري). الرقم الإشاري مرساة صالحة
    # لصيغ بلا كود زبون (مثل A06/صافي) — متّسق مع stabilization.looks_like_complete_transfer.
    # حماية من false positives: الرقم الإشاري يُلتقط بنمط صارم على مقطع مستقلّ (_REFERENCE_RE)،
    # والمبلغ لا يُلتقط إلا بسياق عملة (_classify_segment §3.5) — فالاجتماع إشارة معاملة قوية.
    strong = (
        leg is not None
        and leg.amount is not None
        and (leg.customer_code is not None or leg.reference_number is not None)
    )

    # §10: رسالة تحكّم (إلغاء/تعديل/تأكيد/تصحيح) — تُحسم فقط إن لم تكن حوالة واضحة
    ctrl = detect_control(text)
    if ctrl and not strong:
        action, value = ctrl
        return ParseResult(
            kind="control", control_action=action, control_value=value,
            reason=f"رسالة تحكّم: {action}", confidence=0.9,
        )

    # §7.2: تسليم/صرف/قبض → تجاهل صامت
    if has_silent_keyword(text) and not strong:
        return ParseResult(kind="silent_ignore", reason="تسليم/صرف/قبض (§7.2)", confidence=1.0)

    if strong:
        return ParseResult(kind="transfer", leg=leg, confidence=conf)

    # هدرزة: بلا بنية حوالة بعد المهلة (§7.2)
    return ParseResult(
        kind="noise", leg=leg, reason="بلا بنية حوالة كاملة (كود+مبلغ) (§7.2)", confidence=0.5,
    )


# ═════════════════════════════════════════════════════════════════════════════
# كشف الصيغة وتفكيكها
# ═════════════════════════════════════════════════════════════════════════════
def _parse_transfer(
    text: str, treasuries: list[TreasuryRecord], suppliers: list[SupplierRecord],
) -> tuple[Optional[ParsedLeg], float]:
    is_si = _looks_like_si(text)
    if is_si:
        fields = _parse_si_fields(text)
    else:
        fields = _parse_a_fields(text, treasuries)
    return _build_leg(fields, text, treasuries, suppliers, is_si)


def _split_label(line: str) -> Optional[tuple[str, str]]:
    """يقسم سطرًا معنونًا على أوّل «:» أو «=» → (العنوان، القيمة). None إن لا فاصل.

    «القيمة: 25000» و«القيمة = 25000» متكافئان (§3.3)."""
    m = _LABEL_SEP_RE.search(line)
    if m is None:
        return None
    return line[:m.start()], line[m.start() + 1:]


def _is_request_noise(seg: str) -> bool:
    """سطر تعليمات بشري («ارجو تحويل…»/«برجاء…») — طلب لا بيانات، يُتجاهَل (§7.2)."""
    return bool(_REQUEST_NOISE_WORDS & set(normalize_ar(seg).split()))


def _looks_like_si(text: str) -> bool:
    """SI إن كان سطران على الأقل يبدآن بعنوان معروف متبوعًا بـ«:» أو «=» (§3.3)."""
    count = 0
    for line in text.splitlines():
        parts = _split_label(line.strip())
        if parts is None:
            continue
        head = normalize_ar(parts[0])
        if any(head.startswith(lb) for lb in _SI_LABELS):
            count += 1
    return count >= 2


# ── الصيغة SI (§3.3) ─────────────────────────────────────────────────────────
def _parse_si_fields(text: str) -> dict:
    f: dict = {}
    for line in text.splitlines():
        parts = _split_label(line.strip())
        if parts is None:
            continue
        raw_head, val = parts
        head, val = normalize_ar(raw_head), val.strip()
        if head.startswith("رقم العمليه"):
            f["reference"] = val.split()[0] if val else None
        elif head.startswith("رقم المستلم"):
            f["phone"] = extract_phone(val)
        elif head.startswith("اسم الزبون"):
            code, name = _extract_code_name(val)
            f["customer_code"], f["customer_name"] = code, name
        elif head.startswith("القيمه قبل"):
            f["currency"] = detect_currency(val) or f.get("currency")
            f["amount"] = parse_amount(val)
        elif head.startswith("القيمه بعد"):
            f["currency"] = detect_currency(val) or f.get("currency")
            f["amount_after"] = parse_amount(val)
        elif head.startswith("القيمه"):
            # «القيمة:» أو «القيمة (صافي):» بلا «قبل/بعد» = المبلغ قبل الخصم (صافي = بلا خصم)
            f["currency"] = detect_currency(val) or f.get("currency")
            f.setdefault("amount", parse_amount(val))
        elif head.startswith("السعر"):
            f["price_raw"] = val or None
        elif head.startswith("نوع التحويل"):
            f["payment"] = normalize_payment(val) or (val or None)
        elif head.startswith("الخزينه"):
            f["treasury_name"] = val or None
        elif head.startswith("المورد"):
            # SI بيع+شراء (§5/§6): «المورد: طه 5.72» → مورد الطرف الثاني وسعره.
            scode, sname, srate = _split_supplier(val)
            f["supplier_code"], f["supplier_name"], f["supplier_rate"] = scode, sname, srate
    # SI بـ«القيمة بعد الخصم» فقط بلا «القيمة قبل الخصم»: «بعد» يمثّل المبلغ الأجنبي — لا يُهمَل
    # (المبلغ=None كان يُسقط الحوالة noise §6). بلا قيمة «قبل» لا فرق خصم محسوب → تُعامَل صافيةً:
    # amount=بعد، وamount_after=None (فلا عمولة وهمية، وخزينة sell_and_buy الافتراضية = «صافي»).
    if f.get("amount") is None and f.get("amount_after") is not None:
        f["amount"] = f["amount_after"]
        f["amount_after"] = None
    return f


def _split_supplier(val: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """سطر «المورد: [كود] اسم [سعر]» → (code, name, rate).

    «طه 5.72» → (None, «طه», «5.72»)؛ «760 طه 5.72» → («760», «طه», «5.72»).
    السعر = آخر رمز رقمي؛ الكود = أول رمز أرقام بحت؛ الباقي اسم.
    """
    tokens = (val or "").split()
    if not tokens:
        return None, None, None
    rate: Optional[str] = None
    if _NUMERIC_TOKEN_RE.match(tokens[-1]):
        rate, tokens = tokens[-1], tokens[:-1]
    code: Optional[str] = None
    if tokens and tokens[0].isdigit():
        code, tokens = tokens[0], tokens[1:]
    name = " ".join(tokens).strip() or None
    return code, name, rate


# ── الصيغة A (§3.2) ──────────────────────────────────────────────────────────
def _parse_a_fields(text: str, treasuries: list[TreasuryRecord]) -> dict:
    """الصيغة A بمنهج **pattern-fishing** (§3.2 §7.3): المرابط الحاسمة تُفتَّش على **كامل النصّ**
    بلا اعتماد على ترتيب الأسطر، ثم تُصنَّف المقاطع للحقول البنيوية.

    المرابط المُفتَّشة كليًّا (`_fish_a_anchors`): الرقم الإشاري (Axxxx)، الهاتف (10-13 خانة)،
    والمبلغ المُعنون («القيمة=»/«المبلغ:»/«القيمة:»). الحقول البنيوية (كود+اسم الزبون، الخزينة،
    المبلغ+العملة الملتصقة، وسيلة الدفع، البلد) تبقى على تصنيف المقاطع لأنها تعتمد على تجاور
    الرموز داخل المقطع. وكلّ ما لا يُطابِق (نصّ حرّ/تعليمات/مدن غير معروفة) يُتجاهَل تلقائيًّا."""
    f: dict = {}
    _fish_a_anchors(text, f)                 # تفتيش كليّ للمرابط أولًا — يفوز على المقاطع (setdefault)
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        segments = [s.strip() for s in line.split("/")] if "/" in line else [line]
        is_header = len(segments) > 1
        for seg in segments:
            if seg:
                _classify_segment(seg, f, treasuries, is_header)
    return f


def _bare_amount(line: str) -> Optional[float]:
    """سطر رقميّ مجرّد (بلا حرف عربيّ) = مبلغ محتمل («50000»/«49.500»→49500 §3.5). يُستثنى مجرى
    الهاتف (10-13 خانة) فلا يُقرأ رقم الهاتف مبلغًا. يُرجع القيمة أو None."""
    if not _BARE_NUMBER_RE.match(line):
        return None
    digits = re.sub(r"\D", "", line)
    if not digits or 10 <= len(digits) <= 13:   # مجرى هاتف — ليس مبلغًا
        return None
    return parse_amount(line)


def _fish_standalone_currency_amount(text: str, f: dict) -> None:
    """عملة على **سطر مستقلّ** («مصر»/«مصري»/«تونس») + مبلغ على **سطر مجاور** («50000»/«49.500»):
    يضبط العملة (setdefault)، والمبلغ = أقرب سطر رقميّ مجرّد (التالي أولًا ثم السابق) (§3.4 §3.5)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for i, ln in enumerate(lines):
        cur = _STANDALONE_CURRENCY.get(normalize_ar(ln))
        if cur is None:
            continue
        f.setdefault("currency", cur)
        if "amount" in f:
            continue
        for j in (i + 1, i - 1):                 # المبلغ في السطر المجاور: التالي أولًا ثم السابق
            if 0 <= j < len(lines):
                amt = _bare_amount(lines[j])
                if amt is not None:
                    f["amount"] = amt
                    break


def _fish_a_anchors(text: str, f: dict) -> None:
    """يُفتّش **كامل نصّ** الرسالة الأولى (حوالة A §7.3) عن المرابط بلا اعتماد على ترتيب الأسطر:

    ١. الرقم الإشاري (Axxxx): أوّل رمز مستقلّ يطابق `_REFERENCE_RE`.
    ٢. الهاتف: أوّل مجرى أرقام 10-13 خانة (يميّزه عن الكود 2-4 والمبلغ ≤7).
    ٣. المبلغ المُعنون: «القيمة = 25000» / «المبلغ : 5000» / «القيمة: 25000» → المبلغ (+عملة إن وُجدت).
    ٤. عملة على سطر مستقلّ («مصر»/«مصري»/«تونس») + مبلغ على سطر مجاور («50000»/«49.500») → عملة+مبلغ.

    المبلغ+العملة الملتصقة («541ج») تبقى على تصنيف المقاطع (§3.5). كلّ ما لا يُطابِق يُتجاهَل."""
    for tok in re.split(r"[\s/]+", text.strip()):
        if tok and "reference" not in f and _REFERENCE_RE.match(tok):
            f["reference"] = tok
            break
    if "phone" not in f:
        # الهاتف على مستوى **السطر** لا الرمز: يبقى مجرى الأرقام متّصلًا («+218 91-...» ليبي كامل
        # فيُرفَض)، والعربية تكسره فيُعزَل عن المبلغ («01... مصر 100000» → الهاتف وحده) (§3.4).
        for line in text.splitlines():
            ph = extract_phone(line)          # مصري/تونسي، يرفض الليبي (classify_phone)
            if ph:
                f["phone"] = ph
                break

    # ٤) عملة على سطر مستقلّ + مبلغ على سطر مجاور — يُفحَص قبل حارس «amount» كي تُضبط العملة دومًا.
    _fish_standalone_currency_amount(text, f)

    if "amount" in f:
        return
    for line in text.splitlines():
        label = _split_label(line.strip())
        if label is None:
            continue
        head = normalize_ar(label[0])
        if any(head.startswith(lb) for lb in _AMOUNT_LABELS):
            amt = parse_amount(label[1])
            if amt is not None:
                cur = detect_currency(label[1])
                if cur is not None:
                    f.setdefault("currency", cur)
                f["amount"] = amt
                return

    # fallback: مبلغ ملصق بالعملة بلا مسافة («50000مصر»/«مصر50000»/«50000م.ج»/«50000مج») — «مصر»
    # وحدها (لا «مصري») لا يلتقطها detect_currency. يُفحَص **بعد** النمط أعلاه فقط، ولا يغيّره (§3.5).
    m = re.search(r"(\d[\d,]*)(?:مصري|مصر|م\.ج|مج)", text) or re.search(r"(?:مصري|مصر)([\d,]+)", text)
    if m:
        amt = parse_amount(m.group(1))
        if amt is not None:
            f.setdefault("currency", Currency.EGP)
            f.setdefault("amount", amt)


def _add_note(f: dict, text: str) -> None:
    """يُراكم نصًّا حرًّا (مؤشّر خصم/ملاحظة) في حقل الملاحظات (§3.2 خانة الملاحظات §11.1)."""
    seg = (text or "").strip()
    if not seg:
        return
    prev = f.get("notes")
    f["notes"] = f"{prev} {seg}".strip() if prev else seg


def _is_discount_indicator(n: str) -> bool:
    """
    مؤشّر نوع الخصم (§3.2 §6.1): «بدون خصم / صافي / خصم 1%» — ملاحظة على الخصم لا خزينة وجهة.
    🔴 تصحيح (بلاغ A11–A16): «صافي» كلمة مزدوجة (مؤشّر «بلا خصم» + اسم خزينة معلّقة). في نصّ
    الحوالة هي مؤشّر خصم → تُوجَّه إلى الملاحظات ولا تُحلّ خزينةً — فالخزينة الحقيقية تأتي صريحة
    (بلس/وليد/طلال…) أو من الرسالة الثانية، وخزينة «صافي/خصم1%» في الطرفين تُسنَد حسابيًّا (§6.1).
    """
    if "بدون" in n or "دون خصم" in n:                # «بدون خصم» / «دون خصم»
        return True
    compact = n.replace(" ", "").replace("%", "")
    return compact in {"صافي", "صافى", "خصم", "خصم1"}


# مؤشّر المنطقة → عملة (لالتقاط مبلغ بمقطع فيه هاتف/نص بلا رمز عملة صريح — #3)
_REGION_CURRENCY = {"مصر": Currency.EGP, "تونس": Currency.TND}


def _amount_beside_phone(seg: str, phone: Optional[str], f: dict) -> None:
    """#3: مقطع يحوي هاتفًا + مبلغ (+ مؤشّر منطقة/عملة) → يلتقط المبلغ والعملة.

    مثال «01064074568 مصر 100000»: الهاتف يُلتقط في الخطوة 4، والمبلغ 100000 هنا.
    لا يُفعَّل إلا بوجود عملة صريحة أو مؤشّر منطقة (مصر/تونس) لتفادي التقاط أرقام عابرة.
    """
    if "amount" in f:
        return
    cur = detect_currency(seg)
    if cur is None:
        tokens = set(normalize_ar(seg).split())
        for word, c in _REGION_CURRENCY.items():
            if word in tokens:
                cur = c
                break
    if cur is None:
        return
    rest = seg.replace(phone, " ") if phone else seg
    amt = parse_amount(rest)
    if amt is not None:
        f.setdefault("currency", cur)
        f.setdefault("amount", amt)


def _classify_segment(seg: str, f: dict, treasuries: list[TreasuryRecord], is_header: bool) -> None:
    """يصنّف سطرًا/جزءًا في الصيغة A ويملأ الحقول (§3.2)."""
    n = normalize_ar(seg)

    # ضجيج مرفق (صورة: «127 كيلوبايت») → يُتجاهَل مبكّرًا كي لا يُقرأ «127» كودًا و«كيلوبايت» اسمًا.
    if any(w in n for w in _ATTACHMENT_NOISE):
        return

    # 0) سطر معنون بالاسم في صيغة A (رسالة تونسية أولى بأسطر معنونة بلا «/»):
    #    «الاسم: محمد عبدالرحيم» / «الاسم = محمد» → اسم المستلم. «الهاتف»/«القيمة» تُلتقط في
    #    الخطوات 3/4؛ هذه الخطوة تلتقط الاسم فقط (لا تجعل الرسالة SI — «الاسم» ليست من _SI_LABELS).
    label = _split_label(seg)
    if label is not None:
        head, val = label
        if normalize_ar(head) in ("الاسم", "اسم") and val.strip():
            f.setdefault("recipient_name", val.strip())
            return

    # 1) الرقم الإشاري (A6xxx/A5xxx/SIxxxx)
    if _REFERENCE_RE.match(seg.replace(" ", "")):
        f.setdefault("reference", seg.replace(" ", ""))
        return

    # 2) مؤشّر الخصم (بدون خصم / صافي / خصم 1%) — ملاحظة لا خزينة وجهة (§3.2 §6.1) → notes.
    #    يُفحص قبل حلّ الخزينة كي لا تُلتقط «صافي» خزينةً معلّقة تحجب الخزينة الحقيقية.
    if _is_discount_indicator(n):
        _add_note(f, seg)
        return

    # 3) المبلغ + العملة (§3.5) — يُفحص قبل الهاتف/الزبون لتجنّب الالتباس
    if detect_currency(seg) or n.startswith("القيمه") or n.startswith("المبلغ"):
        cur = detect_currency(seg)
        if cur:
            f.setdefault("currency", cur)
        amt = parse_amount(seg)
        if amt is not None:
            f.setdefault("amount", amt)
        return

    # 4) رقم الهاتف (مجرى أرقام طويل) — وقد يصاحبه مبلغ في نفس المقطع (#3)
    if is_phone_like(seg):
        ph = extract_phone(seg)
        if ph:
            f.setdefault("phone", ph)
        _amount_beside_phone(seg, ph, f)   # «01064074568 مصر 100000» → المبلغ 100000
        return

    # 5) سطر الزبون/المورد: كود + اسم + [سعر] (§3.2)
    cust = _parse_customer_line(seg)
    if cust:
        code, name, price = cust
        f.setdefault("customer_code", code)
        f.setdefault("customer_name", name)
        if price is not None:
            f.setdefault("price_raw", price)
        return

    # 5.5) سطر تعليمات بشري («ارجو تحويل انستا باي») → يُتجاهَل. يُفحص **بعد** الزبون/المبلغ
    #      (فلا يُسقط بيانات) و**قبل** الدفع (فلا يُلتقط «انستا باي» في جملة طلب) (§7.2).
    if _is_request_noise(seg):
        return

    # 6) وسيلة الدفع (§3.4)
    pay = normalize_payment(seg)
    if pay:
        f.setdefault("payment", pay)
        return

    # 7) البلد/المدينة (§11.1)
    if n in _CITIES:
        f["country"] = _CITIES[n]     # المدينة تتقدّم على البلد
        return
    if n in _COUNTRIES:
        f.setdefault("country", _COUNTRIES[n])
        return

    # 8) الخزينة (§4.5) — مطابقة تامّة، أول تطابق فقط. تُجرَّب **قبل** «اسم المستلم»
    #    كي تُلتقط في سطر الترويسة «/» أيضًا (مثل «… / بلس») ولا تُبتلع كاسم مستلم.
    #    exact-match (§0 #2) يمنع false-positive: اسم مستلم حقيقي لا يُطابِق خزينة فيسقط للخطوة 9.
    #    (مؤشّرات الخصم «صافي/خصم» عُولجت في الخطوة 2 فلا تصل هنا.)
    if "treasury_record" not in f:
        rec = resolve_treasury(seg, treasuries)
        if rec is not None:
            f["treasury_record"] = rec
            return

    # 9) في سطر الترويسة (/): نص عربي غير مصنّف = اسم المستلم
    if is_header and _ARABIC_RE.search(seg):
        f.setdefault("recipient_name", seg.strip())
        return


def _parse_customer_line(seg: str) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """سطر الزبون: `كود + اسم + [سعر]`. يُرجع (code, name, price) أو None.

    الكود قد يكون ملصقًا بالاسم بلا مسافة («1188زبون عام») — الفراغ اختياري، والاسم يبدأ **بحرف
    عربيّ** (§3.2) فلا يُلتقط سطر مبلغ عشريّ «8.391» كزبون. الهاتف يُرفَض (الحدّ 6 خانات)."""
    m = re.match(r"^(\d{1,6})\s*([ء-ي].*)$", seg)
    if not m:
        return None
    code, rest = m.group(1), m.group(2).strip()
    if not _ARABIC_RE.search(rest):   # لا بد من اسم عربي (تمييزه عن مبلغ/هاتف)
        return None
    tokens = rest.split()
    price: Optional[str] = None
    if tokens and _NUMERIC_TOKEN_RE.match(tokens[-1]):
        price = tokens[-1].replace("،", ".")   # الفاصلة العربية → عشرية (5،82 → 5.82 §3.6)
        tokens = tokens[:-1]
    name = " ".join(tokens).strip()
    if not name:
        return None
    return code, name, price


def _parse_name_price_supplier(
    seg: str, suppliers: list[SupplierRecord]
) -> Optional[tuple[str, str, Optional[str]]]:
    """سطر «اسم [سعر]» **بلا كود رقمي** («مومن عريبي 5.90») → يُطابَق موردًا مسجّلًا فيصير
    (كود المورد من db، اسمه، السعر). يمكّن Path A من ربط سطر مورد بلا كود بالقائمة البيضاء
    (§5.4 §7.3). المطابقة عبر resolve_supplier الصارمة (§5.4) فلا يُطابَق نصّ حرّ/مدينة/خزينة."""
    tokens = seg.split()
    if not tokens:
        return None
    price: Optional[str] = None
    if _NUMERIC_TOKEN_RE.match(tokens[-1]):
        price = tokens[-1].replace("،", ".")     # الفاصلة العربية → عشرية (§3.6)
        tokens = tokens[:-1]
    name = " ".join(tokens).strip()
    if not name or not _ARABIC_RE.search(name):
        return None
    srec = resolve_supplier(name, suppliers)
    if srec is None or not srec.code:            # مورد مسجّل بكود فقط (بلا كود → لا يُكتب §0)
        return None
    return srec.code, srec.name, price


def extract_code_name_price_lines(
    text: str, suppliers: Optional[list[SupplierRecord]] = None,
) -> list[tuple[str, str, Optional[str]]]:
    """يستخرج أسطر «كود + اسم + [سعر]» من النصّ — للرسالة الثانية بلا رقم إشاري (سطر زبون ثم سطر
    مورد §7.3). المقاطع تُفصَل بسطر جديد **أو بـ«/»** («1300 عبد الله 5.82 / 760 طه 5.90» = سطران:
    زبون + مورد). يتجاهل المقاطع غير المطابقة (هاتف/عملة/خزينة…).

    🔴 سطر المورد قد يأتي **بلا كود** («مومن عريبي 5.90»): عند تمرير `suppliers` يُطابَق بالاسم
    في القائمة البيضاء فيُستكمَل كوده من db (§5.4) — فيصير الزوج صالحًا وPath A يعمل. بلا
    `suppliers` (السلوك الأصلي) تُقبَل الأسطر ذات الكود الرقمي فقط."""
    pairs: list[tuple[str, str, Optional[str]]] = []
    for raw_line in normalize_digits(text or "").splitlines():
        for ln in raw_line.split("/"):       # «/» فاصل مقاطع كالسطر الجديد (§7.3)
            ln = ln.strip()
            if not ln or detect_currency(ln):   # مقطع مبلغ (فيه رمز عملة) → ليس سطر زبون
                continue
            r = _parse_customer_line(ln)
            if r is not None and r[0] and r[1]:   # كود رقمي + اسم (السعر اختياري)
                pairs.append(r)
                continue
            if suppliers:                        # بلا كود: طابِق موردًا مسجّلًا بالاسم (كوده من db)
                s = _parse_name_price_supplier(ln, suppliers)
                if s is not None:
                    pairs.append(s)
    return pairs


def _extract_code_name(val: str) -> tuple[Optional[str], Optional[str]]:
    """يستخرج الكود والاسم من نص الزبون: «مروان الشاوش كود 1284» → (1284, مروان الشاوش).

    الفاصل بعد «كود» اختياري ومتعدّد الأشكال: فراغ/نقطة/نقطتان/شرطة أو بلا فاصل —
    «كود 1284» / «كود1201» / «حميد بن غارات كود.793» كلها تُلتقط (§3.2).
    والكود قد يأتي في **نهاية** الاسم بلا كلمة «كود»: «عبد القادر حبيب 769» → (769, «عبد القادر حبيب»)،
    أو الكود يسبق كلمة «كود» في النهاية: «عبد القادر حبيب 769 كود» → (769, «عبد القادر حبيب»)."""
    # الكود يسبق كلمة «كود» الختامية («عبد القادر حبيب 769 كود») — يُفحص أولًا كي لا يُطابَق
    # «769» رقمًا عابرًا. يُشترط اسم عربيّ قبله (تمييزه عن رقم/هاتف).
    m0 = re.match(r"^(.+?)\s+(\d+)\s+كود\s*$", val.strip())
    if m0 and _ARABIC_RE.search(m0.group(1)):
        return m0.group(2), m0.group(1).strip()
    code = None
    m = re.search(r"كود[\s.:\-]*(\d+)", val)
    if m:
        code = m.group(1)
        name = re.sub(r"كود[\s.:\-]*\d+", "", val).strip()
    else:
        name = val.strip()
        m2 = re.match(r"^(\d+)\s+(.+)$", name)      # كود في البداية
        if m2:
            code, name = m2.group(1), m2.group(2).strip()
        else:
            # الكود في النهاية بلا كلمة «كود» («عبد القادر حبيب 769»)، وقد يتبعه نقطة/فاصلة
            # («الوروار 1298.») — يُشترط أن يبدأ المقطع باسم عربيّ كي لا يُلتقط هاتفٌ/رقم عابر.
            m3 = re.match(r"^(.+?)\s+(\d+)[.،]?$", name)
            if m3 and _ARABIC_RE.search(m3.group(1)):
                name, code = m3.group(1).strip(), m3.group(2)
    return code, (name or None)


# ═════════════════════════════════════════════════════════════════════════════
# الرسالة الثانية (completion_fragment) — منهج pattern-fishing (§7.3)
# ═════════════════════════════════════════════════════════════════════════════
# بدل التحليل سطرًا-بسطر (يفشل حين يختلف الترتيب: الكود آخر السطر، السعر بسطر مستقلّ…)
# نُفتّش النصّ كلّه عن الأنماط بلا اعتماد على الترتيب.
_FRAGMENT_CURRENCY = {"تونس": Currency.TND, "تونسي": Currency.TND,
                      "مصر": Currency.EGP, "مصري": Currency.EGP}
_DECIMAL_RE = re.compile(r"^\d+[.,،]\d+$")     # رقم عشري (سعر): فيه فاصلة عشرية
_CODE_RE = re.compile(r"^\d{2,4}$")            # كود الزبون: صحيح 2-4 خانات
_GLUED_NAME_NUM_RE = re.compile(r"^(.*[ء-ي])(\d+(?:[.,،]\d+)?)$")   # «قريش35.5»→(«قريش»,«35.5»)
_GLUED_CODE_NAME_RE = re.compile(r"^(\d{2,4})([ء-ي].*)$")           # «728معتصم»→(«728»,«معتصم»)


def _split_glued_name_number(tok: str) -> list[str]:
    """يفصل رقمًا ملصقًا باسم عربيّ في الرسالة الثانية (§3.2):
    - سعر بنهاية الاسم: «قريش35.5»→[«قريش»,«35.5»].
    - كود ببداية الاسم: «728معتصم»→[«728»,«معتصم»].
    ثم يُقشَّر رقم بنقطة/فاصلة ختامية («1298.»→«1298»)."""
    tok = tok.rstrip(".،")                              # «1298.»→«1298» (نقطة ختامية §3.2)
    m = _GLUED_NAME_NUM_RE.match(tok)
    if m:
        return [m.group(1), m.group(2)]
    m2 = _GLUED_CODE_NAME_RE.match(tok)
    if m2:
        return [m2.group(1), m2.group(2)]
    return [tok] if tok else []


def _fish_treasury_from_tokens(
    tokens: list[str], treasuries: list[TreasuryRecord]
) -> tuple[Optional[TreasuryRecord], list[str]]:
    """خزينة متعدّدة الكلمات مدموجة مع بيانات: نوافذ متجاورة (3 ثم 2 كلمة) بلا أرقام، مطابقة
    تامّة (§0). يُرجع (السجلّ أو None، والرموز بعد إزالة المطابقة)."""
    for size in (3, 2):
        for i in range(len(tokens) - size + 1):
            window = tokens[i:i + size]
            if any(_NUMERIC_TOKEN_RE.match(t) for t in window):
                continue
            rec = resolve_treasury(" ".join(window), treasuries)
            if rec is not None:
                return rec, tokens[:i] + tokens[i + size:]
    return None, tokens


def parse_completion_fragment(
    text: str, treasuries: list[TreasuryRecord], suppliers: list[SupplierRecord],
) -> ParsedLeg:
    """يحلّل الرسالة الثانية (completion_fragment §7.3) بـ **pattern-fishing**: تفتيش كامل النصّ
    عن الأنماط بلا اعتماد على ترتيب الأسطر — أمتن للصيغ المتنوّعة من التحليل سطرًا-بسطر.

    يلتقط: كود الزبون (2-4 خانات مستقلّة)، الاسم (كلمات عربية متبقّية)، السعر (أصغر رقم عشري —
    السعر لا المبلغ)، **المبلغ** (سطر فيه رمز عملة ج.م/د.ت — يميّزه عن الكود)، الخزينة
    (resolve_treasury التامّة)، الهاتف، والعملة. وكلّ ما لا يُطابِق (فودافون كاش/بنك/…) يُتجاهَل.
    """
    # «/» فاصل مقاطع كالسطر الجديد — رسالة ثانية على سطر واحد («… / طه 5.90») تُفكَّك سليمة
    text = normalize_digits(text or "")     # أرقام عربية-هندية → لاتينية (§3.5)
    lines = [ln.strip() for ln in text.replace("/", "\n").splitlines() if ln.strip()]
    currency: Optional[Currency] = None
    treasury_rec: Optional[TreasuryRecord] = None
    amount: Optional[float] = None
    residual: list[str] = []                    # سطور غير مصنّفة → تُفتَّش على مستوى الرمز

    for ln in lines:
        n = normalize_ar(ln)
        if normalize_payment(ln) or _is_discount_indicator(n) or "بنك" in n or _is_request_noise(ln):
            continue                             # وسيلة دفع/مؤشّر خصم/بنك/طلب بشري → يُتجاهَل
        if n in _FRAGMENT_CURRENCY:              # «تونس/مصر» سطرًا كاملًا → عملة
            currency = currency or _FRAGMENT_CURRENCY[n]
            continue
        cur = detect_currency(ln)                # سطر مبلغ (فيه رمز عملة ج.م/د.ت) → المبلغ + العملة
        if cur is not None:                      #   (يميّز المبلغ عن الكود، ويمنع تلويث الاسم بـ«ج م»)
            currency = currency or cur
            if amount is None:
                amount = parse_amount(ln)
            continue
        if treasury_rec is None:                 # خزينة سطر-كامل (مطابقة تامّة — آمنة §0)
            rec = resolve_treasury(ln, treasuries)
            if rec is not None:
                treasury_rec = rec
                continue
        residual.append(ln)

    tokens = " ".join(residual).split()
    if treasury_rec is None:                     # خزينة inline مدموجة (احتياطي)
        treasury_rec, tokens = _fish_treasury_from_tokens(tokens, treasuries)
    tokens = [t for tok in tokens for t in _split_glued_name_number(tok)]  # «قريش35.5»→«قريش»,«35.5»

    prices: list[str] = []
    code: Optional[str] = None
    code_idx = -1
    last_name_idx = -1
    phone: Optional[str] = None
    name_tokens: list[str] = []
    for i, tok in enumerate(tokens):
        ph = classify_phone(tok)
        if ph:                                   # هاتف مستلم (مصري/تونسي، يرفض الليبي)
            phone = phone or ph
        elif _DECIMAL_RE.match(tok):             # رقم عشري = سعر
            prices.append(tok)
        elif _CODE_RE.match(tok):                # 2-4 خانات = كود الزبون (قد يُعاد تفسيره سعرًا أدناه)
            if code is None:
                code, code_idx = tok, i
        elif _NUMERIC_TOKEN_RE.match(tok):       # عدد آخر (مبلغ/رقم طويل) → يُتجاهَل
            continue
        else:
            ntok = normalize_ar(tok)
            if ntok in _FRAGMENT_CURRENCY:
                currency = currency or _FRAGMENT_CURRENCY[ntok]
            elif "بنك" not in ntok and _ARABIC_RE.search(tok):
                name_tokens.append(tok)          # كلمة عربية = جزء من الاسم
                last_name_idx = i

    # عدد صحيح 2-خانة يقع **بعد** الاسم (لا قبله) + عملة/خزينة تونسية + بلا سعر عشريّ = **سعر تونسي
    # صحيح** لا كود («محمد عاشور 35 / فتحي»→سعر 35). «53 احمد» الكود قبل الاسم فيبقى كودًا (§3.6).
    tnd = (currency == Currency.TND) or (treasury_rec is not None and treasury_rec.currency == Currency.TND)
    if (not prices and tnd and name_tokens and code is not None
            and len(code) == 2 and code_idx > last_name_idx >= 0):
        prices.append(code)
        code = None

    # السعر = أصغر رقم عشري (السعر لا المبلغ) — الفاصلة العربية «،» → عشرية (§3.6)
    price_raw = (
        min(prices, key=lambda p: float(p.replace("،", ".").replace(",", "."))).replace("،", ".")
        if prices else None
    )
    name = " ".join(name_tokens).strip() or None
    if currency is None and treasury_rec is not None:
        currency = treasury_rec.currency
    _raw, price_norm = normalize_price(price_raw, currency or Currency.EGP)

    tref: Optional[TreasuryRef] = None
    if treasury_rec is not None:
        tref = TreasuryRef(code=treasury_rec.code, name=treasury_rec.name,
                           type=treasury_rec.type, currency=treasury_rec.currency or currency)

    supplier_ref: Optional[SupplierRef] = None   # مورد؟ (نحافظ على مسار رد المورد §5.3)
    is_supplier = False
    if name:
        srec = resolve_supplier(name, suppliers)
        if srec is not None:
            supplier_ref = SupplierRef(code=srec.code, name=srec.name)
            is_supplier = True

    return ParsedLeg(
        operation=OperationType.SELL,
        customer_code=code,
        customer_name=name,
        supplier=supplier_ref,
        is_supplier_counterpart=is_supplier,
        amount=amount,
        price_raw=price_raw,
        price_normalized=price_norm,
        treasury=tref,
        phone=phone,
        currency=currency,
    )


# ═════════════════════════════════════════════════════════════════════════════
# بناء الطرف (leg) — نوع العملية §5، السعر §3.6، الخزينة §4
# ═════════════════════════════════════════════════════════════════════════════
def _build_leg(
    f: dict, full_text: str, treasuries: list[TreasuryRecord], suppliers: list[SupplierRecord],
    is_si: bool = False,
) -> tuple[Optional[ParsedLeg], float]:
    if not f:
        return None, 0.0

    currency: Optional[Currency] = f.get("currency")

    # الخزينة (§4) — من سطر A أو من عنوان SI. «صافي/خصم1%/بدون خصم» ليست خزينة وجهة بل
    # مؤشّر «المبلغ صافي/نوع الخصم» → تُتجاهَل عند البحث عن الخزينة (كأسماء المدن) فتبقى
    # treasury=None وتُعامَل كملاحظة (§3.2 §6.1). خزينة الطرفين الحسابية تُسنَد لاحقًا لا من هنا.
    trec: Optional[TreasuryRecord] = f.get("treasury_record")
    tname = f.get("treasury_name")
    if trec is None and tname and not _is_discount_indicator(tname):
        trec = resolve_treasury(tname, treasuries)
    tref: Optional[TreasuryRef] = None
    if trec is not None:
        if currency is None:
            currency = trec.currency
        tref = TreasuryRef(
            code=trec.code, name=trec.name, type=trec.type,
            currency=trec.currency or currency,
        )

    # نوع العملية (§5) — الأصل بيع؛ الكلمة الصريحة تحكم؛ ثم تمييز الطرف (§5.3)
    op, explicit = detect_explicit_operation(full_text)
    if op is None:
        op = OperationType.SELL
    supplier_ref: Optional[SupplierRef] = None
    is_supplier = False
    name = f.get("customer_name")
    if name:
        srec = resolve_supplier(name, suppliers)
        if srec is not None:
            supplier_ref = SupplierRef(code=srec.code, name=srec.name)
            is_supplier = True
            if not explicit:                 # §5.3: الطرف مورد ⇒ شراء
                op = OperationType.BUY

    # SI بيع+شراء (§5/§6): سطر «المورد: طه 5.72» → الزبون طرف بيع، والمورد طرف الشراء.
    # الزبون يبقى بيعًا (op=SELL، is_supplier=False)؛ المورد يُحمَل في leg.supplier ليُشتقّ منه
    # طرف الشراء لاحقًا (pipeline._maybe_synthesize_buy_leg). لو لم تُذكَر «الخزينة:» صراحةً →
    # خزينة sell_and_buy افتراضيًا (خصم1% عند وجود «بعد الخصم»، وإلا «صافي») ليُخلَّق الطرفان (§6.1).
    supplier_name_si = f.get("supplier_name")
    if is_si and supplier_name_si:
        srec2 = resolve_supplier(supplier_name_si, suppliers)
        supplier_ref = (
            SupplierRef(code=srec2.code, name=srec2.name) if srec2 is not None
            else SupplierRef(code=f.get("supplier_code"), name=supplier_name_si)
        )
        if tref is None:                     # بلا «الخزينة:» → خزينة sell_and_buy افتراضية (§6.1)
            default_tname = "خصم 1%" if f.get("amount_after") is not None else "صافي"
            trec_sab = resolve_treasury(default_tname, treasuries)
            if trec_sab is not None:
                if currency is None:
                    currency = trec_sab.currency
                tref = TreasuryRef(
                    code=trec_sab.code, name=trec_sab.name, type=trec_sab.type,
                    currency=trec_sab.currency or currency,
                )

    # السعر حسب العملة (§3.6)
    price_raw, price_norm = normalize_price(f.get("price_raw"), currency or Currency.EGP)

    # ينتظر طرفًا ثانيًا؟ (§5.3): الطرف «شراء» فقط (مورد/كلمة صريحة).
    # 🔴 أُلغِيت قاعدة «خزينة sell_and_buy ⇒ طرفان» — كانت تجعل بيعًا مفردًا على «صافي»
    # ينتظر شراءً ويُصعَّد بلا داعٍ. التمييز الصحيح = وصول رسالة ثانية بمورد بنفس الـref
    # (يُدمَج في try_group)؛ والبيع المفرد يُنهى كطرف واحد عند انتهاء المهلة (لا تصعيد).
    expects_pair = op == OperationType.BUY

    # العمولة للطرف المفرد ذي القيمتين الصريحتين (صيغة SI بخصم §6.2): بعد − قبل (سالبة).
    # 🔴 الطرفان (A) وAخصم من رسالتين: amount_after هنا None (يصلان لاحقًا) → تُحسب عمولتهما
    #    في _resolve_two_leg / _merge_discount. لذا هذا يخصّ SI المعنونة بقيمتين حصرًا.
    amount = f.get("amount")
    amount_after = f.get("amount_after")
    commission = (
        round(amount_after - amount, 2)
        if amount_after is not None and amount is not None
        else None
    )

    leg = ParsedLeg(
        operation=op,
        customer_code=f.get("customer_code"),
        customer_name=name,
        supplier=supplier_ref,
        supplier_price_raw=f.get("supplier_rate"),
        price_raw=price_raw,
        price_normalized=price_norm,
        amount=amount,
        currency=currency,
        treasury=tref,
        reference_number=f.get("reference"),
        phone=f.get("phone"),
        payment_method=f.get("payment"),
        recipient_name=f.get("recipient_name"),
        notes=f.get("notes"),
        country=f.get("country"),
        amount_after_discount=amount_after,
        commission=commission,
        expects_pair=expects_pair,
        explicit_operation=explicit,
        is_supplier_counterpart=is_supplier,
        is_si_format=is_si,
    )
    return leg, _confidence(leg, tref, currency)


def _confidence(leg: ParsedLeg, tref: Optional[TreasuryRef], currency: Optional[Currency]) -> float:
    """ثقة تعكس الشكّ (القاعدة الذهبية §0): تنخفض عند نقص عنصر حرج."""
    conf = 0.95
    if leg.customer_code is None or leg.amount is None:
        conf = min(conf, 0.4)
    if currency is None:
        conf = min(conf, 0.6)
    # خزينة غير محلولة وليس طرف مورد (قد تُحسم لاحقًا §6) → شكّ أخفّ
    if tref is None and not leg.is_supplier_counterpart:
        conf = min(conf, 0.75)
    return conf
