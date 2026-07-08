/**
 * تحويل رسالة Baileys → مستند raw_messages (يطابق core/models.py::RawMessage).
 * دوال نقيّة (تعتمد keys.js فقط) — قابلة للاختبار بلا Baileys/Mongo.
 */
import { encodeKey, replyKeyFromContext } from './keys.js';

// proto.Message.ProtocolMessage.Type.MESSAGE_EDIT = 14 (§7.2 «الحرف»)
const PROTO_TYPE_MESSAGE_EDIT = 14;

/** استخراج نص الرسالة من أشكال Baileys المختلفة (بما فيها التعديل). */
export function extractText(message) {
  if (!message) return '';
  if (typeof message.conversation === 'string') return message.conversation;
  if (message.extendedTextMessage?.text) return message.extendedTextMessage.text;
  if (message.imageMessage?.caption) return message.imageMessage.caption;
  if (message.videoMessage?.caption) return message.videoMessage.caption;
  if (message.documentMessage?.caption) return message.documentMessage.caption;
  // أغلفة التعديل
  if (message.editedMessage?.message) return extractText(message.editedMessage.message);
  if (message.protocolMessage?.editedMessage) return extractText(message.protocolMessage.editedMessage);
  return '';
}

/** استخراج contextInfo (لبناء reply_to_key من الردود §10). */
export function extractContextInfo(message) {
  if (!message) return null;
  return (
    message.extendedTextMessage?.contextInfo ||
    message.imageMessage?.contextInfo ||
    message.videoMessage?.contextInfo ||
    message.documentMessage?.contextInfo ||
    null
  );
}

/**
 * كشف التعديل (§7.2). يعيد {key, editedMessage} إن كانت الرسالة تعديلًا لأخرى، وإلا null.
 * التعديل يصل كـ protocolMessage(type=MESSAGE_EDIT) يحمل مفتاح الأصل والنص الجديد.
 */
export function detectEdit(message) {
  const pm = message?.protocolMessage;
  if (pm && pm.key && (pm.type === PROTO_TYPE_MESSAGE_EDIT || pm.editedMessage)) {
    return { key: pm.key, editedMessage: pm.editedMessage };
  }
  return null;
}

/** تسلسل آمن للحمولة الخام (يتفادى انفجار Long/Buffer). */
export function toPlain(obj) {
  try {
    return JSON.parse(
      JSON.stringify(obj, (_k, v) => {
        if (typeof v === 'bigint') return v.toString();
        return v;
      }),
    );
  } catch {
    return {};
  }
}

/**
 * يبني مستند RawMessage. الحقول تطابق core/models.py::RawMessage حرفيًا:
 * message_key, chat_jid, sender_jid, text, received_at, edited_at,
 * reply_to_key, is_from_me, raw, processed.
 */
export function buildRawDoc(msg, botJid) {
  const key = msg.key;
  const chatJid = key.remoteJid;
  const text = extractText(msg.message);
  const ctx = extractContextInfo(msg.message);
  const replyToKey = ctx ? replyKeyFromContext(chatJid, ctx, botJid) : null;
  const tsSec = Number(msg.messageTimestamp) || Math.floor(Date.now() / 1000);
  return {
    message_key: encodeKey(key),
    chat_jid: chatJid,
    sender_jid: key.participant || key.remoteJid || null,
    text,
    received_at: new Date(tsSec * 1000),
    edited_at: null,
    reply_to_key: replyToKey,
    is_from_me: !!key.fromMe,
    raw: toPlain(msg),
    processed: false,
  };
}
