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
import subprocess
import sys
from typing import Optional

from core.logging_setup import get_logger

log = get_logger(__name__)

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


def _kernel_status() -> dict:
    """حالة الكيرنل من مصدرها الحقيقيّ: **المنفذ 8000** (مستمع = يعمل) لا من مُشرِف.

    مقصود ألّا نصدّق المُشرِف وحده: تجربة pm2 أظهرت «online» بينما العملية تتقلّب. المنفذ
    هو الحقيقة التي يهمّ المستخدم (هل تُخدَم اللوحة والمعالجة؟).
    """
    status, pid = "stopped", None
    try:
        res = subprocess.run(["cmd", "/c", "netstat", "-ano", "-p", "TCP"], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=20)
        for line in (res.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "TCP" and parts[1].endswith(":8000") \
                    and parts[3].upper() == "LISTENING":
                status, pid = "online", int(parts[4])
                break
    except Exception as exc:
        log.warning("تعذّر فحص منفذ الكيرنل (متابعة): %s", exc)
        return {"name": KERNEL, "status": "unknown", "restarts": 0, "pid": None,
                "supervisor": "scheduled-task"}
    return {"name": KERNEL, "status": status, "restarts": 0, "pid": pid,
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
#    uvicorn حيّةً ممسكةً بالمنفذ 8000 — يتيمةً خارج قبضته. تحقّقتُ منه تجريبيًّا 2026-07-20:
#    تشغيلٌ مباشر ⇒ /End يقتل؛ تشغيلٌ عبر الجسر ⇒ /End يترك العملية حيّة.
#    الأثر لو تُرك: اللوحة تقول «موقوف» والكيرنل يواصل الكتابة في MONEYADO — وهو بالضبط
#    الفخّ الذي بُني عليه هذا الملفّ («الحقيقة من المنفذ لا من المُشرِف»).
#    العلاج: بعد /End نقتل **مالك المنفذ 8000** صراحةً. المنفذ ثابت في الكود لا من المستخدم.
_KILL_PORT_8000 = (
    'powershell -NoProfile -ExecutionPolicy Bypass -Command '
    '"Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | '
    'ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"'
)


def _run_kernel_task(action: str) -> dict:
    """أوامر الكيرنل عبر مُشرِف ويندوز (schtasks) — يُنفَّذ **منفصلًا مؤجَّلًا دائمًا** لأن
    كل أوامره تمسّ الخادم الذي يخدم الطلب (حتى start: يعقبه /End في restart)."""
    if action == "start":
        inner = f'schtasks /Run /TN {TASK_KERNEL}'
    elif action == "stop":
        inner = f'schtasks /End /TN {TASK_KERNEL} & {_KILL_PORT_8000}'
    else:                                          # restart — أُنهِ (وأجهِز على اليتيم) ثم شغّل
        inner = (f'schtasks /End /TN {TASK_KERNEL} & {_KILL_PORT_8000}'
                 f' & timeout /t 3 /nobreak >nul & schtasks /Run /TN {TASK_KERNEL}')
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    cmd = ["cmd", "/c", f"timeout /t {SELF_KILL_DELAY} /nobreak >nul & {inner}"]
    try:
        subprocess.Popen(cmd, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("مهمّة الكيرنل: جُدوِل «%s» منفصلًا بعد %ss.", action, SELF_KILL_DELAY)
        return {"ok": True, "deferred": True,
                "detail": f"سيُنفَّذ «{action}» على مهمّة الكيرنل خلال {SELF_KILL_DELAY} ثوانٍ."}
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
