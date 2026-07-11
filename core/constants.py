"""
الثوابت الحاكمة للنظام — العملات، الخزائن، العلامات، الحالات.
مصدرها المواصفات §4 و§8. القوائم القابلة للتعديل (خزائن/موردون) تُدار من Dashboard
وتُخزَّن في MongoDB؛ ما هنا هو الافتراضيات الثابتة (seed) وأكواد MONEYADO المؤكّدة.
"""
from __future__ import annotations

from enum import Enum


# ── العملات (§4.1) — الكود هو كود MONEYADO الحقيقي ──────────────────────────
class Currency(str, Enum):
    TND = "TND"   # دينار تونسي
    EGP = "EGP"   # جنيه مصري


# نص خيار «نوع العملة» كما يظهر في منسدلة شاشة MONEYADO.
# نختار بالنص لا بالفهرس: الأسماء ثابتة، أما ترتيب القائمة فقد يتغيّر (§4.1).
MONEYADO_CURRENCY_LABEL: dict[Currency, str] = {
    Currency.TND: "دينار تونسي",   # تونسي
    Currency.EGP: "جنيه مصري",     # مصري
}

# كود العملة في MONEYADO — تُستخدم في **شاشة الشراء** (تُكتب رمزًا لا اسمًا، قرار صاحب العمل
# من الشاشة الحقيقية): 4=مصري، 3=تونسي. (شاشة البيع تبقى باختيار الاسم عبر MONEYADO_CURRENCY_LABEL.)
MONEYADO_CURRENCY_CODE: dict[Currency, str] = {
    Currency.EGP: "4",   # جنيه مصري
    Currency.TND: "3",   # دينار تونسي
}

# كود وسيلة الدفع في MONEYADO — يُكتب في خانة «البلد» بشاشة الشراء (إعادة استخدام الحقل، قرار
# صاحب العمل): فودافون=17. تُطابَق على نصّ وسيلة الدفع المطبَّع (يحتوي «فودافون»). قابلة للتوسّع.
MONEYADO_PAYMENT_CODE: dict[str, str] = {
    "فودافون": "17",
}


# ── نوع الخزينة (§5.2) ───────────────────────────────────────────────────────
class TreasuryType(str, Enum):
    SELL_ONLY = "sell_only"        # بيع فقط — لا يُنتظر شراء
    SELL_AND_BUY = "sell_and_buy"  # بيع وشراء — ينتظر الطرف الثاني


# ── نوع العملية داخل MONEYADO ────────────────────────────────────────────────
class OperationType(str, Enum):
    SELL = "sell"   # بيع عملة
    BUY = "buy"     # شراء عملة


# ── نوع الغرفة (§2.2) — التصنيف يُدار من MongoDB (اكتشاف تلقائي + Dashboard) ──
# 🔴 حدود الكتابة (المركزية/المسؤول) تبقى مثبّتة في .env والكود — لا تُقرأ من DB أبدًا
#    (Option A). هذا التصنيف يخدم القراءة الصامتة (زبائن/خزائن) والالتقاط والاكتشاف فقط.
class RoomType(str, Enum):
    CENTRAL = "central"            # المركزية — القراءة + Reply (السلوك من env)
    ADMIN = "admin"               # غرفة المسؤول — التصعيد (السلوك من env)
    CUSTOMER = "customer"          # غرفة زبون — قراءة صامتة للمطابقة (§8)
    TREASURY = "treasury"          # غرفة خزينة — قراءة صامتة للمطابقة (§8)
    SUPPLIER = "supplier"          # غرفة مورد — قراءة صامتة لمطابقة طرف الشراء الثلاثية (§8، صفقات الطرفين)
    IGNORE = "ignore"             # غرفة مُتجاهَلة صراحةً — لا التقاط
    UNCLASSIFIED = "unclassified"  # مكتشَفة تلقائيًا، تنتظر تصنيف المشغّل


