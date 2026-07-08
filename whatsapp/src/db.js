/**
 * طبقة MongoDB للجسر — نفس قاعدة نواة Python (§7.1).
 * نستهلك: outgoing (طابور الصادر الذي تكتبه النواة).
 * ننتج:  raw_messages (الالتقاط الخام — يطابق core/models.py::RawMessage).
 * نشارك: rooms (تصنيف الغرف — نقرأ نطاق الالتقاط، ونكتب الاكتشاف metadata فقط §2.2).
 */
import { MongoClient } from 'mongodb';

export async function connectDb(uri, dbName, logger) {
  const client = new MongoClient(uri, { ignoreUndefined: true });
  await client.connect();
  const db = client.db(dbName);
  logger.info('اتصال MongoDB: %s / %s', uri, dbName);

  const raw = db.collection('raw_messages'); // ننتج (§7.1 بند 1)
  const outgoing = db.collection('outgoing'); // نستهلك (§2.2 §8.3)
  const rooms = db.collection('rooms'); // تصنيف الغرف (يطابق core/models.py::Room)

  // فهرس فريد على message_key (يطابق فهرس النواة — منع الازدواج §9)
  await raw.createIndex({ message_key: 1 }, { unique: true }).catch((e) => {
    logger.error({ err: e }, 'تعذّر إنشاء فهرس raw_messages.message_key');
  });
  // فهرس فريد على jid (يطابق فهرس النواة على rooms)
  await rooms.createIndex({ jid: 1 }, { unique: true }).catch((e) => {
    logger.error({ err: e }, 'تعذّر إنشاء فهرس rooms.jid');
  });

  return { client, db, raw, outgoing, rooms };
}
