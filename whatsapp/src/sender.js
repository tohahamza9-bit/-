/**
 * الإرسال (§2.2 §8.3) — يستهلك طابور outgoing الذي تكتبه النواة.
 * كل بند: { chat_jid, text, reply_to_key, reaction, sent }.
 *   - reaction موجود → تفاعل صامت (🔸/✅) على الرسالة الهدف.
 *   - غير ذلك       → Reply بمفتاح الرسالة (لا رسائل «طايرة»، لا منشن @ — تفادي LID §8.3).
 * يمرّ حتمًا عبر whitelist (§2.2) + حمايات عدم الحظر (W0/W1/W1-B + warm-up §14.1).
 * T5: كل خطأ يُسجَّل. الفشل لا يعلّم البند sent → إعادة محاولة.
 */
import { assertAllowedDestination, OutputBlockedError } from './whitelist.js';
import { decodeKey } from './keys.js';
import { sendDelayMs, roomGapMs, sleep } from './antiban.js';

export function makeSender({ sock, outgoing, dests, breaker, warmup, logger }) {
  let running = false;
  let lastJid = null;

  /** إرسال بند واحد فعليًا عبر Baileys. */
  async function sendOne(d) {
    if (d.reaction) {
      // تفاعل صامت — reply_to_key يحمل مفتاح الرسالة الهدف (bus.react)
      const key = decodeKey(d.reply_to_key);
      await sock.sendMessage(d.chat_jid, { react: { text: d.reaction, key } });
      logger.info('تفاعل %s → %s', d.reaction, d.chat_jid);
      return;
    }
    const opts = {};
    if (d.reply_to_key) {
      // Reply بمفتاح — نعيد بناء مفتاح الأصل (لا نحتاج الرسالة الأصلية)
      opts.quoted = { key: decodeKey(d.reply_to_key), message: { conversation: '' } };
    }
    await sock.sendMessage(d.chat_jid, { text: d.text || '' }, opts);
    logger.info('Reply → %s: %s', d.chat_jid, (d.text || '').slice(0, 60));
  }

  /** جولة واحدة على الطابور. */
  async function tick() {
    const docs = await outgoing
      .find({ sent: false })
      .sort({ created_at: 1 })
      .limit(20)
      .toArray();

    for (const d of docs) {
      // W0 — القاطع مفتوح → لا إرسال (تدخّل يدوي مطلوب)
      if (breaker.isOpen()) {
        logger.error('🔒 قاطع الدائرة مفتوح — تعليق الإرسال: %s', breaker.reason);
        return;
      }

      // §2.2 🔴 دفاع عميق: حتى لو تسرّبت وجهة ممنوعة إلى الطابور، ترفض هنا
      try {
        assertAllowedDestination(d.chat_jid, dests, logger);
      } catch (e) {
        if (e instanceof OutputBlockedError) {
          // نعلّمها مُرسلة+محظورة كي لا تُعاد للأبد، ونسجّل (T5)
          logger.error({ jid: d.chat_jid, id: String(d._id) }, '🔴 بند صادر لوجهة ممنوعة — أُسقط');
          await outgoing.updateOne(
            { _id: d._id },
            { $set: { sent: true, blocked: true, sent_at: new Date() } },
          );
          continue;
        }
        throw e;
      }

      // warm-up — احترام سقف الساعة
      if (!warmup.canSend()) {
        logger.warn('⏳ بلغ سقف warm-up لهذه الساعة (%d) — تأجيل', warmup.cap());
        return;
      }

      // W1-B — فاصل بين الغرف المختلفة
      if (lastJid && lastJid !== d.chat_jid) await sleep(roomGapMs());
      // W1 — تأخير عشوائي gaussian قبل كل إرسال
      await sleep(sendDelayMs());

      try {
        await sendOne(d);
        warmup.record();
        lastJid = d.chat_jid;
        await outgoing.updateOne({ _id: d._id }, { $set: { sent: true, sent_at: new Date() } });
      } catch (e) {
        // لا نعلّمها sent → تُعاد المحاولة. نسجّل (T5)
        logger.error({ err: e, jid: d.chat_jid }, 'فشل إرسال بند صادر — ستُعاد المحاولة');
      }
    }
  }

  /** حلقة دائمة. */
  async function loop(intervalMs = 2000) {
    running = true;
    while (running) {
      try {
        await tick();
      } catch (e) {
        logger.error({ err: e }, 'خطأ في حلقة الإرسال'); // T5
      }
      await sleep(intervalMs);
    }
  }

  function stop() {
    running = false;
  }

  function isRunning() {
    return running;
  }

  return { tick, loop, stop, sendOne, isRunning };
}
