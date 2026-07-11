"""
ناقل الإرسال — قاعدة الإخراج الصارمة (§2.2) 🔴 غير قابلة للتفاوض.

> البوت لا يرسل أي رسالة إطلاقًا إلا في مكانين فقط: المركزية (Reply) وغرفة المسؤول.

التنفيذ: قائمة بيضاء (whitelist) صارمة. أي وجهة غير مسموحة → ترفض + تُسجَّل المحاولة.
حتى الخطأ البرمجي لا يكتب في غرفة ممنوعة. كل مخرجات البوت تمرّ من هنا حصرًا.
"""
from __future__ import annotations

import asyncio
from typing import Optional

from .db import Database
from .logging_setup import get_logger
from .models import OutgoingMessage

log = get_logger(__name__)


class OutputBlocked(Exception):
    """محاولة إرسال لوجهة ممنوعة — تُلتقط وتُسجَّل، لا تُنفَّذ."""


class Bus:
    """
    الوجهات المسموحة تُحقَن عند الإنشاء (المركزية + المسؤول فقط).
    غرف الزبائن/الخزائن ليست هنا إطلاقًا → قراءة صامتة مطلقة (§2.2).
    """

    def __init__(self, db: Database, allowed_jids: set[str], central_jid: str, admin_jid: str):
        self._db = db
        self._allowed = {j for j in allowed_jids if j}
        self.central_jid = central_jid
        self.admin_jid = admin_jid

    def _guard(self, chat_jid: str) -> None:
        if chat_jid not in self._allowed:
            # 🔴 لا نرمي بصمت — نُسجّل المحاولة (T5) ثم نرفض
            log.error(
                "🔴 محاولة إرسال محظورة لوجهة غير مسموحة: %s — رُفضت (whitelist §2.2). المسموح: %s",
                chat_jid, self._allowed,
            )
            raise OutputBlocked(f"وجهة ممنوعة: {chat_jid}")

    async def reply(self, chat_jid: str, text: str, reply_to_key: Optional[str]) -> None:
        """Reply على رسالة معيّنة بمفتاحها (§8.3) — لا رسائل «طايرة»، لا منشن @ (LID)."""
        self._guard(chat_jid)
        await self._db.outgoing.enqueue(
            OutgoingMessage(chat_jid=chat_jid, text=text, reply_to_key=reply_to_key)
        )
        log.info("Reply → %s (ردًّا على %s): %s", chat_jid, reply_to_key, text[:80])

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
        log.info("Reaction %s → %s على %s", emoji, chat_jid, message_key)

    # ── مساعدات وجهة صريحة (تمنع الأخطاء) ──
    async def reply_central(self, text: str, reply_to_key: Optional[str]) -> None:
        """تنبيه/تذكير في المركزية (Reply)."""
        await self.reply(self.central_jid, text, reply_to_key)

    async def notify_admin(self, text: str, reply_to_key: Optional[str] = None) -> None:
        """تصعيد لغرفة المسؤول (§8.1 §7.3 §10)."""
        await self.reply(self.admin_jid, text, reply_to_key)

    async def mark_central(self, message_key: str, emoji: str) -> None:
        """علامة 🔸/✅ على الحوالة في المركزية (§8.3)."""
        await self.react(self.central_jid, message_key, emoji)
