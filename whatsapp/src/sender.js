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

const STALE_REACTION_MS = 300_000; // §8.3: تفاعل بائت > 5د لا يُرسَل (لا تفاعل على رسالة قديمة).

export function makeSender({ sock, outgoing, raw, dests, breaker, warmup, logger }) {
  let running = false;
  let ticking = false; // حارس تزامن: يمنع تشابك tick الدوري مع tick القادم من /flush (إرسال مزدوج)
  let lastJid = null;

  /** إرسال بند مع إعادة واحدة بعد ثانية عند الفشل (§8.3). يُرجع true عند النجاح. */
  async function sendWithRetry(d) {
    try {
      await sendOne(d);
      return true;
    } catch (e) {
      logger.warn({ err: e, jid: d.chat_jid }, 'فشل إرسال بند صادر — إعادة واحدة بعد ثانية'); // T5
      await sleep(1000);
      try {
        await sendOne(d);
        return true;
      } catch (e2) {
        logger.error({ err: e2, jid: d.chat_jid }, 'فشل الإرسال مرتين'); // T5
        return false;
      }
    }
  }

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

    // forward الرسالة الأصلية بعد نصّ التنبيه (تصعيد الفشل §8.3): نجلبها من raw ونعيد توجيهها.
    if (d.forward_key && raw) {
      try {
        const orig = await raw.findOne({ message_key: d.forward_key });
        if (orig && orig.raw && orig.raw.message) {
          await sock.sendMessage(d.chat_jid, { forward: orig.raw });
          logger.info('forward → %s (%s)', d.chat_jid, d.forward_key);
        } else {
          logger.warn({ key: d.forward_key }, 'forward: الرسالة الأصلية غير متوفّرة');
        }
      } catch (e) {
        logger.error({ err: e, key: d.forward_key }, 'فشل forward — التنبيه النصّي أُرسِل'); // T5
      }
    }
  }

  /** جولة واحدة على الطابور. محميّة بحارس تزامن (لا تشابك loop مع /flush). */
  async function tick() {
    if (ticking) return; // جولة جارية بالفعل → تفادي إرسال مزدوج لنفس البنود (sent=false)
    ticking = true;
    try {
      await _tickInner();
    } finally {
      ticking = false;
    }
  }

  async function _tickInner() {
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
          // نعلّمها مُرسلة+محظورة كي لا تُعاد للأبد, ونسجّل (T5)
          logger.error({ jid: d.chat_jid, id: String(d._id) }, '🔴 بند صادر لوجهة ممنوعة — أُسقط');
          await outgoing.updateOne(
            { _id: d._id },
            { $set: { sent: true, blocked: true, sent_at: new Date() } },
          );
          continue;
        }
        throw e;
      }

      // §8.3 حماية التراكم: تفاعل بائت (رسالة قديمة > 5د) لا يُرسَل — يُعلَّم مُرسَلًا+بائتًا كي لا
      //   يُعاد، ولا نتفاعل على رسالة قديمة. يُفحص قبل warm-up كي يُنظَّف حتى عند بلوغ السقف.
      //   (النصوص المهمّة — Reply/تصعيد — لا تُسقَط بالعمر هنا؛ تبقى للإعادة.)
      if (d.reaction && d.created_at) {
        const ageMs = Date.now() - new Date(d.created_at).getTime();
        if (ageMs > STALE_REACTION_MS) {
          logger.warn({ id: String(d._id), age_s: Math.round(ageMs / 1000) },
            '⏭️ تفاعل بائت (> 5د) — تخطٍّ بلا إرسال');
          await outgoing.updateOne(
            { _id: d._id },
            { $set: { sent: true, stale: true, sent_at: new Date() } },
          );
          continue;
        }
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

      const ok = await sendWithRetry(d);
      if (ok) {
        warmup.record();
        lastJid = d.chat_jid;
        await outgoing.updateOne({ _id: d._id }, { $set: { sent: true, sent_at: new Date() } });
      } else if (d.reaction) {
        // تفاعل (مرآة تجميلية §8.3) فشل مرتين → يُسقَط (sent+failed) كي لا يكسر الطابور.
        logger.error({ id: String(d._id) }, '🔴 تفاعل فشل مرتين — إسقاط (مرآة فقط)');
        await outgoing.updateOne(
          { _id: d._id },
          { $set: { sent: true, failed: true, sent_at: new Date() } },
        );
      } else {
        // نصّ مهم (Reply/تصعيد مسؤول) — لا يُسقَط؛ يبقى sent=false للإعادة في الجولة التالية.
        logger.error({ id: String(d._id), jid: d.chat_jid },
          '🔴 نصّ فشل مرتين — سيُعاد (لا إسقاط)'); // T5
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
