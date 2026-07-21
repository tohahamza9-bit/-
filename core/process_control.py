"""
تحكّم تشغيل مكوّنات البوت عبر pm2 (الميزة ١، قرار المالك 2026-07-19).

🔴 أمان: **قائمة أوامر مغلقة تمامًا** — لا شيء من إدخال المستخدم يصل سطر الأوامر. الاسم والفعل
   يُطابَقان حرفيًّا على مجموعتين ثابتتين، وأي قيمة خارجهما ترفع ValueError قبل أي تنفيذ.
   الاستدعاء بلا shell=True، والوسائط قائمة (لا سلسلة تُفسَّر).

🔴 معماريّة «الأمر القاتل»: إيقاف/إعادة تشغيل الكيرنل ينفّذه **الكيرنل نفسه** (اللوحة يخدمها
   core/app.py). لذا الأوامر التي تمسّ الكيرنل تُنفَّذ في **عملية منفصلة مؤجَّلة** (DETACHED مع
   تأخير قصير) فيعود ردّ HTTP للمتصفّح قبل أن يموت الخادم. الأوامر التي لا تمسّه تُنفَّذ فورًا.

ملاحظة: تشغيل الكيرنل وهو **مطفأ** لا يمكن أن يمرّ من هنا (لا خادم يستقبل) — المسار البديل
هو نقطة `POST /pm2` في جسر Node (يعمل دائمًا)، ويوسّطها الداشبورد. راجع whatsapp/src/index.js.
"""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.logging_setup import get_logger

log = get_logger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
KERNEL_PORT = 8000                       # ثابت في الكود — لا يأتي من إدخال المستخدم
PORT_FREE_TIMEOUT = 10.0                 # ثوانٍ: مهلة الانتظار حتى يتحرّر المنفذ (قرار المالك)

KERNEL = "moneyado-kernel"               # ⚠️ ليس تحت pm2 — مهمّة ويندوز مجدولة (انظر أدناه)
BRIDGE = "moneyado-wa"
ALL = "all"

# 🔴 الكيرنل أُخرِج من pm2 (2026-07-19): كان يُقتَل كل دقيقة بـ3221225786 (CTRL_C لمجموعة
#    العمليات) لأن python.exe في .venv وسيطٌ يُعيد التنفيذ بمفسّر آخر فيضيع تتبّع pm2 للـPID.
#    يُدار الآن بمهمّة ويندوز مجدولة `moneyado-kernel` عبر schtasks — قائمة مغلقة أيضًا.
TASK_KERNEL = "moneyado-kernel"          # اسم المهمّة المجدولة (ثابت، لا يأتي من المستخدم)

TARGETS = (KERNEL, BRIDGE, ALL)          # لا شيء غيرها — قائمة مغلقة
ACTIONS = ("start", "stop", "restart")   # لا delete/kill/save من الويب

# تأخير التنفيذ المؤجَّل (ثوانٍ): يكفي لعودة ردّ HTTP وإغلاق الاتصال قبل موت الكيرنل.
SELF_KILL_DELAY = 2


def affects_kernel(target: str) -> bool:
    """هل يمسّ الأمرُ العمليةَ التي تخدم اللوحة (فيلزم التنفيذ المؤجَّل المنفصل)؟"""
    return target in (KERNEL, ALL)


def _validate(action: str, target: str) -> None:
    if action not in ACTIONS:
        raise ValueError(f"فعل غير مسموح: {action!r}")
    if target not in TARGETS:
        raise ValueError(f"هدف غير مسموح: {target!r}")


