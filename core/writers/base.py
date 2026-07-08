"""
واجهة الكاتب القابل للتبديل (§2.1) — العقد بين وحدة الفهم والكُتّاب.

> التبديل بين MONEYADO وOdoo بتغيير إعداد واحد، بلا إعادة بناء.

كل كاتب يستقبل WriteJob (أمر تعبئة شاشة واحدة) ويُرجع WriteResult.
الكاتب لا يقرّر «هل يُخزّن؟» — هذا قرار الأنبوب (Kill Switch §13). الكاتب ينفّذ فقط.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import WriteJob, WriteResult


class Writer(ABC):
    """عقد الكاتب. أي كاتب جديد يرث هذا فقط."""

    name: str = "base"

    @abstractmethod
    async def write(self, job: WriteJob, *, commit: bool) -> WriteResult:
        """
        يعبّئ الشاشة من job.leg بالترتيب المحدّد (§11.1).

        - commit=True  → يضغط «تخزين» (Kill Switch مفعّل §13).
        - commit=False → يعبّئ ويتوقّف عند «تخزين» ثم STOP آمن (وضع الإيقاف §13 / DRY_RUN).

        يجب ألا يلمس أي شاشة/زر خارج المسموح (§2.3). النافذة الطارئة → STOP + لقطة + needs_review.
        """
        raise NotImplementedError

    async def health_check(self) -> bool:
        """هل الكاتب جاهز (الشاشة مفتوحة/الاتصال قائم)؟ افتراضيًا True."""
        return True
