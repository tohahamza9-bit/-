"""
الميزة ١ — تحكّم تشغيل المكوّنات عبر pm2 (قرار المالك 2026-07-19).

المحور الأمنيّ: **قائمة أوامر مغلقة** — لا شيء من إدخال المستخدم يصل سطر الأوامر، وأي هدف/فعل
خارج المجموعتين يُرفض قبل أي تنفيذ. والمحور المعماريّ: الأوامر التي تمسّ الكيرنل تُنفَّذ منفصلةً
مؤجَّلة كي يعود ردّ HTTP قبل موت الخادم.
"""
from __future__ import annotations

import pytest

from core import process_control as pc


# ── القائمة المغلقة (الحدّ الأمنيّ) ──────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "moneyado-kernel; rm -rf /", "moneyado-kernel && calc", "kernel", "",
    "all | whoami", "../../etc/passwd", "moneyado-wa`whoami`",
])
def test_rejects_targets_outside_allowlist(bad):
    """أي هدف خارج القائمة (بما فيه محاولات الحقن) → ValueError قبل أي تنفيذ."""
    with pytest.raises(ValueError):
        pc.run_action("restart", bad)


@pytest.mark.parametrize("bad", ["delete", "kill", "save", "stop; ls", "", "flush"])
def test_rejects_actions_outside_allowlist(bad):
    """أي فعل خارج {start, stop, restart} → ValueError. delete/kill ممنوعان من الويب."""
    with pytest.raises(ValueError):
        pc.run_action(bad, "moneyado-wa")


def test_allowlists_are_exactly_as_agreed():
    """عقد القائمة المغلقة مثبَّت — توسيعها قرار صريح لا تسرّب."""
    assert set(pc.TARGETS) == {"moneyado-kernel", "moneyado-wa", "all"}
    assert set(pc.ACTIONS) == {"start", "stop", "restart"}


# ── المعماريّة: الأمر القاتل يُنفَّذ منفصلًا مؤجَّلًا ───────────────────────────────────
@pytest.mark.parametrize("target,expected", [
    ("moneyado-kernel", True), ("all", True), ("moneyado-wa", False),
])
def test_kernel_touching_commands_are_deferred(target, expected):
    """ما يمسّ الكيرنل يُؤجَّل (ليعود الردّ أولًا)؛ أوامر الجسر تُنفَّذ فورًا."""
    assert pc.affects_kernel(target) is expected


def test_deferred_command_is_detached_and_delayed(monkeypatch):
    """أمر الكيرنل: Popen منفصل (DETACHED) وفيه تأخير — لا subprocess.run يقتل الخادم فورًا."""
    seen = {}

    class _P:
        def __init__(self, cmd, **kw):
            seen["cmd"] = cmd
            seen["kw"] = kw

    monkeypatch.setattr(pc.subprocess, "Popen", _P)
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: pytest.fail("لا تنفيذ متزامن للكيرنل"))
    res = pc.run_action("restart", "moneyado-kernel")
    assert res["ok"] and res["deferred"] is True
    joined = " ".join(seen["cmd"])
    assert "pm2 restart moneyado-kernel" in joined
    assert "timeout" in joined                       # تأخير يسمح بعودة الردّ
    assert seen["kw"].get("creationflags", 0) != 0    # منفصل عن العملية الأمّ


def test_immediate_command_runs_inline(monkeypatch):
    """أمر الجسر يُنفَّذ فورًا ويُرجِع نتيجة العملية (لا تأجيل)."""
    class _R:
        returncode = 0
        stdout = "ok"
        stderr = ""

    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: _R())
    monkeypatch.setattr(pc.subprocess, "Popen", lambda *a, **k: pytest.fail("لا تأجيل للجسر"))
    res = pc.run_action("restart", "moneyado-wa")
    assert res["ok"] is True and res["deferred"] is False


# ── القراءة: pm2 غير متاح لا يكسر اللوحة (T5) ────────────────────────────────────
def test_list_processes_survives_pm2_failure(monkeypatch):
    """فشل pm2 → قائمة فارغة وتسجيل، لا استثناء يكسر الصفحة."""
    def _boom(*a, **k):
        raise OSError("pm2 not found")

    monkeypatch.setattr(pc.subprocess, "run", _boom)
    assert pc.list_processes() == []


def test_list_processes_filters_foreign_apps(monkeypatch):
    """عمليات pm2 الأخرى لا تُعرَض — المكوّنان فقط."""
    class _R:
        returncode = 0
        stderr = ""
        stdout = ('[{"name":"moneyado-wa","pid":1,"pm2_env":{"status":"online","restart_time":2}},'
                  '{"name":"some-other-app","pid":9,"pm2_env":{"status":"online"}}]')

    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **k: _R())
    out = pc.list_processes()
    assert [p["name"] for p in out] == ["moneyado-wa"]
    assert out[0]["status"] == "online" and out[0]["restarts"] == 2
