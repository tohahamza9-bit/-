/**
 * عدم الحظر والاستقرار (§14) 🔴 — دوال نقيّة قابلة للاختبار بلا Baileys.
 *   W0   قاطع دائرة (circuit breaker): حد 5 QR، توقّف بعد 3 رفض → تدخّل يدوي.
 *   W1   تأخير عشوائي 4–9s بتوزيع gaussian (الأقوى).
 *   W1-B فاصل 1.5–4s بين الغرف (كسر التزامن الآلي).
 *   W2   تنويع بصمة الجهاز (browser variants).
 *   —    warm-up متدرّج: 10 رسائل/ساعة → 50 → ... → 1500.
 */

/** نوم غير حاجز. */
export function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

// ── W1/W1-B: تأخير عشوائي بتوزيع طبيعي (gaussian) ──────────────────────────────

/** عيّنة معياريّة N(0,1) عبر Box–Muller. */
export function gaussianRandom() {
  let u = 0;
  let v = 0;
  while (u === 0) u = Math.random();
  while (v === 0) v = Math.random();
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
}

/** عيّنة gaussian محصورة ضمن [min,max] (المتوسط في المنتصف، ±3σ عند الحدّين). */
export function gaussianBetween(min, max) {
  const mean = (min + max) / 2;
  const std = (max - min) / 6;
  let x = mean + gaussianRandom() * std;
  if (x < min) x = min;
  if (x > max) x = max;
  return x;
}

/** W1 — تأخير قبل كل إرسال (4–9 ثوانٍ). */
export function sendDelayMs() {
  return Math.round(gaussianBetween(4000, 9000));
}

/** W1-B — فاصل بين الغرف المختلفة (1.5–4 ثوانٍ). */
export function roomGapMs() {
  return Math.round(gaussianBetween(1500, 4000));
}

// ── W2: تنويع بصمة الجهاز ──────────────────────────────────────────────────────
// كل عنصر بشكل Baileys `Browsers`: [platform, browser, version].
export const BROWSER_VARIANTS = [
  ['MoneyadoBridge', 'Chrome', '120.0.0'],
  ['MoneyadoBridge', 'Firefox', '121.0'],
  ['Ubuntu', 'Chrome', '119.0.0'],
  ['Mac OS', 'Safari', '16.5'],
  ['Windows', 'Edge', '120.0.0'],
];

/** اختيار بصمة عشوائية ثابتة طوال الجلسة (تُختار مرّة عند الإقلاع). */
export function pickBrowser(rng = Math.random) {
  return BROWSER_VARIANTS[Math.floor(rng() * BROWSER_VARIANTS.length)];
}

// ── تصنيف أكواد الإغلاق (DisconnectReason) — حميد (إعادة اتصال) مقابل رفض حقيقي ──────
// المرجع: Baileys DisconnectReason. القيم مثبّتة هنا كي تبقى الدالة نقيّة (بلا استيراد Baileys).
//   حميد (transient) → إعادة اتصال مع backoff، لا يُحسب في W0:
//     515 restartRequired (إلزامي بعد الاقتران)، 428 connectionClosed،
//     408 connectionLost/timedOut، 503 unavailableService.
//   رفض حقيقي (fatal) → توقّف فوري + تدخّل يدوي، لا إعادة اتصال:
//     401 loggedOut، 500 badSession، 403 forbidden، 411 multideviceMismatch، 440 connectionReplaced.
export const BENIGN_RECONNECT_CODES = new Set([515, 428, 408, 503]);
export const FATAL_CODES = new Set([401, 500, 403, 411, 440]);

/**
 * يصنّف كود الإغلاق: 'benign' (إعادة اتصال)، 'fatal' (توقّف)، 'unknown' (يُعامَل حميدًا بحذر).
 * @param {number|undefined} code statusCode من lastDisconnect.error.output
 */
export function classifyDisconnect(code) {
  if (FATAL_CODES.has(code)) return 'fatal';
  if (BENIGN_RECONNECT_CODES.has(code)) return 'benign';
  return 'unknown'; // كود غير معروف → الافتراض الآمن: إعادة اتصال حميدة مع backoff (لا نُسقط الخدمة)
}

