/**
 * الالتقاط (§7.1 §7.2) — رسائل الغرف المصنّفة النشطة تُخزَّن خام فورًا في Mongo.
 * - messages.upsert  → رسائل جديدة (تخزين خام + كشف التعديل «الحرف»).
 * - messages.update  → تعديلات لاحقة (تحديث النص + edited_at خلال المهلة §7.2).
 *
 * تصنيف الغرف مصدره مجموعة `rooms` في MongoDB (لا env) — hot-reload بلا إعادة تشغيل:
 *   • كل غرفة مجموعة (@g.us) نشطة و type ≠ ignore → التقاط كامل (نص + خام).
 *   • غرفة مجهولة (@g.us) → تُكتشَف metadata (jid + اسم) كـ unclassified **وتُلتقَط** (تُصنَّف لاحقًا من الداشبورد).
 *   • type=ignore أو غرفة موقوفة (active=false) أو الرسائل الفردية → تجاهل (بلا نص).
 * ملاحظة: الالتقاط الشامل لا يمسّ منطق Python — الرسائل غير المركزية مصدر مطابقة صامت (matching محصور
 *   بالنوع)، وتُعلَّم مُعالَجة فورًا. «غير مصنّفة = زبائن» قرار Python منفصل (لا يخصّ هذا الملف).
 * الالتقاط منفصل عن المعالجة (النواة تعالج). T5: كل خطأ يُسجَّل، لا رفض صامت.
 */
import { buildRawDoc, detectEdit, extractText } from './messages.js';
import { encodeKey } from './keys.js';

// أنواع الغرف التي **لا** نلتقط نصوصها (تبقى مُتجاهَلة §13). ما عداها من غرف المجموعات النشطة يُلتقَط.
export const NO_CAPTURE_TYPES = new Set(['ignore']);

/** الغرف مجموعات واتساب (@g.us). الرسائل الفردية (@s.whatsapp.net) ليست غرفًا. */
export function isGroupJid(jid) {
  return typeof jid === 'string' && jid.endsWith('@g.us');
}

/** خريطة jid → {type, active} من صفوف مجموعة rooms (دالة نقيّة قابلة للاختبار). */
export function buildScopeMap(rows) {
  const m = new Map();
  for (const r of rows || []) {
    if (r && r.jid) m.set(r.jid, { type: r.type, active: r.active !== false });
  }
  return m;
}

/** هل نلتقط نصّ هذه الغرفة؟ كل مجموعة (@g.us) نشطة و type ≠ ignore. المجهولة (ليست في الخريطة) تُلتقَط
 *  أيضًا (جديدة نشطة) ثم تُكتشَف/تُصنَّف من الداشبورد. الرسائل الفردية/الموقوفة/المتجاهَلة → لا. */
export function inScopeFrom(scopeMap, jid) {
  if (!isGroupJid(jid)) return false;
  const r = scopeMap.get(jid);
  if (!r) return true;                               // مجهولة/جديدة نشطة → تُلتقَط
  return r.active && !NO_CAPTURE_TYPES.has(r.type);  // نشطة وغير متجاهَلة
}

/**
 * @param {object}   deps
 * @param {import('mongodb').Collection} deps.raw   مجموعة raw_messages
 * @param {import('mongodb').Collection} deps.rooms مجموعة rooms (نطاق + اكتشاف)
 * @param {object}   deps.logger
 * @param {Function} deps.getBotJid
 * @param {Function} [deps.resolveRoomName] async (jid)→اسم المجموعة أو null (cache + lazy §شرط 2)
 * @param {number}   [deps.refreshMs] مهلة تحديث نطاق الغرف من DB (hot-reload §شرط 5)
 * @param {Function} [deps.nowFn] مزوّد وقت (للاختبار)
 */
