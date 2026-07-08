/**
 * ترميز/فكّ مفتاح رسالة واتساب (WAMessageKey) ↔ نص ثابت (message_key / reply_to_key).
 * دالة نقيّة بلا تبعيات.
 *
 * الشكل: `remoteJid|id|fromMe|participant`
 * - remoteJid و id لا يحتويان على `|` في واتساب → الفصل آمن.
 * - يُخزَّن كامل المفتاح كي يعيد المُرسِل بناء WAMessageKey بدقّة (Reply/Reaction §8.3)
 *   دون الحاجة للرسالة الأصلية (المُرسِل عملية منفصلة يقرأ Mongo فقط).
 * - المطابقة (§7.3 §10) = مساواة نصّية بسيطة على الجانب Python.
 */

const SEP = '|';

/** ترميز مفتاح Baileys إلى النص المخزَّن. */
export function encodeKey(key) {
  if (!key || !key.remoteJid || !key.id) {
    throw new Error('مفتاح رسالة ناقص (remoteJid/id مطلوبان)');
  }
  const fromMe = key.fromMe ? '1' : '0';
  const participant = key.participant || '';
  return [key.remoteJid, key.id, fromMe, participant].join(SEP);
}

/** فكّ النص المخزَّن إلى WAMessageKey (لإرسال Reply/Reaction). */
export function decodeKey(str) {
  if (typeof str !== 'string' || !str) {
    throw new Error('مفتاح غير صالح (نص فارغ)');
  }
  const parts = str.split(SEP);
  if (parts.length < 3) {
    throw new Error(`صيغة مفتاح غير صالحة: ${str}`);
  }
  const [remoteJid, id, fromMe, ...rest] = parts;
  const participant = rest.join(SEP);
  const key = { remoteJid, id, fromMe: fromMe === '1' };
  if (participant) key.participant = participant;
  return key;
}

/** مقارنة JID متسامحة (تتجاهل جزء الجهاز `:xx` قبل @). */
export function jidsEqual(a, b) {
  if (!a || !b) return false;
  const norm = (j) => j.split('@')[0].split(':')[0] + '@' + (j.split('@')[1] || '');
  return norm(a) === norm(b);
}

/**
 * يبني reply_to_key من contextInfo لرسالة رد — بنفس ترميز message_key الأصلي.
 * - remoteJid = غرفة المحادثة الحالية.
 * - id = stanzaId (معرّف الرسالة الأصلية).
 * - participant = مُرسِل الأصلية (من السياق).
 * - fromMe يُشتقّ من مطابقة participant لرقم البوت (متّسق في الاتجاهين → المطابقة تصحّ).
 */
export function replyKeyFromContext(chatJid, contextInfo, botJid) {
  if (!contextInfo || !contextInfo.stanzaId) return null;
  const participant = contextInfo.participant || '';
  const fromMe = !!(botJid && participant && jidsEqual(participant, botJid));
  return encodeKey({ remoteJid: chatJid, id: contextInfo.stanzaId, fromMe, participant });
}
