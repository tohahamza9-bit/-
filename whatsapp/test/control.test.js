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

test('فعل مسموح بتوكن صحيح → ينفَّذ بوسائط ثابتة', () => {
  const v = resolveControlRequest({ token: TOK, action: 'start-kernel', expectedToken: TOK });
  assert.equal(v.ok, true);
  assert.deepEqual(v.args, ['/Run', '/TN', TASK_KERNEL]);
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

test('🔴 لا حقن أوامر: الوسائط ثابتة ولا يأتي شيء منها من الطلب', () => {
  const v = resolveControlRequest({ token: TOK, action: 'start-kernel', expectedToken: TOK });
  assert.ok(v.args.every((a) => typeof a === 'string'));
  assert.ok(!v.args.some((a) => /[&|;><$`]/.test(a)), 'لا رموز صدفة في الوسائط');
  assert.equal(v.args.at(-1), TASK_KERNEL, 'اسم المهمّة ثابت من الكود');
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

test('تعديل الوسائط المُعادة لا يلوّث القائمة الأصليّة', () => {
  const v = resolveControlRequest({ token: TOK, action: 'start-kernel', expectedToken: TOK });
  v.args.push('/EXTRA');
  const again = resolveControlRequest({ token: TOK, action: 'start-kernel', expectedToken: TOK });
  assert.deepEqual(again.args, ['/Run', '/TN', TASK_KERNEL]);
});
