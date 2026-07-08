/**
 * التركيب — اتصال Baileys + capture + sender + antiban + إغلاق آمن (§14).
 * لا يُشغَّل ضمن الاختبارات (يستورد Baileys/Mongo). يُشغَّل: `node src/index.js`.
 */
import makeWASocket, { useMultiFileAuthState, DisconnectReason, fetchLatestWaWebVersion } from '@whiskeysockets/baileys';
import qrcode from 'qrcode-terminal';

import { loadConfig } from './config.js';
import { makeLogger } from './logger.js';
import { connectDb } from './db.js';
import { makeCapture } from './capture.js';
import { makeSender } from './sender.js';
import { CircuitBreaker, WarmupLimiter, classifyDisconnect, reconnectDelayMs } from './antiban.js';

/**
 * بذر الغرف من الإعداد (env) أول تشغيل (§شرط 4) — idempotent ($setOnInsert لا يلمس تصنيفًا موجودًا).
 * 🔴 المركزية/المسؤول تبقى حدود الكتابة من env (whitelist.js) — هذا للالتقاط/العرض فقط (Option A).
 */
async function seedRoomsFromConfig(rooms, cfg, logger) {
  const seeds = [
    [cfg.centralJid, 'central'],
    [cfg.adminJid, 'admin'],
    ...cfg.customerRoomJids.map((j) => [j, 'customer']),
    ...cfg.treasuryRoomJids.map((j) => [j, 'treasury']),
  ];
  for (const [jid, type] of seeds) {
    if (!jid) continue;
    try {
      await rooms.updateOne(
        { jid },
        { $setOnInsert: { jid, type, name: null, active: true, discovered_at: new Date(), updated_by: 'seed' } },
        { upsert: true },
      );
    } catch (e) {
      logger.warn({ err: e, jid }, 'تعذّر بذر غرفة من env'); // T5 — غير حرج للإقلاع
    }
  }
}

/**
 * معالجة أحداث groups.upsert/update (§شرط 2) — تحديث ذاكرة الأسماء و rooms.name بلا نداء شبكة.
 * fire-and-forget: لا نحجب حلقة الأحداث؛ كل خطأ يُسجَّل (T5).
 */
function onGroupsMeta(groups, cache, rooms, logger) {
  for (const g of groups || []) {
    if (!g || !g.id || !g.subject) continue;
    cache.set(g.id, g.subject);
    rooms.updateOne(
      { jid: g.id },
      {
        $set: { name: g.subject, updated_at: new Date() },
        $setOnInsert: { jid: g.id, type: 'unclassified', active: true, discovered_at: new Date(), updated_by: 'discovery' },
      },
      { upsert: true },
    ).catch((e) => logger.error({ err: e, jid: g.id }, 'تعذّر تحديث اسم الغرفة')); // T5
  }
}

