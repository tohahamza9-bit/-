/**
 * اختبار وحدة خفيف (node --test) — منطق تصنيف/نطاق الغرف النقيّ (§2.2)، بلا Mongo/Baileys.
 * يغطّي: تمييز المجموعات، بناء خريطة النطاق، قرار الالتقاط، وأنواع الالتقاط المسموحة.
 * التشغيل: node --test test/
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import { isGroupJid, buildScopeMap, inScopeFrom, NO_CAPTURE_TYPES } from '../src/capture.js';

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

// ── أنواع لا تُلتقَط (الالتقاط الشامل عدا ignore) ──────────────────────────────
test('NO_CAPTURE_TYPES: ignore فقط لا يُلتقَط', () => {
  assert.ok(NO_CAPTURE_TYPES.has('ignore'));
  for (const t of ['central', 'admin', 'customer', 'treasury', 'supplier', 'unclassified'])
    assert.ok(!NO_CAPTURE_TYPES.has(t));   // كلها تُلتقَط الآن
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

// ── قرار الالتقاط (شامل عدا ignore/الموقوفة/الفرديّة) ──────────────────────────
test('inScopeFrom: يلتقط كل النشطة عدا ignore والموقوفة', () => {
  const m = buildScopeMap([
    { jid: CENTRAL, type: 'central', active: true },
    { jid: CUSTOMER, type: 'customer', active: false },      // موقوفة → خارج النطاق
    { jid: 'ig@g.us', type: 'ignore', active: true },        // مُتجاهَلة → خارج النطاق
    { jid: 'un@g.us', type: 'unclassified', active: true },  // غير مصنّفة نشطة → تُلتقَط الآن
  ]);
  assert.equal(inScopeFrom(m, CENTRAL), true);
  assert.equal(inScopeFrom(m, CUSTOMER), false);       // موقوفة (active=false)
  assert.equal(inScopeFrom(m, 'ig@g.us'), false);      // متجاهَلة صراحةً
  assert.equal(inScopeFrom(m, 'un@g.us'), true);       // 🆕 غير مصنّفة نشطة = تُلتقَط
  assert.equal(inScopeFrom(m, 'unknown@g.us'), true);  // 🆕 مجهولة نشطة = تُلتقَط (ثم تُكتشَف)
  assert.equal(inScopeFrom(m, DM), false);             // رسالة فرديّة ليست غرفًا
});
