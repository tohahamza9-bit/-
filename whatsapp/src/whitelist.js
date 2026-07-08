/**
 * قاعدة الإخراج الصارمة (§2.2) 🔴 — غير قابلة للتفاوض.
 * البوت لا يرسل إلا للمركزية (Reply) وغرفة المسؤول. أي وجهة أخرى تُرفض وتُسجَّل.
 * حتى الخطأ البرمجي لا يكتب في غرفة ممنوعة. دالة نقيّة بلا تبعيات (قابلة للاختبار وحدها).
 */

export class OutputBlockedError extends Error {
  constructor(jid, allowed) {
    super(`وجهة ممنوعة: ${jid}`);
    this.name = 'OutputBlockedError';
    this.jid = jid;
    this.allowed = allowed;
  }
}

/** هل الوجهة مسموحة؟ المركزية أو المسؤول فقط. */
export function isAllowedDestination(jid, { centralJid, adminJid } = {}) {
  if (!jid) return false;
  return jid === centralJid || jid === adminJid;
}

/**
 * يتحقّق أو يرمي (throw) — يُستدعى حتمًا قبل أي إرسال (دفاع عميق فوق نواة Python).
 * @param {string} jid وجهة الإرسال
 * @param {{centralJid:string, adminJid:string}} dests
 * @param {{error:Function}} [logger] لتسجيل المحاولة المحظورة (T5 — لا رفض صامت)
 */
export function assertAllowedDestination(jid, dests = {}, logger = null) {
  if (!isAllowedDestination(jid, dests)) {
    const allowed = [dests.centralJid, dests.adminJid].filter(Boolean);
    // 🔴 نُسجّل المحاولة (T5) ثم نرفض — لا نمرّرها بصمت
    if (logger && typeof logger.error === 'function') {
      logger.error(
        { jid, allowed },
        `🔴 محاولة إرسال محظورة لوجهة غير مسموحة: ${jid} — رُفضت (whitelist §2.2)`,
      );
    }
    throw new OutputBlockedError(jid, allowed);
  }
  return true;
}
