"""
الطابور الصارم وتجميع الطرفين والخصم (§6 §7 §12) — الواجهة العامّة.

- stabilization: انتظار الاستقرار (§7.2) — دوال نقيّة.
- grouping: تجميع الطرفين بالرقم الإشاري + الهاتف (§7.3) — دوال نقيّة.
- commission: الخصم والعمولة بالسالب (§6) — دوال نقيّة.
- service: QueueService — الالتقاط والتجميع والتصعيد وبناء أوامر الكتابة (يستعمل DB).
"""
from __future__ import annotations

from .commission import compute_commission, resolve_two_leg_treasury
from .grouping import compute_grouping_key, is_same_deal
from .service import QueueService
from .stabilization import (
    is_short_placeholder,
    is_stable,
    looks_like_complete_transfer,
    should_ignore_as_noise,
)

__all__ = [
    "QueueService",
    "compute_commission",
    "resolve_two_leg_treasury",
    "compute_grouping_key",
    "is_same_deal",
    "is_stable",
    "should_ignore_as_noise",
    "looks_like_complete_transfer",
    "is_short_placeholder",
]
