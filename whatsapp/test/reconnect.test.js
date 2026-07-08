/**
 * اختبار وحدة خفيف (node --test) — تصنيف أكواد الإغلاق + backoff + فصل W0 (§428 §14.1).
 * منطق نقيّ بلا Baileys/Mongo. التشغيل: node --test test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  classifyDisconnect,
  reconnectDelayMs,
  CircuitBreaker,
  BENIGN_RECONNECT_CODES,
  FATAL_CODES,
} from '../src/antiban.js';

// ── تصنيف أكواد الإغلاق (DisconnectReason) ────────────────────────────────────
test('classifyDisconnect: الأكواد الحميدة (إعادة اتصال)', () => {
  for (const code of [515, 428, 408, 503]) {
    assert.equal(classifyDisconnect(code), 'benign', `يجب أن يكون ${code} حميدًا`);
    assert.ok(BENIGN_RECONNECT_CODES.has(code));
  }
});

test('classifyDisconnect: أكواد الرفض الحقيقي (توقّف)', () => {
  for (const code of [401, 500, 403, 411, 440]) {
    assert.equal(classifyDisconnect(code), 'fatal', `يجب أن يكون ${code} رفضًا حقيقيًا`);
    assert.ok(FATAL_CODES.has(code));
  }
});

test('classifyDisconnect: كود غير معروف/غائب → unknown (يُعامَل حميدًا)', () => {
  assert.equal(classifyDisconnect(9999), 'unknown');
  assert.equal(classifyDisconnect(undefined), 'unknown');
});

test('classifyDisconnect: 428 (سبب المشكلة) ليس fatal', () => {
  // 🔴 جوهر الإصلاح: 428 إغلاق حميد لإعادة الاتصال، لا يُحسب رفضًا يقطع W0.
  assert.equal(classifyDisconnect(428), 'benign');
  // و515 (إعادة الاتصال الإلزامية بعد الاقتران) حميد أيضًا.
  assert.equal(classifyDisconnect(515), 'benign');
});

// ── backoff أُسّي + jitter ────────────────────────────────────────────────────
test('reconnectDelayMs: ضمن [ceil/2, ceil] ويتصاعد مع المحاولة', () => {
  // rng ثابت للتحقّق الحتمي
  assert.equal(reconnectDelayMs(1, { rng: () => 0 }), 1000);   // ceil=2000 → 1000
  assert.equal(reconnectDelayMs(1, { rng: () => 1 }), 2000);   // ceil=2000 → 2000
  assert.equal(reconnectDelayMs(2, { rng: () => 0 }), 2000);   // ceil=4000 → 2000
  assert.equal(reconnectDelayMs(3, { rng: () => 0 }), 4000);   // ceil=8000 → 4000
});

test('reconnectDelayMs: يثبت عند السقف (cap)', () => {
  const d = reconnectDelayMs(20, { rng: () => 1 }); // 2000·2^19 ضخم → مقصوص إلى cap
  assert.equal(d, 30000);
  const dHalf = reconnectDelayMs(20, { rng: () => 0 });
  assert.equal(dHalf, 15000); // ceil/2
});

test('reconnectDelayMs: عيّنات عشوائية ضمن الحدود دائمًا', () => {
  for (let i = 0; i < 1000; i++) {
    const d = reconnectDelayMs(1);
    assert.ok(d >= 1000 && d <= 2000, `خارج الحد: ${d}`);
  }
});

// ── فصل W0: الحميد لا يقطع، الرفق الحقيقي يقطع ────────────────────────────────
test('W0: إعادة الاتصال الحميدة لا تقطع مهما تكرّرت', () => {
  const cb = new CircuitBreaker({ maxRejections: 3 });
  for (let i = 0; i < 50; i++) {
    const attempt = cb.onBenignReconnect();
    assert.equal(attempt, i + 1);
    assert.equal(cb.isOpen(), false); // 🔴 لا يقطع أبدًا على الحميد (إصلاح مصافحة الرقم الجديد)
  }
  assert.equal(cb.rejections, 0); // لم يُحسب أي رفض
});

test('W0: الرفض الحقيقي المتكرّر يقطع عند الحد', () => {
  const cb = new CircuitBreaker({ maxRejections: 3 });
  assert.equal(cb.onRejection('a'), false);
  assert.equal(cb.onRejection('b'), false);
  assert.equal(cb.onRejection('c'), true); // الثالثة → قطع
  assert.equal(cb.isOpen(), true);
});

test('W0: الاتصال الناجح يصفّر عدّاد المحاولات الحميدة والرفض', () => {
  const cb = new CircuitBreaker();
  cb.onBenignReconnect();
  cb.onBenignReconnect();
  cb.onRejection('x');
  cb.onConnectionOpen();
  assert.equal(cb.reconnectAttempts, 0);
  assert.equal(cb.rejections, 0);
});

test('W0: reset يصفّر عدّاد المحاولات الحميدة أيضًا', () => {
  const cb = new CircuitBreaker({ maxRejections: 1 });
  cb.onBenignReconnect();
  cb.onRejection('x');
  assert.equal(cb.isOpen(), true);
  cb.reset();
  assert.equal(cb.isOpen(), false);
  assert.equal(cb.reconnectAttempts, 0);
});
