"""
دعم مشترك لاختبار/تشخيص البيانات الذهبية (tests/fixtures/messages_golden.json).

يقود **الأنبوب الحقيقي** (Pipeline._ingest) على قاعدة وهمية، فيختبر نفس مسار الإنتاج
(تحليل + ربط الرسالة الثانية + إسناد الخزينة/الصافي) لا محاكاةً يدويّة.

يُستعمَل من:
- tests/test_golden_messages.py  (اختبار انحدار دائم + قائمة استثناءات)
- tools/golden_diff.py            (تقرير تشخيصيّ)
"""
from __future__ import annotations

import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from core.bus import Bus
from core.constants import Currency, SEED_TREASURIES
from core.models import RawMessage, TreasuryRecord, WriteResult
from core.parsing.normalize import normalize_ar, normalize_payment
from core.pipeline import Pipeline

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "messages_golden.json"

CENTRAL = "central@g.us"
ADMIN = "admin@g.us"
SENDER = "20100@s.whatsapp.net"
NOW0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)

_CUR_STR = {Currency.EGP: "ج.م", Currency.TND: "د.ت"}

# لواحق تصدير واتساب النصيّة (لا تظهر في الرسائل الحيّة) — تُجرَّد قبل التحليل لإنصاف المقارنة.
_EXPORT_NOISE = re.compile(r"<[^>]*>|لم يتم إدراج الصورة|لم يتم تضمين الوسائط")


def clean(text):
    return _EXPORT_NOISE.sub("", text).strip() if text else text


def _norm(s):
    return normalize_ar(str(s)).replace(" ", "") if s is not None else None


# ── مطابقة الخزينة (alias→الاسم القانوني) للمقارنة المتساهلة ───────────────────
_TREASURY_CANON: dict[str, str] = {}
for _t in SEED_TREASURIES:
    _TREASURY_CANON[_norm(_t["name"])] = _t["name"]
    for _a in _t.get("aliases", []):
        _TREASURY_CANON[_norm(_a)] = _t["name"]


def canon_treasury(name):
    if not name:
        return None
    return _TREASURY_CANON.get(_norm(name), _norm(name))


# ── بذر خزائن الأشخاص الحقيقية (طه/البراق/وليد…) من الملف، مُزال التكرار ─────────
# في الإنتاج تُدار هذه من DB (لوحة V2)؛ SEED الافتراضية لا تحويها. بذرها يجعل اختبار
# **استخراج** الخزينة منصفاً. نُزيل التكرار بالشكل المطبَّع كي لا يُحدِث التباساً (§0) يُسقِط الحلّ.
async def seed_golden_treasuries(db):
    data = load_fixture()
    covered = set(_TREASURY_CANON)          # الأسماء/aliases المبذورة أصلاً
    for name in data.get("treasury_aliases", []):
        key = _norm(name)
        if key in covered:
            continue
        covered.add(key)
        _TREASURY_CANON[key] = name          # اجعلها قانونيّة لنفسها (للمقارنة)
        await db.treasuries.upsert(
            TreasuryRecord(name=name, type="sell_only", aliases=[], active=True))


# ── محرّك الأنبوب ─────────────────────────────────────────────────────────────
class _NullWriter:
    name = "null"

    async def write(self, job, *, commit):
        return WriteResult(ok=True)


class _NullVerifier:
    enabled = False

    async def verify_transaction(self, *a, **k):
        return (False, None)

    async def find_last_pending(self, *a, **k):
        return None


def build_pipeline(db) -> Pipeline:
    bus = Bus(db, {CENTRAL, ADMIN}, CENTRAL, ADMIN)
    return Pipeline(db, bus, _NullWriter(), _NullVerifier(),
                    customer_room_jids=[], treasury_room_jids=[])


async def ingest_case(pipe: Pipeline, messages: list[str]):
    """يُغذّي الأنبوب برسائل الحالة بالتسلسل ويُرجع الصفقة المدموجة (آخر ناتج غير None)."""
    deal = None
    for i, msg in enumerate(messages):
        if not msg:
            continue
        raw = RawMessage(
            message_key=f"gk-{id(messages)}-{i}", chat_jid=CENTRAL, sender_jid=SENDER,
            text=clean(msg), received_at=NOW0 + timedelta(seconds=i * 2), reply_to_key=None)
        d = await pipe._ingest(raw, NOW0 + timedelta(seconds=i * 2 + 1))
        if d is not None:
            deal = d
    return deal


# ── adapter: ParsedLeg → مفاتيح JSON ─────────────────────────────────────────
def adapt(deal) -> dict:
    got = {k: None for k in
           ("code", "recipient", "amount_gross", "amount_net", "currency",
            "channel", "supplier_code", "supplier_name", "rate", "treasury")}
    if deal is None or deal.sell_leg is None:
        return got
    leg = deal.sell_leg
    got["code"] = leg.reference_number
    got["recipient"] = leg.phone
    got["amount_gross"] = leg.amount
    got["amount_net"] = leg.amount_after_discount
    got["currency"] = _CUR_STR.get(leg.currency)
    got["channel"] = leg.payment_method
    got["supplier_code"] = leg.customer_code or (leg.supplier.code if leg.supplier else None)
    got["supplier_name"] = leg.customer_name or (leg.supplier.name if leg.supplier else None)
    got["rate"] = leg.price_raw
    got["treasury"] = leg.treasury.name if leg.treasury else None
    return got


# ── مقارنة ────────────────────────────────────────────────────────────────────
def _as_float(x):
    try:
        return float(x) if x is not None else None
    except (ValueError, TypeError):
        return None


def _name_eq(a, b):
    if a is None or b is None:
        return a == b
    na, nb = _norm(a), _norm(b)
    return na == nb or na in nb or nb in na


def compare(expected: dict, got: dict, skip: set[str] | None = None):
    skip = skip or set()
    diffs = []
    for key, exp in expected.items():
        if key in skip:
            continue
        g = got.get(key)
        if key in ("amount_gross", "amount_net", "rate"):
            ok = _as_float(exp) == _as_float(g)
        elif key == "channel":
            ok = normalize_payment(exp) == normalize_payment(g)
        elif key == "currency":
            ok = (exp or None) == (g or None)
        elif key == "supplier_name":
            ok = _name_eq(exp, g)
        elif key == "treasury":
            ok = canon_treasury(exp) == canon_treasury(g)
        else:  # code, recipient, supplier_code
            ok = (None if exp is None else str(exp)) == (None if g is None else str(g))
        if not ok:
            diffs.append((key, exp, g))
    return diffs


# ── قراءة الحالات ─────────────────────────────────────────────────────────────
def load_fixture() -> dict:
    return json.load(io.open(FIXTURE, encoding="utf-8"))


def iter_cases(data: dict | None = None):
    data = data or load_fixture()
    for shape in data.get("shapes", []):
        for i, case in enumerate(shape["cases"]):
            yield f"{shape['id']}#{i}", case.get("messages", []), case["expected"]
    for edge in data.get("edge_cases", []):
        for i, case in enumerate(edge["cases"]):
            msgs = case.get("transfer_messages") or [case.get("message")]
            yield f"{edge['id']}#{i}", msgs, case["expected"]
