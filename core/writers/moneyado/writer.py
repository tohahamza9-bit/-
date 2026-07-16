"""
MoneyadoWriter — يعبّئ شاشة «بيع/شراء عملة» ثم «تخزين» (commit) أو STOP آمن (§11).

صمّام الأمان أولوية: أي شكّ = STOP بلا حفظ + needs_review، لا تخمين (القاعدة الذهبية §0).
pywinauto متزامن → يُشغَّل عبر asyncio.to_thread. الشاشة تُحقن (ScreenController) لتُختبر بلا pywinauto.
"""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from ...config import get_settings
from ...constants import OperationType
from ...logging_setup import get_logger
from ...models import WriteJob, WriteResult
from ..base import Writer
from .fields import ENTER_ONLY, build_buy_fields, build_sell_fields
from .screens import MoneyadoScreen, ScreenController

log = get_logger(__name__)

# ── تطبيع الأسماء العربية (للتحقّق من اسم الزبون الظاهر §11.2) ──────────────────
_TASHKEEL = re.compile(r"[ً-ْٰ]")


def _normalize_ar(text: str) -> str:
    """تطبيع خفيف: يشيل التشكيل ويوحّد الألف/الهمزة/التاء المربوطة (تسامح إملاء §8.2)."""
    text = _TASHKEEL.sub("", text or "")
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ة", "ه"), ("ى", "ي")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def _name_reasonable(displayed: Optional[str], expected: Optional[str]) -> bool:
    """§11.2: الاسم الظاهر معقول؟ فارغ = لا. غريب تمامًا = لا. تسامح مع خطأ الإملاء (الكود أضمن)."""
    d = _normalize_ar(displayed or "")
    if not d:
        return False  # فارغ → ⚠️ لا يكمل
    if not expected or not expected.strip():
        return True  # لا اسم مرجعي؛ الكود مرساة الهوية، واسم ظاهر غير فارغ يكفي
    e = _normalize_ar(expected)
    d_tokens = {t for t in d.split() if len(t) >= 2}
    e_tokens = {t for t in e.split() if len(t) >= 2}
    if d_tokens & e_tokens:
        return True  # كلمة مشتركة على الأقل → معقول
    # لا كلمة مشتركة: نقبل التشابه الحرفي القريب (إملاء)، ونرفض «الغريب تمامًا»
    return SequenceMatcher(None, d, e).ratio() >= 0.4


