/**
 * اختبار وحدة (node --test) — تحليل مسار الجلسة (إصلاح استئناف الجلسة عبر إعادة التشغيل).
 * منطق نقيّ بلا Baileys/Mongo. التشغيل: node --test test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';

import { resolveSessionDir } from '../src/config.js';

const PKG = process.platform === 'win32' ? 'C:\\srv\\whatsapp' : '/srv/whatsapp';

// ── الافتراضي: <جذر الحزمة>/session (مطلق، مستقلّ عن cwd) ─────────────────────
test('resolveSessionDir: فارغ → <جذر الحزمة>/session مطلقًا', () => {
  const out = resolveSessionDir('', PKG);
  assert.equal(out, path.join(PKG, 'session'));
  assert.ok(path.isAbsolute(out));
});

test('resolveSessionDir: undefined → نفس الافتراضي المطلق', () => {
  const out = resolveSessionDir(undefined, PKG);
  assert.equal(out, path.join(PKG, 'session'));
  assert.ok(path.isAbsolute(out));
});

// ── مطلق → يُحترم كما هو ──────────────────────────────────────────────────────
test('resolveSessionDir: مسار مطلق يُستعمل كما هو', () => {
  const abs = process.platform === 'win32' ? 'D:\\wa-session' : '/var/lib/wa-session';
  assert.equal(resolveSessionDir(abs, PKG), abs);
});

// ── نسبي → يُحلّ نسبةً لجذر الحزمة، لا لـ cwd (جوهر الإصلاح) ──────────────────
test('resolveSessionDir: نسبي يُحلّ نسبةً لجذر الحزمة لا cwd', () => {
  const out = resolveSessionDir('./session', PKG);
  assert.equal(out, path.join(PKG, 'session'));
  assert.ok(path.isAbsolute(out));
  // مهما كان cwd الحالي، الناتج ثابت مربوط بالجذر الممرَّر
  assert.notEqual(out, path.resolve(process.cwd(), 'session'));
});

test('resolveSessionDir: نسبي متعدّد المقاطع يُحلّ تحت الجذر', () => {
  const out = resolveSessionDir('data/wa', PKG);
  assert.equal(out, path.join(PKG, 'data', 'wa'));
});

test('resolveSessionDir: يشذّب الفراغات', () => {
  assert.equal(resolveSessionDir('   ', PKG), path.join(PKG, 'session'));
});

// ── الافتراضي الحقيقي (بلا تمرير جذر) مطلق ومنتهٍ بـ /session ─────────────────
test('resolveSessionDir: الجذر الافتراضي الحقيقي مطلق (whatsapp/session)', () => {
  const out = resolveSessionDir('');
  assert.ok(path.isAbsolute(out));
  assert.equal(path.basename(out), 'session');
  // مربوط بمجلد حزمة whatsapp (يحتوي src/)
  assert.equal(path.basename(path.dirname(out)), 'whatsapp');
});