// ── 401: عابر أم خروج حقيقي؟ ─────────────────────────────────────────────────
// 🔴 حادثة 2026-07-20 00:42: أُغلق الاتصال بـ401 مرّتين متتاليتين فرُفع علم loggedOut
//    وتوقّف الجسر طالبًا مسح الجلسة وإعادة ربط QR — **والجلسة كانت سليمة**: إعادة تشغيل
//    بسيطة بعدها بدقائق اقترنت فورًا بلا QR. أي أن معاملة كل 401 خروجًا نهائيًّا أوقفت
//    الخدمة بلا سبب وطلبت مسح جلسة صالحة.
//    السبب: واتساب يُصدر 401 أيضًا في حالات عابرة (تعارض جلسات/شبكة/إعادة تفاوض)،
//    ولا يوجد في حدث الإغلاق ما يميّزها عن الخروج الحقيقي — إلّا **إعادة المحاولة**:
//    الخروج الحقيقي يُعيد 401 دائمًا، والعابر ينجح.
export const UNAUTHORIZED_CODE = 401;
export const UNAUTHORIZED_MAX_RETRIES = 3;
export const UNAUTHORIZED_RETRY_DELAY_MS = 5000;

/**
 * يحكم على 401: إعادة محاولة (عابر مرجَّح) أم توقّف نهائي (خروج حقيقي).
 *
 * القاعدة: حتى `maxRetries` محاولات متباعدة `delayMs`؛ فإن عاد 401 بعدها كلّها
 * فهو خروج حقيقي ⇒ توقّف وطلب QR. أوّل اتصال ناجح يصفّر العدّاد.
 *
 * استثناء يقطع الانتظار: خروج **مقصود** صريح (`sock.logout()` أو رسالة Baileys
 * "Intentional Logout") — لا معنى لإعادة محاولته.
 */
export class UnauthorizedArbiter {
  constructor({ maxRetries = UNAUTHORIZED_MAX_RETRIES,
                delayMs = UNAUTHORIZED_RETRY_DELAY_MS } = {}) {
    this.maxRetries = maxRetries;
    this.delayMs = delayMs;
    this.attempts = 0;
  }

  /** @returns {{action:'retry'|'stop', attempt:number, delayMs:number, reason:string}} */
  onUnauthorized({ intentional = false } = {}) {
    if (intentional) {
      return { action: 'stop', attempt: this.attempts, delayMs: 0,
               reason: 'خروج مقصود صريح (logout) — لا إعادة محاولة' };
    }
    if (this.attempts < this.maxRetries) {
      this.attempts += 1;
      return { action: 'retry', attempt: this.attempts, delayMs: this.delayMs,
               reason: `401 قد يكون عابرًا — محاولة ${this.attempts}/${this.maxRetries}` };
    }
    return { action: 'stop', attempt: this.attempts, delayMs: 0,
             reason: `401 متكرّر بعد ${this.maxRetries} محاولات — خروج حقيقي` };
  }

  /** اتصال ناجح ⇒ الـ401 السابق كان عابرًا فعلًا. */
  onConnectionOpen() {
    this.attempts = 0;
  }
}

/**
 * هل يحمل خطأ الإغلاق علامة خروج **مقصود** صريح؟ (لا يُعاد المحاولة عليه)
 * @param {any} err lastDisconnect.error
 */
export function isIntentionalLogout(err) {
  const msg = err?.output?.payload?.message ?? err?.message ?? '';
  return typeof msg === 'string' && /intentional logout/i.test(msg);
}

/**
 * تأخير إعادة الاتصال بـ backoff أُسّي + jitter متساوٍ (§14.1 — ألطف على واتساب، يمنع الحلقة اللحظية).
 * المحاولة تبدأ من 1. الناتج ضمن [ceil/2, ceil] حيث ceil = min(cap, base·2^(attempt-1)).
 * @param {number} attempt رقم المحاولة (1 = أول إعادة اتصال)
 * @param {{base?:number, cap?:number, rng?:Function}} [opts]
 */
export function reconnectDelayMs(attempt, { base = 2000, cap = 30000, rng = Math.random } = {}) {
  const n = Math.max(1, Math.floor(attempt));
  const ceil = Math.min(cap, base * 2 ** (n - 1));
  const half = ceil / 2;
  return Math.round(half + rng() * half); // jitter متساوٍ: [ceil/2, ceil]
}