async function main() {
  const cfg = loadConfig();
  const logger = makeLogger(cfg.logLevel);

  // مجلد الجلسة المطلق — كل إعادات التشغيل تستأنف منه (منع الانحراف الصامت بين cwd مختلفة).
  logger.info({ sessionDir: cfg.sessionDir }, '📁 مجلد جلسة واتساب (مطلق) — الاقتران وإعادات التشغيل تستعمله جميعًا');

  if (!cfg.centralJid || !cfg.adminJid) {
    logger.warn('⚠️ CENTRAL_ROOM_JID أو ADMIN_ROOM_JID غير مضبوط — الإرسال سيُرفض حتى ضبطهما (§2.2)');
  }

  const dbh = await connectDb(cfg.mongoUri, cfg.mongoDb, logger);

  // W0 + warm-up (§14.1) — تُهيّأ قبل أول ربط
  const breaker = new CircuitBreaker({ maxQr: 5, maxRejections: 3 });
  const warmup = new WarmupLimiter();

  // W2 — بصمة جهاز ثابتة عبر كل إعادات التشغيل (لا عشوائية → لا 428 من تغيّر التوقيع).
  // 🔴 يجب أن تبقى ثابتة لأن الجلسة المقترنة مرتبطة بها؛ تغيّرها بين التشغيلات يُرفض بـ 428.
  // (BROWSER_VARIANTS/pickBrowser محفوظة في antiban.js للتوثيق/الاختبار، لا تُستدعى هنا.)
  const browser = ['MoneyadoBridge', 'Chrome', '120.0.0'];
  logger.info('بصمة الجهاز (W2، ثابتة): %s', browser.join(' / '));

  const { state, saveCreds } = await useMultiFileAuthState(cfg.sessionDir);
  // إصدار بروتوكول واتساب الحيّ (§428): fetchLatestWaWebVersion يقرأ client_revision الحيّ من
  // web.whatsapp.com (أدقّ من ملف GitHub). نحترم isLatest ولا نسقط بصمت لنسخة قديمة تُرفض بـ 428.
  const { version, isLatest, error: verErr } = await fetchLatestWaWebVersion();
  if (isLatest) {
    logger.info({ version }, 'إصدار واتساب الحيّ (الأحدث)');
  } else {
    logger.warn(
      { err: verErr, version },
      '⚠️ تعذّر جلب أحدث إصدار واتساب — استخدام نسخة احتياطية مثبّتة قد تُرفض فورًا بـ 428. تحقّق من الشبكة/البروكسي قبل الربط.',
    );
  }

  let sock;
  let sender;
  let closing = false;          // T3 — علم الإغلاق الآمن (يوقف إعادة الاتصال)
  let reconnectPending = false; // يمنع تكديس إعادة الاتصال (backoff واحد فعّال في المرّة)
  let botJid = state.creds?.me?.id || '';
  const getBotJid = () => botJid;

  // بذر الغرف من env أول تشغيل (§شرط 4) — يضمن صحّة نطاق الالتقاط من الرسالة الأولى.
  // 🔴 Option A: المركزية/المسؤول تبقى حدود الكتابة من env؛ البذر للالتقاط/العرض فقط.
  await seedRoomsFromConfig(dbh.rooms, cfg, logger);

  // ذاكرة أسماء المجموعات (§شرط 2): تُملأ من أحداث groups.upsert (بلا شبكة).
  // groupMetadata() لا تُستدعى إلا لغرفة مجهولة أول مرة فقط، ونُثبّت النتيجة دائمًا.
  const roomNameCache = new Map();
  async function resolveRoomName(jid) {
    if (roomNameCache.has(jid)) return roomNameCache.get(jid); // cache دائم (يشمل null الفاشل)
    let name = null;
    try {
      const meta = await sock.groupMetadata(jid); // نداء شبكة وحيد لكل jid مجهول (§14.1)
      name = meta?.subject || null;
    } catch (e) {
      logger.warn({ err: e, jid }, 'تعذّر جلب groupMetadata — سيُثبَّت null لتفادي التكرار'); // T5
    }
    roomNameCache.set(jid, name); // تثبيت (حتى null) → لا استدعاء ثانٍ
    return name;
  }

  const capture = makeCapture({
    raw: dbh.raw,
    rooms: dbh.rooms,
    logger,
    getBotJid,
    resolveRoomName,
  });

  // إعادة اتصال حميدة مع backoff (§14.1): تأخير متصاعد + jitter، لا تُحسب رفضًا في W0.
  function scheduleReconnect() {
    if (closing || reconnectPending) return;
    reconnectPending = true;
    const attempt = breaker.onBenignReconnect();       // عدّاد المحاولات (لا يقطع W0)
    const delay = reconnectDelayMs(attempt);
    logger.info('إعادة اتصال حميدة بعد %dms (محاولة %d)…', delay, attempt);
    setTimeout(() => {
      reconnectPending = false;
      if (!closing) buildSocket();
    }, delay);
  }

  function buildSocket() {
    sock = makeWASocket({
      version,
      auth: state,
      browser, // W2 تنويع البصمة
      markOnlineOnConnect: false, // W2 — لا نعلن التواجد (قراءة أكثر من كتابة §14.1)
      syncFullHistory: true, // §12 — استرجاع رسائل فترة الانقطاع
      logger, // pino متوافق
    });

    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', (u) => {
      const { connection, lastDisconnect, qr } = u;
      if (qr) {
        if (breaker.onQr()) {
          logger.error('🔒 قاطع الدائرة (W0): %s — يلزم تدخّل يدوي، توقّف.', breaker.reason);
          shutdown('circuit-breaker', 1);
          return;
        }
        logger.info('امسح رمز QR (محاولة %d/%d):', breaker.qrCount, breaker.maxQr);
        qrcode.generate(qr, { small: true });
      }
      if (connection === 'open') {
        breaker.onConnectionOpen();
        botJid = sock.user?.id || botJid;
        logger.info('✅ اتصل واتساب — البوت: %s', botJid);
      }
      if (connection === 'close') {
        const code = lastDisconnect?.error?.output?.statusCode;
        const category = classifyDisconnect(code); // benign | fatal | unknown (§428)
        logger.warn({ code, category }, 'انقطع الاتصال');

        // رفض حقيقي (401/500/403/411/440) → إعادة الاتصال بلا جدوى/ضارّة → توقّف فوري + تدخّل يدوي.
        if (category === 'fatal') {
          breaker.onRejection(`رفض حقيقي code=${code}`);
          if (code === DisconnectReason.loggedOut) {
            logger.error('🔴 تسجيل خروج (loggedOut) — احذف مجلد الجلسة وأعد الربط يدويًا. لا إعادة اتصال.');
          } else {
            logger.error('🔴 رفض نهائي code=%s (%s) — توقّف بلا إعادة اتصال، يلزم تدخّل يدوي.', code, category);
          }
          return;
        }

        // حميد (515/428/408/503) أو غير معروف → إعادة اتصال مع backoff، لا تُحسب في W0.
        // هذا هو إصلاح 428: مصافحة الرقم الجديد تُغلق حميدًا مرّة/مرّتين ثم تنجح، بلا قطع W0.
        if (category === 'unknown') {
          logger.warn('كود إغلاق غير مصنّف code=%s — يُعامَل كإعادة اتصال حميدة مع backoff.', code);
        }
        scheduleReconnect();
      }
    });

    // الالتقاط (§7.1 §7.2)
    sock.ev.on('messages.upsert', (arg) => { capture.onUpsert(arg).catch((e) => logger.error({ err: e }, 'upsert handler')); });
    sock.ev.on('messages.update', (arg) => { capture.onUpdate(arg).catch((e) => logger.error({ err: e }, 'update handler')); });

    // أسماء المجموعات (§شرط 2) — من الأحداث مباشرة، بلا نداء شبكة. تُحدّث cache + rooms.name.
    sock.ev.on('groups.upsert', (groups) => { onGroupsMeta(groups, roomNameCache, dbh.rooms, logger); });
    sock.ev.on('groups.update', (groups) => { onGroupsMeta(groups, roomNameCache, dbh.rooms, logger); });
  }

  buildSocket();

  // الإرسال (§2.2 §8.3) — يبدأ بالتوازي؛ tick يحترم القاطع/warm-up
  sender = makeSender({
    sock: new Proxy({}, { get: (_t, p) => (...a) => sock[p](...a) }), // يشير دومًا لأحدث مقبس بعد إعادة الاتصال
    outgoing: dbh.outgoing,
    dests: cfg.allowedDests,
    breaker,
    warmup,
    logger,
  });
  sender.loop(cfg.senderIntervalMs).catch((e) => logger.error({ err: e }, 'حلقة الإرسال توقّفت'));

  // ── T3: إغلاق آمن (SIGTERM graceful drain) ──
  async function shutdown(signal, exitCode = 0) {
    if (closing) return;
    closing = true;
    logger.info('T3 — إغلاق آمن (%s)…', signal);
    try {
      sender?.stop();
      await new Promise((r) => setTimeout(r, 500)); // درين قصير للجولة الجارية
      await saveCreds();
      sock?.end?.(undefined); // يغلق المقبس دون تسجيل خروج (يحفظ الجلسة)
      await dbh.client.close();
    } catch (e) {
      logger.error({ err: e }, 'خطأ أثناء الإغلاق'); // T5
    } finally {
      logger.info('انتهى الإغلاق الآمن.');
      process.exit(exitCode);
    }
  }

  process.on('SIGTERM', () => shutdown('SIGTERM'));
  process.on('SIGINT', () => shutdown('SIGINT'));
  process.on('uncaughtException', (e) => { logger.error({ err: e }, '🔴 uncaughtException'); });
  process.on('unhandledRejection', (e) => { logger.error({ err: e }, '🔴 unhandledRejection'); });

  logger.info('جسر MONEYADO WhatsApp يعمل.');
}

main().catch((e) => {
  // T5 — لا نموت بصمت
  console.error('🔴 فشل إقلاع الجسر:', e);
  process.exit(1);
});