def _listeners_on_port(port: int = KERNEL_PORT) -> list[int]:
    """PIDs المستمعة (LISTENING) على المنفذ — من netstat. [] إن لا أحد أو تعذّر الفحص.

    قد تُرجِع أكثر من PID (عفريتٌ + عملية جديدة تحت السباق) — نقتلها كلّها في free_port."""
    pids: list[int] = []
    try:
        res = subprocess.run(["cmd", "/c", "netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=20)
        for line in (res.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[1].endswith(f":{port}") \
                    and parts[3].upper() == "LISTENING":
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    except Exception as exc:
        log.warning("تعذّر فحص المنفذ %s (متابعة): %s", port, exc)
    return list(dict.fromkeys(pids))          # فريدة، محافِظةً على الترتيب


def _port_is_free(port: int = KERNEL_PORT) -> bool:
    """True إن **رفض** المنفذ الاتصال (لا مستمع) — إشارة «connection refused» الحقيقيّة التي
    طلبها المالك، أدقّ من غياب سطر netstat وحده (تلتقط TIME_WAIT/الإغلاق الجاري)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.6)
            return s.connect_ex(("127.0.0.1", port)) != 0   # 0 = يوجد مستمع؛ غيره = مرفوض/حرّ
    except OSError:
        return True


def _kill_pid(pid: int) -> None:
    """قتلٌ قسريّ (taskkill /F) — الوحيد الذي يضمن موت العفريت (schtasks /End لا يضمنه)."""
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, text=True, timeout=15)
    except Exception as exc:
        log.warning("تعذّر قتل PID %s: %s", pid, exc)


def free_port(port: int = KERNEL_PORT, timeout: float = PORT_FREE_TIMEOUT,
              poll_interval: float = 0.4) -> bool:
    """يضمن تحرّر المنفذ: يقتل كلّ مالكٍ له (taskkill /F) ثمّ **يستطلع حتى connection refused**
    أو انقضاء المهلة. يُرجِع True إن تحرّر، False إن بقي مشغولًا (عفريتٌ عنيد — يُسجَّل خطأً).

    هذا جوهر إصلاح عفريت الكيرنل: schtasks /End يُرسِل إشارةً لا تضمن الموت، فتبقى عمليةٌ
    قديمةٌ تحجز المنفذ وتردّ /health=200 بينما عاملها ميت. القتل بالـPID + الاستطلاع يحسمها."""
    deadline = time.monotonic() + timeout
    killed: set[int] = set()
    while True:
        if _port_is_free(port):
            return True
        for pid in _listeners_on_port(port):
            log.info("(free_port) قتل مالك المنفذ %s: PID=%s", port, pid)
            _kill_pid(pid)
            killed.add(pid)
        if time.monotonic() >= deadline:
            free = _port_is_free(port)
            if not free:
                log.error("المنفذ %s ما زال مشغولًا بعد %.0fs رغم قتل %s — عفريتٌ عنيد.",
                          port, timeout, sorted(killed) or "—")
            return free
        time.sleep(poll_interval)


def _kernel_status() -> dict:
    """حالة الكيرنل من مصدرها الحقيقيّ: **المنفذ 8000** (مستمع = يعمل) لا من مُشرِف.

    مقصود ألّا نصدّق المُشرِف وحده: تجربة pm2 أظهرت «online» بينما العملية تتقلّب. المنفذ
    هو الحقيقة التي يهمّ المستخدم (هل تُخدَم اللوحة والمعالجة؟).
    """
    pids = _listeners_on_port(KERNEL_PORT)
    if pids:
        return {"name": KERNEL, "status": "online", "restarts": 0, "pid": pids[0],
                "supervisor": "scheduled-task"}
    return {"name": KERNEL, "status": "stopped", "restarts": 0, "pid": None,
            "supervisor": "scheduled-task"}


def list_processes() -> list[dict]:
    """حالة المكوّنات من `pm2 jlist` — [{name, status, restarts, pid, uptime_ms, cpu, memory}].

    لا يرفع عند فشل pm2 (T5): يُرجِع [] ويسجّل — اللوحة تعرض «غير متاح» بدل أن تنكسر.
    """
    try:
        res = subprocess.run(["cmd", "/c", "pm2", "jlist"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=20)
        raw = json.loads(res.stdout or "[]")
    except Exception as exc:                      # pm2 غير مثبّت/عفريت مطفأ/JSON تالف
        # 🔴 الكيرنل مُشرِفه مختلف (مهمّة مجدولة) — عطبُ pm2 يجب ألّا يُخفي حالته.
        log.warning("تعذّرت قراءة pm2 jlist (متابعة، حالة الكيرنل مستقلّة): %s", exc)
        return [_kernel_status()]
    out = []
    for app in raw:
        if not isinstance(app, dict):
            continue
        env = app.get("pm2_env") or {}
        name = app.get("name")
        if name not in (KERNEL, BRIDGE):          # لا نعرض عمليات أخرى للمستخدم
            continue
        out.append({
            "name": name,
            "status": env.get("status"),          # online | stopped | errored | …
            "restarts": env.get("restart_time", 0),
            "pid": app.get("pid") or None,
            "uptime_ms": env.get("pm_uptime"),
            "cpu": (app.get("monit") or {}).get("cpu"),
            "memory": (app.get("monit") or {}).get("memory"),
            "supervisor": "pm2",
        })
    out.append(_kernel_status())                  # الكيرنل خارج pm2 — حالته من المنفذ
    return out


# 🔴 `schtasks /End` **لا يكفي** لقتل الكيرنل: إن كانت المهمّة قد شُغِّلت من الجسر
#    (POST /pm2، عملية node تحت pm2) فإن المجدوِل يُعلن المهمّة «Ready» بينما تبقى عملية
#    uvicorn حيّةً ممسكةً بالمنفذ 8000 — يتيمةً خارج قبضته (تحقّقتُ منه تجريبيًّا 2026-07-20،
#    وتكرّر مرارًا في إعادات التشغيل اليدويّة). العلاج (قرار المالك 2026-07-21): قبل أيّ تشغيل
#    جديد، نقتل مالك المنفذ بالـPID (taskkill /F) ثمّ **نستطلع حتى connection refused** بمهلة
#    ١٠ث، فلا يبدأ الكيرنل الجديد على منفذٍ محجوز. المنطق في بايثون (free_port) — مُختبَرٌ
#    وحتميّ، لا `timeout` أعمى ولا Stop-Process يبتلع أخطاءه.


def _schtasks(verb: str) -> None:
    """schtasks /End أو /Run على مهمّة الكيرنل — قائمة مغلقة (verb ثابت، لا إدخال مستخدم)."""
    if verb not in ("End", "Run"):
        raise ValueError(f"verb غير مسموح: {verb!r}")
    try:
        subprocess.run(["schtasks", f"/{verb}", "/TN", TASK_KERNEL],
                       capture_output=True, text=True, timeout=20)
    except Exception as exc:
        log.warning("schtasks /%s فشل (متابعة): %s", verb, exc)


def _wlog(msg: str) -> None:
    """سجلّ العامل المنفصل — إلى ملفٍ مخصّص (العامل بلا setup_logging، فلوقه العاديّ يضيع)."""
    try:
        p = _ROOT / "artifacts" / "logs" / "kernel_ctl.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {msg}\n")
    except Exception:
        pass


def _detached_worker(action: str) -> None:
    """يُنفَّذ في **عمليةٍ منفصلة** تعيش بعد موت الكيرنل: تحرير المنفذ حتمًا ثمّ التشغيل.

    الترتيب حاسم — القتل **قبل** /Run (بند ٣ من الإصلاح): stop/restart يُنهي المهمّة ثمّ يحرّر
    المنفذ (يقتل العفريت)، وrestart/start لا يشغّل إلّا بعد تأكّد التحرّر (وإلّا فشل الربط)."""
    time.sleep(SELF_KILL_DELAY)                    # عودة ردّ HTTP قبل موت الخادم الذي يخدم الطلب
    _wlog(f"worker start: action={action}")
    if action in ("stop", "restart"):
        _schtasks("End")                           # يمنع إعادة التشغيل التلقائيّ للمهمّة أولًا
        freed = free_port(KERNEL_PORT)
        _wlog(f"after End+free_port: freed={freed}")
    if action in ("start", "restart"):
        # ضمانٌ إضافيّ: لا نشغّل على منفذٍ محجوز (start قد يجد عفريتًا؛ restart بعد تحريرٍ سابق).
        freed = free_port(KERNEL_PORT)
        _schtasks("Run")
        _wlog(f"after free_port+Run: freed={freed}")


def bridge_start_kernel() -> int:
    """يُستدعى من الجسر حين الكيرنل **مطفأ** (لا خادم يستقبل «شغّله»): يحرّر المنفذ — يقتل أيّ
    عفريتٍ عالقٍ يحجزه — ثمّ يشغّل المهمّة، **متزامنًا** (الجسر عمليةٌ منفصلة لا يقتل نفسه، فلا
    تأجيل/انفصال). يُرجِع 0 إن تحرّر المنفذ وشُغِّلت المهمّة، 1 إن بقي محجوزًا (عفريتٌ عنيد).

    (بند ٤ من الإصلاح: نفس منطق free_port في مسار الجسر — مصدرُ حقيقةٍ واحد لا تكرار في node.)"""
    freed = free_port(KERNEL_PORT)
    _schtasks("Run")
    _wlog(f"bridge_start_kernel: freed={freed}")
    return 0 if freed else 1


def _run_kernel_task(action: str) -> dict:
    """يُطلِق العامل المنفصل (kill+poll+start) — يعيش بعد موت الكيرنل الذي يخدم الطلب.

    يُشغَّل بمفسّر الـvenv نفسه (sys.executable) مع cwd=جذر المستودع كي يستورد core.*.
    قائمة مغلقة: action محقَّقٌ سلفًا في run_action، ولا شيء من إدخال المستخدم يصل هنا."""
    worker = (
        "import sys; sys.path.insert(0, r'%s'); "
        "from core.process_control import _detached_worker; _detached_worker('%s')"
        % (str(_ROOT), action)
    )
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen([sys.executable, "-c", worker], cwd=str(_ROOT), creationflags=flags,
                         close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("مهمّة الكيرنل: جُدوِل «%s» عاملًا منفصلًا (قتل بالـPID + استطلاع %.0fs).",
                 action, PORT_FREE_TIMEOUT)
        return {"ok": True, "deferred": True,
                "detail": f"سيُنفَّذ «{action}» على مهمّة الكيرنل خلال {SELF_KILL_DELAY} ثوانٍ "
                          f"(تحرير المنفذ بالقتل ثمّ الاستطلاع)."}
    except Exception as exc:
        log.error("تعذّرت جدولة أمر الكيرنل %s: %s", action, exc)
        return {"ok": False, "deferred": True, "detail": str(exc)}


def run_action(action: str, target: str, *, deferred: Optional[bool] = None) -> dict:
    """ينفّذ `pm2 <action> <target>` بعد التحقّق من القائمة المغلقة.

    deferred=None (الافتراضي) → يُقرَّر تلقائيًّا: مؤجَّل منفصل إن كان الأمر يمسّ الكيرنل.
    يُرجِع {'ok', 'deferred', 'detail'}؛ لا يرفع إلا على قيمة غير مسموحة (ValueError).
    """
    _validate(action, target)
    if deferred is None:
        deferred = affects_kernel(target)

    # الكيرنل خارج pm2 → أوامره عبر schtasks (أسماء ثابتة، لا إدخال مستخدم).
    #   start → /Run · stop → /End · restart → /End ثم /Run.
    if target == KERNEL:
        return _run_kernel_task(action)
    if target == ALL:
        # «الكل» = الجسر عبر pm2 + الكيرنل عبر schtasks؛ الكيرنل آخرًا (يقتل الخادم).
        bridge = run_action(action, BRIDGE, deferred=False)
        kernel = _run_kernel_task(action)
        return {"ok": bridge["ok"] and kernel["ok"], "deferred": True,
                "detail": f"الجسر: {bridge['detail'][:120]} | الكيرنل: {kernel['detail'][:120]}"}

    if not deferred:
        try:
            res = subprocess.run(["cmd", "/c", "pm2", action, target], capture_output=True,
                                 text=True, encoding="utf-8", errors="replace", timeout=60)
            ok = res.returncode == 0
            log.info("pm2 %s %s → rc=%s", action, target, res.returncode)
            return {"ok": ok, "deferred": False,
                    "detail": (res.stdout or res.stderr or "").strip()[-400:]}
        except Exception as exc:
            log.error("فشل تنفيذ pm2 %s %s: %s", action, target, exc)
            return {"ok": False, "deferred": False, "detail": str(exc)}

    # مؤجَّل + منفصل: الأمر يقتل الخادم الذي يخدم الطلب. `timeout` يمنح الردّ فرصة العودة،
    # وأعلام الانفصال تمنع موت الأمر مع العملية الأمّ.
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    cmd = ["cmd", "/c", f"timeout /t {SELF_KILL_DELAY} /nobreak >nul & pm2 {action} {target}"]
    try:
        subprocess.Popen(cmd, creationflags=flags, close_fds=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        log.info("pm2 %s %s — جُدوِل تنفيذًا منفصلًا بعد %ss (يمسّ الكيرنل).",
                 action, target, SELF_KILL_DELAY)
        return {"ok": True, "deferred": True,
                "detail": f"سيُنفَّذ «{action} {target}» خلال {SELF_KILL_DELAY} ثوانٍ."}
    except Exception as exc:
        log.error("تعذّرت جدولة pm2 %s %s: %s", action, target, exc)
        return {"ok": False, "deferred": True, "detail": str(exc)}
