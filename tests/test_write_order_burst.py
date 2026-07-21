"""
العرض 1 — «التنزيل العشوائيّ»: ترتيب الكتابة الحتميّ عند الدفعة (root-fix).

الجذر: `created_at` يُختَم **وقت النبضة** لا وقت الرسالة، فدفعةٌ كاملة (12 صفقة في بلاغ
الإنتاج 2026-07-21 17:51) تحمل `created_at` متطابقًا للمِلّي ثانية. `by_status`/`waiting_in_room`
كانا يرتّبان بـ`created_at` فقط، فعند التساوي يُرجِع Mongo الوثائق بترتيب الإدراج/`$natural`
لا بترتيب الوصول الحقيقيّ → الكتابة في MONEYADO بترتيب عشوائيّ.

الإصلاح: كسر تعادلٍ ثانويّ `(created_at, first_received_at, _id)`. `first_received_at` يحمل
وقت وصول الرسالة الأولى الحقيقيّ (متمايز)، فيعود الترتيب لترتيب الوصول بلا لمس دلالة `created_at`.

المعيار الحاسم: صفقاتٌ بنفس `created_at` بالضبط، مُدرَجةٌ بترتيبٍ مبعثر، أوقاتُ وصولها متدرّجة →
`by_status` يجب أن يُعيدها بترتيب `first_received_at` (الوصول)، لا ترتيب الإدراج.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.constants import Status
from core.models import Deal

NOW = datetime(2026, 7, 21, 17, 51, 20, 313000, tzinfo=timezone.utc)  # ختم الدفعة الموحّد (مِلّي ثانية)


def _deal(n: int, recv_offset: int) -> Deal:
    """صفقة PARSED بنفس `created_at` (وقت النبضة) و`first_received_at` متمايز (وقت الوصول)."""
    return Deal(
        deal_id=f"deal-{n:02d}",
        status=Status.PARSED,
        created_at=NOW,                                       # ← موحّد لكل الدفعة (الجذر)
        updated_at=NOW,
        first_received_at=NOW - timedelta(seconds=10) + timedelta(seconds=recv_offset),
        chat_jid="central@g.us",
    )


async def test_by_status_orders_burst_by_arrival_not_insertion(db):
    """دفعة بنفس created_at، مُدرَجةٌ مبعثرةً، تُقرأ بترتيب الوصول (first_received_at)."""
    # أوقات وصول متدرّجة 0..6ث (كبلاغ 17:51:13→17:51:17)، مُدرَجة بترتيبٍ **مبعثر** عمدًا.
    insertion = [3, 1, 6, 0, 4, 2, 5]
    for n, off in enumerate(insertion):
        await db.deals.upsert(_deal(n, off))

    ordered = await db.deals.by_status(Status.PARSED)
    arrivals = [d.first_received_at.replace(tzinfo=None) for d in ordered]

    # الترتيب الناتج = تصاعديّ بوقت الوصول، لا ترتيب الإدراج المبعثر.
    assert arrivals == sorted(arrivals), (
        f"ترتيب الكتابة ليس بوقت الوصول: {[a.second for a in arrivals]}"
    )
    # وتحديدًا: الأقدم وصولًا (offset=0) أوّلًا، والأحدث (offset=6) آخرًا.
    naive = NOW.replace(tzinfo=None)
    assert arrivals[0] == naive - timedelta(seconds=10)
    assert arrivals[-1] == naive - timedelta(seconds=4)


async def test_waiting_in_room_orders_burst_by_arrival(db):
    """نفس كسر التعادل يسري على مسار مطابقة الرسالة الثانية (waiting_in_room)."""
    insertion = [2, 0, 3, 1]
    for n, off in enumerate(insertion):
        d = _deal(n, off)
        d.status = Status.WAITING_SECOND_LEG
        await db.deals.upsert(d)

    ordered = await db.deals.waiting_in_room("central@g.us")
    arrivals = [d.first_received_at.replace(tzinfo=None) for d in ordered]
    assert arrivals == sorted(arrivals)
    assert arrivals[0] == NOW.replace(tzinfo=None) - timedelta(seconds=10)
