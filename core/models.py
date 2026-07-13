"""
نماذج البيانات المشتركة — العقد الذي تعتمد عليه كل الوحدات.
تُخزَّن في MongoDB. مصدر الحقيقة للحالة = الدفتر (LedgerEntry) — §8.3 §9.
لا يغيّر أي وكيل فرعي هذا الملف؛ إن احتاج حقلًا يضيفه Optional فقط.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

from .constants import (
    Currency,
    Mark,
    OperationType,
    RoomType,
    Status,
    TreasuryType,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1) الرسالة الخام — تُخزَّن فورًا قبل أي معالجة (§7.1 بند 1)
# ─────────────────────────────────────────────────────────────────────────────
class RawMessage(BaseModel):
    """رسالة واتساب كما وصلت — بلا تفسير. انقطاع الكهرباء لا يضيّعها."""
    message_key: str = Field(..., description="مفتاح رسالة واتساب — المعرّف الأثبت (§9)")
    chat_jid: str = Field(..., description="معرّف الغرفة (المركزية/زبون/خزينة/مسؤول)")
    sender_jid: Optional[str] = None
    text: str = ""
    received_at: datetime = Field(..., description="ختم وقت بالثانية/الميلي لحظة الوصول (§7.1 بند 3)")
    edited_at: Optional[datetime] = None       # §7.2 «الحرف» — التعديل خلال المهلة
    reply_to_key: Optional[str] = None         # Reply → الربط بمفتاح الأصل (§10)
    is_from_me: bool = False
    raw: dict = Field(default_factory=dict, description="حمولة Baileys الكاملة")
    processed: bool = False                     # §7.1 بند 4 «اتعالجت»


# ─────────────────────────────────────────────────────────────────────────────
# 1-ب) الغرفة — تصنيف الغرف يُدار في MongoDB (اكتشاف تلقائي + Dashboard §2.2 §13)
# 🔴 المركزية/المسؤول (حدود الكتابة) تبقى مثبّتة في .env — هذا السجل للقراءة/الالتقاط فقط.
# ─────────────────────────────────────────────────────────────────────────────
class Room(BaseModel):
    """غرفة واتساب مكتشَفة/مصنّفة. المفتاح jid (معرّف المجموعة)."""
    jid: str = Field(..., description="معرّف المجموعة (remoteJid @g.us)")
    name: Optional[str] = None                  # اسم المجموعة (subject) — يُملأ من groups.upsert
    type: RoomType = RoomType.UNCLASSIFIED      # التصنيف — الافتراضي: تنتظر تصنيف المشغّل
    active: bool = True                         # إيقاف بلا حذف (§13)
    treasury_code: Optional[str] = None         # ربط غرفة الخزينة بخزينة محدّدة (كود MONEYADO) — فارغ لغير الخزائن
    discovered_at: Optional[datetime] = None    # لحظة أول اكتشاف
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None            # من صنّفها (dashboard/system/discovery)


# ─────────────────────────────────────────────────────────────────────────────
# 2) الحوالة المفكّكة — ناتج وحدة الفهم (§3)
# ─────────────────────────────────────────────────────────────────────────────
class TreasuryRef(BaseModel):
    """مرجع خزينة محلول (بعد المطابقة مع القوائم §4)."""
    code: Optional[str] = None          # كود MONEYADO (None = معلّق، لا يُنزَّل)
    name: str
    type: TreasuryType
    currency: Optional[Currency] = None


class SupplierRef(BaseModel):
    """مرجع مورد محلول (§5.4)."""
    code: Optional[str] = None
    name: str


class ParsedLeg(BaseModel):
    """طرف واحد من الصفقة (بيع أو شراء)."""
    operation: OperationType
    customer_code: Optional[str] = None       # الكود = مرساة الهوية (§1.1)
    customer_name: Optional[str] = None       # يُتسامح مع خطأ الإملاء
    supplier: Optional[SupplierRef] = None     # للطرف الثاني (شراء من مورد)
    supplier_price_raw: Optional[str] = None   # سعر المورد من سطر «المورد: طه 5.72» في SI بيع+شراء
    price_raw: Optional[str] = None            # السعر كما ورد
    price_normalized: Optional[str] = None     # بعد §3.6 (تونسي 0.xxxx)
    amount: Optional[float] = None             # المبلغ قبل الخصم (§6.2)
    currency: Optional[Currency] = None
    treasury: Optional[TreasuryRef] = None
    reference_number: Optional[str] = None     # الرقم الإشاري A6xxx (غير حاسم §9)
    phone: Optional[str] = None                # رقم المستلم — مميّز التجميع (§7.3)
    payment_method: Optional[str] = None       # فودافون كاش / إنستا باي (§3.4)
    recipient_name: Optional[str] = None       # اسم المستلم → خانة الملاحظات (§11.1-12)
    notes: Optional[str] = None                # نص حرّ من الرسالة الأولى (صافي/خصم/ملاحظات §3.2) → خانة الملاحظات
    country: Optional[str] = None              # العاصمة/سوسة.. أو من مفتاح الهاتف
    commission: Optional[float] = None         # الفرق بالسالب (§6.2)
    commission_rate: float = 0.0               # 0 دائمًا (§6.2)
    amount_after_discount: Optional[float] = None  # §6: صيغة SI تعطي القيمة بعد الخصم صريحة
    source_message_key: Optional[str] = None   # الرسالة التي جاء منها هذا الطرف
    sender_jid: Optional[str] = None           # مُرسِل الرسالة — لربط الرد بنفس المُرسِل (§7.3)
    # ── إشارات نوع العملية والتجميع (§5) — تحسبها وحدة الفهم، يستهلكها الطابور ──
    expects_pair: bool = False                 # §5.2/§5.3: ينتظر طرفًا ثانيًا (شراء/خزينة بيع-وشراء)
    explicit_operation: bool = False           # §5.1: كلمة «بيع»/«شراء» صريحة (تتجاوز الافتراضي)
    is_supplier_counterpart: bool = False      # §5.3: الطرف اسمه مورد (⇒ شراء) لا زبون
    is_si_format: bool = False                 # §3.3: حوالة SI معنونة مكتملة برسالة واحدة (لا تنتظر طرفًا ثانيًا)
    unresolved_treasury: Optional[str] = None  # اسم خزينة SI معنون لم يُحلّ → يُلتقط مجهولًا (db.unknown_terms)


class ParseResult(BaseModel):
    """ناتج محاولة الفهم — قد يكون حوالة، هدرزة، أو رسالة يُتجاهلها."""
    kind: str = Field(..., description="'transfer' | 'noise' | 'silent_ignore' | 'control' | 'out_of_scope'")
    leg: Optional[ParsedLeg] = None            # عند kind='transfer'
    control_action: Optional[str] = None       # 'cancel'|'edit'|'confirm'|'correct' (§10)
    control_value: Optional[float] = None       # قيمة التعديل/التصحيح
    reason: Optional[str] = None                # سبب noise/ignore (للتسجيل T5)
    confidence: float = 1.0
    # 🔴 توسّعات «ألف/آلاف» (§3.5): «32 ألف»→32000 — لتنبيه المركزية+المسؤول. [{original, value}]
    alf_expansions: list[dict] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 3) الصفقة — وحدة المعالجة (بيع فقط أو بيع + شراء) — §5 §7.3
# ─────────────────────────────────────────────────────────────────────────────
class Deal(BaseModel):
    deal_id: str
    status: Status = Status.RAW
    sell_leg: Optional[ParsedLeg] = None
    buy_leg: Optional[ParsedLeg] = None        # موجود فقط عند بيع + شراء
    is_two_legged: bool = False
    created_at: datetime
    updated_at: datetime
    # 🔴 ختم وصول **الرسالة الأولى** للصفقة (§7.3): معيار جدولة المعالجة — تُعالَج الصفقات
    #    مرتّبةً بهذا الختم لا بوقت الاكتمال، فلا يتجاوز زوجٌ اكتمل متأخّرًا رسالةً مستقلّة
    #    (SI) وصلت قبله. يُضبط مرّة عند إنشاء الصفقة ويُحفَظ عبر دمج الطرف الثاني (لا يُستبدَل).
    first_received_at: Optional[datetime] = None
    chat_jid: Optional[str] = None             # الغرفة التي وردت منها (§7.3 ربط الرد بنفس الغرفة)
    # التجميع (§7.3)
    grouping_key: Optional[str] = None         # رقم إشاري + هاتف / أو message key
    waiting_deadline: Optional[datetime] = None  # ≤ 90s
    # المطابقة (§8)
    matched_customer_room: bool = False
    matched_treasury_room: bool = False
    # §8: طرف الشراء (buy_leg) في صفقات الطرفين يُطابَق في غرفة المورد (رقم إشاري + مبلغ)
    matched_supplier_room: bool = False
    # §8.1: خزينة الصفقة بلا غرفة واتساب مرتبطة → لا اعتماد تلقائي، بل «تم» يدوي من موظف معتمد.
    treasury_no_room: bool = False
    # §8: صفقة طرفين بلا غرفة مورد مضافة → نفس منطق الخزينة بلا غرفة («تم» يدوي).
    supplier_no_room: bool = False
    mark: Optional[Mark] = None
    hold_reason: Optional[str] = None          # سبب ⚠️
    # التذكيرات (§8.1)
    reminders_sent: int = 0
    last_reminder_at: Optional[datetime] = None
    # حوالة A ناقصة (§7.3): أُرسِل تنبيه «أكمل البيانات» الخفيف في المركزية؟ (منع تكراره كل نبضة)
    incomplete_warned: bool = False
    # الإلغاء عبر Reply (ميزة الإلغاء): متى/بأي رسالة أُلغيت + السبب الإضافيّ إن وُجد
    cancelled_at: Optional[datetime] = None
    cancelled_by_key: Optional[str] = None     # مفتاح رسالة الإلغاء (من عمل Reply)
    cancellation_reason: Optional[str] = None  # النص الإضافيّ بعد كلمة الإلغاء إن وُجد
    # سجلّ التعديلات (ميزة التعديل عبر Reply): [{amended_at, amended_by_key, old_net, new_net,
    # old_commission, new_commission, reason}]. الصافي/العمولة الحاليّان محفوظان حيّاً في sell_leg.
    amendments: list[dict] = Field(default_factory=list)
    # التتبّع
    source_message_keys: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 4) بند الـ Outbox — أمر كتابة قابل للتبديل بين الكُتّاب (§2.1)
# ─────────────────────────────────────────────────────────────────────────────
class WriteJob(BaseModel):
    """أمر تعبئة شاشة واحدة (بيع أو شراء). الترتيب ثابت: بيع أولًا (§7.3)."""
    job_id: str
    deal_id: str
    operation: OperationType
    leg: ParsedLeg
    order_index: int = 0                        # 0=بيع، 1=شراء
    is_reversal: bool = False                   # قيد عكسي للإلغاء/التعديل (§10)
    attempts: int = 0
    max_attempts: int = 1                       # الشراء: محاولة واحدة (§11.4)
    created_at: datetime


class WriteResult(BaseModel):
    """ناتج محاولة الكتابة من أي Writer."""
    ok: bool
    moneyado_ref: Optional[str] = None          # رقم المعاملة المولّد
    error: Optional[str] = None
    screenshot_path: Optional[str] = None       # عند النافذة الطارئة (§11.3)
    needs_review: bool = False                   # dead-letter
    dry_run: bool = False                        # DRY_RUN: عُبّئت الشاشة بلا «تخزين»/«خروج» (معاينة بصرية)


# ─────────────────────────────────────────────────────────────────────────────
# 5) دفتر البوت — مصدر الحقيقة لمنع التكرار (§9) — Append-only (§10)
# ─────────────────────────────────────────────────────────────────────────────
class LedgerEntry(BaseModel):
    """ما نزّله البوت فعلًا. يخزّن لكل عملية (§9 نقطة الدفتر)."""
    entry_id: str
    deal_id: str
    message_key: str                            # رقم رسالة واتساب
    reference_number: Optional[str] = None
    operation: OperationType
    is_reversal: bool = False
    amount: float
    currency: Currency
    customer_code: Optional[str] = None
    treasury_code: Optional[str] = None
    moneyado_ref: Optional[str] = None          # مرجع MONEYADO بعد تأكيد SQL
    status: Status
    sql_verified: bool = False
    created_at: datetime


# ─────────────────────────────────────────────────────────────────────────────
# 6) قوائم Dashboard — تُدار من §13 وتُخزَّن في MongoDB
# ─────────────────────────────────────────────────────────────────────────────
class TreasuryRecord(BaseModel):
    code: Optional[str] = None
    name: str
    type: TreasuryType
    currency: Optional[Currency] = None
    aliases: list[str] = Field(default_factory=list)
    active: bool = True


class SupplierRecord(BaseModel):
    code: Optional[str] = None
    name: str
    aliases: list[str] = Field(default_factory=list)
    active: bool = True


class EmployeeRecord(BaseModel):
    """موظف معتمد — «تم»/الإلغاء تُقبل منه فقط (§8.3 §10 §13)."""
    whatsapp_number: str
    name: str
    active: bool = True


class UnknownTerm(BaseModel):
    """كلمة (خزينة/مورد) تعذّر حلّها — تُلتقط للمراجعة والإسناد اليدويّ من اللوحة (§4.5 §5.4).

    بدل التخمين الفضفاض (خطر ماليّ §0)، يُجمَع الاسم غير المعروف مع سياقه وعدّاده وآخر ظهور،
    فيُسنِده المشرف يدويًّا كـ alias للخزينة/المورد الصحيح (POST /api/unknown-terms/{term}/assign)."""
    term: str                                    # النص المطبَّع للمطابقة (مفتاح مع context)
    context: str                                 # 'treasury' | 'supplier'
    count: int = 1                               # مرّات الظهور (يُزاد عند التكرار)
    last_seen: Optional[datetime] = None


class BotControl(BaseModel):
    """حالة التحكّم — Kill Switch (§13). الافتراضي عند التشغيل: إيقاف."""
    storage_enabled: bool = False               # False = يعبّئ ويتوقف عند «تخزين»
    auto_trust: bool = False                     # True = «وضع تلقائي»: تخطّي مطابقة الغرف → بوابة الثقة مباشرة (§8.1)
    state: str = "stopped"                       # running | stopped | error
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# 7) وجهة الإرسال — قاعدة الإخراج الصارمة (§2.2)
# ─────────────────────────────────────────────────────────────────────────────
class OutgoingMessage(BaseModel):
    """كل مخرجات البوت = Reply. الوجهات المسموحة: المركزية + غرفة المسؤول فقط."""
    chat_jid: str
    text: str
    reply_to_key: Optional[str] = None          # Reply بمفتاح الرسالة (§8.3)
    reaction: Optional[str] = None              # علامة صامتة (🟡/✅/🔴)
    forward_key: Optional[str] = None           # forward للرسالة الأصلية (مفتاحها) — لتصعيد الفشل (§8.3)
    is_alert: bool = False                       # تنبيه حرج (⚠️/🔴/🚨 sweep/تصعيد/ردّ مباشر) — يُعفى من
    #                                              سقف warm-up في الجسر فيصل فورًا (لا يعلق كنصّ عادي)