// ── W0: قاطع الدائرة ───────────────────────────────────────────────────────────
/**
 * يحصي محاولات QR و«الرفض الحقيقي» ويتوقّف نهائيًا (يتطلّب تدخّلًا يدويًا) عند تجاوز الحدود.
 * 🔴 غرض W0: يقطع على الرفض الحقيقي المتكرّر فقط (fatal). إعادة الاتصال الحميدة (515/428/408/503)
 *    تُتابَع عبر onBenignReconnect ولا تُحسب رفضًا — كي لا تقطع مصافحةُ رقم جديد الخدمةَ.
 * الاتصال الناجح يعيد ضبط كل العدّادات (لا يلمس tripped).
 */
export class CircuitBreaker {
  constructor({ maxQr = 5, maxRejections = 3 } = {}) {
    this.maxQr = maxQr;
    this.maxRejections = maxRejections;
    this.qrCount = 0;
    this.rejections = 0;              // الرفض الحقيقي (fatal) فقط
    this.reconnectAttempts = 0;       // إعادة الاتصال الحميدة (لعدّاد backoff) — لا يقطع W0
    this.tripped = false;
    this.reason = null;
  }

  onQr() {
    this.qrCount += 1;
    if (this.qrCount > this.maxQr) this._trip(`تجاوز حد QR (${this.maxQr})`);
    return this.tripped;
  }

  onConnectionOpen() {
    // نجح الاتصال → صفّر كل العدّادات (لا نلمس tripped: القطع يتطلّب إعادة ضبط يدوية)
    this.qrCount = 0;
    this.rejections = 0;
    this.reconnectAttempts = 0;
  }

  /** إعادة اتصال حميدة (كود عابر) — يزيد عدّاد المحاولات لحساب backoff، ولا يقطع W0 أبدًا. */
  onBenignReconnect() {
    this.reconnectAttempts += 1;
    return this.reconnectAttempts;
  }

  /** رفض حقيقي (fatal) — يقطع W0 عند بلوغ الحد. */
  onRejection(reason = 'رفض اتصال') {
    this.rejections += 1;
    if (this.rejections >= this.maxRejections) {
      this._trip(`تجاوز حد الرفض (${this.maxRejections}): ${reason}`);
    }
    return this.tripped;
  }

  _trip(reason) {
    this.tripped = true;
    this.reason = reason;
  }

  isOpen() {
    return this.tripped;
  }

  /** إعادة ضبط يدوية بعد تدخّل بشري (§14.1 W0). */
  reset() {
    this.qrCount = 0;
    this.rejections = 0;
    this.reconnectAttempts = 0;
    this.tripped = false;
    this.reason = null;
  }
}

// ── warm-up متدرّج ──────────────────────────────────────────────────────────────
export const WARMUP_SCHEDULE = [10, 50, 150, 300, 600, 1000, 1500]; // رسائل/ساعة

/** يحدّ عدد الرسائل المرسلة في الساعة حسب جدول تصاعدي منذ الإقلاع. */
export class WarmupLimiter {
  constructor({ startTime = Date.now(), schedule = WARMUP_SCHEDULE } = {}) {
    this.startTime = startTime;
    this.schedule = schedule;
    this.windowStart = startTime;
    this.countInWindow = 0;
  }

  _hourIndex(now) {
    return Math.floor((now - this.startTime) / 3600000);
  }

  /** السقف المسموح للساعة الحالية (يثبت على آخر قيمة بعد نهاية الجدول). */
  cap(now = Date.now()) {
    const i = this._hourIndex(now);
    return i >= this.schedule.length ? this.schedule[this.schedule.length - 1] : this.schedule[i];
  }

  _rollWindow(now) {
    if (now - this.windowStart >= 3600000) {
      this.windowStart = now;
      this.countInWindow = 0;
    }
  }

  canSend(now = Date.now()) {
    this._rollWindow(now);
    return this.countInWindow < this.cap(now);
  }

  record(now = Date.now()) {
    this._rollWindow(now);
    this.countInWindow += 1;
  }
}
