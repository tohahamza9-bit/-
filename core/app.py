"""
تطبيق FastAPI — يربط كل الوحدات ويشغّل عامل المعالجة الدوري (§15) ويركّب اللوحة (§13).

التكامل مع WhatsApp عبر MongoDB (§7.1): خدمة Baileys تكتب في `raw_messages`
ويقرأ العامل هنا الطابور؛ ومخرجات البوت تُكتب في `outgoing` لترسلها Baileys.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .bus import Bus
from .config import ROOT, get_settings, load_json_config
from .constants import SEED_TREASURIES, RoomType
from .db import Database, utcnow
from .logging_setup import get_logger, setup_logging
from .pipeline import Pipeline
from .verification import recover_pending
from .verification.sql_verifier import SqlVerifier
from .writers.moneyado.writer import MoneyadoWriter

log = get_logger(__name__)

# فاصل نبضة العامل (§7.1: المعالجة تفرّغ بتأنٍّ)
WORKER_INTERVAL_SECONDS = 2.0


async def _worker_loop(pipeline: Pipeline, stop: asyncio.Event) -> None:
    """عامل المعالجة: فهم/تجميع الجديد ثم نبضة (مطابقة/تصعيد/كتابة). لا يتوقّف على خطأ فردي (T5)."""
    log.info("بدء عامل المعالجة (كل %.1fs)", WORKER_INTERVAL_SECONDS)
    while not stop.is_set():
        now = utcnow()
        try:
            await pipeline.process_inbox(now)
            await pipeline.tick(now)
        except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونكمل الدورة التالية
            log.exception("خطأ في دورة العامل: %s", exc)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=WORKER_INTERVAL_SECONDS)
    log.info("توقّف عامل المعالجة")


async def _seed_rooms_from_env(db, settings) -> None:
    """بذر الغرف من .env أول تشغيل (§شرط 4) — لا يكسر الإعداد القائم.

    🔴 Option A: المركزية/المسؤول تبقى حدود الكتابة من env (bus._guard) — البذر هنا
    للعرض/الالتقاط/الاكتشاف فقط، لا يمنح صلاحية كتابة. لا يلمس أي تصنيف يدوي موجود.
    """
    repo = getattr(db, "rooms", None)
    if repo is None:
        return
    try:
        await repo.seed_if_missing(settings.central_room_jid, RoomType.CENTRAL.value)
        await repo.seed_if_missing(settings.admin_room_jid, RoomType.ADMIN.value)
        for jid in settings.customer_rooms:
            await repo.seed_if_missing(jid, RoomType.CUSTOMER.value)
        for jid in settings.treasury_rooms:
            await repo.seed_if_missing(jid, RoomType.TREASURY.value)
    except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونكمل (البذر غير حرج للإقلاع)
        log.warning("تعذّر بذر الغرف من env: %s", exc)


def _build_verifier(settings) -> SqlVerifier:
    """يبني SqlVerifier من الإعداد؛ يقرأ استعلامات SQL إن توفّر ملفها (وإلا معطّل §11.4)."""
    queries: dict = {}
    if settings.sql_enabled:
        try:
            queries = load_json_config(str(ROOT / "config" / "sql_queries.json"))
        except FileNotFoundError as exc:
            log.error("SQL مفعّل لكن ملف الاستعلامات مفقود: %s — سيُعطَّل التحقّق.", exc)
    return SqlVerifier(settings.sql_dsn, queries, settings.sql_enabled and bool(queries))


def create_app(db: Optional[Database] = None, settings=None, *, run_worker: bool = True) -> FastAPI:
    """
    مصنع التطبيق. يُمرَّر db/settings للاختبار؛ وإلا يُبنيان من البيئة.
    run_worker=False يعطّل العامل الخلفي (للاختبار).
    """
    settings = settings or get_settings()
    setup_logging(settings.log_dir)

    app = FastAPI(title="MONEYADO Bot", version="1.0")
    state: dict = {}

    async def _startup() -> None:
        _db = db or Database(settings.mongo_uri, settings.mongo_db)
        if db is None:
            await _db.connect()
            await _db.ensure_indexes()
        await _db.treasuries.seed_if_empty(SEED_TREASURIES)
        await _db.treasuries.dedupe_by_code()  # تنظيف ذاتي: خزينة واحدة لكل كود (يزيل المكرّرات)
        await _seed_rooms_from_env(_db, settings)  # §شرط 4 — بذر الغرف الحالية من env

        bus = Bus(_db, settings.allowed_output_jids, settings.central_room_jid, settings.admin_room_jid)
        writer = MoneyadoWriter(settings=settings)
        verifier = _build_verifier(settings)
        pipeline = Pipeline(
            _db, bus, writer, verifier,
            customer_room_jids=settings.customer_rooms,
            treasury_room_jids=settings.treasury_rooms,
        )
        state["db"] = _db
        state["pipeline"] = pipeline
        state["stop"] = asyncio.Event()

        # ركّب اللوحة (§13) بعد جهوزية db — الراوتر يحمل بادئة /api داخليًا
        from dashboard.app import get_router
        app.include_router(get_router(_db, settings))

        # 🔴 صمّام الاسترجاع بعد الإطفاء (§12، §9): قبل تشغيل العامل، افحص SQL لما كان قيد
        # الإدخال لحظة الإطفاء ونظّف الدفتر — لا إدخال مزدوج أبدًا.
        if verifier.enabled:
            try:
                report = await recover_pending(_db, verifier)
                if report:
                    log.info("استرجاع بعد الإطفاء (§12): فُحصت %d صفقة قيد الإدخال", len(report))
            except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونمنع التشغيل التلقائي حتى المراجعة
                log.exception("فشل الاسترجاع بعد الإطفاء (§12): %s — لن يبدأ العامل تلقائيًا.", exc)
                run_worker_now = False
            else:
                run_worker_now = run_worker
        else:
            log.warning("SQL معطّل — تخطّي الاسترجاع بعد الإطفاء (§12). فعّله قبل التخزين الحقيقي.")
            run_worker_now = run_worker

        # 🔴 حجْر الإقلاع (§7.3): صفقات معلّقة قديمة (>120s) لا تُعالَج تلقائيًّا عند إعادة التشغيل
        #    (تفادي إدخال حوالة قديمة) — تُحجَر ESCALATED مع تنبيه المسؤول، قبل تشغيل العامل.
        try:
            quarantined = await pipeline.expire_stale_on_startup(utcnow())
            if quarantined:
                log.warning("حجْر الإقلاع: %d صفقة معلّقة قديمة حُجِرت (ESCALATED) بلا كتابة.",
                            len(quarantined))
        except Exception as exc:  # T5 — لا نبتلع؛ نسجّل ونكمل
            log.exception("فشل حجْر الإقلاع: %s — متابعة.", exc)

        if run_worker_now:
            state["worker"] = asyncio.create_task(_worker_loop(pipeline, state["stop"]))
        # القيمة الحقيقية المحفوظة في DB (§13) — لا نصّ ثابت مضلِّل: التخزين يُقرأ حيًّا من bot_control،
        # فالإعداد يبقى بعد إعادة التشغيل (اللوق كان يطبع «إيقاف» دائمًا بغضّ النظر عن DB).
        ctrl = await _db.control.get()
        log.info(
            "MONEYADO Bot جاهز — dry_run=%s، التخزين: %s، auto_trust=%s، state=%s (§13).",
            settings.dry_run,
            "تشغيل" if ctrl.storage_enabled else "إيقاف",
            ctrl.auto_trust, ctrl.state,
        )

    async def _shutdown() -> None:
        if "stop" in state:
            state["stop"].set()
        if "worker" in state:
            with contextlib.suppress(Exception):
                await state["worker"]
        if db is None and "db" in state:
            await state["db"].close()

    app.add_event_handler("startup", _startup)
    app.add_event_handler("shutdown", _shutdown)

    @app.get("/health")
    async def health():  # noqa: ANN201
        ctrl = await state["db"].control.get() if "db" in state else None
        return JSONResponse({
            "status": "ok",
            "storage_enabled": ctrl.storage_enabled if ctrl else None,
            "dry_run": settings.dry_run,
        })

    @app.get("/")
    async def index():  # noqa: ANN201
        html = ROOT / "dashboard" / "static" / "index.html"
        return FileResponse(str(html)) if html.exists() else JSONResponse({"status": "ok"})

    @app.get("/rooms")
    async def rooms_page():  # noqa: ANN201
        """صفحة إدارة الغرف (§2.2) — تصنيف بالأسماء فقط، بلا إدخال JID يدوي."""
        html = ROOT / "dashboard" / "static" / "rooms.html"
        return FileResponse(str(html)) if html.exists() else JSONResponse({"status": "ok"})

    return app


# نقطة الدخول للتشغيل: uvicorn core.app:app
app = None  # يُبنى بواسطة uvicorn factory أدناه إن لزم


def get_app() -> FastAPI:
    return create_app()