# ── الخزائن الافتراضية (seed) — §4.2 §4.3 §4.4 ──────────────────────────────
# البنية: code, name, currency(None=كلاهما), type, aliases
# ملاحظة: أكواد «هادم مصر» و«صافي» و«تونسي خارجي» معلّقة (ملحق ب-3) → None، تُستكمل من
#         Dashboard. البوت لا يُنزّل خزينة بلا كود. «خصم 1%» كودها = 72 (الافتراضية لـ SI مع مورد).
SEED_TREASURIES: list[dict] = [
    # مصر — بيع فقط (§4.2)
    {"code": "77", "name": "أبو يوسف جديد", "currency": "EGP", "type": "sell_only", "aliases": ["أبو يوسف", "ابو يوسف"]},
    {"code": "74", "name": "بلاس فون", "currency": "EGP", "type": "sell_only", "aliases": ["بلاس", "بلس"]},
    {"code": None, "name": "هادم مصر", "currency": "EGP", "type": "sell_only", "aliases": ["هادم"]},
    # تونس — بيع فقط (§4.3)
    {"code": "51", "name": "وليد تونس العاصمة", "currency": "TND", "type": "sell_only", "aliases": ["وليد"]},
    {"code": "58", "name": "عمر العاصمة", "currency": "TND", "type": "sell_only", "aliases": ["عمر"]},
    {"code": "60", "name": "رحيم تونس العاصمة", "currency": "TND", "type": "sell_only", "aliases": ["رحيم"]},
    {"code": "76", "name": "طلال العاصمة", "currency": "TND", "type": "sell_only", "aliases": ["طلال"]},
    {"code": "29", "name": "صالح جربة تونس", "currency": "TND", "type": "sell_only", "aliases": ["صالح"]},
    {"code": "80", "name": "فتحي جربة", "currency": "TND", "type": "sell_only", "aliases": ["فتحي"]},
    {"code": "69", "name": "عصام سوسة", "currency": "TND", "type": "sell_only", "aliases": ["عصام"]},
    {"code": "59", "name": "جمال سوسة", "currency": "TND", "type": "sell_only", "aliases": ["جمال"]},
    {"code": "78", "name": "محمود صفاقس", "currency": "TND", "type": "sell_only", "aliases": ["محمود"]},
    {"code": "79", "name": "محمد حمامات", "currency": "TND", "type": "sell_only", "aliases": ["محمد"]},
    # بيع وشراء (§4.4) — الأكواد معلّقة، تُستكمل من Dashboard
    {"code": "72", "name": "خصم 1%", "currency": "EGP", "type": "sell_and_buy", "aliases": ["خصم", "خصم1", "خصم 1", "خصم 1%", "خصم1%"]},  # الخزينة الافتراضية لـ SI مع مورد
    {"code": "85", "name": "فودافون بالخصم", "currency": "EGP", "type": "sell_and_buy", "aliases": ["فودافون بالخصم", "فودافون خصم"]},  # الخزينة الافتراضية لطرف مورد حوالة A الثانية
    {"code": None, "name": "صافي", "currency": None, "type": "sell_and_buy", "aliases": ["صافى"]},
    {"code": None, "name": "تونسي خارجي", "currency": "TND", "type": "sell_and_buy", "aliases": ["تونسي خارجى", "خارجي"]},
]


# ── الموردون الافتراضيون (seed §5.4) — القائمة البيضاء لطرف الشراء؛ تُدار من Dashboard وتُبذَر هنا ──
SEED_SUPPLIERS: list[dict] = [
    {"code": "1280", "name": "البراق", "aliases": ["براق"]},
]


# ── العلامات (§8.3) ──────────────────────────────────────────────────────────
class Mark(str, Enum):
    MATCHED = "🟡"     # 🟡 وصلت للمراجعة/الانتظار (مطابقة الغرف) — تفاعل على المركزية (قرار المستخدم)
    DONE = "✅"        # ✅ سُجِّلت في MONEYADO بنجاح (auto_trust أو بعد موافقة)
    WARN = "⚠️"        # شك — تحتاج مراجعة/تصعيد (Reply نصّي بالسبب، ليس تفاعلًا)
    FAILED = "🔴"      # 🔴 فشل تقني — تفاعل على المركزية + تصعيد للمسؤول (قرار المستخدم الجديد)
    INCOMPLETE = "❌"  # ❌ حوالة A ناقصة 15د بلا رسالة ثانية — تفاعل على المركزية، بلا تصعيد للمسؤول


