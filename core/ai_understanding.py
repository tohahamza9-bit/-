"""
طبقة الفهم الذكي (OpenRouter) — **طبقة إنقاذ فقط** قبل التصعيد.

المبدأ الصارم (قرار المالك): **الذكاء الصناعي للفهم، والكود الحتميّ للفلوس.**
النموذج لا يكتب قيدًا أبدًا ولا يُغيّر حقلًا حسمه الفهم الحتميّ — يقترح بيانات منظَّمة (JSON)،
ثم يتحقّق **الكود** منها حرفًا حرفًا. أيّ شكّ → تصعيد بالمسار الحاليّ نفسه.

الخطوط الحمراء المنفَّذة هنا (§0):
  1. كل كيان مقترح (خزينة/مورد) لازم يطابق كيانًا **مسجَّلًا فعلًا** — بالكود أو بالاسم الرسميّ/
     إملاء بديل مسجَّل، **مطابقة تامّة بعد التطبيع فقط** (لا fuzzy، لا تشابه، لا تخمين).
     غير مسجَّل = تصعيد.
  2. المبلغ/السعر المقترح لازم يكون **موجودًا في نصّ الرسالة فعلًا** — ممنوع رقم مخترع.
  3. ثقة أيّ حقل جوهريّ < العتبة (0.9 افتراضيًّا) → تصعيد، مع إرفاق اقتراح النموذج للمسؤول.
  4. أيّ فشل/بطء/ردّ غير صالح → تصعيد عاديّ **كأن الطبقة غير موجودة** (ممنوع أن تتوقّف حوالة
     على توفّر API خارجيّ).

الطبقة معزولة تمامًا: لا تُستدعى إلا على المسار الذي كان **سيُصعَّد** أصلًا.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from core.models import Currency, SupplierRecord, TreasuryRecord
from core.parsing.normalize import normalize_ar
from core.parsing.resolve import normalize_arabic_for_matching

log = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# الحقول الجوهريّة: ثقة أيّ منها دون العتبة → تصعيد. (بقيّة الحقول تكميليّة لا تحجب.)
ESSENTIAL_FIELDS = ("operation", "amount", "currency", "treasury")

# صيغة «ألف/آلاف» (§3.5) — لقبول 32000 حين يكتب النصّ «32 ألف» (توسّع موثَّق لا اختراع).
_ALF_WORDS = ("الف", "آلاف", "الاف")


# ═════════════════════════════════════════════════════════════════════════════
# نتائج الطبقة
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class AiProposal:
    """اقتراح النموذج الخام + قياسات المراقبة (زمن/توكِنات/كلفة)."""
    data: dict[str, Any]
    model: str
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: Optional[float] = None

    def summary(self) -> str:
        """ملخّص عربيّ قصير للاقتراح — يُرفق في تنبيه المسؤول."""
        d = self.data
        bits: list[str] = []
        for key, label in (("operation", "العملية"), ("customer", "الزبون"),
                           ("amount", "المبلغ"), ("currency", "العملة"),
                           ("treasury", "الخزينة"), ("supplier", "المورد"),
                           ("price", "السعر"), ("discount", "الخصم")):
            val = d.get(key)
            if val not in (None, "", []):
                bits.append(f"{label}: {val}")
        return "؛ ".join(bits) if bits else "(بلا حقول)"


@dataclass
class AiVerdict:
    """حُكم التحقّق الحتميّ على اقتراح النموذج."""
    ok: bool
    reason: Optional[str] = None                  # سبب الرفض (للتصعيد/اللوق)
    treasury: Optional[TreasuryRecord] = None     # كيان مسجَّل مُتحقَّق منه
    supplier: Optional[SupplierRecord] = None
    amount: Optional[float] = None                # مُتحقَّق من وجوده في النصّ
    currency: Optional[Currency] = None
    fields: list[str] = field(default_factory=list)   # الحقول التي اجتازت التحقّق


# ═════════════════════════════════════════════════════════════════════════════
# (1) النداء — OpenRouter (متوافق OpenAI)
# ═════════════════════════════════════════════════════════════════════════════
_SYSTEM_PROMPT = (
    "أنت محلّل رسائل حوالات ماليّة عربيّة (تونس/مصر/ليبيا). مهمّتك **الفهم فقط**: تستخرج بيانات "
    "منظَّمة من نصّ رسالة. لا تخترع أيّ رقم أو اسم غير موجود في النصّ. إن لم تجد حقلًا فاتركه "
    "null وضع ثقته 0.\n\n"
    "🔴 الأهمّ: **الأخطاء الإملائيّة في أسماء الخزائن والموردين واردة جدًّا** — الموظّف يكتب بسرعة "
    "فيسقط حرف أو يلتصق حرفان أو تُبدَل حروف متقاربة النطق/الرسم (مثال: «عد الدين» يقصد «عزالدين»، "
    "«محموذ» يقصد «محمود»). مهمّتك الأساسيّة هي ردّ الاسم المكتوب خطأً إلى **الاسم المسجَّل الأقرب "
    "معنىً ونطقًا** من القوائم المعطاة أدناه.\n\n"
    "قواعد ملزِمة: كل كيان (خزينة/مورد) تُعيده يجب أن يكون **منسوخًا حرفيًّا من القوائم المعطاة** "
    "— لا تعيد اسمًا من عندك ولا تعيد ما كُتب في الرسالة إن كان مخالفًا للقائمة. وإن لم تجد في "
    "القائمة اسمًا تثق أنه المقصود فأعِد null (لا تخمّن على العمياء). "
    "ولكل اسم صحّحته أضِف مدخلًا في «corrections» يوضّح النصّ كما ورد في الرسالة حرفيًّا مقابل "
    "الاسم المسجَّل. أعِد JSON فقط بلا أيّ شرح."
)

_SCHEMA_HINT = """أعِد JSON بهذا الشكل بالضبط:
{
  "operation": "بيع" | "شراء" | null,
  "customer_code": "<كود الزبون كما في النص>" | null,
  "customer_name": "<اسم الزبون>" | null,
  "amount": <رقم كما ورد في النص> | null,
  "currency": "EGP" | "TND" | "LYD" | "USD" | null,
  "discount": <رقم> | null,
  "treasury": "<الاسم الرسميّ من قائمة الخزائن>" | null,
  "treasury_code": "<كود الخزينة إن عُرف>" | null,
  "supplier": "<الاسم الرسميّ من قائمة الموردين>" | null,
  "price": <رقم السعر كما ورد> | null,
  "phone": "<رقم الهاتف>" | null,
  "reference_number": "<الرقم الإشاري>" | null,
  "corrections": [
    {"raw": "<الاسم كما ورد في الرسالة حرفيًّا>",
     "entity_type": "supplier" | "treasury" | "currency",
     "official": "<الاسم المسجَّل المقابل من القائمة>",
     "confidence": 0.0}
  ],
  "line_parse": [
    {"raw": "<السطر الملتصق كما ورد>", "code": "<كود>", "name": "<الاسم>",
     "price": "<السعر>", "confidence": 0.0}
  ],
  "is_postal": true | false,
  "postal_confidence": 0.0,
  "confidence": {"operation": 0.0, "amount": 0.0, "currency": 0.0,
                 "treasury": 0.0, "supplier": 0.0, "price": 0.0, "customer": 0.0}
}"""


def build_prompt(text: str, treasuries: list[TreasuryRecord],
                 suppliers: list[SupplierRecord],
                 known_shapes: Optional[list[str]] = None,
                 sender_context: Optional[object] = None) -> str:
    """يبني الـprompt المنظَّم: نصّ الرسالة + الكيانات المسجَّلة **لحظة النداء** + سياق المُرسِل.

    🔴 القوائم تُمرَّر من المُستدعي بعد قراءتها من DB في هذه اللحظة — لا نسخة محفوظة هنا ولا
       cache يحتاج إبطالًا. كيانٌ أُضيف قبل ثانية يظهر في هذا النداء.
    """
    t_lines = [
        f"- {t.name} (كود {t.code}, {getattr(t.currency, 'value', t.currency) or '؟'})"
        + (f" [أيضًا: {'، '.join(t.aliases)}]" if getattr(t, "aliases", None) else "")
        for t in treasuries
    ]
    s_lines = [
        f"- {s.name}" + (f" (كود {s.code})" if s.code else "")
        + (f" [أيضًا: {'، '.join(s.aliases)}]" if getattr(s, "aliases", None) else "")
        for s in suppliers
    ]
    shapes = known_shapes or [
        "شكل A: كود الزبون + الاسم + السعر، ثم رسالة ثانية بالخزينة/المورد.",
        "شكل SI: رسالة واحدة معنونة تحمل كل الحقول (زبون/مبلغ/عملة/خزينة/سعر).",
        "شكل الخصم: رسالتان مرتبطتان بالرقم الإشاري (هوية ثم تسوية).",
    ]
    return (
        f"نصّ الرسالة:\n«««\n{text}\n»»»\n\n"
        f"قائمة الخزائن المسجَّلة (لا شيء خارجها مقبول):\n" + ("\n".join(t_lines) or "- (فارغة)") +
        f"\n\nقائمة الموردين المسجَّلين (لا شيء خارجها مقبول):\n" + ("\n".join(s_lines) or "- (فارغة)") +
        f"\n\nأشكال الرسائل المعروفة:\n" + "\n".join(f"- {s}" for s in shapes) +
        (f"\n\n{sender_context.as_prompt_block()}"
         if sender_context is not None and hasattr(sender_context, "as_prompt_block") else "") +
        f"\n\n{_SCHEMA_HINT}"
    )


class OpenRouterClient:
    """عميل OpenRouter رفيع. أيّ خطأ/بطء → يُرجِع None (المُستدعي يُصعّد كالمعتاد)."""

    def __init__(self, api_key: str, model: str, timeout: float = 10.0,
                 url: str = OPENROUTER_URL) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.url = url

    async def propose(self, text: str, treasuries: list[TreasuryRecord],
                      suppliers: list[SupplierRecord],
                      known_shapes: Optional[list[str]] = None,
                      sender_context: Optional[object] = None) -> Optional[AiProposal]:
        if not self.api_key or not self.model:
            log.info("(الفهم الذكي) بلا مفتاح/نموذج — تخطٍّ (تصعيد عاديّ).")
            return None
        payload = {
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": build_prompt(text, treasuries, suppliers, known_shapes,
                                                          sender_context)},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json",
                   "X-Title": "moneyado-bot"}
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, json=payload, headers=headers)
                resp.raise_for_status()
                body = resp.json()
        except Exception as exc:      # T5 — لا نبتلع: نسجّل ونُرجِع None (تصعيد عاديّ)
            log.warning("(الفهم الذكي) فشل نداء OpenRouter (%.0fms): %s — تصعيد عاديّ",
                        (time.monotonic() - t0) * 1000, exc)
            return None
        latency = int((time.monotonic() - t0) * 1000)
        try:
            content = body["choices"][0]["message"]["content"]
            data = _loads_lenient(content)
            if not isinstance(data, dict):
                raise ValueError("الردّ ليس كائن JSON")
        except Exception as exc:
            log.warning("(الفهم الذكي) ردّ غير صالح من %s: %s — تصعيد عاديّ", self.model, exc)
            return None
        usage = body.get("usage") or {}
        prop = AiProposal(
            data=data, model=self.model, latency_ms=latency,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=usage.get("cost"),
        )
        # مراقبة الكلفة/الزمن (بند 5) — سطر لوق لكل نداء
        log.info("(الفهم الذكي) %s — %dms، توكِنات %d/%d، كلفة %s",
                 self.model, latency, prop.prompt_tokens, prop.completion_tokens,
                 f"${prop.cost_usd:.5f}" if prop.cost_usd is not None else "؟")
        return prop


def _loads_lenient(content: str) -> Any:
    """JSON من ردّ النموذج — يتسامح مع سياج ```json``` أو نصّ حوله."""
    s = (content or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.S).strip()
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", s, flags=re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ═════════════════════════════════════════════════════════════════════════════
# (2) التحقّق الحتميّ — الخط الأحمر. الكود هو الحَكَم، لا النموذج.
# ═════════════════════════════════════════════════════════════════════════════
def _num_in_text(value: float, text: str) -> bool:
    """هل الرقم موجود **فعلًا** في نصّ الرسالة؟ (منع الأرقام المخترعة §0)

    يُقارَن على النصّ بعد تطبيع الأرقام العربيّة-الهنديّة، متجاهلًا فواصل الآلاف. يُقبل أيضًا
    توسّع «ألف» الموثَّق (§3.5): «32 ألف» ⇒ 32000.
    """
    norm = normalize_ar(text) or ""
    # 🔴 مطابقة **رقميّة تامّة** لا سلسلةً جزئيّة: «100» يجب ألّا يُقبَل لأن النصّ فيه «1000».
    #    نستخرج كل الأعداد الواردة في النصّ ونقارن قيمها عدديًّا.
    tokens: set[float] = set()
    for m in re.finditer(r"\d[\d,٬]*(?:\.\d+)?", norm):
        try:
            tokens.add(float(m.group(0).replace(",", "").replace("٬", "")))
        except ValueError:
            continue
    if any(abs(value - t) < 1e-9 for t in tokens):
        return True
    # توسّع «ألف» الموثَّق (§3.5): 32000 مقبول إن كان النصّ «32 ألف» — توسّع لا اختراع.
    if float(value).is_integer():
        iv = int(value)
        if iv % 1000 == 0 and iv >= 1000:
            head = iv // 1000
            if any(re.search(rf"(?<!\d){head}(?!\d)\s*{w}", norm) for w in _ALF_WORDS):
                return True
    return False


def _match_registered(name: Optional[str], code: Optional[str], records: list):
    """مطابقة **تامّة فقط** (كود، أو اسم رسميّ/إملاء بديل مسجَّل بعد التطبيع). لا fuzzy إطلاقًا.

    🔴 هذا هو الخط الأحمر (§0): اقتراح النموذج لا يُنشئ كيانًا ولا يُقارَب — إمّا يطابق سجلًّا
    قائمًا حرفيًّا أو يُرفَض فيُصعَّد.
    """
    if code:
        for r in records:
            if r.code and normalize_ar(str(r.code)) == normalize_ar(str(code)):
                return r
    q = normalize_ar(name)
    if not q:
        return None
    for r in records:
        forms = [normalize_ar(r.name)] + [normalize_ar(a) for a in getattr(r, "aliases", [])]
        if q in [f for f in forms if f]:
            return r
    return None


@dataclass
class VerifiedCorrection:
    """تصحيح إملائيّ **مُتحقَّق منه**: نصٌّ ورد حرفيًّا في الرسالة ⇒ سجلّ كيان مسجَّل فعلًا.

    هذا كل ما نأخذه من النموذج في مسار الإعادة: **تصحيح اسم فقط، لا قيمة ماليّة**. المبالغ
    والأسعار والأدوار والخزينة يستخرجها المفكِّك الحتميّ من النصّ كما لو كُتب الاسم صحيحًا.
    """
    raw: str                 # النصّ كما ورد في الرسالة (مُتحقَّق من وجوده حرفيًّا)
    official: str            # اسم السجلّ المسجَّل
    entity_type: str         # "supplier" | "treasury"
    record: object           # SupplierRecord | TreasuryRecord
    confidence: float


def validate_corrections(prop: AiProposal, text: str, treasuries: list[TreasuryRecord],
                         suppliers: list[SupplierRecord],
                         threshold: float = 0.9) -> list[VerifiedCorrection]:
    """يتحقّق حتميًّا من تصحيحات الإملاء المقترحة. يُرجِع المقبولة فقط (قد تكون فارغة).

    شروط القبول الثلاثة (كلها إلزاميّة):
      1. النصّ الخام `raw` موجود **حرفيًّا** في الرسالة (بعد التطبيع) — فلا يخترع النموذج كلمةً.
      2. `official` يطابق سجلًّا مسجَّلًا **مطابقةً تامّة** (كود/اسم رسميّ/إملاء بديل) — لا fuzzy.
      3. ثقة التصحيح ≥ العتبة.
    """
    out: list[VerifiedCorrection] = []
    items = prop.data.get("corrections")
    if not isinstance(items, list):
        return out
    norm_text = normalize_arabic_for_matching(text)
    for it in items:
        if not isinstance(it, dict):
            continue
        raw = (it.get("raw") or "").strip()
        official = (it.get("official") or "").strip()
        etype = (it.get("entity_type") or "").strip().lower()
        if not raw or not official or etype not in ("supplier", "treasury"):
            continue
        try:
            conf = float(it.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if conf < threshold:
            log.info("(الفهم الذكي) رُفض تصحيح «%s»→«%s»: ثقة %.2f < %.2f",
                     raw, official, conf, threshold)
            continue
        # (1) النصّ الخام موجود فعلًا في الرسالة — لا كلمات مخترعة
        if normalize_arabic_for_matching(raw) not in norm_text:
            log.warning("(الفهم الذكي) رُفض تصحيح: «%s» غير موجود حرفيًّا في نصّ الرسالة", raw)
            continue
        # (2) الوجهة كيان مسجَّل فعلًا — مطابقة تامّة
        records = suppliers if etype == "supplier" else treasuries
        rec = _match_registered(official, None, records)
        if rec is None:
            log.warning("(الفهم الذكي) رُفض تصحيح: «%s» ليس كيانًا مسجَّلًا (%s)", official, etype)
            continue
        out.append(VerifiedCorrection(raw=raw, official=rec.name, entity_type=etype,
                                      record=rec, confidence=conf))
    return out


def with_ephemeral_alias(records: list, rec, raw_token: str) -> list:
    """نسخة **مؤقّتة في الذاكرة** من القائمة، مضافًا فيها `raw_token` إملاءً بديلًا للسجلّ `rec`.

    🔴 لا تُكتب في قاعدة البيانات إطلاقًا: التصحيح صالح لهذه الرسالة وحدها. التعلّم الدائم قرارُ
    مالكٍ يدويّ من اللوحة (كما أُلغي تعلّم التشابه) — فلا يترسّخ خطأ نموذجٍ إملاءً معتمدًا للأبد.
    """
    out = []
    for r in records:
        if r is rec or (r.name == rec.name and r.code == rec.code):
            clone = r.model_copy(deep=True)
            if raw_token not in clone.aliases:
                clone.aliases = list(clone.aliases) + [raw_token]
            out.append(clone)
        else:
            out.append(r)
    return out


# كلمات العملة المسموح التصحيح إليها — قائمة مغلقة (النموذج لا يخترع كلمةً خارجها).
CURRENCY_WORDS = (
    "جنيه", "جنيه مصري", "ج.م", "جم", "دينار تونسي", "دينار", "د.ت", "دت",
    "دينار ليبي", "د.ل", "دولار",
)


def validate_text_corrections(prop: AiProposal, text: str,
                              threshold: float = 0.9) -> list[tuple[str, str]]:
    """تصحيحات **كلمة العملة** فقط، للرسالة الأولى التي فشل تفكيكها (م: X1325 «جني م»).

    القيود (كلها إلزاميّة):
      1. النصّ الخام موجود حرفيًّا في الرسالة.
      2. البديل من `CURRENCY_WORDS` حصرًا — لا كلمات من عند النموذج.
      3. الثقة ≥ العتبة.
      4. التصحيح **إضافيّ لا حذفيّ**: البديل يبدأ بالخام (جني→جنيه) — فلا يُستبدَل رقمٌ أو اسم.
    """
    out: list[tuple[str, str]] = []
    items = prop.data.get("corrections")
    if not isinstance(items, list):
        return out
    norm_text = normalize_ar(text)
    for it in items:
        if not isinstance(it, dict) or (it.get("entity_type") or "").lower() != "currency":
            continue
        raw = (it.get("raw") or "").strip()
        official = (it.get("official") or "").strip()
        try:
            conf = float(it.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if not raw or not official or conf < threshold:
            continue
        if normalize_ar(raw) not in norm_text:
            log.warning("(الفهم الذكي) رُفض تصحيح عملة: «%s» غير موجود في النصّ", raw)
            continue
        if normalize_ar(official) not in [normalize_ar(w) for w in CURRENCY_WORDS]:
            log.warning("(الفهم الذكي) رُفض تصحيح عملة: «%s» خارج القائمة المغلقة", official)
            continue
        # (4) إضافيّ لا حذفيّ — يمنع «تصحيحًا» يبتلع رقمًا أو اسمًا
        if not normalize_ar(official).startswith(normalize_ar(raw)):
            log.warning("(الفهم الذكي) رُفض تصحيح عملة: «%s»←«%s» ليس امتدادًا للخام",
                        raw, official)
            continue
        out.append((raw, official))
    return out


def apply_text_corrections(text: str, fixes: list[tuple[str, str]]) -> str:
    """يطبّق الاستبدالات الرمزيّة على النصّ الأصليّ (أوّل ظهور لكلٍّ) — بلا إعادة صياغة."""
    out = text
    for raw, official in fixes:
        out = out.replace(raw, official, 1)
    return out


@dataclass
class VerifiedLine:
    """سطر ملتصق فُكّ إلى (كود، اسم، سعر) — **بعد** التحقّق الحتميّ."""
    raw: str
    code: str
    name: str
    price: Optional[str]
    confidence: float


def validate_line_parse(prop: AiProposal, text: str, threshold: float = 0.9,
                        entities: Optional[list] = None) -> list[VerifiedLine]:
    """(نقطة الاستدعاء ج) يتحقّق من تفكيك النموذج لسطرٍ ملتصق («1160عبد السلام زكري6.04»).

    الشروط (كلها إلزاميّة):
      1. السطر الخام موجود حرفيًّا في الرسالة.
      2. الكود والاسم والسعر **كلها مقاطع من السطر الخام نفسه** بعد إزالة الفراغات —
         أي أن النموذج **فكّك** ولم **يخترع**. (أقوى ضمان ممكن هنا.)
      3. الثقة ≥ العتبة.
      4. إن مُرِّرت `entities` فالكود أو الاسم يجب أن يطابق كيانًا مسجَّلًا (كود/اسم/إملاء بديل).
    """
    out: list[VerifiedLine] = []
    items = prop.data.get("line_parse")
    if not isinstance(items, list):
        return out
    norm_text = normalize_ar(text)
    for it in items:
        if not isinstance(it, dict):
            continue
        raw = (it.get("raw") or "").strip()
        code = str(it.get("code") or "").strip()
        name = (it.get("name") or "").strip()
        price = (str(it.get("price")).strip() if it.get("price") not in (None, "") else None)
        try:
            conf = float(it.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            conf = 0.0
        if not raw or not code or not name or conf < threshold:
            continue
        if normalize_ar(raw) not in norm_text:
            log.warning("(الفهم الذكي) رُفض تفكيك سطر: «%s» غير موجود في الرسالة", raw)
            continue
        # (2) كل جزء مقطعٌ من الخام — التفكيك لا يضيف حرفًا
        compact = re.sub(r"\s+", "", normalize_ar(raw))
        parts_ok = all(
            re.sub(r"\s+", "", normalize_ar(p)) in compact
            for p in (code, name) + ((price,) if price else ())
        )
        if not parts_ok:
            log.warning("(الفهم الذكي) رُفض تفكيك سطر «%s»: جزءٌ ليس من السطر نفسه", raw)
            continue
        if entities is not None and _match_registered(name, code, entities) is None:
            log.warning("(الفهم الذكي) رُفض تفكيك سطر «%s»: (%s/%s) ليس كيانًا مسجَّلًا",
                        raw, code, name)
            continue
        out.append(VerifiedLine(raw=raw, code=code, name=name, price=price, confidence=conf))
    return out


def validate_postal(prop: AiProposal, threshold: float = 0.9) -> Optional[bool]:
    """(نمط البريد) هل الرسالة حوالة **بلا رقم مستلم** بثقة كافية؟

    يُرجِع True (بريد مؤكَّد) / False (ليست بريدًا) / None (لا حسم ⇒ تصعيد بسؤال).
    """
    val = prop.data.get("is_postal")
    if not isinstance(val, bool):
        return None
    try:
        conf = float(prop.data.get("postal_confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if conf < threshold:
        return None
    return val


def validate_proposal(prop: AiProposal, text: str, treasuries: list[TreasuryRecord],
                      suppliers: list[SupplierRecord], threshold: float = 0.9,
                      require: tuple[str, ...] = ("treasury",)) -> AiVerdict:
    """التحقّق الحتميّ من اقتراح النموذج. يُرجِع AiVerdict — `ok=False` ⇒ تصعيد.

    `require`: الحقول التي **يجب** أن يقدّمها النموذج ويجتاز تحقّقها (وإلا لا فائدة من الطبقة).
    الحقول الأخرى تُتحقَّق إن وُجدت وتُهمَل إن غابت (لا تحجب).
    """
    d = prop.data
    conf = d.get("confidence") or {}
    if not isinstance(conf, dict):
        return AiVerdict(False, "حقل الثقة غير صالح في ردّ النموذج")
    verdict = AiVerdict(True)

    def _conf(key: str) -> float:
        try:
            return float(conf.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # ── الخزينة: كيان مسجَّل + ثقة كافية ──
    if d.get("treasury") or d.get("treasury_code"):
        rec = _match_registered(d.get("treasury"), d.get("treasury_code"), treasuries)
        if rec is None:
            return AiVerdict(False,
                             f"خزينة مقترحة غير مسجَّلة: «{d.get('treasury') or d.get('treasury_code')}»")
        if _conf("treasury") < threshold:
            return AiVerdict(False, f"ثقة الخزينة {_conf('treasury'):.2f} < العتبة {threshold:.2f}")
        verdict.treasury = rec
        verdict.fields.append("treasury")

    # ── المورد: كيان مسجَّل + ثقة كافية ──
    if d.get("supplier"):
        rec = _match_registered(d.get("supplier"), d.get("supplier_code"), suppliers)
        if rec is None:
            return AiVerdict(False, f"مورد مقترح غير مسجَّل: «{d.get('supplier')}»")
        if _conf("supplier") < threshold:
            return AiVerdict(False, f"ثقة المورد {_conf('supplier'):.2f} < العتبة {threshold:.2f}")
        verdict.supplier = rec
        verdict.fields.append("supplier")

    # ── الأرقام: موجودة في النصّ فعلًا (ممنوع الاختراع) ──
    for key in ("amount", "price", "discount"):
        val = d.get(key)
        if val in (None, ""):
            continue
        try:
            num = float(val)
        except (TypeError, ValueError):
            return AiVerdict(False, f"قيمة «{key}» المقترحة ليست رقمًا: {val!r}")
        if not _num_in_text(num, text):
            return AiVerdict(False, f"رقم «{key}» المقترح ({num:g}) غير موجود في نصّ الرسالة")
        if key == "amount":
            if _conf("amount") < threshold:
                return AiVerdict(False, f"ثقة المبلغ {_conf('amount'):.2f} < العتبة {threshold:.2f}")
            verdict.amount = num
            verdict.fields.append("amount")

    # ── العملة: من قائمة العملات المعروفة ──
    if d.get("currency"):
        try:
            verdict.currency = Currency(str(d["currency"]).strip().upper())
        except ValueError:
            return AiVerdict(False, f"عملة مقترحة غير معروفة: «{d.get('currency')}»")
        if _conf("currency") < threshold:
            return AiVerdict(False, f"ثقة العملة {_conf('currency'):.2f} < العتبة {threshold:.2f}")
        verdict.fields.append("currency")

    # ── الحقول المطلوبة للإنقاذ: لو لم يقدّمها النموذج فلا فائدة → تصعيد ──
    missing = [f for f in require if f not in verdict.fields]
    if missing:
        return AiVerdict(False, f"النموذج لم يقدّم حقلًا مطلوبًا مُتحقَّقًا: {'، '.join(missing)}")
    return verdict
