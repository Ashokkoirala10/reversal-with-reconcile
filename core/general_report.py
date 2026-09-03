"""General report: live analytics computed directly from the switch DB
(core.switch_db), not from any uploaded/processed file — the "General"
dashboard tab exists specifically so member/aggregator/On-Us-Off-Us/
issuer/acquirer numbers can be pulled for a date range straight from the
switch's own transaction_entry table, independent of whether anyone has
run a reversal/reconcile file for that period at all.

Reuses the exact same classification rules the rest of the app already
applies to file-based rows (core.services.is_on_us /
normalize_failure_reason, reconcile.engine.bucket_key_for_amount) so
these numbers agree with the file-based dashboards whenever both cover
the same period — only the data source differs.

A row counts as "success" by its Overall Status (SUCCESS, the same field
core.services._compute_totals keys off of for file-based rows) —
everything else counts as "failed", including REVERSAL (a transaction
that was later reversed is not a successful one for this report). Any
status besides SUCCESS/FAILED/REVERSAL still falls into a separate
"other" count/amount rather than being silently dropped, so total_count
always matches a plain `SELECT COUNT(*)` on transaction_entry for the
same window.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from reconcile.engine import AMOUNT_BUCKETS, bucket_key_for_amount

from .services import is_on_us, normalize_failure_reason, to_float


def _empty_split() -> dict:
    return {
        "success_count": 0, "success_amount": 0.0,
        "failed_count": 0, "failed_amount": 0.0,
        "other_count": 0, "other_amount": 0.0,
    }


def _finalize_split_rows(by_key: dict) -> list[dict]:
    rows = []
    for key in sorted(by_key.keys(), key=lambda k: (k == "—", k)):
        v = by_key[key]
        rows.append(
            {
                "name": key,
                "success_count": v["success_count"],
                "success_amount": round(v["success_amount"], 2),
                "failed_count": v["failed_count"],
                "failed_amount": round(v["failed_amount"], 2),
                "other_count": v["other_count"],
                "other_amount": round(v["other_amount"], 2),
                "total_count": v["success_count"] + v["failed_count"] + v["other_count"],
                "total_amount": round(v["success_amount"] + v["failed_amount"] + v["other_amount"], 2),
            }
        )
    return rows


def _ordered_reasons(bucket: dict, total: int) -> list[dict]:
    rows = []
    for reason, count in sorted(bucket.items(), key=lambda kv: -kv[1]):
        rows.append({"reason": reason, "count": count, "pct": round(100 * count / total) if total else 0})
    return rows


def compute_general_report(rows: list[dict[str, Any]]) -> dict:
    member_totals: dict = defaultdict(_empty_split)
    aggregator_totals: dict = defaultdict(_empty_split)
    issuer_totals: dict = defaultdict(_empty_split)
    acquirer_totals: dict = defaultdict(_empty_split)

    onus_reason_totals: dict = defaultdict(int)
    offus_reason_totals: dict = defaultdict(int)
    failed_onus_count = failed_onus_amount = 0
    failed_offus_count = failed_offus_amount = 0.0

    onus_buckets = {key: {"count": 0, "amount": 0.0} for key, _l, _u in AMOUNT_BUCKETS}
    offus_buckets = {key: {"count": 0, "amount": 0.0} for key, _l, _u in AMOUNT_BUCKETS}

    success_count = failed_count = other_count = 0
    success_amount = failed_amount = other_amount = 0.0
    other_status_totals: dict = defaultdict(int)

    for row in rows:
        overall = str(row.get("Overall Status") or "").strip().upper()
        if overall == "REVERSAL":
            overall = "FAILED"
        member = row.get("Member Name") or "—"
        aggregator = row.get("Aggregator") or "—"
        debtor_bank = row.get("Debtor Bank") or "—"
        creditor_bank = row.get("Creditor Bank") or "—"
        amount = to_float(row.get("Transaction Amount"))
        onus = is_on_us(row.get("Debtor Bank"), row.get("Creditor Bank"))

        if overall not in ("SUCCESS", "FAILED"):
            other_count += 1
            other_amount += amount
            other_status_totals[overall or "Unknown"] += 1
            for totals, key in (
                (member_totals, member),
                (aggregator_totals, aggregator),
                (issuer_totals, debtor_bank),
                (acquirer_totals, creditor_bank),
            ):
                totals[key]["other_count"] += 1
                totals[key]["other_amount"] += amount
            continue

        target = "success" if overall == "SUCCESS" else "failed"
        for totals, key in (
            (member_totals, member),
            (aggregator_totals, aggregator),
            (issuer_totals, debtor_bank),
            (acquirer_totals, creditor_bank),
        ):
            totals[key][f"{target}_count"] += 1
            totals[key][f"{target}_amount"] += amount

        if overall == "SUCCESS":
            success_count += 1
            success_amount += amount
            bucket_key = bucket_key_for_amount(amount)
            buckets = onus_buckets if onus else offus_buckets
            buckets[bucket_key]["count"] += 1
            buckets[bucket_key]["amount"] += amount
        else:
            failed_count += 1
            failed_amount += amount
            reason = normalize_failure_reason(row.get("Source Message"))
            if onus:
                failed_onus_count += 1
                failed_onus_amount += amount
                onus_reason_totals[reason] += 1
            else:
                failed_offus_count += 1
                failed_offus_amount += amount
                offus_reason_totals[reason] += 1

    bucket_rows = []
    for key, label, _upper in AMOUNT_BUCKETS:
        o = onus_buckets[key]
        f = offus_buckets[key]
        bucket_rows.append(
            {
                "label": label,
                "onus_count": o["count"],
                "onus_amount": round(o["amount"], 2),
                "offus_count": f["count"],
                "offus_amount": round(f["amount"], 2),
                "total_count": o["count"] + f["count"],
                "total_amount": round(o["amount"] + f["amount"], 2),
            }
        )

    other_status_breakdown = [
        {"status": status, "count": count}
        for status, count in sorted(other_status_totals.items(), key=lambda kv: -kv[1])
    ]

    return {
        "totals": {
            "total_count": success_count + failed_count + other_count,
            "success_count": success_count,
            "success_amount": round(success_amount, 2),
            "failed_count": failed_count,
            "failed_amount": round(failed_amount, 2),
            "other_count": other_count,
            "other_amount": round(other_amount, 2),
            "other_status_breakdown": other_status_breakdown,
        },
        "member_rows": _finalize_split_rows(member_totals),
        "aggregator_rows": _finalize_split_rows(aggregator_totals),
        "issuer_rows": _finalize_split_rows(issuer_totals),
        "acquirer_rows": _finalize_split_rows(acquirer_totals),
        "onus_offus": {
            "onus": {
                "count": failed_onus_count,
                "amount": round(failed_onus_amount, 2),
                "reasons": _ordered_reasons(onus_reason_totals, failed_onus_count),
            },
            "offus": {
                "count": failed_offus_count,
                "amount": round(failed_offus_amount, 2),
                "reasons": _ordered_reasons(offus_reason_totals, failed_offus_count),
            },
        },
        "buckets": bucket_rows,
    }
