/**
 * الإعدادات — من متغيّرات البيئة فقط (§14.3: لا مفاتيح في الكود).
 * يطابق أسماء .env.example للنواة (MONGO_URI, CENTRAL_ROOM_JID, ...).
 * دالة نقيّة (تأخذ env كوسيط) — بلا تبعيات.
 */
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// جذر حزمة whatsapp/ محسوب من موقع هذا الملف (whatsapp/src/config.js → whatsapp/).
// مستقلّ تمامًا عن process.cwd() — كي تُستأنف الجلسة نفسها مهما اختلف مجلد تشغيل الخدمة
// (npm من whatsapp/، أو PM2/node من الجذر). هذا جوهر إصلاح «QR + 428 عند إعادة التشغيل».
const PKG_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

function splitJids(v) {
  return (v || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * يحلّ مجلد الجلسة إلى مسار مطلق ثابت عبر إعادات التشغيل:
 *   - فارغ → الافتراضي: <جذر الحزمة>/session.
 *   - مطلق → يُستعمل كما هو.
 *   - نسبي → يُحلّ نسبةً لجذر الحزمة (لا cwd) — كي لا يتغيّر بتغيّر مجلد التشغيل.
 * دالة نقيّة (تأخذ الجذر صراحةً) — قابلة للاختبار وحدها.
 */
export function resolveSessionDir(raw, pkgRoot = PKG_ROOT) {
  const val = (raw || '').trim();
  if (!val) return path.join(pkgRoot, 'session');
  if (path.isAbsolute(val)) return val;
  return path.resolve(pkgRoot, val);
}

export function loadConfig(env = process.env) {
  const centralJid = env.CENTRAL_ROOM_JID || '';
  const adminJid = env.ADMIN_ROOM_JID || '';
  const cfg = {
    mongoUri: env.MONGO_URI || 'mongodb://localhost:27017',
    mongoDb: env.MONGO_DB || 'moneyado',

    // الغرف (§2.2)
    centralJid,
    adminJid,
    customerRoomJids: splitJids(env.CUSTOMER_ROOM_JIDS), // قراءة صامتة مطلقة
    treasuryRoomJids: splitJids(env.TREASURY_ROOM_JIDS), // قراءة صامتة مطلقة

    internalToken: env.INTERNAL_TOKEN || '', // SEC-002 (احتياطي HTTP)
    // توكن تشغيل الكيرنل من اللوحة عبر POST /pm2. **منفصل عن internalToken عمدًا**:
    // تلك للقراءة وهذه تنفّذ أمر نظام. غيابه ⇒ النقطة معطّلة (fail-closed).
    controlToken: env.BRIDGE_CONTROL_TOKEN || '',

    // تشغيل الجسر — مسار مطلق ثابت عبر إعادات التشغيل (مستقلّ عن cwd)
    sessionDir: resolveSessionDir(env.WA_SESSION_DIR),
    logLevel: env.LOG_LEVEL || 'info',
    senderIntervalMs: Number(env.WA_SENDER_INTERVAL_MS || 2000),
    // منفذ خادم الإشعار الفوري (§8.3): النواة تطلب POST /flush ليُفرِغ الطابور بلا انتظار polling.
    flushPort: Number(env.WA_FLUSH_PORT || 3001),
  };

  // الوجهات المسموحة (§2.2) — للحقن في whitelist
  cfg.allowedDests = { centralJid, adminJid };
  cfg.allowedJids = [centralJid, adminJid].filter(Boolean);
  // الغرف المقروءة (كلها تُلتقط بصمت؛ الكتابة محصورة في allowed فقط)
  cfg.readRoomJids = [centralJid, adminJid, ...cfg.customerRoomJids, ...cfg.treasuryRoomJids].filter(Boolean);
  return cfg;
}
