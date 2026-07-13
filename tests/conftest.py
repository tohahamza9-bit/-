"""
تجهيزات اختبار مشتركة. الأمثلة نصوص حقيقية من المواصفات (أسماء وهمية).
كل وكيل يستخدم هذه التجهيزات ليختبر وحدته على بيانات واقعية.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── قاعدة بيانات وهمية (mongomock-motor) — للوحدات التي تلمس DB ──────────────
@pytest.fixture
async def db():
    """قاعدة MongoDB وهمية جاهزة مع الخزائن الافتراضية والفهارس."""
    from mongomock_motor import AsyncMongoMockClient

    from core.constants import SEED_TREASURIES
    from core import db as dbmod

    class _MockDatabase(dbmod.Database):
        async def connect(self):
            self._client = AsyncMongoMockClient()
            self.mdb = self._client[self._db_name]
            # نفس ربط المستودعات كما في Database.connect
            self.raw = dbmod.RawMessageRepo(self.mdb, "raw_messages", "message_key")
            self.rooms = dbmod.RoomRepo(self.mdb, "rooms", "jid")
            self.deals = dbmod.DealRepo(self.mdb, "deals", "deal_id")
            self.ledger = dbmod.LedgerRepo(self.mdb, "ledger", "entry_id")
            self.outbox = dbmod.OutboxRepo(self.mdb, "outbox", "job_id")
            self.outgoing = dbmod.OutgoingRepo(self.mdb, "outgoing", "_id")
            self.treasuries = dbmod.TreasuryListRepo(self.mdb, "treasuries", "name")
            self.suppliers = dbmod.SupplierListRepo(self.mdb, "suppliers", "name")
            self.employees = dbmod.EmployeeListRepo(self.mdb, "employees", "whatsapp_number")
            self.control = dbmod.ControlRepo(self.mdb, "bot_control", "_key")
            self.dead_letter = dbmod.DeadLetterRepo(self.mdb, "dead_letter", "_id")
            self.pending_replies = dbmod.PendingReplyRepo(self.mdb, "pending_replies", "message_key")
            self.unknown_terms = dbmod.UnknownTermRepo(self.mdb, "unknown_terms", "term")
            self.sender_slots = dbmod.SenderSlotRepo(self.mdb, "sender_slots", "slot_key")
            self.reconciliation = dbmod.ReconciliationRepo(self.mdb, "reconciliation_reports", "deal_id")
            # مصادقة اللوحة (§14.3) — نفس الربط كما في Database.connect
            self.users = dbmod.UserListRepo(self.mdb, "users", "username")
            self.sessions = dbmod.SessionRepo(self.mdb, "sessions", "token_hash")
            self.auth_events = dbmod.AuthEventRepo(self.mdb, "auth_events", "_id")
            self.reviews = dbmod.DashboardReviewRepo(self.mdb, "dashboard_reviews", "deal_id")
            self.detection = dbmod.DashboardConfigRepo(self.mdb, "dashboard_config", "_key")

    d = _MockDatabase("mongodb://mock", "moneyado_test")
    await d.connect()
    await d.treasuries.seed_if_empty(SEED_TREASURIES)
    yield d
    await d.close()


# ── الأمثلة الحقيقية من المواصفات (§3، §5) ───────────────────────────────────
FORMAT_A_SIMPLE = """1208 فداء شاكونه 5.84
بلاس
A5169
010954227116
1600 ج م
فودافون
بدون خصم"""

FORMAT_SI = """رقم العملية: SI0464
رقم المستلم: 01093232832
اسم الزبون: مروان الشاوش كود 1284
القيمة قبل الخصم: 3540 ج.م
القيمة بعد الخصم 1%: 3505 ج.م
السعر: 5.9
نوع التحويل: فودافون كاش
الخزينة: بلاس فون"""

# طرفان (§5.5) A6779
TWO_LEG_SELL = """A6779
01115233493
8.475 ج م
فود فون كاش
53 احمد العكاري 5.90"""

TWO_LEG_BUY = """A6779
01115233493
8.391 ج م
فود فون كاش
760 طه 5.86"""

# بيع فقط + خصم تونسي (§5.6) A6604
TND_SELL_ONLY = """A6604 / تونس/العاصمه / محمد عبدالرحيم / 0925135252 / القيمه: 2000 دت
526 بكر همالي 35.75
وليد"""


@pytest.fixture
def examples() -> dict[str, str]:
    return {
        "format_a_simple": FORMAT_A_SIMPLE,
        "format_si": FORMAT_SI,
        "two_leg_sell": TWO_LEG_SELL,
        "two_leg_buy": TWO_LEG_BUY,
        "tnd_sell_only": TND_SELL_ONLY,
    }