class MoneyadoWriter(Writer):
    """كاتب MONEYADO عبر أتمتة الشاشة (§2.3: بيع/شراء فقط، تخزين/STOP فقط)."""

    name = "moneyado"

    # فترة السبر بين محاولات قراءة اسم الزبون (تُصفَّر في الاختبار للسرعة).
    _NAME_POLL_INTERVAL = 0.5

    def __init__(self, screen: Optional[ScreenController] = None, settings=None) -> None:
        self.settings = settings or get_settings()
        # الشاشة تُحقن في الاختبار؛ في الإنتاج تُبنى من ملف الحقول (coord/class §11.3)
        self._screen = screen
        # مهل قابلة للضبط من .env (جهاز بطيء §11.3)؛ getattr يحمي إعدادات الاختبار المبسّطة.
        # ENTER_WAIT هو ميزانية انتظار ظهور اسم الزبون بعد الكود+Enter (بحث MONEYADO التلقائي):
        # عدد المحاولات = الميزانية ÷ الفترة (3.0 ÷ 0.5 = 6). الفترة تُصفَّر في الاختبار → سبر فوري.
        enter_wait = getattr(self.settings, "moneyado_enter_wait", 3.0)
        self._ENTER_WAIT = enter_wait
        self._NAME_POLL_RETRIES = max(1, round(enter_wait / self._NAME_POLL_INTERVAL))
        # المهلة بعد «تخزين» قبل فحص نافذة طارئة (رصيد/خطأ/تأكيد §11.3).
        self._POST_STORE_WAIT = getattr(self.settings, "moneyado_post_store_wait", 2.0)
        # المهلة بعد Enter على النافذة الرئيسية (إغلاق رسالة التأكيد) حتى تظهر القائمة الرئيسية.
        self._POST_STORE_CLOSE_WAIT = getattr(self.settings, "moneyado_post_store_close_wait", 1.0)
        # DRY_RUN: مهلة معاينة بصرية بعد التعبئة (الشاشة تبقى مفتوحة بلا «تخزين»/«خروج»).
        self._DRY_RUN_WAIT = getattr(self.settings, "moneyado_dry_run_wait", 3.0)
        # 🔴 تأكيد التخزين بحالة الزرّ (§11.3): انتظار الجاهزية قبل الإدخال، وانتظار الإباهت بعد الضغط.
        self._STORE_READY_TIMEOUT = getattr(self.settings, "moneyado_store_ready_timeout", 8.0)
        self._STORE_CONFIRM_TIMEOUT = getattr(self.settings, "moneyado_store_confirm_timeout", 10.0)
        self._STORE_POLL_INTERVAL = getattr(self.settings, "moneyado_store_poll_interval", 0.25)

    def _get_screen(self) -> ScreenController:
        if self._screen is None:
            self._screen = MoneyadoScreen.from_settings(self.settings)
        return self._screen

    async def write(self, job: WriteJob, *, commit: bool) -> WriteResult:
        # pywinauto متزامن → خيط منفصل حتى لا نحجب حلقة asyncio
        return await asyncio.to_thread(self._write_sync, job, commit)

    # ── التنفيذ المتزامن الفعلي ────────────────────────────────────────────────
    def _write_sync(self, job: WriteJob, commit: bool) -> WriteResult:
        op = job.operation
        screen = self._get_screen()
        try:
            # (1) بناء الخانات بالترتيب (نقيّ، بلا شاشة)
            ops = build_sell_fields(job.leg) if op == OperationType.SELL else build_buy_fields(job.leg)

            # (2) التحقّق من استكمال إحداثيات الحقول — لا نخمّن (§11.3). coord=null → رفض + مراجعة
            fcfg = screen.field_config(op)

            def _locatable(cfg: dict) -> bool:
                """الحقل قابل للتحديد إن كان له ترتيب Tab أو إحداثي (§11.3)."""
                return cfg.get("tab_index") is not None or cfg.get("coord") is not None

            # الحقول المطلوب تعبئتها يجب أن تكون قابلة للتحديد (Tab أو إحداثي) — لا نخمّن (§11.1).
            # استثناء: حقول enter_only (يحسبها البرنامج، Enter فقط على الحقل النشط) لا تُحدَّد بإحداثي.
            missing = [o.key for o in ops
                       if o.method != ENTER_ONLY and not _locatable(fcfg.get(o.key, {}))]
            if missing:
                msg = f"حقول غير قابلة للتحديد (لا tab_index/coord): {', '.join(sorted(set(missing)))}"
                log.error("رفض تعبئة %s (job=%s): %s", op.value, job.job_id, msg)
                return WriteResult(ok=False, needs_review=True, error=msg)

            # (3) فتح شاشة العملية من القائمة الرئيسية بنفس البوت (بالنص، أكثر استقرارًا §11.3).
            #     البيع يفتح «بيع عملة»؛ الشراء يفتح «شراء عملة» (بعد أن يكون البيع قد خُزّن
            #     وأُغلقت شاشته بـ confirm_store_on_main، فالبوت الآن على القائمة الرئيسية).
            if op == OperationType.SELL:
                screen.open_sell_screen()
            else:
                screen.open_buy_screen()
            unexpected = screen.check_unexpected_window()
            if unexpected:
                return self._unexpected(screen, op, job, unexpected)

            # (3.5) 🔴 جاهزية الفورم للإدخال مضمونة بحارس عدد الحقول في _bind_form (~21 خانة §0) — فلا
            #       ننتظر تفعيل «تخزين» **قبل** الإدخال: زرّ شاشة الشراء يبقى باهتًا (disabled) حتى تُملأ
            #       الحقول (بخلاف البيع)، فانتظاره قبل الإدخال كان يُحدث deadlock (م: X910 22:00). فحصُ
            #       تفعيل الزرّ نُقِل إلى **بعد التعبئة وقبل الضغط** (أدناه) = «جاهز للحفظ».

            # (4) التعبئة بالترتيب مع فحص النافذة الطارئة بعد كل خطوة
            for field_op in ops:
                # 🔴 حقل enter_only (المبلغ المخصوم §11.1): يحسبه البرنامج — لا تحديد بإحداثي ولا
                #    كتابة ولا مسح. Enter فقط على الحقل النشط (بعد Enter العمولة يكون التركيز عليه)
                #    لتأكيد القيمة المحسوبة والانتقال/تفعيل «البلد». لا يُتخطّى رغم غياب الإحداثي.
                if field_op.method == ENTER_ONLY:
                    screen.press_enter_on_active()
                    unexpected = screen.check_unexpected_window()
                    if unexpected:
                        return self._unexpected(screen, op, job, unexpected)
                    continue

                screen.fill(field_op, fcfg.get(field_op.key, {}))

                # منطق حقل الزبون (§11.2): بعد الكود+Enter يبحث MONEYADO تلقائيًا ويُظهر الاسم.
                if field_op.key == "customer" and op == OperationType.SELL:
                    name_cfg = fcfg.get("customer_name_display", {})
                    if _locatable(name_cfg):
                        # ننتظر ظهور الاسم (البحث التلقائي غير فوري): retry حتى _NAME_POLL_RETRIES.
                        displayed = ""
                        for _ in range(self._NAME_POLL_RETRIES):
                            displayed = screen.read_text(name_cfg)
                            if displayed:
                                break
                            time.sleep(self._NAME_POLL_INTERVAL)
                        if not displayed:
                            # لم يظهر اسم خلال المهلة → لا نعطّل الإدخال (الاسم للتحقّق فقط §11.2،
                            # الكود هو مرساة الهوية). نسجّل تحذيرًا فقط.
                            log.warning("اسم الزبون لم يظهر بعد الكود+Enter خلال المهلة (كود=%s, "
                                        "job=%s) — متابعة الإدخال (الاسم للتحقّق فقط).",
                                        job.leg.customer_code, job.job_id)
                        elif not _name_reasonable(displayed, job.leg.customer_name):
                            # ظهر اسم لكنه غير معقول (مختلف تمامًا) → شكّ حقيقي → STOP + مراجعة (§0).
                            return self._safe_review(
                                screen, op, job,
                                error=f"اسم الزبون الظاهر غير معقول (كود={job.leg.customer_code!r}, ظاهر={displayed!r})",
                            )
                    else:
                        # §11.2 معطّل: موضع «اسم الزبون الظاهر» غير محدَّد (لا coord/tab_index).
                        # ننتظر enter_wait كي يكتمل بحث MONEYADO التلقائي (الكود+Enter) قبل الحقل
                        # التالي — بديل القراءة للتحقّق (الاسم للتحقّق فقط، الكود مرساة الهوية).
                        time.sleep(self._ENTER_WAIT)
                        log.warning("تخطّي تحقّق اسم الزبون الظاهر (§11.2): customer_name_display "
                                    "بلا coord/tab_index (job=%s).", job.job_id)

                unexpected = screen.check_unexpected_window()
                if unexpected:
                    return self._unexpected(screen, op, job, unexpected)

            # (5) القرار: DRY_RUN؟ تخزين؟ أم STOP آمن (الافتراضي الآمن: عدم الحفظ)

            # DRY_RUN (§13، مستقلّ عن Kill Switch): تُعبّأ الشاشة ثم تُترك مفتوحة للمعاينة
            # البصرية — لا «تخزين» ولا «خروج». ✅ «تمّ — DRY_RUN» تُوضع لاحقًا في الأنبوب.
            if self.settings.dry_run:
                time.sleep(self._DRY_RUN_WAIT)   # مهلة معاينة (الشاشة مفتوحة أمام المستخدم)
                log.info("DRY_RUN — %s عُبّئ بلا «تخزين» وبلا «خروج»؛ الشاشة مفتوحة للمعاينة (job=%s).",
                         op.value, job.job_id)
                return WriteResult(ok=True, dry_run=True)

            do_store = commit   # dry_run=False مؤكّد هنا → التخزين يحكمه Kill Switch وحده

            if do_store:
                # (0.5) 🔴 قاعدة صارمة (§11.3): بعد التعبئة وقبل الضغط — «تخزين» يجب أن يكون **مفعَّلًا**
                #       (الحقول اكتملت فقَبِل البرنامجُ الإدخال). إن بقي باهتًا خلال المهلة فالإدخال
                #       ناقص/غير مقبول → **لا نضغط**: إغلاق آمن + تصعيد (لا حفظ ناقص، لا شاشة معلّقة).
                if not screen.wait_store_enabled(op, self._STORE_READY_TIMEOUT, self._STORE_POLL_INTERVAL):
                    log.error("زر «تخزين» لم يُفعَّل بعد التعبئة خلال %ss (إدخال ناقص/غير مقبول) — لا ضغط (job=%s).",
                              self._STORE_READY_TIMEOUT, job.job_id)
                    self._try_stop(screen, op)
                    return WriteResult(ok=False, needs_review=True,
                                       error="زر «تخزين» لم يُفعَّل بعد التعبئة — إدخال ناقص/غير مقبول (لم يُحفَظ)")
                screen.press_store(op)
                # (1) مهلة قصيرة لظهور رسالة التأكيد/نافذة طارئة، ثم فحص النافذة الطارئة **قبل**
                #     تأكيدها بـ Enter (رصيد غير كافٍ/خطأ) — لو ظهرت → إغلاق آمن + تصعيد (RuntimeError).
                time.sleep(self._POST_STORE_CLOSE_WAIT)
                post = screen.check_unexpected_window()
                if post:
                    self._try_stop(screen, op)   # أغلقها (STOP/رجوع) — best-effort
                    raise RuntimeError(f"نافذة غير متوقّعة بعد «تخزين»: {post}")
                # (1.5) 🔴 قاعدة صارمة (§11.3): تأكيد الحفظ قبل أي عملية تالية — ننتظر **إباهت** زرّ
                #       «تخزين» (VB6 يعطّله بعد الحفظ الناجح؛ اختفاء الفورم تأكيدٌ أيضًا). إن بقي
                #       مفعَّلًا خلال المهلة فالتخزين **غير مؤكَّد** → لا ننتقل للتالية إطلاقًا:
                #       نُرجِع فشلًا فيتولّى الأنبوب التصعيد 🔴 + TECH_FAILED ووقف الصفقة (§11.3).
                if not screen.wait_store_confirmed(op, self._STORE_CONFIRM_TIMEOUT, self._STORE_POLL_INTERVAL):
                    log.error("تخزين %s غير مؤكَّد: زر «تخزين» لم يُعطَّل خلال %ss (job=%s) — توقّف + تصعيد.",
                              op.value, self._STORE_CONFIRM_TIMEOUT, job.job_id)
                    return WriteResult(ok=False, needs_review=True,
                                       error=f"تخزين لم يتأكد — زر «تخزين» لم يُعطَّل خلال {self._STORE_CONFIRM_TIMEOUT}s")
                # (2) «رجوع»/Enter على النافذة الرئيسية → إغلاق الشاشة والعودة للقائمة، **مع التحقّق
                #     من إغلاق الشاشة فعلًا** (confirm_store_on_main يعيد الضغط حتى is_main_screen §11.3).
                screen.confirm_store_on_main()
                # (3) انتظار POST_STORE_WAIT (5s) بعد العودة للقائمة **قبل** أي عملية كتابة تالية (buy)
                #     — كي يستقرّ البرنامج تمامًا قبل فتح «شراء عملة» للطرف الثاني (§11.3).
                time.sleep(self._POST_STORE_WAIT)
                log.info("تم «تخزين» %s والعودة للقائمة الرئيسية بعد التحقّق (job=%s).",
                         op.value, job.job_id)
                return WriteResult(ok=True)

            # Kill Switch إيقاف (commit=False، بلا dry_run): عُبّئت الشاشة ثم STOP بلا حفظ (§2.3)
            screen.press_stop(op)
            log.info("تعبئة فقط بلا تخزين ثم STOP آمن %s (job=%s).", op.value, job.job_id)
            return WriteResult(ok=True)

        except Exception as exc:  # لا silent catch (T5) — نسجّل، نوقف بأمان، نصعّد للمراجعة
            log.exception("فشل تقني أثناء كتابة %s (job=%s): %s", op.value, job.job_id, exc)
            self._try_stop(screen, op)
            return WriteResult(ok=False, needs_review=True, error=f"فشل تقني: {exc}")

    # ── مساعدات صمّام الأمان ────────────────────────────────────────────────────
    def _unexpected(self, screen: ScreenController, op: OperationType, job: WriteJob, title: str) -> WriteResult:
        """نافذة طارئة (§11.3): لقطة شاشة + STOP آمن + dead-letter (needs_review)."""
        log.error("نافذة غير متوقّعة أثناء %s (job=%s): %s", op.value, job.job_id, title)
        path = self._screenshot(screen, op, job)
        self._try_stop(screen, op)
        return WriteResult(
            ok=False, needs_review=True, screenshot_path=path,
            error=f"نافذة غير متوقّعة: {title}",
        )

    def _safe_review(self, screen: ScreenController, op: OperationType, job: WriteJob, error: str) -> WriteResult:
        """شكّ (اسم زبون غير معقول): لقطة + STOP آمن + needs_review، بلا حفظ."""
        log.warning("STOP آمن للمراجعة أثناء %s (job=%s): %s", op.value, job.job_id, error)
        path = self._screenshot(screen, op, job)
        self._try_stop(screen, op)
        return WriteResult(ok=False, needs_review=True, screenshot_path=path, error=error)

    def _screenshot(self, screen: ScreenController, op: OperationType, job: WriteJob) -> Optional[str]:
        """يحفظ لقطة في screenshot_dir؛ الفشل لا يُسقط صمّام الأمان (best-effort مسجَّل)."""
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            safe_id = re.sub(r"[^0-9A-Za-z_.-]", "_", str(job.job_id))
            path = str(Path(self.settings.screenshot_dir) / f"{op.value}_{safe_id}_{stamp}.png")
            screen.screenshot(path)
            return path
        except Exception as exc:
            log.warning("تعذّر حفظ لقطة الشاشة (job=%s): %s", job.job_id, exc)
            return None

    def _try_stop(self, screen: ScreenController, op: OperationType) -> None:
        """STOP آمن best-effort — لا يرمي حتى لا يحجب نتيجة المراجعة."""
        try:
            screen.press_stop(op)
        except Exception as exc:
            log.warning("تعذّر STOP الآمن أثناء %s: %s", op.value, exc)
