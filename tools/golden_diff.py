"""
أداة تشخيص: تقود الأنبوب الحقيقي على البيانات الذهبية وتقارن الناتج بالحقول المتوقّعة.

تُشغَّل يدوياً:  python tools/golden_diff.py  [--verbose] [--only <case_id_substr>]

تكتب تقريراً بترميز UTF-8 إلى tools/golden_diff_report.txt وتطبع ملخّصاً.
ملاحظة: حقول expected مستخرَجة آلياً وبعضها خاطئ (تحذير الملف) — الأداة تشخيصية للفرز اليدوي.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from core import db as dbmod  # noqa: E402
from core.constants import SEED_SUPPLIERS, SEED_TREASURIES  # noqa: E402
from tests.golden_support import (  # noqa: E402
    adapt, build_pipeline, clean, compare, ingest_case, iter_cases, load_fixture,
    seed_golden_treasuries,
)


async def _mk_db():
    d = dbmod.Database("mongodb://mock", "moneyado_golden")
    d._client = AsyncMongoMockClient()
    d.mdb = d._client[d._db_name]
    for attr, (cls, coll, key) in {
        "raw": (dbmod.RawMessageRepo, "raw_messages", "message_key"),
        "rooms": (dbmod.RoomRepo, "rooms", "jid"),
        "deals": (dbmod.DealRepo, "deals", "deal_id"),
        "ledger": (dbmod.LedgerRepo, "ledger", "entry_id"),
        "outbox": (dbmod.OutboxRepo, "outbox", "job_id"),
        "outgoing": (dbmod.OutgoingRepo, "outgoing", "_id"),
        "treasuries": (dbmod.TreasuryListRepo, "treasuries", "name"),
        "suppliers": (dbmod.SupplierListRepo, "suppliers", "name"),
        "payment_channels": (dbmod.PaymentChannelListRepo, "payment_channels", "name"),
        "employees": (dbmod.EmployeeListRepo, "employees", "whatsapp_number"),
        "control": (dbmod.ControlRepo, "bot_control", "_key"),
        "dead_letter": (dbmod.DeadLetterRepo, "dead_letter", "_id"),
        "pending_replies": (dbmod.PendingReplyRepo, "pending_replies", "message_key"),
        "unknown_terms": (dbmod.UnknownTermRepo, "unknown_terms", "term"),
        "sender_slots": (dbmod.SenderSlotRepo, "sender_slots", "slot_key"),
        "reconciliation": (dbmod.ReconciliationRepo, "reconciliation_reports", "deal_id"),
    }.items():
        setattr(d, attr, cls(d.mdb, coll, key))
    await d.treasuries.seed_if_empty(SEED_TREASURIES)
    await d.suppliers.seed_if_missing(SEED_SUPPLIERS)
    await seed_golden_treasuries(d)
    return d


async def run(args):
    data = load_fixture()
    field_diff = Counter()
    total = clean_n = 0
    detail = []
    for cid, msgs, expected in iter_cases(data):
        if args.only and args.only not in cid:
            continue
        total += 1
        db = await _mk_db()               # قاعدة نظيفة لكل حالة (عزل تام)
        pipe = build_pipeline(db)
        deal = await ingest_case(pipe, msgs)
        got = adapt(deal)
        diffs = compare(expected, got)
        if not diffs:
            clean_n += 1
            continue
        for (f, e, g) in diffs:
            field_diff[f] += 1
        detail.append((cid, msgs, diffs, deal))

    out = io.StringIO()
    out.write(f"\n{'='*70}\nإجمالي: {total} | مطابِقة: {clean_n} | بها فروق: {total-clean_n}\n")
    out.write(f"{'-'*70}\nفروق لكل حقل:\n")
    for f, n in field_diff.most_common():
        out.write(f"  {f:16s} {n}\n")
    out.write(f"{'='*70}\nالتفاصيل:\n")
    for cid, msgs, diffs, deal in detail:
        st = deal.status.value if deal is not None else None
        out.write(f"\n[{cid}]  deal={'yes' if deal else 'None'} status={st}\n")
        if args.verbose and msgs:
            out.write(f"  msg1: {clean(msgs[0])[:90]!r}\n")
            if len(msgs) > 1:
                out.write(f"  msg2: {clean(msgs[1])[:90]!r}\n")
        for (f, e, g) in diffs:
            out.write(f"    {f:14s} expected={e!r:30}  got={g!r}\n")
    report = out.getvalue()
    (ROOT / "tools" / "golden_diff_report.txt").write_text(report, encoding="utf-8")
    print(f"report -> tools/golden_diff_report.txt  ({total} cases, {clean_n} clean, "
          f"{total-clean_n} with diffs)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--only", default=None)
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()
