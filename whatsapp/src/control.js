/**
 * تحكّم تشغيل الكيرنل من الجسر (POST /pm2) — منطق نقيّ قابل للاختبار بلا شبكة/عمليات.
 *
 * 🔴 لماذا يوجد هذا المسار أصلًا: الكيرنل هو مَن يخدم لوحة التحكّم، فإن كان **مطفأً** لا
 *    أحد يستقبل «شغّله». الجسر يعمل دائمًا (pm2) فهو المنفذ الوحيد الباقي. راجع التعليق
 *    في core/process_control.py.
 *
 * 🔴 الأمان (نفس عقد process_control.py):
 *    - قائمة أفعال **مغلقة**؛ أيّ قيمة خارجها تُرفض قبل أيّ تنفيذ.
 *    - الوسائط **ثابتة في الكود** (اسم المهمّة لا يأتي من الطلب إطلاقًا) وتُمرَّر كمصفوفة
 *      إلى execFile — بلا shell، فلا مجال لحقن أوامر.
 *    - التوكن إلزاميّ: غيابه من البيئة **يُعطّل النقطة** (fail-closed) لا يفتحها.
 *    - الخادم مربوط على 127.0.0.1 فقط (index.js) — لا يُسمَع من الشبكة.
 */

/** اسم المهمّة المجدولة — ثابت، لا يأتي من الطلب أبدًا. */
export const TASK_KERNEL = 'moneyado-kernel';

/** الأفعال المسموحة ووسائطها الثابتة. لا stop/restart هنا: الكيرنل الحيّ ينفّذهما بنفسه. */
export const CONTROL_ACTIONS = Object.freeze({
  'start-kernel': ['/Run', '/TN', TASK_KERNEL],
});

/**
 * مقارنة توكن بزمن ثابت — تمنع تسريب الطول/البادئة عبر توقيت الردّ.
 * @param {string} a @param {string} b
 */
export function safeEqual(a, b) {
  const x = String(a ?? '');
  const y = String(b ?? '');
  if (x.length !== y.length) return false;
  let diff = 0;
  for (let i = 0; i < x.length; i += 1) diff |= x.charCodeAt(i) ^ y.charCodeAt(i);
  return diff === 0;
}

/**
 * يقرّر ما يجب فعله بطلب تحكّم.
 * @param {{token?:string, action?:string, expectedToken?:string}} input
 * @returns {{ok:true, args:string[]} | {ok:false, status:number, error:string}}
 */
export function resolveControlRequest({ token, action, expectedToken } = {}) {
  // (١) fail-closed: بلا توكن مضبوط في البيئة، النقطة **معطّلة** — لا «مفتوحة بلا حماية».
  if (!expectedToken) {
    return { ok: false, status: 503, error: 'التحكّم معطّل — BRIDGE_CONTROL_TOKEN غير مضبوط' };
  }
  if (!token || !safeEqual(token, expectedToken)) {
    return { ok: false, status: 403, error: 'forbidden' };
  }
  const args = CONTROL_ACTIONS[action];
  if (!args) {
    return { ok: false, status: 400, error: `فعل غير مسموح: ${String(action)}` };
  }
  return { ok: true, args: args.slice() };
}
