"""
ناقل الإرسال — قاعدة الإخراج الصارمة (§2.2) 🔴 غير قابلة للتفاوض.

> البوت لا يرسل أي رسالة إطلاقًا إلا في مكانين فقط: المركزية (Reply) وغرفة المسؤول.

التنفيذ: قائمة بيضاء (whitelist) صارمة. أي وجهة غير مسموحة → ترفض + تُسجَّل المحاولة.
حتى الخطأ البرمجي لا يكتب في غرفة ممنوعة. كل مخرجات البوت تمرّ من هنا حصرًا.
"""
from __future__ import annotations

import asyncio
from typing import Optional

import httpx

from .db import Database
from .logging_setup import get_logger
from .models import OutgoingMessage

log = get_logger(__name__)

# §8.3 ضمان إرسال التفاعلات قبل الصفقة التالية: فاصل الاستطلاع ومهلته القصوى.
REACTION_WAIT_INTERVAL = 0.5
REACTION_WAIT_TIMEOUT = 5.0


class OutputBlocked(Exception):
    """محاولة إرسال لوجهة ممنوعة — تُلتقط وتُسجَّل، لا تُنفَّذ."""


class Bus:
    """
    الوجهات المسموحة تُحقَن عند الإنشاء (المركزية + المسؤول فقط).
    غرف الزبائن/الخزائن ليست هنا إطلاقًا → قراءة صامتة مطلقة (§2.2).
    """

    def __init__(
        self,
        db: Database,
        allowed_jids: set[str],
        central_jid: str,
        admin_jid: str,
        *,
        bridge_url: str = "",
        internal_token: str = "",
    ):
        self._db = db
        self._allowed = {j for j in allowed_jids if j}
        self.central_jid = central_jid
        self.admin_jid = admin_jid
        # جسر واتساب — إشعار فوري بالدفع (§8.3): بعد كل كتابة في outgoing نطلب POST /flush
        #   فيُفرِغ الجسر الطابور فورًا بلا انتظار polling. الفشل يُبتلع (polling يبقى fallback).
        self._bridge_url = (bridge_url or "").rstrip("/")
        self._internal_token = internal_token
        self._bg_tasks: set[asyncio.Task] = set()  # مراجع للمهام الخلفية (منع جمعها بـ GC)

    # ── إشعار الجسر بالدفع الفوري (§8.3) ──
    async def _flush_bridge(self) -> None:
        """يطلب من الجسر إفراغ طابور outgoing فورًا. الفشل (الجسر متوقّف/بطيء) يُبتلع
        بلا تعطيل المسار — polling الدوري في الجسر يبقى fallback (T5: يُسجَّل debug)."""
        if not self._bridge_url:
            return
        try:
            headers = {"X-Internal-Token": self._internal_token} if self._internal_token else {}
            async with httpx.AsyncClient(timeout=1.0) as client:
                await client.post(f"{self._bridge_url}/flush", headers=headers)
        except Exception as exc:  # noqa: BLE001 — الجسر متوقّف/بطيء → polling fallback (لا نُعطّل)
            log.debug("تعذّر إشعار الجسر بالدفع الفوري (%s) — polling fallback.", exc)

    def _poke_bridge(self) -> None:
        """إشعار فوري fire-and-forget بعد كل enqueue — لا ينتظر (لا يبطّئ مسار المعالجة)."""
        if not self._bridge_url:
            return
        task = asyncio.create_task(self._flush_bridge())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def flush_reactions(self) -> None:
        """انتظار دفع الطابور للجسر (best-effort) — يُستدعى بعد وضع العلامة (§8.3) لضمان ظهور
        التفاعل قبل معالجة الحوالة التالية. فشل الجسر يُبتلع (polling fallback)."""
        await self._flush_bridge()

    async def wait_for_reaction_sent(
        self, message_keys: list[str], timeout: float = REACTION_WAIT_TIMEOUT
    ) -> None:
        """انتظار مضمون (best-effort) لتأكيد إرسال تفاعلات هذه المفاتيح فعليًّا (sent=True) قبل
        متابعة الصفقة التالية (§8.3) — فلا تتراكم/تتسابق التفاعلات لو كان الجسر (Baileys) بطيئًا.

        يستطلع db.outgoing كل REACTION_WAIT_INTERVAL ثانية حتى لا يبقى تفاعل غير مُرسَل لهذه
        المفاتيح، أو حتى انقضاء المهلة (5ث افتراضيًّا) فيسجّل تحذيرًا **ويتابع** (لا يوقف البوت).

        بلا جسر مُهيَّأ (اختبار/تشغيل مركزية فقط بلا Baileys) → لا انتظار: لا مُرسِل خارجيّ
        يُعلِّم الطابور sent، فالانتظار سيبلغ المهلة دائمًا بلا فائدة."""
        if not self._bridge_url:
            return
        keys = [k for k in message_keys if k]
        if not keys:
            return
        waited = 0.0
        while True:
            if await self._db.outgoing.pending_reactions(keys) == 0:
                return  # كل التفاعلات أُرسِلت فعلًا → آمن للمتابعة
            if waited >= timeout:
                log.warning(
                    "انتهت مهلة انتظار إرسال التفاعلات (%.1fs) — مفاتيح %s ما زالت غير مُرسَلة؛ "
                    "متابعة بلا توقّف (§8.3، الجسر بطيء/متوقّف).", timeout, keys,
                )
                return
            await asyncio.sleep(REACTION_WAIT_INTERVAL)
            waited += REACTION_WAIT_INTERVAL

    def _guard(self, chat_jid: str) -> None:
        if chat_jid not in self._allowed:
            # 🔴 لا نرمي بصمت — نُسجّل المحاولة (T5) ثم نرفض
            log.error(
                "🔴 محاولة إرسال محظورة لوجهة غير مسموحة: %s — رُفضت (whitelist §2.2). المسموح: %s",
                chat_jid, self._allowed,
            )
            raise OutputBlocked(f"وجهة ممنوعة: {chat_jid}")

    async def reply(self, chat_jid: str, text: str, reply_to_key: Optional[str],
                    *, is_alert: bool = False) -> None:
        """Reply على رسالة معيّنة بمفتاحها (§8.3) — لا رسائل «طايرة»، لا منشن @ (LID).
        is_alert=True: تنبيه حرج يُعفى من سقف warm-up في الجسر (يصل فورًا)."""
        self._guard(chat_jid)
        await self._db.outgoing.enqueue(
            OutgoingMessage(chat_jid=chat_jid, text=text, reply_to_key=reply_to_key, is_alert=is_alert)
        )
        self._poke_bridge()  # إشعار فوري (§8.3)
        log.info("Reply → %s (ردًّا على %s)%s: %s", chat_jid, reply_to_key,
                 " [تنبيه]" if is_alert else "", text[:80])

    async def react(self, chat_jid: str, message_key: str, emoji: str) -> None:
        """تفاعل صامت (🟡/✅/🔴) — مرآة للحالة فقط (§8.3). يُسمح فقط في المركزية.

        فشل الإدراج (عابر) → إعادة محاولة واحدة بعد ثانية (لا نُسقط مرآة الحالة بصمت T5).
        `_guard` خارج الإعادة: رفض الوجهة سياسة دائمة لا تُعاد (§2.2)."""
        self._guard(chat_jid)
        msg = OutgoingMessage(chat_jid=chat_jid, text="", reply_to_key=message_key, reaction=emoji)
        try:
            await self._db.outgoing.enqueue(msg)
        except Exception as exc:  # noqa: BLE001 — نُسجّل ونعيد المحاولة مرّة (لا ابتلاع صامت T5)
            log.warning(
                "فشل إدراج التفاعل %s على %s (%s) — إعادة محاولة واحدة بعد ثانية.",
                emoji, message_key, exc,
            )
            await asyncio.sleep(1)
            await self._db.outgoing.enqueue(msg)
        self._poke_bridge()  # إشعار فوري (§8.3)
        log.info("Reaction %s → %s على %s", emoji, chat_jid, message_key)

    # ── مساعدات وجهة صريحة (تمنع الأخطاء) ──
    async def reply_central(self, text: str, reply_to_key: Optional[str],
                            *, is_alert: bool = False) -> None:
        """تنبيه/تذكير في المركزية (Reply). is_alert=True للتنبيهات الحرجة (⚠️/🔴) — تُعفى من warm-up."""
        await self.reply(self.central_jid, text, reply_to_key, is_alert=is_alert)

    async def notify_admin(
        self, text: str, reply_to_key: Optional[str] = None, forward_key: Optional[str] = None,
    ) -> None:
        """تصعيد لغرفة المسؤول (§8.1 §7.3 §10). forward_key: مفتاح الرسالة الأصلية لإعادة توجيهها
        (forward) مع التنبيه — يُستعمَل عند فشل الصفقة كي يرى المسؤول الحوالة نفسها (§8.3).
        🔴 كل تصعيدات المسؤول تنبيهات حرجة (is_alert=True) — تُعفى من warm-up فتصل فورًا."""
        self._guard(self.admin_jid)
        await self._db.outgoing.enqueue(OutgoingMessage(
            chat_jid=self.admin_jid, text=text, reply_to_key=reply_to_key,
            forward_key=forward_key, is_alert=True,
        ))
        self._poke_bridge()  # إشعار فوري (§8.3)
        log.info("تصعيد للمسؤول%s: %s", " (+forward)" if forward_key else "", text[:80])

    async def mark_central(self, message_key: str, emoji: str) -> None:
        """علامة 🟡/✅/🔴 على الحوالة في المركزية (§8.3)."""
        await self.react(self.central_jid, message_key, emoji)