export function makeCapture({ raw, rooms, logger, getBotJid, resolveRoomName = null, refreshMs = 15000, nowFn = () => Date.now() }) {
  let scope = new Map();          // jid → {type, active}
  let lastLoad = 0;               // آخر تحديث للنطاق من DB
  const discovered = new Set();   // jids حاولنا اكتشافها هذه الجلسة (مرة واحدة لكل jid §شرط 2)

  /** تحميل/تحديث نطاق الغرف من مجموعة rooms (cache قصير — hot-reload §شرط 5). */
  async function loadScope(force = false) {
    const now = nowFn();
    if (!force && lastLoad !== 0 && now - lastLoad < refreshMs) return;
    lastLoad = now;
    try {
      const arr = await rooms.find({}).toArray();
      scope = buildScopeMap(arr);
    } catch (e) {
      logger.error({ err: e }, 'تعذّر تحميل تصنيف الغرف من rooms'); // T5
    }
  }

  function inScope(jid) {
    return inScopeFrom(scope, jid);
  }

  /**
   * اكتشاف غرفة مجهولة — metadata فقط (§شرط 3): jid + اسم كـ unclassified.
   * 🔴 لا يُخزَّن نص أي رسالة من غرفة غير مصنّفة. مرة واحدة لكل jid (§شرط 2).
   */
  async function discover(jid) {
    if (discovered.has(jid)) return;
    discovered.add(jid);
    let name = null;
    try {
      if (resolveRoomName) name = await resolveRoomName(jid);
    } catch (e) {
      logger.warn({ err: e, jid }, 'تعذّر جلب اسم المجموعة عند الاكتشاف'); // T5
    }
    try {
      const update = {
        $setOnInsert: {
          jid,
          type: 'unclassified',
          active: true,
          discovered_at: new Date(),
          updated_by: 'discovery',
        },
      };
      if (name) update.$set = { name, updated_at: new Date() };
      await rooms.updateOne({ jid }, update, { upsert: true });
      logger.info({ jid, name }, 'اكتشاف غرفة جديدة (unclassified) — بانتظار التصنيف');
    } catch (e) {
      logger.error({ err: e, jid }, 'فشل تسجيل اكتشاف الغرفة'); // T5
    }
  }

  /** تخزين خام لرسالة جديدة — upsert على message_key (يطابق النواة، لا ازدواج §12). */
  async function storeRaw(msg) {
    const doc = buildRawDoc(msg, getBotJid());
    await raw.updateOne(
      { message_key: doc.message_key },
      { $setOnInsert: doc },
      { upsert: true },
    );
    logger.debug({ chat: doc.chat_jid, key: doc.message_key }, 'التقاط خام');
  }

  /** تطبيق تعديل «الحرف» (§7.2) — تحديث النص وختم edited_at على الرسالة الأصلية. */
  async function applyEdit(edit) {
    const messageKey = encodeKey(edit.key);
    const text = extractText(edit.editedMessage);
    const res = await raw.updateOne(
      { message_key: messageKey },
      { $set: { text, edited_at: new Date() } },
    );
    if (res.matchedCount === 0) {
      // الأصل لم يُلتقط بعد (تعديل قبل تخزين، أو غرفة غير مصنّفة) — نسجّل ولا نفقد بصمت (T5)
      logger.warn({ key: messageKey }, '«حرف»: تعديل لرسالة غير مخزَّنة بعد');
    } else {
      logger.info({ key: messageKey }, '«حرف» — التُقط تعديل الرسالة (§7.2)');
    }
  }

  async function onUpsert(arg) {
    const messages = arg?.messages || [];
    await loadScope();
    for (const msg of messages) {
      try {
        if (!msg || !msg.key || !msg.message) continue;
        const jid = msg.key.remoteJid;
        if (!isGroupJid(jid)) continue;              // رسائل فرديّة (@s.whatsapp.net) — ليست غرفًا
        // اكتشاف الغرف المجهولة (تسجيل metadata كـ unclassified للتصنيف من الداشبورد §شرط 3)
        if (!scope.has(jid)) await discover(jid);
        // خارج نطاق الالتقاط (موقوفة active=false أو type=ignore صراحةً) → لا نصّ
        if (!inScope(jid)) continue;
        const edit = detectEdit(msg.message);
        if (edit) {
          await applyEdit(edit);
          continue;
        }
        await storeRaw(msg);
      } catch (e) {
        logger.error({ err: e, key: msg?.key }, 'فشل التقاط رسالة (upsert)'); // T5
      }
    }
  }

  async function onUpdate(updates) {
    for (const u of updates || []) {
      try {
        const inner = u?.update?.message;
        if (!inner) continue; // تحديثات حالة (تسليم/قراءة) — تُتجاهل
        const edit = detectEdit(inner) || (u.key ? { key: u.key, editedMessage: inner } : null);
        if (edit) await applyEdit(edit);
      } catch (e) {
        logger.error({ err: e, key: u?.key }, 'فشل معالجة تعديل (update)'); // T5
      }
    }
  }

  return { onUpsert, onUpdate, storeRaw, applyEdit, discover, loadScope };
}
