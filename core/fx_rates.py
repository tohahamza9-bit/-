"""
نظام الأسعار — قراءة غرفتَي الأسعار + تخزين لقطات fx_rates_egp/tnd بنوافذ صلاحية ميلي.
docs/FX_RATES_SPEC.md — الخطوة ٢ (بوّابة + تحليل + تسجيل). **لا ربط بالـpipeline في هذه الخطوة**:
`ingest_price_message` دالّة نقيّة جاهزة للاستدعاء، لا تُستدعى تلقائيًّا بعد (الربط مسيّج بـfx_rates_enabled
في خطوة لاحقة). لا تلمس هذه الوحدة أيّ منطق matching/writer/cancellation/amendment/parser/pipeline.

المحلّل (parse_price_message) يقرأ قالبًا يحدّده المالك من الداشبورد (FxRatesConfig.*_price_template)؛
قالب فارغ ⇒ محلّل مرن افتراضيّ بقوائم كلمات §3 (قنوات مصر / مدن تونس).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .constants import Currency
from .models import FxRateSnapshot, FxRatesConfig, RawMessage

log = logging.getLogger(__name__)


# ── محلّل افتراضيّ (fallback) — قنوات مصر بمفاتيح عربية معياريّة حين لا قالب من الداشبورد ───
# كل عنصر: (كلمات مفتاحية، المفتاح المعياريّ العربيّ). المطابقة بالاحتواء بعد إزالة المسافات/النقاط
# («فود فون»→«فودفون» يطابق «فود»). تونس: المدن مفتوحة — تُستخرَج تسمياتها العربية مباشرةً (لا قائمة).
FALLBACK_EGP: list[tuple[list[str], str]] = [
    (["فود", "vodafone"], "فودافون"),      # فودافون/فودا فون/فود فون
    (["انستا", "insta"], "انستا"),          # انستا/انستاباي
    (["بنك", "bank"], "بنك"),
    (["بريد", "post"], "بريد"),
]

# تطبيع الأرقام العربية/الشرقية → ASCII (محليّ ومستقلّ — لا يمسّ core.parsing.normalize)
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")

# رقم عشريّ (قد يحوي فاصلتين كصيغة تونس X.XX.CC): تُدمَج الكسور بعد أوّل فاصل.
_NUM_RE = re.compile(r"\d+(?:[.,،]\d+)*")
# شريحة مبلغ: «تحت/فوق N [ألف]». «ألف» تُوسِّع ×1000؛ ومصر بلا «ألف» (N<1000) تُعتبَر بالآلاف (الحدّ الألفيّ).
_TIER_RE = re.compile(r"(تحت|فوق)\s*(\d+)\s*(الاف|آلاف|[أاآ]لاف|[أاآ]لف|الف)?")


def _to_currency(currency) -> Currency:
    """يقبل Currency أو نصًّا ('EGP'/'TND') ويُرجِع Currency."""
    if isinstance(currency, Currency):
        return currency
    return Currency(str(currency).upper())


def _compact(s: str) -> str:
    """للمطابقة المرنة للأسماء: إزالة المسافات والنقاط والفواصل الشائعة («فود فون»→«فودفون»)."""
    return re.sub(r"[\s.:/*،]", "", s or "")


def _normalize_number(tok: str) -> Optional[float]:
    """رقم من نصّ. صيغة تونس X.XX.CC (فاصلتان) → دمج الكسور بعد أوّل فاصل («0.34.50»→0.345،
    «0.35.00»→0.35). رقم بفاصل واحد أو بلا فاصل → عاديّ."""
    parts = [p for p in tok.replace("،", ".").replace(",", ".").split(".") if p != ""]
    if not parts:
        return None
    text = parts[0] if len(parts) == 1 else parts[0] + "." + "".join(parts[1:])
    try:
        return float(text)
    except ValueError:
        return None


def _extract_numbers(line: str) -> list[float]:
    """كل الأرقام العشرية في سطر (بعد تطبيع الأرقام العربية ودمج صيغة X.XX.CC)."""
    norm = line.translate(_AR_DIGITS)
    out: list[float] = []
    for tok in _NUM_RE.findall(norm):
        v = _normalize_number(tok)
        if v is not None:
            out.append(v)
    return out


def _parse_template(template: str) -> list[tuple[list[str], str]]:
    """قالب المالك → قائمة (كلمات مفتاحية، مفتاح). صيغة السطر: «كلمة, كلمة = المفتاح».
    أسطر بلا «=» تُتجاهَل. قالب فارغ ⇒ قائمة فارغة (يستدعي المُنادي الـfallback)."""
    entries: list[tuple[list[str], str]] = []
    for raw in (template or "").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        left, key = line.split("=", 1)
        key = key.strip()
        kws = [k.strip() for part in left.split(",") for k in part.split("،") if k.strip()]
        if key and kws:
            entries.append((kws, key))
    return entries


def _extract_tier(norm: str, cur: Currency) -> tuple[Optional[str], str]:
    """يستخرج شريحة «تحت/فوق N» ويُرجِع (وسمها، السطر بعد حذف عبارتها). مصر: N<1000 تُعتبَر بالآلاف."""
    m = _TIER_RE.search(norm)
    if not m:
        return None, norm
    direction, n, alf = m.group(1), int(m.group(2)), m.group(3)
    if alf or (cur is Currency.EGP and n < 1000):
        n *= 1000
    tier = f"{direction}_{n}"
    return tier, norm[:m.start()] + " " + norm[m.end():]


def _kind(norm: str, cur: Currency) -> str:
    """net أم gross؟ تونس: net دائمًا (§7). مصر: «بدون خصم»/«صافي»→net؛ «خصم»/«%»/«٪»→gross؛ وإلا net."""
    if cur is not Currency.EGP:
        return "net"
    if "بدون خصم" in norm or "بدون عمولة" in norm or "بدون عموله" in norm or "صافي" in norm:
        return "net"
    if "خصم" in norm or "%" in norm or "٪" in norm:
        return "gross"
    return "net"


def _generic_city_key(norm: str) -> Optional[str]:
    """تونس: اسم المدينة = النصّ قبل أوّل رقم (مدن مفتوحة، مفاتيح عربية). المسافات → «_»."""
    m = re.search(r"\d", norm)
    label = re.sub(r"\s+", "_", (norm[:m.start()] if m else norm).strip(" :/*-\t.،").strip())
    return label or None


def _resolve_key(norm: str, entries: list, cur: Currency, tier: Optional[str]) -> Optional[str]:
    """مفتاح القناة/المدينة: القالب أولًا، ثم fallback (مصر: قنوات؛ تونس: مدينة عربية عامّة).
    سطر شريحة تونسيّ بلا مدينة → None (المُنادي يجعل المفتاح = الشريحة نفسها)."""
    compact = _compact(norm)
    for kws, key in entries:                               # القالب لكلا العملتين
        if any(_compact(kw) in compact for kw in kws if kw):
            return key
    if cur is Currency.EGP:
        for kws, key in FALLBACK_EGP:
            if any(_compact(kw) in compact for kw in kws if kw):
                return key
        return None
    if tier is not None:                                   # تونس: سطر شريحة عامّ
        return None
    return _generic_city_key(norm)                         # تونس: مدينة عربية


def parse_price_message(text: str, template: str, currency) -> dict:
    """يستخرج حمولة الأسعار من رسالة أسعار حسب قالب المالك (أو fallback افتراضيّ إن فرغ القالب).

    الخرج: {key: {"net"|"gross": rate, "tier"?: str}}.
      • القناة/المدينة → المفتاح (مصر: فودافون/انستا/بنك/بريد · تونس: اسم المدينة العربيّ).
      • «خصم»/«%»/«٪» ⇒ gross · «صافي»/«بدون خصم» ⇒ net · وإلا net. تونس net دائمًا (§7).
      • «تحت/فوق N» ⇒ شريحة؛ مع قناة ⇒ المفتاح «{key}_{tier}» (فلا تُدهَس شريحة أخرى لنفس القناة).
      • صيغة تونس X.XX.CC (فاصلتان) ⇒ تُدمَج («0.34.50»→0.345، «0.35.00»→0.35).
      • سطر بلا رقم (شرط/تنبيه/دوام) ⇒ يُتجاهَل.
    القالب أولًا (كلمة=مفتاح)؛ إن فرغ ⇒ المحلّل الافتراضيّ. كل سطر يُطابِق مفتاحًا واحدًا على الأكثر."""
    cur = _to_currency(currency)
    entries = _parse_template(template)
    rates: dict = {}
    for raw in (text or "").splitlines():
        norm = raw.translate(_AR_DIGITS).strip()
        if not norm:
            continue
        tier, norm_wo = _extract_tier(norm, cur)
        nums = _extract_numbers(norm_wo)
        if not nums:
            continue                                       # لا رقم → تجاهل (شرط/تنبيه)
        key = _resolve_key(norm, entries, cur, tier)
        if key is None:
            if tier is None:
                continue                                   # لا قناة/مدينة ولا شريحة
            dict_key = tier                                # سطر شريحة عامّ (تونس)
        else:
            dict_key = f"{key}_{tier}" if tier is not None else key
        entry: dict = {_kind(norm, cur): nums[0]}
        if tier is not None:
            entry["tier"] = tier
        rates[dict_key] = {**rates.get(dict_key, {}), **entry}
    return rates


def price_room_currency(jid: str, cfg: FxRatesConfig) -> Optional[Currency]:
    """يُرجِع عملة غرفة الأسعار المطابقة لـjid (من FxRatesConfig)، أو None إن ليست غرفة أسعار."""
    j = (jid or "").strip()
    if not j:
        return None
    if cfg.egp_room_jid and j == cfg.egp_room_jid.strip():
        return Currency.EGP
    if cfg.tnd_room_jid and j == cfg.tnd_room_jid.strip():
        return Currency.TND
    return None


def _sender_number(sender_jid: Optional[str]) -> str:
    """رقم المُرسِل من JID ('201...@s.whatsapp.net' أو ':device') — لبوّابة المُشرف المعتمد."""
    return (sender_jid or "").split("@", 1)[0].split(":", 1)[0].strip()


async def ingest_price_message(db, msg: RawMessage, cfg: FxRatesConfig) -> Optional[FxRateSnapshot]:
    """يبتلع رسالة أسعار محتملة ويُسجّل لقطة إن اجتازت البوّابة. **دالّة نقيّة — لا تُستدعى من pipeline**.

    الخطوات (docs/FX_RATES_SPEC.md §2):
      ١. البوّابة: الغرفة من غرفتَي الأسعار (cfg)؟ المُرسِل مُشرف معتمد (db.employees)؟ وإلا → None + تسجيل.
      ٢. التحليل: parse_price_message بقالب العملة (أو fallback).
      ٣. دمج جزئيّ: رسالة جزئية تُحدِّث ما فيها فقط فوق اللقطة الحاليّة (§2) — تبقى الحاليّة كاملة.
      ٤. التسجيل: record بنافذة صلاحية valid_from=received_at (السفر الزمنيّ §3 ط٥).
    يُرجِع اللقطة المُسجَّلة أو None (ليست غرفة أسعار / مُرسِل غير معتمد / لا شيء استُخرِج).
    لا يستشير fx_rates_enabled: القراءة/التخزين آمنان دائمًا؛ العلم يحكم **استخدام** السعر لاحقًا (خطوات لاحقة)."""
    currency = price_room_currency(msg.chat_jid, cfg)
    if currency is None:
        return None                            # ليست غرفة أسعار — لا شأن لنا
    number = _sender_number(msg.sender_jid)
    if not number or not await db.employees.is_authorized(number):
        log.info("رسالة أسعار من مُرسِل غير معتمد (%s) في غرفة %s — تُتجاهَل", number or "?", msg.chat_jid)
        return None
    template = cfg.egp_price_template if currency is Currency.EGP else cfg.tnd_price_template
    new_rates = parse_price_message(msg.text or "", template, currency)
    if not new_rates:
        log.info("رسالة أسعار بلا أسعار قابلة للاستخراج (غرفة %s) — تُتجاهَل", msg.chat_jid)
        return None
    # دمج جزئيّ فوق اللقطة الحاليّة (§2): تُحدَّث المفاتيح الواردة فقط، وتبقى الحاليّة كاملة.
    prev = await db.fx_rates.current(currency)
    merged: dict = dict(prev.rates) if prev else {}
    for key, entry in new_rates.items():
        merged[key] = {**(merged.get(key) or {}), **entry}
    snap = FxRateSnapshot(
        currency=currency, rates=merged, valid_from=msg.received_at,
        source_room_jid=msg.chat_jid, source_sender=msg.sender_jid,
        source_message_key=msg.message_key,
    )
    await db.fx_rates.record(snap)
    log.info("سعر %s مُسجَّل (%d مفتاح، منها %d محدَّث) valid_from=%s",
             currency.value, len(merged), len(new_rates), msg.received_at)
    return snap


async def current_rate(db, currency) -> Optional[FxRateSnapshot]:
    """غلاف: اللقطة المفتوحة الحاليّة لعملة."""
    return await db.fx_rates.current(currency)


async def rate_at(db, currency, ts) -> Optional[FxRateSnapshot]:
    """غلاف: اللقطة السارية في لحظة ts (السفر الزمنيّ §3 ط٥ — received_at)."""
    return await db.fx_rates.rate_at(currency, ts)
