/**
 * اختبار وحدة للمُرسِل (node --test) — مسار التفاعلات السريع (§8.3) بمُحاكيات، بلا Baileys/Mongo.
 * يغطّي: تجاوز warm-up/breaker للتفاعلات، عتبة البائت 30ث، العدّاد المنفصل، بقاء تقييد النصوص.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { makeSender } from '../src/sender.js';
import { encodeKey } from '../src/keys.js';

const CENTRAL = '120363000000000001@g.us';
const ADMIN = '120363000000000002@g.us';
const CUSTOMER = '120363000000000003@g.us';
const DESTS = { centralJid: CENTRAL, adminJid: ADMIN };
const KEY = encodeKey({ remoteJid: CENTRAL, id: 'MSG1', fromMe: false, participant: 'emp@s.whatsapp.net' });

const silentLogger = { info() {}, warn() {}, error() {} };

// مُحاكي مجموعة outgoing (سلسلة find→sort→limit→toArray + updateOne)
function makeOutgoing(docs) {
  return {
    docs,
    find() { return this; },
    sort() { return this; },
    limit() { return this; },
    async toArray() { return this.docs.filter((d) => !d.sent); },
    async updateOne(filter, update) {
      const d = this.docs.find((x) => x._id === filter._id);
      if (d) Object.assign(d, update.$set);
    },
  };
}

function makeSock({ fail = false } = {}) {
  const calls = [];
  return {
    calls,
    async sendMessage(jid, content, opts) {
      calls.push({ jid, content, opts });
      if (fail) throw new Error('فشل إرسال محاكى');
    },
  };
}

function reactionDoc(id, { ageMs = 0, jid = CENTRAL, emoji = '✅' } = {}) {
  return { _id: id, chat_jid: jid, reaction: emoji, reply_to_key: KEY,
    created_at: new Date(Date.now() - ageMs), sent: false };
}

// ── التفاعل يُرسَل رغم قاطع الدائرة المفتوح وبلوغ سقف warm-up ──────────────────
test('reaction: يُرسَل رغم breaker مفتوح + warm-up ممتلئ (لا يخضع لهما §8.3)', async () => {
  const sock = makeSock();
  const breaker = { isOpen: () => true, reason: 'مفتوح' };
  const warmup = { canSend: () => false, cap: () => 10, records: 0, record() { this.records += 1; } };
  const doc = reactionDoc('r1');
  const outgoing = makeOutgoing([doc]);
  const sender = makeSender({ sock, outgoing, raw: null, dests: DESTS, breaker, warmup, logger: silentLogger });

  await sender.tick();

  assert.equal(sock.calls.length, 1, 'أُرسل التفاعل رغم الحجب');
  assert.ok(sock.calls[0].content.react, 'حمولة react');
  assert.equal(sock.calls[0].content.react.text, '✅');
  assert.equal(doc.sent, true);
  assert.equal(warmup.records, 0, 'warm-up.record لم يُستدعَ للتفاعل (عدّاد منفصل)');
  assert.equal(sender.reactionsSent(), 1, 'العدّاد المنفصل ازداد');
});

// ── التفاعل البائت (> 30ث) يُطرَح سريعًا بلا إرسال ────────────────────────────
test('reaction: بائت > 30ث → يُطرَح (sent+stale) بلا إرسال', async () => {
  const sock = makeSock();
  const doc = reactionDoc('r2', { ageMs: 40_000 });   // 40ث > 30ث
  const outgoing = makeOutgoing([doc]);
  const sender = makeSender({ sock, outgoing, raw: null, dests: DESTS,
    breaker: { isOpen: () => false }, warmup: { canSend: () => true, record() {}, cap: () => 10 },
    logger: silentLogger });

  await sender.tick();

  assert.equal(sock.calls.length, 0, 'لم يُرسَل البائت');
  assert.equal(doc.sent, true);
  assert.equal(doc.stale, true);
  assert.equal(sender.reactionsSent(), 0);
});

// ── التفاعل الطازج (< 30ث) يُرسَل (ليس بائتًا) ────────────────────────────────
test('reaction: طازج (< 30ث) → يُرسَل (ليس بائتًا)', async () => {
  const sock = makeSock();
  const doc = reactionDoc('r3', { ageMs: 20_000 });   // 20ث < 30ث
  const outgoing = makeOutgoing([doc]);
  const sender = makeSender({ sock, outgoing, raw: null, dests: DESTS,
    breaker: { isOpen: () => false }, warmup: { canSend: () => true, record() {}, cap: () => 10 },
    logger: silentLogger });

  await sender.tick();

  assert.equal(sock.calls.length, 1);
  assert.equal(doc.sent, true);
  assert.equal(doc.stale, undefined);
});

// ── التفاعل لوجهة ممنوعة يُسقَط (الأمن لا يُتخطّى) ─────────────────────────────
test('reaction: وجهة ممنوعة → إسقاط (sent+blocked)، لا إرسال', async () => {
  const sock = makeSock();
  const doc = reactionDoc('r4', { jid: CUSTOMER });
  const outgoing = makeOutgoing([doc]);
  const sender = makeSender({ sock, outgoing, raw: null, dests: DESTS,
    breaker: { isOpen: () => false }, warmup: { canSend: () => true, record() {}, cap: () => 10 },
    logger: silentLogger });

  await sender.tick();

  assert.equal(sock.calls.length, 0);
  assert.equal(doc.sent, true);
  assert.equal(doc.blocked, true);
});

// ── النصّ يبقى خاضعًا لقاطع الدائرة (لم يتغيّر سلوكه) ──────────────────────────
test('text: يبقى محجوبًا عند breaker مفتوح (لا يُرسَل، يبقى للإعادة)', async () => {
  const sock = makeSock();
  const doc = { _id: 't1', chat_jid: CENTRAL, text: 'تنبيه', reaction: null,
    created_at: new Date(), sent: false };
  const outgoing = makeOutgoing([doc]);
  const sender = makeSender({ sock, outgoing, raw: null, dests: DESTS,
    breaker: { isOpen: () => true, reason: 'مفتوح' },
    warmup: { canSend: () => true, record() {}, cap: () => 10 }, logger: silentLogger });

  await sender.tick();

  assert.equal(sock.calls.length, 0, 'النصّ لم يُرسَل (breaker مفتوح)');
  assert.equal(doc.sent, false, 'يبقى للإعادة');
});

// ── التفاعلات تُرسَل حتى مع نصّ محجوب أمامها (تمريرتان منفصلتان) ───────────────
test('reaction: يُرسَل حتى لو سبقه نصّ محجوب بـ breaker (تمريرة التفاعلات مستقلّة)', async () => {
  const sock = makeSock();
  const textDoc = { _id: 't2', chat_jid: CENTRAL, text: 'تنبيه', reaction: null,
    created_at: new Date(Date.now() - 1000), sent: false };
  const reactDoc = reactionDoc('r5');                 // أحدث من النصّ
  const outgoing = makeOutgoing([textDoc, reactDoc]);
  const sender = makeSender({ sock, outgoing, raw: null, dests: DESTS,
    breaker: { isOpen: () => true, reason: 'مفتوح' },
    warmup: { canSend: () => false, cap: () => 10, record() {} }, logger: silentLogger });

  await sender.tick();

  // التفاعل أُرسِل (تمريرة ١)، والنصّ حُجِب (تمريرة ٢) — لا تجويع للتفاعل خلف نصّ محجوب
  assert.equal(sock.calls.length, 1);
  assert.ok(sock.calls[0].content.react);
  assert.equal(reactDoc.sent, true);
  assert.equal(textDoc.sent, false);
});

// ── نصّ التنبيه (is_alert) يُرسَل رغم سقف warm-up ممتلئ + لا يستهلك الميزانية + لا يتجوّع ──
test('alert text: يُرسَل رغم warm-up ممتلئ، بلا استهلاك ميزانية، حتى خلف نصّ عاديّ محجوب', async () => {
  const sock = makeSock();
  const warmup = { canSend: () => false, cap: () => 10, records: 0, record() { this.records += 1; } };
  const normal = { _id: 'n0', chat_jid: CENTRAL, text: 'تأكيد عاديّ', reaction: null,
    reply_to_key: KEY, is_alert: false, created_at: new Date(Date.now() - 1000), sent: false };
  const alert = { _id: 'a0', chat_jid: CENTRAL, text: '⚠️ A9004 — ناقص: الخزينة', reaction: null,
    reply_to_key: KEY, is_alert: true, created_at: new Date(), sent: false };
  const outgoing = makeOutgoing([normal, alert]);
  const sender = makeSender({ sock, outgoing, raw: null, dests: DESTS,
    breaker: { isOpen: () => false }, warmup, logger: silentLogger });

  await sender.tick();

  assert.equal(sock.calls.length, 1, 'التنبيه فقط أُرسِل');
  assert.equal(alert.sent, true, 'التنبيه أُرسِل رغم السقف');
  assert.equal(normal.sent, false, 'النصّ العاديّ حُجِب بالسقف (continue لا يكسر الحماية)');
  assert.equal(warmup.records, 0, 'التنبيه لا يستهلك سقف warm-up');
});

// ── نصّ عاديّ عند سقف ممتلئ → يبقى غير مُرسَل (حماية الحظر سليمة) ─────────────
test('normal text: يبقى غير مُرسَل عند سقف warm-up ممتلئ', async () => {
  const sock = makeSock();
  const doc = { _id: 'n1', chat_jid: CENTRAL, text: 'تأكيد عاديّ', reaction: null,
    reply_to_key: KEY, is_alert: false, created_at: new Date(), sent: false };
  const outgoing = makeOutgoing([doc]);
  const sender = makeSender({ sock, outgoing, raw: null, dests: DESTS,
    breaker: { isOpen: () => false },
    warmup: { canSend: () => false, cap: () => 10, record() {} }, logger: silentLogger });

  await sender.tick();

  assert.equal(sock.calls.length, 0, 'النصّ العاديّ لم يُرسَل (السقف)');
  assert.equal(doc.sent, false, 'يبقى للإعادة');
});
