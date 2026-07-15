"""
اختبارات نظام الأسعار — الخطوة ٢ (core/fx_rates.py + FxRatesRepo).
تحليل بقالب/fallback + نوافذ صلاحية ميلي + سفر زمنيّ + بوّابة + دمج جزئيّ + test-parse.
اختبارات المستودع/التحليل تعمل بلا fastapi؛ اختبار HTTP يُتخطّى بوضوح عند غيابه.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import pytest

from core.constants import Currency
from core.fx_rates import (
    ingest_price_message,
    parse_price_message,
    price_room_currency,
)
from core.models import EmployeeRecord, FxRateSnapshot, FxRatesConfig, RawMessage

T0 = datetime(2026, 7, 1, 10, 0, 0)


def _snap(currency=Currency.EGP, rates=None, valid_from=T0):
    return FxRateSnapshot(currency=currency, rates=rates or {"vodafone": {"net": 6.15}},
                          valid_from=valid_from)


# ── المستودع: نوافذ الصلاحية + السفر الزمنيّ (§3 ط٥) ──────────────────────────
async def test_record_and_current(db):
    await db.fx_rates.record(_snap(rates={"vodafone": {"net": 6.15}}, valid_from=T0))
    cur = await db.fx_rates.current(Currency.EGP)
    assert cur is not None and cur.rates["vodafone"]["net"] == 6.15
    assert cur.valid_until is None, "اللقطة الحاليّة مفتوحة"


async def test_new_record_closes_previous_window(db):
    t1, t2 = T0, T0 + timedelta(hours=2)
    await db.fx_rates.record(_snap(rates={"vodafone": {"net": 6.10}}, valid_from=t1))
    await db.fx_rates.record(_snap(rates={"vodafone": {"net": 6.20}}, valid_from=t2))
    # السابقة أُغلقت عند valid_from الجديد (نوافذ غير متداخلة)
    docs = [d async for d in db.fx_rates.col.find({"currency": "EGP"}).sort("valid_from", 1)]
    assert docs[0]["valid_until"] == t2, "السابقة تُغلق عند بداية الجديدة"
    assert docs[1]["valid_until"] is None
    cur = await db.fx_rates.current(Currency.EGP)
    assert cur.rates["vodafone"]["net"] == 6.20


async def test_rate_at_time_travel(db):
    t1, t2 = T0, T0 + timedelta(hours=2)
    await db.fx_rates.record(_snap(rates={"vodafone": {"net": 6.10}}, valid_from=t1))
    await db.fx_rates.record(_snap(rates={"vodafone": {"net": 6.20}}, valid_from=t2))
    assert (await db.fx_rates.rate_at(Currency.EGP, t1 - timedelta(seconds=1))) is None  # قبل أول سعر
    assert (await db.fx_rates.rate_at(Currency.EGP, t1)).rates["vodafone"]["net"] == 6.10
    assert (await db.fx_rates.rate_at(Currency.EGP, t1 + timedelta(hours=1))).rates["vodafone"]["net"] == 6.10
    assert (await db.fx_rates.rate_at(Currency.EGP, t2)).rates["vodafone"]["net"] == 6.20


async def test_valid_from_millisecond_precision(db):
    t = T0.replace(microsecond=123000)          # 123 ميلي
    await db.fx_rates.record(_snap(valid_from=t))
    assert (await db.fx_rates.current(Currency.EGP)).valid_from == t, "الدقّة الميلي محفوظة"


async def test_currencies_independent(db):
    await db.fx_rates.record(_snap(Currency.EGP, {"vodafone": {"net": 6.15}}, T0))
    await db.fx_rates.record(_snap(Currency.TND, {"capital": {"net": 0.34}}, T0))
    assert (await db.fx_rates.current(Currency.EGP)).rates["vodafone"]["net"] == 6.15
    assert (await db.fx_rates.current(Currency.TND)).rates["capital"]["net"] == 0.34


# ── المحلّل: قالب + fallback + net/gross + أرقام عربية ────────────────────────
EGP_TEMPLATE = "فودافون = vodafone\nانستا, انستاباي = insta\nبنك = bank\nبريد = post"


def test_parse_template_egp_net_and_gross():
    text = "فودافون: 6.10 / صافي 6.15\nانستا 6.12\nبنك 6.08"
    rates = parse_price_message(text, EGP_TEMPLATE, Currency.EGP)
    assert rates["vodafone"] == {"gross": 6.10, "net": 6.15}   # «صافي» ⇒ الثاني net، الأول gross
    assert rates["insta"] == {"net": 6.12}
    assert rates["bank"] == {"net": 6.08}


def test_parse_fallback_when_template_empty():
    text = "فودافون 6.10\nانستاباي 6.12"
    rates = parse_price_message(text, "", Currency.EGP)      # قالب فارغ ⇒ fallback §3
    assert rates["vodafone"] == {"net": 6.10}
    assert rates["insta"] == {"net": 6.12}


def test_parse_tnd_net_only():
    text = "العاصمة 0.34\nجربة 0.33"
    rates = parse_price_message(text, "", Currency.TND)
    assert rates["capital"] == {"net": 0.34} and rates["djerba"] == {"net": 0.33}
    assert "gross" not in rates["capital"], "تونس net فقط (§7 لا خصم)"


def test_parse_arabic_digits_and_comma():
    rates = parse_price_message("فودافون ٦،١٠", "", Currency.EGP)   # أرقام عربية + فاصلة عربية
    assert rates["vodafone"] == {"net": 6.10}


# ── البوّابة + الابتلاع (دالّة نقيّة، لا pipeline) ────────────────────────────
def _cfg(**over):
    base = dict(egp_room_jid="eg@g.us", tnd_room_jid="tn@g.us", egp_price_template=EGP_TEMPLATE)
    base.update(over)
    return FxRatesConfig(**base)


def _msg(jid, sender, text, ts=T0, key="m1"):
    return RawMessage(message_key=key, chat_jid=jid, sender_jid=sender, text=text, received_at=ts)


def test_price_room_currency_maps_jids():
    cfg = _cfg()
    assert price_room_currency("eg@g.us", cfg) is Currency.EGP
    assert price_room_currency("tn@g.us", cfg) is Currency.TND
    assert price_room_currency("other@g.us", cfg) is None


async def _seed_supervisor(db, number="201000000000"):
    await db.employees.upsert(EmployeeRecord(whatsapp_number=number, name="مشرف"))


async def test_ingest_records_for_authorized_sender(db):
    await _seed_supervisor(db)
    msg = _msg("eg@g.us", "201000000000@s.whatsapp.net", "فودافون 6.10\nانستا 6.12")
    snap = await ingest_price_message(db, msg, _cfg())
    assert snap is not None and snap.currency is Currency.EGP
    cur = await db.fx_rates.current(Currency.EGP)
    assert cur.rates["vodafone"]["net"] == 6.10 and cur.rates["insta"]["net"] == 6.12
    assert cur.source_message_key == "m1"


async def test_ingest_ignores_non_price_room(db):
    await _seed_supervisor(db)
    msg = _msg("customer@g.us", "201000000000@s.whatsapp.net", "فودافون 6.10")
    assert await ingest_price_message(db, msg, _cfg()) is None
    assert await db.fx_rates.current(Currency.EGP) is None, "لا تسجيل من غير غرفة أسعار"


async def test_ingest_ignores_unauthorized_sender(db):
    # لا مشرف مزروع → المُرسِل غير معتمد
    msg = _msg("eg@g.us", "999@s.whatsapp.net", "فودافون 6.10")
    assert await ingest_price_message(db, msg, _cfg()) is None
    assert await db.fx_rates.current(Currency.EGP) is None


async def test_ingest_partial_message_merges_over_current(db):
    """رسالة كاملة ثم جزئية: الجزئية تُحدِّث ما فيها فقط، وتبقى اللقطة الحاليّة كاملة (§2)."""
    await _seed_supervisor(db)
    full = _msg("eg@g.us", "201000000000@s.whatsapp.net",
                "فودافون 6.10\nانستا 6.12", ts=T0, key="full")
    await ingest_price_message(db, full, _cfg())
    partial = _msg("eg@g.us", "201000000000@s.whatsapp.net",
                   "بنك 6.08", ts=T0 + timedelta(hours=1), key="part")
    await ingest_price_message(db, partial, _cfg())
    cur = await db.fx_rates.current(Currency.EGP)
    assert set(cur.rates) == {"vodafone", "insta", "bank"}, "الجزئية أضافت بنك وأبقت الباقي"
    assert cur.rates["vodafone"]["net"] == 6.10 and cur.rates["bank"]["net"] == 6.08


# ── نقطة test-parse (HTTP) — تحليل تجريبيّ بلا تخزين ─────────────────────────
def _settings(**over):
    from core.config import Settings
    return Settings(_env_file=None, **over)


@asynccontextmanager
async def _anon_client(db):
    from httpx import ASGITransport, AsyncClient
    from dashboard.app import create_app
    app = create_app(db, _settings())
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@asynccontextmanager
async def _client(db, role=None, *, username="admin"):
    from core.constants import Role
    from core.db import utcnow
    from core.models import UserRecord
    from dashboard.auth import hash_password
    await db.users.create(UserRecord(
        username=username, password_hash=hash_password("pw-123456"),
        role=role or Role.MANAGER, active=True, created_at=utcnow()))
    async with _anon_client(db) as ac:
        r = await ac.post("/api/auth/login", json={"username": username, "password": "pw-123456"})
        assert r.status_code == 200
        yield ac


async def test_test_parse_endpoint_manager_and_no_storage(db):
    pytest.importorskip("fastapi")
    async with _client(db) as ac:
        r = await ac.post("/api/settings/fx-rates/test-parse",
                          json={"text": "فودافون 6.10\nانستا 6.12", "currency": "EGP",
                                "template": EGP_TEMPLATE})
        assert r.status_code == 200
        rates = r.json()["rates"]
        assert rates["vodafone"] == {"net": 6.10} and rates["insta"] == {"net": 6.12}
    assert await db.fx_rates.current(Currency.EGP) is None, "الاختبار لا يُخزّن شيئًا"


async def test_test_parse_endpoint_reviewer_forbidden(db):
    pytest.importorskip("fastapi")
    from core.constants import Role
    async with _client(db, Role.REVIEWER, username="rev") as ac:
        r = await ac.post("/api/settings/fx-rates/test-parse",
                          json={"text": "فودافون 6.10", "currency": "EGP"})
        assert r.status_code == 403
