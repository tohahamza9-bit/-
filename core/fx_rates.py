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
from typing import Optional

from .constants import Currency
from .models import FxRateSnapshot, FxRatesConfig, RawMessage

log = logging.getLogger(__name__)


# ── محلّل افتراضيّ (fallback) — قوائم كلمات §3 حين لا قالب من الداشبورد ───────────
# كل عنصر: (الكلمات المفتاحية، المفتاح). المطابقة بالاحتواء (substring) على سطر الرسالة.
FALLBACK_EGP: list[tuple[list[str], str]] = [
    (["فودافون", "فودا", "vodafone"], "vodafone"),
    (["انستا", "انستاباي", "instapay", "insta"], "insta"),
    (["بنك", "bank"], "bank"),
    (["بريد", "post"], "post"),
]
FALLBACK_TND: list[tuple[list[str], str]] = [
    (["العاصمة", "العاصمه", "تونس العاصمة", "capital"], "capital"),
    (["جربة", "جربه", "djerba"], "djerba"),
    (["سوسة", "سوسه", "sousse"], "sousse"),
    (["صفاقس", "sfax"], "sfax"),
]

# تطبيع الأرقام العربية/الشرقية → ASCII (محليّ ومستقلّ — لا يمسّ core.parsing.normalize)
_AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _to_currency(currency) -> Currency:
    """يقبل Currency أو نصًّا ('EGP'/'TND') ويُرجِع Currency."""
    if isinstance(currency, Currency):
        return currency
    return Currency(str(currency).upper())


def _extract_numbers(line: str) -> list[float]:
    """أرقام عشرية من سطر — يطبّع الأرقام العربية ويعامل «،»/«,» كفاصل عشريّ (الأسعار صغيرة)."""
    norm = line.translate(_AR_DIGITS)
    out: list[float] = []
    token = ""
    for ch in norm:
        if ch.isdigit():
            token += ch
        elif ch in ".,،" and token and token[-1].isdigit():
            token += "."          # فاصل عشريّ موحّد
        else:
            if token:
                out.append(token); token = ""
    if token:
        out.append(token)
    vals: list[float] = []
    for t in out:
        t = t.strip(".")
        if t:
            try:
                vals.append(float(t))
            except ValueError:
                pass
    return vals


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


def parse_price_message(text: str, template: str, currency) -> dict:
    """يستخرج حمولة الأسعار من رسالة أسعار حسب قالب المالك (أو fallback إن فرغ القالب).

    الخرج: مصر {key: {"net": x, "gross": y?}} · تونس {city_key: {"net": x}} (§7 لا خصم بتونس).
    net/gross (مصر): «صافي» ⇒ net · «خصم/قبل» ⇒ gross. سطر بلا كلمة نوع ⇒ net افتراضًا.
    كل سطر يُطابِق مفتاحًا واحدًا على الأكثر (أوّل تطابق بترتيب القالب)."""
    cur = _to_currency(currency)
    entries = _parse_template(template) or (FALLBACK_EGP if cur is Currency.EGP else FALLBACK_TND)
    rates: dict = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        matched_key = None
        for kws, key in entries:
            if any(kw and kw in line for kw in kws):
                matched_key = key
                break
        if matched_key is None:
            continue
        nums = _extract_numbers(line)
        if not nums:
            continue
        if cur is Currency.EGP:
            if "صافي" in line:
                entry = {"gross": nums[0], "net": nums[-1]} if len(nums) >= 2 else {"net": nums[0]}
            elif "خصم" in line or "قبل" in line:
                entry = {"gross": nums[0]}
                if len(nums) >= 2:
                    entry["net"] = nums[-1]
            else:
                entry = {"net": nums[0]}
        else:                                  # تونس: net فقط (§7)
            entry = {"net": nums[0]}
        rates[matched_key] = entry
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
