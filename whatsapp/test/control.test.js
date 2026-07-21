/**
 * اختبار وحدة (node --test) — تحكّم تشغيل الكيرنل عبر الجسر (POST /pm2).
 * منطق نقيّ بلا شبكة/عمليات: القائمة المغلقة + التوكن + fail-closed.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  resolveControlRequest, safeEqual, CONTROL_ACTIONS, TASK_KERNEL,
} from '../src/control.js';

const TOK = 'secret-token-abc';

test('فعل مسموح بتوكن صحيح → verdict رمزيّ (الأمر يُبنى في index.js: عامل بايثون يحرّر المنفذ ثمّ يشغّل)', () => {
  const v = resolveControlRequest({ token: TOK, action: 'start-kernel', expectedToken: TOK });
  assert.equal(v.ok, true);
  assert.equal(v.action, 'start-kernel');       // رمزيّ — لا وسائط schtasks خام (إصلاح عفريت الكيرنل)
});

test('🔴 fail-closed: بلا توكن في البيئة النقطة معطّلة (لا مفتوحة)', () => {
  const v = resolveControlRequest({ token: 'anything', action: 'start-kernel', expectedToken: '' });
  assert.equal(v.ok, false);
  assert.equal(v.status, 503);
});

test('توكن خاطئ/غائب → 403', () => {
  for (const t of ['wrong', '', undefined, null]) {
    const v = resolveControlRequest({ token: t, action: 'start-kernel', expectedToken: TOK });
    assert.equal(v.ok, false, `التوكن ${JSON.stringify(t)} يجب أن يُرفض`);
    assert.equal(v.status, 403);
  }
});

test('🔴 فعل خارج القائمة المغلقة → 400 قبل أيّ تنفيذ', () => {
  for (const a of ['stop-kernel', 'restart', 'delete', '', undefined, 'start-kernel; rm -rf /']) {
    const v = resolveControlRequest({ token: TOK, action: a, expectedToken: TOK });
    assert.equal(v.ok, false, `الفعل ${JSON.stringify(a)} يجب أن يُرفض`);
    assert.equal(v.status, 400);
  }
});

test('🔴 لا حقن أوامر: verdict لا يعكس شيئًا من الطلب سوى الفعل من القائمة المغلقة', () => {
  const v = resolveControlRequest({ token: TOK, action: 'start-kernel', expectedToken: TOK });
  assert.equal(v.action, 'start-kernel');                 // من allow-list حصرًا
  assert.equal(typeof v.args, 'undefined', 'لا وسائط خام تُمرَّر — الأمر يُبنى بمسارات ثابتة في index.js');
});

test('القائمة المغلقة لا تحوي stop/restart (الكيرنل الحيّ ينفّذهما بنفسه)', () => {
  assert.deepEqual(Object.keys(CONTROL_ACTIONS), ['start-kernel']);
});

test('safeEqual: مقارنة صحيحة وطول مختلف', () => {
  assert.equal(safeEqual('abc', 'abc'), true);
  assert.equal(safeEqual('abc', 'abd'), false);
  assert.equal(safeEqual('abc', 'abcd'), false);
  assert.equal(safeEqual('', ''), true);
  assert.equal(safeEqual(undefined, ''), true);
});

test('القائمة المغلقة مُجمّدة (Object.freeze) — لا تُعدَّل من الخارج', () => {
  assert.ok(Object.isFrozen(CONTROL_ACTIONS));
  const v = resolveControlRequest({ token: TOK, action: 'start-kernel', expectedToken: TOK });
  assert.equal(v.action, 'start-kernel');       // ثابتٌ بين النداءات
});
