"""
اختبار دخان لتطبيق FastAPI (core/app.py): الجهوزية، /health، وتركيب اللوحة (§13).
يستخدم ASGITransport (نفس حلقة الأحداث الخاصة بـ fixture db) بلا خادم فعلي ولا عامل خلفي.
"""
from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from core.app import create_app
from core.config import get_settings


async def _client(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_health_and_dashboard_mounted(db):
    settings = get_settings()
    app = create_app(db=db, settings=settings, run_worker=False)

    # تشغيل أحداث بدء/إيقاف FastAPI يدويًا حول الطلبات
    async with (await _client(app)) as client:
        # startup handler يعمل ضمن سياق lifespan
        import asyncio
        # نشغّل startup يدويًا (add_event_handler)
        for handler in app.router.on_startup:
            await handler()

        r = await client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        # الافتراضي: التخزين إيقاف (§13)
        assert body["storage_enabled"] is False

        # اللوحة مركّبة تحت /api (§13): حالة التحكّم
        r2 = await client.get("/api/control")
        assert r2.status_code == 200

        # صفحة إدارة الغرف تُقدَّم من التطبيق الرئيسي (core.app) وليس فقط من create_app اللوحة.
        # حارس ارتداد: /rooms كانت 404 لأن مسار الصفحة عُرّف في dashboard فقط.
        r3 = await client.get("/rooms")
        assert r3.status_code == 200
        assert "إدارة الغرف" in r3.text

        for handler in app.router.on_shutdown:
            await handler()
