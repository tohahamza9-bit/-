"""
إصلاح عفريت الكيرنل (Orphan Kernel Race) — free_port: قتل بالـPID + استطلاع حتى التحرّر.

schtasks /End يرسل إشارةً لا تضمن الموت، فتبقى عمليةٌ قديمةٌ تحجز المنفذ وتردّ /health=200
بينما عاملها ميت. الإصلاح: قتل مالك المنفذ بالـPID (taskkill /F) ثمّ الاستطلاع حتى
connection refused بمهلة، قبل أيّ تشغيلٍ جديد.
"""
from __future__ import annotations

import core.process_control as pc


def test_free_port_returns_true_when_already_free(monkeypatch):
    """المنفذ حرٌّ أصلًا (لا مستمع) → True فورًا، بلا قتل."""
    monkeypatch.setattr(pc, "_port_is_free", lambda port=pc.KERNEL_PORT: True)
    killed = []
    monkeypatch.setattr(pc, "_kill_pid", lambda pid: killed.append(pid))
    assert pc.free_port(timeout=1.0) is True
    assert killed == []                                   # لا قتل حين المنفذ حرّ


def test_free_port_kills_owner_then_frees(monkeypatch):
    """عفريتٌ يحجز المنفذ → يُقتَل بالـPID ثمّ يتحرّر → True."""
    state = {"free": False}
    monkeypatch.setattr(pc, "_port_is_free", lambda port=pc.KERNEL_PORT: state["free"])
    monkeypatch.setattr(pc, "_listeners_on_port", lambda port=pc.KERNEL_PORT: [4504])
    killed = []

    def _kill(pid):
        killed.append(pid)
        state["free"] = True                              # القتل حرّر المنفذ

    monkeypatch.setattr(pc, "_kill_pid", _kill)
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    assert pc.free_port(timeout=5.0) is True
    assert killed == [4504]                                # قُتِل مالك المنفذ الفعليّ


def test_free_port_kills_multiple_listeners(monkeypatch):
    """عمليتان تحت السباق (عفريت + جديدة) → تُقتلان كلتاهما."""
    state = {"free": False}
    monkeypatch.setattr(pc, "_port_is_free", lambda port=pc.KERNEL_PORT: state["free"])
    monkeypatch.setattr(pc, "_listeners_on_port", lambda port=pc.KERNEL_PORT: [111, 222])
    killed = []
    monkeypatch.setattr(pc, "_kill_pid",
                        lambda pid: (killed.append(pid), state.__setitem__("free", len(killed) == 2)))
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    assert pc.free_port(timeout=5.0) is True
    assert set(killed) == {111, 222}


def test_free_port_returns_false_for_stubborn_orphan(monkeypatch):
    """عفريتٌ عنيدٌ لا يموت رغم القتل → False بعد المهلة (لا حلقة لا نهائيّة، ولا كذب بالنجاح)."""
    monkeypatch.setattr(pc, "_port_is_free", lambda port=pc.KERNEL_PORT: False)   # يبقى محجوزًا
    monkeypatch.setattr(pc, "_listeners_on_port", lambda port=pc.KERNEL_PORT: [9999])
    monkeypatch.setattr(pc, "_kill_pid", lambda pid: None)
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    assert pc.free_port(timeout=0.5, poll_interval=0.1) is False


def test_bridge_start_kernel_frees_then_runs(monkeypatch):
    """مسار الجسر (بند ٤): يحرّر المنفذ **قبل** /Run (لا يشغّل على منفذٍ محجوز)."""
    order = []
    monkeypatch.setattr(pc, "free_port", lambda *a, **k: (order.append("free"), True)[1])
    monkeypatch.setattr(pc, "_schtasks", lambda verb: order.append(f"schtasks:{verb}"))
    monkeypatch.setattr(pc, "_wlog", lambda m: None)
    rc = pc.bridge_start_kernel()
    assert rc == 0
    assert order == ["free", "schtasks:Run"]               # التحرير قبل التشغيل


def test_detached_worker_restart_order(monkeypatch):
    """restart: End (يمنع الإحياء التلقائيّ) → free_port (يقتل العفريت) → free_port → Run.
    الترتيب حاسم: القتل قبل التشغيل، وEnd قبل القتل."""
    order = []
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    monkeypatch.setattr(pc, "_schtasks", lambda verb: order.append(f"schtasks:{verb}"))
    monkeypatch.setattr(pc, "free_port", lambda *a, **k: (order.append("free"), True)[1])
    monkeypatch.setattr(pc, "_wlog", lambda m: None)
    pc._detached_worker("restart")
    assert order[0] == "schtasks:End"                      # يُنهى أولًا (يمنع إعادة التشغيل)
    assert "free" in order and order.index("free") < order.index("schtasks:Run")   # قتلٌ قبل تشغيل
    assert order[-1] == "schtasks:Run"


def test_detached_worker_stop_does_not_run(monkeypatch):
    """stop: End + free_port فقط — لا /Run (لا يُحيي ما أوقفه المستخدم)."""
    order = []
    monkeypatch.setattr(pc.time, "sleep", lambda s: None)
    monkeypatch.setattr(pc, "_schtasks", lambda verb: order.append(f"schtasks:{verb}"))
    monkeypatch.setattr(pc, "free_port", lambda *a, **k: (order.append("free"), True)[1])
    monkeypatch.setattr(pc, "_wlog", lambda m: None)
    pc._detached_worker("stop")
    assert "schtasks:Run" not in order                     # لا تشغيل بعد الإيقاف
    assert order == ["schtasks:End", "free"]


def test_listeners_on_port_parses_netstat(monkeypatch):
    """_listeners_on_port يستخرج PIDs من مخرجات netstat (LISTENING على المنفذ)، فريدةً."""
    fake = (
        "  TCP    0.0.0.0:8000           0.0.0.0:0              LISTENING       4504\n"
        "  TCP    [::]:8000              [::]:0                 LISTENING       4504\n"
        "  TCP    0.0.0.0:9000           0.0.0.0:0              LISTENING       7777\n"
        "  TCP    127.0.0.1:8000         127.0.0.1:5555         ESTABLISHED     8888\n"
    )

    class _R:
        stdout = fake

    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: _R())
    assert pc._listeners_on_port(8000) == [4504]           # 9000 و ESTABLISHED مُستبعَدان، بلا تكرار
