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
    detect_currency,
    extract_phone,
    is_phone_like,
    normalize_ar,
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

    # خارج النطاق (§0): تسليم يدوي/باليد → أولوية فوق «حوالة» → يُصعَّد لا يُدخَل.
    if is_out_of_scope(text):
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


def _looks_like_si(text: str) -> bool:
    """SI إن كان سطران على الأقل يبدآن بعنوان معروف متبوعًا بنقطتين (§3.3)."""
    count = 0
    for line in text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        head = normalize_ar(line.split(":", 1)[0])
        if any(head.startswith(lb) for lb in _SI_LABELS):
            count += 1
    return count >= 2


# ── الصيغة SI (§3.3) ─────────────────────────────────────────────────────────
def _parse_si_fields(text: str) -> dict:
    f: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        raw_head, val = line.split(":", 1)
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
    f: dict = {}
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
    if "بدون" in n:                                  # «بدون خصم»
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

    # 0) سطر معنون بالاسم في صيغة A (رسالة تونسية أولى بأسطر معنونة بلا «/»):
    #    «الاسم: محمد عبدالرحيم» → اسم المستلم. «الهاتف:»/«القيمة:» تُلتقط في الخطوات 3/4
    #    كما هي؛ هذه الخطوة تلتقط الاسم فقط (لا تجعل الرسالة SI — «الاسم» ليست من _SI_LABELS).
    if ":" in seg:
        head, _, val = seg.partition(":")
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
    if detect_currency(seg) or n.startswith("القيمه"):
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
    """سطر الزبون: `كود + اسم + [سعر]`. يُرجع (code, name, price) أو None."""
    m = re.match(r"^(\d{1,6})\s+(.+)$", seg)
    if not m:
        return None
    code, rest = m.group(1), m.group(2).strip()
    if not _ARABIC_RE.search(rest):   # لا بد من اسم عربي (تمييزه عن مبلغ/هاتف)
        return None
    tokens = rest.split()
    price: Optional[str] = None
    if tokens and _NUMERIC_TOKEN_RE.match(tokens[-1]):
        price = tokens[-1]
        tokens = tokens[:-1]
    name = " ".join(tokens).strip()
    if not name:
        return None
    return code, name, price


def _extract_code_name(val: str) -> tuple[Optional[str], Optional[str]]:
    """يستخرج الكود والاسم من نص الزبون: «مروان الشاوش كود 1284» → (1284, مروان الشاوش)."""
    code = None
    m = re.search(r"كود\s*(\d+)", val)
    if m:
        code = m.group(1)
        name = re.sub(r"كود\s*\d+", "", val).strip()
    else:
        name = val.strip()
        m2 = re.match(r"^(\d+)\s+(.+)$", name)      # كود في البداية
        if m2:
            code, name = m2.group(1), m2.group(2).strip()
    return code, (name or None)


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

    # الخزينة (§4) — من سطر A أو من عنوان SI
    trec: Optional[TreasuryRecord] = f.get("treasury_record")
    if trec is None and f.get("treasury_name"):
        trec = resolve_treasury(f["treasury_name"], treasuries)
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
