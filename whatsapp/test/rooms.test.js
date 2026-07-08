/**
 * اختبار وحدة خفيف (node --test) — منطق تصنيف/نطاق الغرف النقيّ (§2.2)، بلا Mongo/Baileys.
 * يغطّي: تمييز المجموعات، بناء خريطة النطاق، قرار الالتقاط، وأنواع الالتقاط المسموحة.
 * التشغيل: node --test test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { isGroupJid, buildScopeMap, inScopeFrom, CAPTURE_TYPES } from '../src/capture.js';

const CENTRAL = '120363000000000001@g.us';
const CUSTOMER = '120363000000000003@g.us';
const DM = '201000000000@s.whatsapp.net';

// ── تمييز المجموعات (الغرف = @g.us فقط) ──────────────────────────────────────
test('isGroupJid: مجموعة @g.us صحيحة، رسالة فردية لا', () => {
  assert.equal(isGroupJid(CENTRAL), true);
  assert.equal(isGroupJid(DM), false);
  assert.equal(isGroupJid(''), false);
  assert.equal(isGroupJid(undefined), false);
});

// ── أنواع الالتقاط المسموحة (§2.2) ────────────────────────────────────────────
test('CAPTURE_TYPES: المصنّفة الأربع فقط', () => {
  for (const t of ['central', 'admin', 'customer', 'treasury']) assert.ok(CAPTURE_TYPES.has(t));
  for (const t of ['ignore', 'unclassified']) assert.ok(!CAPTURE_TYPES.has(t));
});

// ── بناء خريطة النطاق من صفوف rooms ──────────────────────────────────────────
test('buildScopeMap: يتخطّى الصفوف بلا jid ويحترم active الافتراضي', () => {
  const m = buildScopeMap([
    { jid: CENTRAL, type: 'central', active: true },
    { jid: CUSTOMER, type: 'customer' }, // active غير محدّد → يُعتبر نشطًا
    { type: 'customer' },                // بلا jid → يُتخطّى
    null,
  ]);
  assert.equal(m.size, 2);
  assert.equal(m.get(CUSTOMER).active, true);
});

// ── قرار الالتقاط ─────────────────────────────────────────────────────────────
test('inScopeFrom: يلتقط المصنّفة النشطة فقط', () => {
  const m = buildScopeMap([
    { jid: CENTRAL, type: 'central', active: true },
    { jid: CUSTOMER, type: 'customer', active: false },      // موقوفة → خارج النطاق
    { jid: 'ig@g.us', type: 'ignore', active: true },        // مُتجاهَلة → خارج النطاق
    { jid: 'un@g.us', type: 'unclassified', active: true },  // غير مصنّفة → خارج النطاق
  ]);
  assert.equal(inScopeFrom(m, CENTRAL), true);
  assert.equal(inScopeFrom(m, CUSTOMER), false);       // نوع مسموح لكن موقوفة
  assert.equal(inScopeFrom(m, 'ig@g.us'), false);
  assert.equal(inScopeFrom(m, 'un@g.us'), false);      // 🔴 غير مصنّفة = لا التقاط نص (§شرط 3)
  assert.equal(inScopeFrom(m, 'unknown@g.us'), false); // مجهولة تمامًا
});
