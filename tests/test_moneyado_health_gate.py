"""
بوابة صحّة MONEYADO (§11.3) — اختبارات معزولة.

السلوك المطلوب (قرار المستخدم):
  • مغلق/مصغّر/نسختان مرئيّتان → **إيقاف** سحب الكتابة. الصفقات تبقى بحالتها القابلة للتنفيذ
    (لا tech_failed بسبب عدم توفّر MONEYADO) + تنبيه 🔴 **مخنوق زمنيًّا** (لا لكل حوالة/تِك).
  • رجوع الجاهزية → استئناف تلقائيّ فوريّ + رسالة ✅ «رجع — جاري تنزيل N».
  • نسختان مرئيّتان → السبب في التنبيه يذكر «نسختان».

يُغطّى مستويان: فحص الجاهزية على مستوى الشاشة (0/1/2 نسخة مرئية)، والبوابة على مستوى الأنبوب.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.constants import OperationType, Status
from core.models import WriteResult
from core.pipeline import Pipeline
from core.writers.moneyado.screens import MoneyadoScreen

NOW = datetime(2026, 7, 16, 12, 0, 0, tzinfo=timezone.utc)
CENTRAL = "central@g.us"
ADMIN = "admin@g.us"


# ═════════════════════════════════════════════════════════════════════════════
# مستوى الشاشة — moneyado_readiness() يعيد استخدام _list/_visible_stock_pids
# ═════════════════════════════════════════════════════════════════════════════
def _screen_with_pids(list_pids, visible_pids, dirty_reason=None):
    """MoneyadoScreen بأدنى إعداد مع حقن نتائج جرد النسخ + فحص الفورم (بلا pywinauto حيّ)."""
    scr = MoneyadoScreen({"sell_screen": {"process_name": "stock.exe"}})
    scr._list_stock_pids = lambda process_name: list_pids
    scr._visible_stock_pids = lambda process_name, pids: visible_pids
    scr._open_form_dirty_reason = lambda pids: dirty_reason   # (د) لا تعداد/ربط نوافذ حيّ في الاختبار
    return scr


def test_readiness_not_running_returns_false():
    ready, reason = _screen_with_pids([], []).moneyado_readiness()
    assert ready is False
    assert "غير مشغّل" in reason


def test_readiness_running_but_not_visible_returns_false():
    # نسخة عاملة لكن لا نافذة مرئية (مغلق/مصغّر)
    ready, reason = _screen_with_pids([111], []).moneyado_readiness()
    assert ready is False
    assert "مغلق" in reason or "مصغّر" in reason


def test_readiness_exactly_one_visible_returns_true():
    ready, reason = _screen_with_pids([111, 222], [111]).moneyado_readiness()
    assert ready is True
    assert reason == ""


def test_readiness_two_visible_returns_false_with_reason():
    ready, reason = _screen_with_pids([111, 222], [111, 222]).moneyado_readiness()
    assert ready is False
    assert "نسخت" in reason  # «نسختان» / «نسختين»


def test_readiness_dirty_open_form_returns_false():
    """(البند د، مُلَيَّن) فورم عملية مفتوح **فيه بيانات** → غير جاهز مع السبب."""
    ready, reason = _screen_with_pids(
        [111, 222], [111], dirty_reason="MONEYADO على فورم فيه بيانات (بقايا حوالة) — راجعه").moneyado_readiness()
    assert ready is False
    assert "بيانات" in reason


def test_readiness_clean_open_form_returns_true():
    """(توجيه المالك) فورم عملية مفتوح **نظيف** (خزينة فقط/فارغ) → جاهز: الكاتب يستأنفه."""
    ready, reason = _screen_with_pids([111, 222], [111], dirty_reason=None).moneyado_readiness()
    assert ready is True
    assert reason == ""


def test_readiness_fail_open_on_exception():
    scr = MoneyadoScreen({"sell_screen": {"process_name": "stock.exe"}})

    def _boom(process_name):
        raise RuntimeError("جرد تعذّر")

    scr._list_stock_pids = _boom
    ready, _ = scr.moneyado_readiness()
    assert ready is True  # لا نحجب الكتابة على خطأ فحص عابر


# ═════════════════════════════════════════════════════════════════════════════
# مستوى الأنبوب — _moneyado_gate()
# ═════════════════════════════════════════════════════════════════════════════
class _FakeBus:
    def __init__(self):
        self.admin_msgs: list[str] = []
        self.central_msgs: list[str] = []

    async def notify_admin(self, text, reply_to_key=None, forward_key=None):
        self.admin_msgs.append(text)

    async def reply_central(self, text, reply_to_key=None, *, is_alert=False):
        self.central_msgs.append(text)


class _FakeWriter:
    name = "fake"

    def __init__(self, ready=True, reason=""):
        self._ready = ready
        self._reason = reason

    def set(self, ready, reason=""):
        self._ready, self._reason = ready, reason

    def moneyado_ready(self):
        return self._ready, self._reason

    async def write(self, job, *, commit):
        return WriteResult(ok=True)


def _make_pipeline(db, writer, bus=None):
    return Pipeline(db, bus or _FakeBus(), writer, None,
                    customer_room_jids=[], treasury_room_jids=[])


async def _seed_pending(db, n):
    """n صفقات PARSED (تُحسب في نصّ التنبيه/الاستئناف)."""
    for i in range(n):
        await db.deals.col.insert_one({"deal_id": f"p{i}", "status": Status.PARSED.value})


async def test_closed_pauses_without_failing_deals(db):
    """مغلق + شغل منتظر → البوابة تُوقف (True) + تنبيه 🔴 واحد، والصفقات لا تتحوّل tech_failed."""
    await _seed_pending(db, 3)
    bus = _FakeBus()
    pipe = _make_pipeline(db, _FakeWriter(ready=False, reason="MONEYADO مغلق/مصغّر (لا نافذة مرئية)"), bus)

    paused = await pipe._moneyado_gate(NOW)

    assert paused is True                      # موقوف
    assert pipe._moneyado_paused is True
    assert len(bus.admin_msgs) == 1
    assert bus.admin_msgs[0].startswith("🔴")
    assert "3 حوالة منتظرة" in bus.admin_msgs[0]
    # لم تُلمَس حالة أي صفقة — تبقى PARSED (قابلة للتنفيذ عند الرجوع)
    still = await db.deals.col.count_documents({"status": Status.PARSED.value})
    assert still == 3


async def test_return_to_ready_resumes_and_notifies(db):
    """بعد إيقاف، رجوع الجاهزية → البوابة تسمح (False) + ✅ استئناف تلقائيّ + خفض العلَم."""
    await _seed_pending(db, 2)
    bus = _FakeBus()
    writer = _FakeWriter(ready=False, reason="MONEYADO مغلق/مصغّر (لا نافذة مرئية)")
    pipe = _make_pipeline(db, writer, bus)

    assert await pipe._moneyado_gate(NOW) is True          # أوّلًا: موقوف
    writer.set(True)                                        # رجع MONEYADO
    resumed = await pipe._moneyado_gate(NOW + timedelta(seconds=5))

    assert resumed is False                                # يسمح بالكتابة
    assert pipe._moneyado_paused is False
    assert pipe._last_gate_alert is None                   # صُفِّر الخنق
    assert any(m.startswith("✅") and "رجع" in m for m in bus.admin_msgs)
    assert any("2 حوالة منتظرة" in m for m in bus.admin_msgs)


async def test_two_instances_pauses_with_reason(db):
    """نسختان مرئيّتان → إيقاف + السبب في التنبيه يذكر «نسخت»."""
    await _seed_pending(db, 1)
    bus = _FakeBus()
    pipe = _make_pipeline(db, _FakeWriter(ready=False, reason="نسختان مرئيتان من MONEYADO (2) — التباس"), bus)

    assert await pipe._moneyado_gate(NOW) is True
    assert len(bus.admin_msgs) == 1
    assert "نسخت" in bus.admin_msgs[0]


async def test_alert_throttled_not_per_tick(db):
    """تنبيهات متتالية داخل نافذة الخنق → تنبيه واحد فقط؛ بعد انقضائها → تنبيه ثانٍ."""
    await _seed_pending(db, 1)
    bus = _FakeBus()
    pipe = _make_pipeline(db, _FakeWriter(ready=False, reason="MONEYADO مغلق/مصغّر (لا نافذة مرئية)"), bus)
    pipe._gate_throttle = 300.0

    # عشر تِكّات خلال ٥٠ ثانية — يجب أن يصدر تنبيه واحد لا عشرة
    for i in range(10):
        assert await pipe._moneyado_gate(NOW + timedelta(seconds=i * 5)) is True
    assert len(bus.admin_msgs) == 1

    # بعد تجاوز نافذة الخنق (٣٠١ ثانية) → تنبيه ثانٍ
    assert await pipe._moneyado_gate(NOW + timedelta(seconds=301)) is True
    assert len(bus.admin_msgs) == 2


async def test_gate_disabled_always_passes(db):
    """البوابة معطّلة (إعداد) → تسمح دائمًا (False) بلا تنبيه حتى لو MONEYADO مغلق."""
    bus = _FakeBus()
    pipe = _make_pipeline(db, _FakeWriter(ready=False, reason="مغلق"), bus)
    pipe._gate_enabled = False

    assert await pipe._moneyado_gate(NOW) is False
    assert bus.admin_msgs == []


async def test_writer_without_readiness_is_fail_open(db):
    """كاتب لا يدعم moneyado_ready (اختبارات مبسّطة) → البوابة تعامله جاهزًا (fail-open)."""
    class _BareWriter:
        name = "bare"

        async def write(self, job, *, commit):
            return WriteResult(ok=True)

    pipe = _make_pipeline(db, _BareWriter())
    assert await pipe._moneyado_gate(NOW) is False