# ── حالة الحوالة في الدفتر (مصدر الحقيقة) ────────────────────────────────────
class Status(str, Enum):
    RAW = "raw"                    # مُلتقطة خام، لم تُعالَج
    STABILIZING = "stabilizing"    # تنتظر استقرار (§7.2)
    PARSED = "parsed"              # فُكّكت
    IGNORED = "ignored"            # هدرزة/تسليم-صرف-قبض → تجاهل صامت
    WAITING_SECOND_LEG = "waiting_second_leg"   # تنتظر الطرف الثاني (§7.3)
    MATCHING = "matching"          # قيد مطابقة الغرف (§8)
    MATCHED = "matched"            # 🔸 تطابقت
    HELD = "held"                  # ⚠️ معلّقة، تنتظر تدخّل
    ESCALATED = "escalated"        # حُوّلت لغرفة المسؤول + أُغلقت في الدفتر
    READY = "ready"                # في Outbox، جاهزة للكتابة
    SELL_DONE = "sell_done"        # بيع نزل (فشل نصفي محتمل §11.4)
    COMPLETED = "completed"        # ✅ تمّت وتأكّدت
    CANCELLED = "cancelled"        # ملغاة (قيد عكسي منزّل)
    TECH_FAILED = "tech_failed"    # 🔴 فشل تقني (dead-letter)


# ── الوسائل التي يتجاهلها البوت بصمت (§7.2) — «صرف/قبض» فقط ──────────────────
SILENT_IGNORE_KEYWORDS = ["صرف", "قبض"]

# ── خارج النطاق (§0): تسليم يدوي/باليد → تصعيد لغرفة المسؤول (ليس حوالة، وليس تجاهلًا) ──
# 🔴 أولوية فوق «حوالة». تُطابَق بعد التطبيع (normalize_ar: ى→ي، ة→ه).
#   كلمات كاملة: «تسليم»، «باليد». عبارات كاملة (لتفادي false positives مثل «بيد» وحدها):
OUT_OF_SCOPE_WORDS = ["تسليم", "باليد"]
OUT_OF_SCOPE_PHRASES = ["في يد", "قاهره بيد", "قاهره في يد"]

# ── إشارات الإلغاء/التعديل (§10) — تُقبل عبر Reply من موظف معتمد فقط ──────────
CANCEL_KEYWORDS = ["إلغاء", "الغاء", "الغى", "إلغى"]
EDIT_KEYWORDS = ["تعديل", "تعدل"]
CONFIRM_KEYWORDS = ["تم", "تمت", "👍", "تمام"]

# ── إشارة نوع العملية الصريحة (§5.1) ─────────────────────────────────────────
EXPLICIT_BUY_KEYWORDS = ["شراء", "شرا"]
EXPLICIT_SELL_KEYWORDS = ["بيع"]

# ── مهل زمنية (بالثواني) ─────────────────────────────────────────────────────
STABILIZE_MIN_SECONDS = 60          # §7.2 استقرار الرسالة القصيرة
STABILIZE_MAX_SECONDS = 90          # §7.2 / §15
SECOND_LEG_MAX_SECONDS = 90         # §7.3 الحد الأقصى لانتظار الطرف الثاني (دقيقة ونصف)
BATCH_PAIR_SECONDS = 3              # §7.3-أ زوج متلاحق: رسالتان في نفس الدفعة بفرق <3ث → ربط فوريّ
INCOMPLETE_DATA_ESCALATE_SECONDS = 15 * 60  # §7.3 حوالة A ناقصة بلا رسالة ثانية → تصعيد للمسؤول
SECOND_MESSAGE_LINK_SECONDS = 120   # §7.3 نافذة ربط رد الخزينة/المورد بحوالة معلّقة (دقيقتان)
PENDING_REPLY_MAX_SECONDS = 90      # §7.3 احتفاظ برد خزينة/مورد وصل قبل حوالته (رد معلّق)
ROOM_MATCH_MIN_SECONDS = 10         # §8.1 نافذة المطابقة
ROOM_MATCH_MAX_SECONDS = 15
ROOM_MATCH_WINDOW_SECONDS = 3600    # §8.1 نافذة البحث في رسائل الغرف حول وقت الحوالة (±ساعة)
REMINDER_INTERVAL_SECONDS = 15 * 60  # §8.1 تذكير كل 15 دقيقة
ACTIVE_WINDOW_DAYS = 15             # §12 الحوالات النشطة القابلة للتعديل
