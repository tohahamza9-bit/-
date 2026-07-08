/**
 * سجلّ pino — T5: لا silent catches؛ كل خطأ يُسجَّل (بوت مالي: خطأ صامت = أموال).
 */
import pino from 'pino';

export function makeLogger(level = 'info') {
  const opts = { level };
  // إخراج مقروء في التطوير؛ JSON خام في الإنتاج (PM2)
  if (process.env.NODE_ENV !== 'production') {
    opts.transport = { target: 'pino-pretty', options: { translateTime: 'SYS:standard' } };
  }
  return pino(opts);
}
