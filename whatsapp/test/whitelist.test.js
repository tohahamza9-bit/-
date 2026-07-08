/**
 * اختبار وحدة خفيف (node --test) — منطق نقيّ فقط، بلا Baileys/Mongo.
 * يغطّي: whitelist (§2.2)، الاستقرار/عدم الحظر (§14)، ترميز المفاتيح، بناء raw.
 * التشغيل: node --test test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  assertAllowedDestination,
  isAllowedDestination,
  OutputBlockedError,
} from '../src/whitelist.js';
import {
  encodeKey,
  decodeKey,
  replyKeyFromContext,
  jidsEqual,
} from '../src/keys.js';
import {
  gaussianBetween,
  sendDelayMs,
  roomGapMs,
  CircuitBreaker,
  WarmupLimiter,
  pickBrowser,
} from '../src/antiban.js';
import { extractText, detectEdit, buildRawDoc, extractContextInfo } from '../src/messages.js';

const CENTRAL = '120363000000000001@g.us';
const ADMIN = '120363000000000002@g.us';
const CUSTOMER = '120363000000000003@g.us';
const TREASURY = '120363000000000004@g.us';
const DESTS = { centralJid: CENTRAL, adminJid: ADMIN };

// ── §2.2 قاعدة الإخراج الصارمة ────────────────────────────────────────────────
test('whitelist: يقبل المركزية', () => {
  assert.equal(isAllowedDestination(CENTRAL, DESTS), true);
  assert.equal(assertAllowedDestination(CENTRAL, DESTS), true);
});

test('whitelist: يقبل غرفة المسؤول', () => {
  assert.equal(isAllowedDestination(ADMIN, DESTS), true);
  assert.equal(assertAllowedDestination(ADMIN, DESTS), true);
});

test('whitelist: يرفض غرفة زبون (throw)', () => {
  assert.equal(isAllowedDestination(CUSTOMER, DESTS), false);
  assert.throws(() => assertAllowedDestination(CUSTOMER, DESTS), OutputBlockedError);
});

test('whitelist: يرفض غرفة خزينة (throw)', () => {
  assert.equal(isAllowedDestination(TREASURY, DESTS), false);
  assert.throws(() => assertAllowedDestination(TREASURY, DESTS), OutputBlockedError);
});

test('whitelist: يرفض jid فارغ/غير معروف', () => {
  assert.equal(isAllowedDestination('', DESTS), false);
  assert.equal(isAllowedDestination(undefined, DESTS), false);
  assert.throws(() => assertAllowedDestination('random@s.whatsapp.net', DESTS), OutputBlockedError);
});

test('whitelist: يسجّل المحاولة المحظورة (T5 — لا رفض صامت)', () => {
  let logged = null;
  const logger = { error: (_o, m) => { logged = m; } };
  assert.throws(() => assertAllowedDestination(CUSTOMER, DESTS, logger), OutputBlockedError);
  assert.ok(logged && logged.includes(CUSTOMER));
});

// ── ترميز المفاتيح ومطابقة الردود ────────────────────────────────────────────
test('keys: encode/decode ذهاب وإياب', () => {
  const key = { remoteJid: CENTRAL, id: 'ABC123', fromMe: false, participant: '2010000@s.whatsapp.net' };
  const enc = encodeKey(key);
  const dec = decodeKey(enc);
  assert.equal(dec.remoteJid, key.remoteJid);
  assert.equal(dec.id, key.id);
  assert.equal(dec.fromMe, false);
  assert.equal(dec.participant, key.participant);
});

test('keys: encode يرفض مفتاحًا ناقصًا', () => {
  assert.throws(() => encodeKey({ remoteJid: CENTRAL }));
  assert.throws(() => encodeKey(null));
});

test('keys: reply_to_key يطابق message_key الأصلي (§7.3/§10 المطابقة)', () => {
  const employee = '2010000000@s.whatsapp.net';
  const original = { remoteJid: CENTRAL, id: 'MSGID99', fromMe: false, participant: employee };
  const originalKey = encodeKey(original);
  // رسالة رد: contextInfo.stanzaId = معرّف الأصل، participant = مُرسِل الأصل
  const ctx = { stanzaId: 'MSGID99', participant: employee };
  const replyKey = replyKeyFromContext(CENTRAL, ctx, 'BOT@s.whatsapp.net');
  assert.equal(replyKey, originalKey); // مساواة نصّية → المطابقة تصحّ
});

test('keys: reply لرسالة البوت نفسه يطابق أيضًا', () => {
  const bot = 'BOT@s.whatsapp.net';
  const original = { remoteJid: CENTRAL, id: 'SELF1', fromMe: true, participant: bot };
  const ctx = { stanzaId: 'SELF1', participant: bot };
  assert.equal(replyKeyFromContext(CENTRAL, ctx, bot), encodeKey(original));
});

test('keys: jidsEqual يتسامح مع جزء الجهاز', () => {
  assert.equal(jidsEqual('2010@s.whatsapp.net', '2010:12@s.whatsapp.net'), true);
  assert.equal(jidsEqual('2010@s.whatsapp.net', '2011@s.whatsapp.net'), false);
});

// ── §14.1 W1/W1-B: تأخير gaussian ضمن الحدود ─────────────────────────────────
test('antiban: gaussianBetween ضمن الحدود دائمًا', () => {
  for (let i = 0; i < 2000; i++) {
    const x = gaussianBetween(100, 200);
    assert.ok(x >= 100 && x <= 200, `خارج الحد: ${x}`);
  }
});

test('antiban: W1 sendDelay في [4000,9000]', () => {
  for (let i = 0; i < 2000; i++) {
    const d = sendDelayMs();
    assert.ok(d >= 4000 && d <= 9000, `sendDelay=${d}`);
  }
});

test('antiban: W1-B roomGap في [1500,4000]', () => {
  for (let i = 0; i < 2000; i++) {
    const g = roomGapMs();
    assert.ok(g >= 1500 && g <= 4000, `roomGap=${g}`);
  }
});

test('antiban: W2 pickBrowser يعيد ثلاثيّة صالحة', () => {
  const b = pickBrowser(() => 0);
  assert.equal(b.length, 3);
  assert.ok(typeof b[0] === 'string' && typeof b[1] === 'string');
});

// ── §14.1 W0: قاطع الدائرة ────────────────────────────────────────────────────
test('W0: يقطع بعد تجاوز حد QR (5)', () => {
  const cb = new CircuitBreaker({ maxQr: 5, maxRejections: 3 });
  for (let i = 0; i < 5; i++) assert.equal(cb.onQr(), false); // 1..5 مسموح
  assert.equal(cb.onQr(), true); // السادسة → قطع
  assert.equal(cb.isOpen(), true);
});

test('W0: يقطع بعد 3 رفض', () => {
  const cb = new CircuitBreaker({ maxRejections: 3 });
  assert.equal(cb.onRejection('a'), false);
  assert.equal(cb.onRejection('b'), false);
  assert.equal(cb.onRejection('c'), true); // الثالثة → قطع
  assert.equal(cb.isOpen(), true);
});

test('W0: الاتصال الناجح يصفّر العدّادات', () => {
  const cb = new CircuitBreaker();
  cb.onQr(); cb.onQr(); cb.onRejection('x');
  cb.onConnectionOpen();
  assert.equal(cb.qrCount, 0);
  assert.equal(cb.rejections, 0);
});

test('W0: reset يفكّ القطع (تدخّل يدوي)', () => {
  const cb = new CircuitBreaker({ maxRejections: 1 });
  cb.onRejection('x');
  assert.equal(cb.isOpen(), true);
  cb.reset();
  assert.equal(cb.isOpen(), false);
});

// ── warm-up متدرّج ────────────────────────────────────────────────────────────
test('warm-up: السقف يتصاعد مع الساعات', () => {
  const start = 0;
  const w = new WarmupLimiter({ startTime: start });
  assert.equal(w.cap(start), 10); // الساعة 1
  assert.equal(w.cap(start + 3600000), 50); // الساعة 2
  assert.equal(w.cap(start + 2 * 3600000), 150); // الساعة 3
  assert.equal(w.cap(start + 100 * 3600000), 1500); // يثبت على الأقصى
});

test('warm-up: يمنع تجاوز سقف الساعة ثم يتجدّد', () => {
  const start = 1000;
  const w = new WarmupLimiter({ startTime: start });
  for (let i = 0; i < 10; i++) {
    assert.equal(w.canSend(start), true);
    w.record(start);
  }
  assert.equal(w.canSend(start), false); // بلغ السقف 10
  // ساعة جديدة → يتجدّد النافذة (السقف 50)
  const later = start + 3600001;
  assert.equal(w.canSend(later), true);
});

// ── §7.2 الالتقاط والتعديل «الحرف» ────────────────────────────────────────────
test('messages: extractText من الأشكال المختلفة', () => {
  assert.equal(extractText({ conversation: 'مرحبا' }), 'مرحبا');
  assert.equal(extractText({ extendedTextMessage: { text: 'رد' } }), 'رد');
  assert.equal(extractText({ imageMessage: { caption: 'صورة' } }), 'صورة');
  assert.equal(extractText(null), '');
});

test('messages: detectEdit يكشف تعديل protocolMessage (§7.2 «الحرف»)', () => {
  const editMsg = {
    protocolMessage: {
      type: 14,
      key: { remoteJid: CENTRAL, id: 'ORIG1', fromMe: false, participant: 'e@s.whatsapp.net' },
      editedMessage: { conversation: 'النص المحدّث' },
    },
  };
  const edit = detectEdit(editMsg);
  assert.ok(edit);
  assert.equal(edit.key.id, 'ORIG1');
  assert.equal(extractText(edit.editedMessage), 'النص المحدّث');
  // رسالة عادية → لا تعديل
  assert.equal(detectEdit({ conversation: 'عادي' }), null);
});

test('messages: extractContextInfo يعيد سياق الرد', () => {
  const m = { extendedTextMessage: { text: 'رد', contextInfo: { stanzaId: 'X' } } };
  assert.equal(extractContextInfo(m).stanzaId, 'X');
  assert.equal(extractContextInfo({ conversation: 'x' }), null);
});

test('messages: buildRawDoc يطابق شكل RawMessage (core/models.py)', () => {
  const msg = {
    key: { remoteJid: CENTRAL, id: 'K1', fromMe: false, participant: 'emp@s.whatsapp.net' },
    message: {
      extendedTextMessage: {
        text: 'حوالة',
        contextInfo: { stanzaId: 'PARENT', participant: 'emp@s.whatsapp.net' },
      },
    },
    messageTimestamp: 1700000000,
  };
  const doc = buildRawDoc(msg, 'BOT@s.whatsapp.net');
  // الحقول المطلوبة موجودة بنفس الأسماء
  for (const f of ['message_key', 'chat_jid', 'sender_jid', 'text', 'received_at', 'edited_at', 'reply_to_key', 'is_from_me', 'raw', 'processed']) {
    assert.ok(f in doc, `حقل ناقص: ${f}`);
  }
  assert.equal(doc.chat_jid, CENTRAL);
  assert.equal(doc.text, 'حوالة');
  assert.equal(doc.sender_jid, 'emp@s.whatsapp.net');
  assert.equal(doc.is_from_me, false);
  assert.equal(doc.processed, false);
  assert.equal(doc.edited_at, null);
  assert.ok(doc.received_at instanceof Date);
  // reply_to_key مبنيّ من السياق ويطابق ترميز الأصل
  assert.equal(doc.reply_to_key, encodeKey({ remoteJid: CENTRAL, id: 'PARENT', fromMe: false, participant: 'emp@s.whatsapp.net' }));
});
