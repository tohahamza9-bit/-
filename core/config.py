"""
الإعدادات — من متغيّرات البيئة فقط (§14.3: لا مفاتيح في الكود).
المعلّقات التقنية (ملحق ب) تُحمَّل من ملفات config/*.json القابلة للإعداد.
"""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ── MongoDB (مصدر الحقيقة §2) ──
    mongo_uri: str = Field(default="mongodb://localhost:27017")
    mongo_db: str = Field(default="moneyado")

    # ── SQL Server (تحقّق قراءة فقط §11.4) ──
    sql_dsn: str = Field(default="")            # سلسلة اتصال pyodbc — مستخدم قراءة فقط
    sql_enabled: bool = Field(default=False)    # يُفعَّل بعد إعداد النسخة التجريبية

    # ── الغرف (ملحق ب-4) — معرّفات JID ──
    central_room_jid: str = Field(default="")   # المركزية — القراءة + Reply
    admin_room_jid: str = Field(default="")     # غرفة المسؤول — التصعيد
    # غرف الزبائن/الخزائن: قوائم JID (قراءة صامتة §2.2)
    customer_room_jids: str = Field(default="")  # مفصولة بفواصل
    treasury_room_jids: str = Field(default="")

    # ── جسر WhatsApp (Baileys ↔ Python) ──
    whatsapp_bridge_url: str = Field(default="http://localhost:3001")
    internal_token: str = Field(default="")     # SEC-002 X-Internal-Token

    # ── MONEYADO RPA ──
    moneyado_fields_file: str = Field(default=str(CONFIG_DIR / "moneyado_fields.json"))
    moneyado_password_encrypted: str = Field(default="")  # DPAPI (§2.1) — ليس نصًّا واضحًا
    screenshot_dir: str = Field(default=str(ROOT / "artifacts" / "screenshots"))

    # ── مهل MONEYADO RPA (جهاز بطيء/Windows قديم §11.3) — ثوانٍ، قابلة للضبط من .env ──
    moneyado_step_delay: float = Field(default=0.3)        # بعد تعبئة كل حقل
    moneyado_enter_wait: float = Field(default=3.0)        # انتظار ظهور اسم الزبون بعد Enter (إجمالي)
    moneyado_post_store_wait: float = Field(default=2.0)   # بعد «تخزين» قبل فحص النافذة الطارئة
    moneyado_post_store_close_wait: float = Field(default=1.0)  # بعد Enter على النافذة الرئيسية حتى تظهر القائمة
    moneyado_connect_wait: float = Field(default=1.0)      # بعد move_window قبل أي تفاعل
    moneyado_open_timeout: float = Field(default=10.0)     # فتح شاشة العملية من القائمة (زر + ظهور الفورم)
    moneyado_dry_run_wait: float = Field(default=3.0)      # DRY_RUN: مهلة معاينة بصرية (الشاشة مفتوحة) قبل ✅

    # ── التشغيل ──
    dashboard_port: int = Field(default=8000)
    log_dir: str = Field(default=str(ROOT / "artifacts" / "logs"))
    # الوجهات المسموحة للإرسال (§2.2) — تُحسب من الغرف أعلاه، غير قابلة للكسر
    dry_run: bool = Field(default=True)         # لا كتابة فعلية في MONEYADO حتى التفعيل

    @property
    def allowed_output_jids(self) -> set[str]:
        """قائمة بيضاء صارمة (§2.2): المركزية + المسؤول فقط."""
        return {j for j in {self.central_room_jid, self.admin_room_jid} if j}

    @property
    def customer_rooms(self) -> list[str]:
        return [j.strip() for j in self.customer_room_jids.split(",") if j.strip()]

    @property
    def treasury_rooms(self) -> list[str]:
        return [j.strip() for j in self.treasury_room_jids.split(",") if j.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_json_config(path: str | Path) -> dict:
    """تحميل ملف إعداد JSON (auto_id، استعلامات SQL). يفشل بوضوح إن غاب (T5)."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"ملف الإعداد غير موجود: {p} — استكمِل المعلّق التقني (ملحق ب) قبل التشغيل."
        )
    return json.loads(p.read_text(encoding="utf-8"))
