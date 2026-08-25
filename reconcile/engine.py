"""The reconciliation engine.

Every actual matching decision here delegates to core.services — the
same, already-tested functions the existing 'core' app uses to check a
need_to_reversal file against a bank statement. This module is the
*dispatch* — which check applies to which row, and how results get
grouped into a report.

Everything is driven strictly off the uploaded Transaction Data file —
FAILED rows, manual-reversal rows, and system-reversal rows are all
identified directly from it (the exact same predicates core's own
process_ibft_file() uses: Overall Status + is_manual_reversal(Source
Message)). No separate reversal file needs to be uploaded at all: the
expected refund Narration is synthesized directly from each row's own
Member Transaction Id via core.services.transform_member_id() — exactly
how the reversal file itself would have built it, minus the Session Id
suffix (see _narration_prefix() below for why).

SCT_NETWORK / NCHL_NETWORK / KHALTI_NETWORK — SUCCESS rows
------------------------------------------------------------
Every SUCCESS row is checked for real — success is never assumed just
because the switch says so, on-us or off-us alike:
  - SCT cross-bank (Debtor != Creditor): CR on the Debtor's own
    statement, DR on the Creditor's own statement, same Network
    Reference Id (core.services.statement_entries_for_reference()).
  - SCT on-us (Debtor == Creditor): most on-us transfers never touch the
    statement at all — normal, no CR/DR expected, reconciled. If the
    reference id *does* show up, the rule is simply: a CR entry AND a DR
    entry both present — that's it, no special-casing for ACQ_SETTL
    settlement rows or any other remarks pattern. Anything short of
    both a CR and a DR present is flagged.
  - NCHL / Khalti: settle through the issuing bank's own statement, but
    the CR and DR legs of the same payment don't share a Network
    Reference Id or ISO id — core.services.is_already_debited_nchl() /
    is_already_debited_khalti() match on the beneficiary name + masked
    settlement account anchors instead.
  - Internal settlement bookkeeping rows (Member Transaction Id like
    "KHALTI_SETTL/00000000PLR6") are excluded from SCT stats entirely —
    see _SETTLEMENT_ARTIFACT_RE below.

FAILED rows
-----------
For every FAILED row (from the transaction file):
  1. Already-succeeded pre-check (see _already_succeeded()) — if the
     statement shows this actually completed (on-us CR+DR / NCHL /
     Khalti / off-us cross-bank CR+DR), that's flagged for review — a
     reversal being needed at all conflicts with evidence the transfer
     went through.
  2. is_failed_but_credited() — if the debtor's statement shows no CR at
     all, this is a clean, ordinary failure: nothing moved, nothing to
     do — reconciled (per instruction: "if not CR then it's failed, it
     doesn't need reconcile, it's already reconciled").
  3. If credited: this is treated exactly like a manual reversal
     candidate (per instruction: "failed but cr also treated as manual
     reversal data") — see below.

Manual reversal rows (Overall Status == REVERSAL, is_manual_reversal())
    — and FAILED-but-credited rows, treated identically
-------------------------------------------------------------------------
The money needs to go back to the customer. The debtor's statement is
searched for DR entries containing the expected refund Narration prefix
(see _narration_prefix()):
  - Zero matches: core's general is_already_reversed() / NCHL / Khalti
    checks are tried as a fallback (the refund may have gone out through
    the automated ISO/anchor path rather than a manually narrated
    payment). Still nothing -> Need To Reversal (pending).
  - Exactly one match: reconciled — reversed properly, once.
  - Two or more matches: flagged as a possible **double reversal** — the
    customer may have been refunded twice.

System reversal rows (Overall Status == REVERSAL, not manual)
-----------------------------------------------------------------
Checked with the same already-succeeded logic as above — on-us CR+DR,
NCHL/Khalti anchor match, or off-us cross-bank CR+DR (all three, if the
relevant statement(s) are available). If the transaction already
completed successfully despite a system reversal being issued against
it, that's flagged for review (mirrors core's own red-flag treatment on
the "Onus Checked-System Reversal" sheet) — otherwise it's a normal,
uneventful system reversal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.services import (
    BankStatementIndex,
    has_duplicate_dr,
    is_already_debited_khalti,
    is_already_debited_nchl,
    is_already_reversed,
    is_failed_but_credited,
    is_khalti_aggregator,
    is_manual_reversal,
    is_on_us,
    is_onus_already_success,
    is_zero_charge_network,
    normalize_failure_reason,
    normalize_reference_id,
    statement_entries_for_reference,
    transform_member_id,
)

from .banks import BANKS, identify_bank

import re

# Internal settlement bookkeeping rows the switch logs for its own audit
# trail — e.g. "KHALTI_SETTL/00000000PLR6" — record the payout leg of an
# *already separately processed* NCHL/Khalti transaction (whose own
# reference id is embedded right after the prefix). They show up as their
# own on-us SCT "SUCCESS" row with a unique reference id of their own, but
# aren't a real, independent customer transfer — the original transaction
# already got reconciled under its own reference id via the NCHL/Khalti
# branch. Counting these separately would just produce a wall of false
# "missing CR" flags (there's no CR to find — the collection already
# happened under the *other* reference id), so they're excluded from SCT
# stats entirely.
_SETTLEMENT_ARTIFACT_RE = re.compile(r"^[A-Z]+_SETTL/", re.IGNORECASE)

SCT = "SCT_NETWORK"
NCHL = "NCHL_NETWORK"
KHALTI = "KHALTI_NETWORK"

# Success amount-range buckets, in order, upper bound inclusive (the last
# one has no upper bound). These are the exact ranges asked for on the
# reversal dashboard's own bucket report ("upto 5000, 5k to 10k, 10k to
# 25k, 25k to 50k, 50k to 100k, and greater than 100k") — reused as-is
# here so both dashboards report the same bands and a monthly submission
# built from either one lines up with the other.
AMOUNT_BUCKETS: tuple[tuple[str, str, float | None], ...] = (
    ("upto_5k", "Up to Rs. 5,000", 5_000),
    ("5k_10k", "Rs. 5,000 - 10,000", 10_000),
    ("10k_25k", "Rs. 10,000 - 25,000", 25_000),
    ("25k_50k", "Rs. 25,000 - 50,000", 50_000),
    ("50k_100k", "Rs. 50,000 - 100,000", 100_000),
    ("above_100k", "Greater than Rs. 100,000", None),
)


def bucket_key_for_amount(amount: float) -> str:
    """Which AMOUNT_BUCKETS key `amount` falls into, by upper bound
    (inclusive) — the last bucket (above_100k) catches everything past
    the highest explicit boundary."""
    for key, _label, upper in AMOUNT_BUCKETS:
        if upper is None or amount <= upper:
            return key
    return AMOUNT_BUCKETS[-1][0]


def _empty_bucket_dict() -> dict:
    return {key: {"count": 0, "amount": 0.0} for key, _label, _upper in AMOUNT_BUCKETS}


def _add_to_bucket(buckets: dict, amount: float) -> None:
    key = bucket_key_for_amount(amount)
    entry = buckets.setdefault(key, {"count": 0, "amount": 0.0})
    entry["count"] += 1
    entry["amount"] += amount


def _to_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip().replace(",", ""))
    except ValueError:
        return 0.0


def _txn_ref(row: dict) -> str:
    return normalize_reference_id(row.get("Network Reference Id"))


def _entry_types(entries: list[dict]) -> tuple[bool, bool]:
    has_cr = any((e.get("entry_type") or "").strip().upper() == "CR" for e in entries)
    has_dr = any((e.get("entry_type") or "").strip().upper() == "DR" for e in entries)
    return has_cr, has_dr


@dataclass
class PairStat:
    debtor: str
    creditor: str
    total: int = 0
    reconciled: int = 0
    flagged: int = 0
    no_statement: int = 0


@dataclass
class FlaggedRow:
    network: str
    debtor: str
    creditor: str
    member_txn_id: str
    ref_id: str
    amount: float
    reason: str


@dataclass
class NoStatementRow:
    network: str
    debtor: str
    creditor: str
    missing_bank: str
    member_txn_id: str
    ref_id: str
    amount: float


@dataclass
class TimeoutRow:
    network: str
    debtor: str
    creditor: str
    member_txn_id: str
    ref_id: str
    amount: float
    source_message: str


@dataclass
class NeedReversalRow:
    source: str
    status: str  # "Reconciled" / "Pending" / "Flagged"
    debtor: str
    creditor: str
    member_txn_id: str
    ref_id: str
    amount: float
    detail: str


@dataclass
class NetworkStat:
    total: int = 0
    reconciled: int = 0
    flagged: int = 0


@dataclass
class ReversalStat:
    total: int = 0
    reconciled: int = 0
    pending: int = 0
    flagged: int = 0  # e.g. reversal issued for an already-successful transaction


@dataclass
class ReconcileResult:
    transaction_count: int = 0
    status_counts: dict[str, int] = field(default_factory=dict)
    network_counts: dict[str, int] = field(default_factory=dict)

    statements_uploaded: list[str] = field(default_factory=list)
    statements_missing: list[str] = field(default_factory=list)

    sct_pairs: dict[tuple[str, str], PairStat] = field(default_factory=dict)
    sct_flagged: list[FlaggedRow] = field(default_factory=list)
    sct_no_statement: list[NoStatementRow] = field(default_factory=list)

    nchl_stat: NetworkStat = field(default_factory=NetworkStat)
    nchl_flagged: list[FlaggedRow] = field(default_factory=list)

    khalti_stat: NetworkStat = field(default_factory=NetworkStat)
    khalti_flagged: list[FlaggedRow] = field(default_factory=list)

    failed_stat: ReversalStat = field(default_factory=ReversalStat)
    manual_reversal_stat: ReversalStat = field(default_factory=ReversalStat)
    system_reversal_stat: ReversalStat = field(default_factory=ReversalStat)

    need_reversal: list[NeedReversalRow] = field(default_factory=list)

    timeout_rows: list[TimeoutRow] = field(default_factory=list)

    warnings: list[str] = field(default_factory=list)

    # --- On-Us / Off-Us failed breakdown (mirrors core's dashboard) ---
    failed_onus_count: int = 0
    failed_onus_amount: float = 0.0
    failed_offus_count: int = 0
    failed_offus_amount: float = 0.0
    failed_reason_breakdown_onus: dict[str, int] = field(default_factory=dict)
    failed_reason_breakdown_offus: dict[str, int] = field(default_factory=dict)

    # --- Success amount-range buckets, split On-Us / Off-Us ---
    success_buckets_onus: dict = field(default_factory=_empty_bucket_dict)
    success_buckets_offus: dict = field(default_factory=_empty_bucket_dict)

    @property
    def sct_total(self) -> int:
        return sum(p.total for p in self.sct_pairs.values())

    @property
    def sct_reconciled(self) -> int:
        return sum(p.reconciled for p in self.sct_pairs.values())

    @property
    def sct_flagged_count(self) -> int:
        return sum(p.flagged for p in self.sct_pairs.values())

    @property
    def sct_no_statement_count(self) -> int:
        return sum(p.no_statement for p in self.sct_pairs.values())

    @property
    def grand_total_reconciled(self) -> int:
        return (
            self.sct_reconciled
            + self.nchl_stat.reconciled
            + self.khalti_stat.reconciled
            + self.failed_stat.reconciled
            + self.manual_reversal_stat.reconciled
            + self.system_reversal_stat.reconciled
        )

    @property
    def grand_total_outstanding(self) -> int:
        return (
            self.sct_flagged_count
            + self.nchl_stat.flagged
            + self.khalti_stat.flagged
            + self.failed_stat.pending
            + self.failed_stat.flagged
            + self.manual_reversal_stat.pending
            + self.manual_reversal_stat.flagged
            + self.system_reversal_stat.flagged
        )


def _get_pair(result: ReconcileResult, debtor, creditor) -> PairStat:
    key = (debtor.key, creditor.key)
    if key not in result.sct_pairs:
        result.sct_pairs[key] = PairStat(debtor=debtor.display_name, creditor=creditor.display_name)
    return result.sct_pairs[key]


def _narration_prefix(member_transaction_id: Any) -> str:
    """The refund narration a manual reversal payment carries, minus the
    trailing '-<Session Id>' suffix. Real DR remarks have been observed
    truncating that suffix (e.g. actual narration
    'REV3072287/AMX202650584063-100224' posts on the statement as
    'REV3072287/AMX202650584063-100' — the bank's own field length cut
    the session id short), so the session id is dropped from the search
    target entirely rather than guessing how much of it survived."""
    return f"REV{transform_member_id(member_transaction_id)}"


def _count_dr_by_narration(index: BankStatementIndex, source: str, narration_prefix: str) -> int:
    """A manual reversal is a staff member manually keying a new payment
    *back* to the original sender, typing the narration into the
    transfer's own remarks — so (unlike a system reversal, which is
    chased by ISO id) it should show up in a DR entry's REMARKS. Returns
    how many DR entries match — 0 (not yet reversed), 1 (reversed,
    normal), 2+ (possible double reversal — the customer may have been
    refunded more than once)."""
    if not narration_prefix:
        return 0
    needle = narration_prefix.upper()
    count = 0
    for entry in index.entries:
        if (entry.get("source") or "") != source:
            continue
        if (entry.get("entry_type") or "").strip().upper() != "DR":
            continue
        if needle in (entry.get("remarks") or "").upper():
            count += 1
    return count


def _reversal_already_confirmed(index, ref_id: str, source: str, payment_processor: Any, aggregator: Any) -> bool:
    """The general (non-narration) fallback dispatch — mirrors
    core.services.apply_bank_statement_to_reversal_file()'s
    coop/imeremit/cityremit/prabhu order exactly."""
    if is_already_reversed(index, ref_id, source):
        return True
    if is_zero_charge_network(payment_processor) and is_already_debited_nchl(index, ref_id, source):
        return True
    if is_khalti_aggregator(aggregator, payment_processor) and is_already_debited_khalti(index, ref_id, source):
        return True
    return False


def _already_succeeded(
    index,
    ref_id: str,
    debtor_bank,
    creditor_bank,
    uploaded_bank_keys: set[str],
    payment_processor: Any,
    aggregator: Any,
) -> bool:
    """Did this transaction actually complete successfully end-to-end,
    regardless of its FAILED/REVERSAL status? On-Us duplicate-DR / name
    match, NCHL/Khalti settlement anchor match, or (now also) an off-us
    cross-bank CR+DR match when both banks' statements are available."""
    if not debtor_bank:
        return False
    source = debtor_bank.key

    # "Already succeeded" is only actionable when the successful transaction
    # was not subsequently reversed. This is especially important for NCHL:
    # the original CR+DR settlement can remain visible even after a later
    # reversal, and that later reversal must take precedence.
    if _reversal_already_confirmed(
        index, ref_id, source, payment_processor, aggregator
    ):
        return False

    if creditor_bank and debtor_bank.key == creditor_bank.key:
        onus_entries = statement_entries_for_reference(index, ref_id, source)
        has_cr, has_dr = _entry_types(onus_entries)
        if (has_cr and has_dr) or has_duplicate_dr(index, ref_id, source) or is_onus_already_success(index, ref_id, source):
            return True

    if is_zero_charge_network(payment_processor):
        nchl_entries = statement_entries_for_reference(index, ref_id, source)
        has_cr, has_dr = _entry_types(nchl_entries)
        if (has_cr and has_dr) or is_already_debited_nchl(index, ref_id, source):
            return True
    if is_khalti_aggregator(aggregator, payment_processor):
        khalti_entries = statement_entries_for_reference(index, ref_id, source)
        has_cr, has_dr = _entry_types(khalti_entries)
        if (has_cr and has_dr) or is_already_debited_khalti(index, ref_id, source):
            return True

    if creditor_bank and debtor_bank.key != creditor_bank.key:
        if debtor_bank.key in uploaded_bank_keys and creditor_bank.key in uploaded_bank_keys:
            cr_entries = statement_entries_for_reference(index, ref_id, debtor_bank.key)
            dr_entries = statement_entries_for_reference(index, ref_id, creditor_bank.key)
            has_cr, _ = _entry_types(cr_entries)
            _, has_dr = _entry_types(dr_entries)
            if has_cr and has_dr:
                return True

    return False


def _check_refund(
    stat: ReversalStat,
    need_reversal: list,
    index,
    source: str,
    ref_id: str,
    narration_prefix: str,
    payment_processor: Any,
    aggregator: Any,
    source_label: str,
    debtor_bank,
    debtor_name: Any,
    creditor_name: Any,
    member_txn_id: str,
    amount: float,
) -> None:
    """Shared refund-confirmation check for both 'Failed but Credited' and
    'Manual Reversal' rows (per instruction, they're treated identically):
    0 matching DR narration hits -> try core's general fallback, then
    pending; exactly 1 -> reconciled; 2+ -> possible double reversal,
    flagged for review. Every outcome is recorded on the Need To Reversal
    sheet (not just pending/flagged ones) so it reads as a full audit
    trail of what happened to each reversal candidate."""
    count = _count_dr_by_narration(index, source, narration_prefix)

    if count == 1:
        stat.reconciled += 1
        need_reversal.append(
            NeedReversalRow(
                source=f"{source_label} - Reversed",
                status="Reconciled",
                debtor=str(debtor_name or ""),
                creditor=str(creditor_name or ""),
                member_txn_id=member_txn_id,
                ref_id=ref_id,
                amount=amount,
                detail=f"Confirmed refunded — one DR entry on {debtor_bank.display_name}'s statement matches narration prefix '{narration_prefix}'.",
            )
        )
        return

    if count >= 2:
        stat.flagged += 1
        need_reversal.append(
            NeedReversalRow(
                source=f"{source_label} - Possible Double Reversal",
                status="Flagged",
                debtor=str(debtor_name or ""),
                creditor=str(creditor_name or ""),
                member_txn_id=member_txn_id,
                ref_id=ref_id,
                amount=amount,
                detail=f"{count} DR entries on {debtor_bank.display_name}'s statement match narration prefix '{narration_prefix}' — the customer may have been refunded more than once.",
            )
        )
        return

    # count == 0: try the general (non-narration) fallback dispatch
    if _reversal_already_confirmed(index, ref_id, source, payment_processor, aggregator):
        stat.reconciled += 1
        need_reversal.append(
            NeedReversalRow(
                source=f"{source_label} - Reversed",
                status="Reconciled",
                debtor=str(debtor_name or ""),
                creditor=str(creditor_name or ""),
                member_txn_id=member_txn_id,
                ref_id=ref_id,
                amount=amount,
                detail=f"Confirmed refunded via {debtor_bank.display_name}'s general reversal/NCHL/Khalti settlement match (no narration-matching DR found, but the automated reversal path confirms it).",
            )
        )
        return

    stat.pending += 1
    need_reversal.append(
        NeedReversalRow(
            source=f"{source_label} Pending",
            status="Pending",
            debtor=str(debtor_name or ""),
            creditor=str(creditor_name or ""),
            member_txn_id=member_txn_id,
            ref_id=ref_id,
            amount=amount,
            detail=f"No DR entry found on {debtor_bank.display_name}'s statement confirming narration prefix '{narration_prefix}'. Reversal may not have gone out yet.",
        )
    )


def reconcile(
    transactions: list[dict],
    index: BankStatementIndex,
    uploaded_bank_keys: set[str],
) -> ReconcileResult:
    result = ReconcileResult()
    result.transaction_count = len(transactions)

    for bank in BANKS:
        if bank.key in uploaded_bank_keys:
            result.statements_uploaded.append(bank.key)
        else:
            result.statements_missing.append(bank.key)

    for row in transactions:
        status = str(row.get("Overall Status") or "").strip().upper()
        network = str(row.get("Payment Processor") or "").strip().upper()
        result.status_counts[status] = result.status_counts.get(status, 0) + 1
        result.network_counts[network] = result.network_counts.get(network, 0) + 1

        ref_id = _txn_ref(row)
        member_txn_id = str(row.get("Member Transaction Id") or "")
        amount = _to_float(row.get("Transaction Amount"))
        debtor_name = row.get("Debtor Bank")
        creditor_name = row.get("Creditor Bank")
        debtor_bank = identify_bank(debtor_name)
        creditor_bank = identify_bank(creditor_name)
        aggregator = row.get("Aggregator")
        source_message = row.get("Source Message")

        # --- TIMEOUT ----------------------------------------------------
        # Never actually resolved to SUCCESS/FAILED/REVERSAL by the switch,
        # so there's no CR/DR pattern to check it against — it's neither
        # reconciled nor flagged, just surfaced on its own sheet (mirrors
        # core.services.process_ibft_file()'s own raw "timeout" sheet) so
        # it isn't silently dropped after being counted in status_counts.
        if status == "TIMEOUT":
            result.timeout_rows.append(
                TimeoutRow(
                    network=network,
                    debtor=str(debtor_name or ""),
                    creditor=str(creditor_name or ""),
                    member_txn_id=member_txn_id,
                    ref_id=ref_id,
                    amount=amount,
                    source_message=str(source_message or ""),
                )
            )
            continue

        # --- FAILED ---------------------------------------------------
        if status == "FAILED":
            result.failed_stat.total += 1

            # On-Us/Off-Us + reason breakdown, computed for every FAILED
            # row regardless of whether a statement is available for it
            # (mirrors core.services.process_ibft_file()'s own failed
            # breakdown, which is likewise counted unconditionally).
            reason = normalize_failure_reason(source_message)
            if is_on_us(debtor_name, creditor_name):
                result.failed_onus_count += 1
                result.failed_onus_amount += amount
                result.failed_reason_breakdown_onus[reason] = (
                    result.failed_reason_breakdown_onus.get(reason, 0) + 1
                )
            else:
                result.failed_offus_count += 1
                result.failed_offus_amount += amount
                result.failed_reason_breakdown_offus[reason] = (
                    result.failed_reason_breakdown_offus.get(reason, 0) + 1
                )

            if not debtor_bank or debtor_bank.key not in uploaded_bank_keys:
                continue
            source = debtor_bank.key

            if _already_succeeded(index, ref_id, debtor_bank, creditor_bank, uploaded_bank_keys, network, aggregator):
                result.failed_stat.flagged += 1
                result.need_reversal.append(
                    NeedReversalRow(
                        source="Failed but Already Succeeded",
                        status="Flagged",
                        debtor=str(debtor_name or ""),
                        creditor=str(creditor_name or ""),
                        member_txn_id=member_txn_id,
                        ref_id=ref_id,
                        amount=amount,
                        detail="Status is FAILED, but the statement shows this transaction actually completed successfully. Needs review.",
                    )
                )
                continue

            if not is_failed_but_credited(index, ref_id, source):
                # No CR at all — a clean, ordinary failure. Nothing moved,
                # nothing to do: already reconciled. Not itemized on the
                # Need To Reversal sheet (there's nothing to review).
                result.failed_stat.reconciled += 1
                continue

            # Credited but FAILED — treated exactly like a manual reversal
            # candidate (the money needs to go back to the customer).
            _check_refund(
                result.failed_stat,
                result.need_reversal,
                index,
                source,
                ref_id,
                _narration_prefix(member_txn_id),
                network,
                aggregator,
                "Failed but Credited",
                debtor_bank,
                debtor_name,
                creditor_name,
                member_txn_id,
                amount,
            )
            continue

        # --- REVERSAL: manual vs system, exactly as core classifies it -
        if status == "REVERSAL":
            if not debtor_bank or debtor_bank.key not in uploaded_bank_keys:
                if is_manual_reversal(source_message):
                    result.manual_reversal_stat.total += 1
                else:
                    result.system_reversal_stat.total += 1
                continue
            source = debtor_bank.key

            if is_manual_reversal(source_message):
                result.manual_reversal_stat.total += 1

                # A successful transaction must only be treated as "already
                # succeeded" when there is no evidence that the subsequent
                # manual/NCHL/Khalti reversal actually happened. In particular,
                # a later NCHL reversal must win over the success evidence and
                # be reported as Reconciled (Reversed).
                manual_reversal_count = _count_dr_by_narration(
                    index, source, _narration_prefix(member_txn_id)
                )
                reversal_confirmed = (
                    manual_reversal_count > 0
                    or _reversal_already_confirmed(
                        index, ref_id, source, network, aggregator
                    )
                )

                if not reversal_confirmed and _already_succeeded(
                    index,
                    ref_id,
                    debtor_bank,
                    creditor_bank,
                    uploaded_bank_keys,
                    network,
                    aggregator,
                ):
                    result.manual_reversal_stat.flagged += 1
                    result.need_reversal.append(
                        NeedReversalRow(
                            source="Manual Reversal - Not Needed (Already Succeeded)",
                            status="Flagged",
                            debtor=str(debtor_name or ""),
                            creditor=str(creditor_name or ""),
                            member_txn_id=member_txn_id,
                            ref_id=ref_id,
                            amount=amount,
                            detail="The statement shows this transaction actually completed successfully and no subsequent reversal was found — the reversal request may not have been necessary. Needs review.",
                        )
                    )
                    continue

                _check_refund(
                    result.manual_reversal_stat,
                    result.need_reversal,
                    index,
                    source,
                    ref_id,
                    _narration_prefix(member_txn_id),
                    network,
                    aggregator,
                    "Manual Reversal",
                    debtor_bank,
                    debtor_name,
                    creditor_name,
                    member_txn_id,
                    amount,
                )
            else:
                result.system_reversal_stat.total += 1
                already = _already_succeeded(index, ref_id, debtor_bank, creditor_bank, uploaded_bank_keys, network, aggregator)
                if already:
                    result.system_reversal_stat.flagged += 1
                    result.need_reversal.append(
                        NeedReversalRow(
                            source="System Reversal - Needs Review (Already Succeeded)",
                            status="Flagged",
                            debtor=str(debtor_name or ""),
                            creditor=str(creditor_name or ""),
                            member_txn_id=member_txn_id,
                            ref_id=ref_id,
                            amount=amount,
                            detail="A system reversal was issued, but the statement shows this transaction had already completed successfully beforehand.",
                        )
                    )
                else:
                    result.system_reversal_stat.reconciled += 1
                    result.need_reversal.append(
                        NeedReversalRow(
                            source="System Reversal - Normal",
                            status="Reconciled",
                            debtor=str(debtor_name or ""),
                            creditor=str(creditor_name or ""),
                            member_txn_id=member_txn_id,
                            ref_id=ref_id,
                            amount=amount,
                            detail="System reversal issued, no evidence the transaction had already succeeded — normal, no conflict found.",
                        )
                    )
            continue

        if status != "SUCCESS":
            continue

        # Amount-range bucket, split On-Us/Off-Us — computed for every
        # SUCCESS row up front (SCT, NCHL, and Khalti alike), independent
        # of the network-specific reconciliation checks below, so a
        # transaction still counts toward the monthly bucket report even
        # if its network-specific statement match is later flagged.
        if is_on_us(debtor_name, creditor_name):
            _add_to_bucket(result.success_buckets_onus, amount)
        else:
            _add_to_bucket(result.success_buckets_offus, amount)

        # --- SUCCESS rows -----------------------------------------------
        if network == SCT:
            if not debtor_bank or not creditor_bank:
                continue

            member_txn_id_raw = str(row.get("Member Transaction Id") or "")
            if _SETTLEMENT_ARTIFACT_RE.match(member_txn_id_raw):
                # Internal settlement bookkeeping row for an
                # already-reconciled NCHL/Khalti transaction — not a real
                # independent transfer, see _SETTLEMENT_ARTIFACT_RE above.
                continue

            pair = _get_pair(result, debtor_bank, creditor_bank)
            pair.total += 1

            debtor_available = debtor_bank.key in uploaded_bank_keys
            creditor_available = creditor_bank.key in uploaded_bank_keys

            if debtor_bank.key == creditor_bank.key:
                if not debtor_available:
                    pair.no_statement += 1
                    result.sct_no_statement.append(
                        NoStatementRow(
                            network="SCT (On-Us)",
                            debtor=str(debtor_name or ""),
                            creditor=str(creditor_name or ""),
                            missing_bank=debtor_bank.display_name,
                            member_txn_id=member_txn_id,
                            ref_id=ref_id,
                            amount=amount,
                        )
                    )
                    continue
                entries = statement_entries_for_reference(index, ref_id, debtor_bank.key)
                if not entries:
                    pair.reconciled += 1
                    continue
                has_cr, has_dr = _entry_types(entries)
                if has_cr and has_dr:
                    pair.reconciled += 1
                else:
                    pair.flagged += 1
                    reason = (
                        "Only a CR entry found (no confirming payout DR yet — may still be a pending settlement leg)"
                        if has_cr and not has_dr
                        else "Only a DR entry found (no matching CR)"
                        if has_dr and not has_cr
                        else "CR/DR entries found but couldn't be confirmed as the same completed transfer"
                    )
                    result.sct_flagged.append(
                        FlaggedRow(
                            network="SCT (On-Us)",
                            debtor=str(debtor_name or ""),
                            creditor=str(creditor_name or ""),
                            member_txn_id=member_txn_id,
                            ref_id=ref_id,
                            amount=amount,
                            reason=reason,
                        )
                    )
                continue

            missing_banks = []
            if not debtor_available:
                missing_banks.append(debtor_bank.display_name)
            if not creditor_available:
                missing_banks.append(creditor_bank.display_name)
            if missing_banks:
                pair.no_statement += 1
                result.sct_no_statement.append(
                    NoStatementRow(
                        network="SCT",
                        debtor=str(debtor_name or ""),
                        creditor=str(creditor_name or ""),
                        missing_bank=" & ".join(missing_banks),
                        member_txn_id=member_txn_id,
                        ref_id=ref_id,
                        amount=amount,
                    )
                )
                continue

            cr_entries = statement_entries_for_reference(index, ref_id, debtor_bank.key)
            dr_entries = statement_entries_for_reference(index, ref_id, creditor_bank.key)
            has_cr, _ = _entry_types(cr_entries)
            _, has_dr = _entry_types(dr_entries)

            if has_cr and has_dr:
                pair.reconciled += 1
            else:
                pair.flagged += 1
                if has_cr and not has_dr:
                    reason = (
                        f"CR found on {debtor_bank.display_name} but no matching DR yet on "
                        f"{creditor_bank.display_name} (may still be a pending settlement leg)."
                    )
                elif has_dr and not has_cr:
                    reason = (
                        f"DR found on {creditor_bank.display_name} but no matching CR yet on "
                        f"{debtor_bank.display_name} (may still be a pending settlement leg)."
                    )
                else:
                    reason = f"No CR on {debtor_bank.display_name} and no DR on {creditor_bank.display_name} at all."
                result.sct_flagged.append(
                    FlaggedRow(
                        network="SCT",
                        debtor=str(debtor_name or ""),
                        creditor=str(creditor_name or ""),
                        member_txn_id=member_txn_id,
                        ref_id=ref_id,
                        amount=amount,
                        reason=reason,
                    )
                )

        elif network in (NCHL, KHALTI):
            label = "NCHL" if network == NCHL else "KHALTI"
            stat = result.nchl_stat if network == NCHL else result.khalti_stat
            flagged_list = result.nchl_flagged if network == NCHL else result.khalti_flagged
            stat.total += 1

            if not debtor_bank or debtor_bank.key not in uploaded_bank_keys:
                stat.flagged += 1
                flagged_list.append(
                    FlaggedRow(
                        network=label,
                        debtor=str(debtor_name or ""),
                        creditor=str(creditor_name or ""),
                        member_txn_id=member_txn_id,
                        ref_id=ref_id,
                        amount=amount,
                        reason=f"No statement available for the issuing bank ({debtor_name}) to verify against.",
                    )
                )
                continue

            source = debtor_bank.key
            entries = statement_entries_for_reference(index, ref_id, source)
            has_cr, has_dr = _entry_types(entries)

            # Primary signal, same as everything else: CR + DR present for
            # this exact reference id, that's it. NCHL/Khalti settlement
            # doesn't always use an anonymous name+account-anchored DR —
            # plenty of it is tagged with the same reference id directly
            # (e.g. a "CIPS/SCT-<ref id>" settlement line) — so check for
            # that first and only fall back to the anchor-matching anchor
            # functions if it isn't directly tagged this way.
            settled = (has_cr and has_dr) or (
                is_already_debited_nchl(index, ref_id, source)
                if network == NCHL
                else is_already_debited_khalti(index, ref_id, source)
            )

            if settled:
                # Two distinct, both-legitimate outcomes once a
                # transaction has settled (CR+DR present):
                #   - "Already Success": it settled and stayed settled —
                #     nothing further happened. Reconciled.
                #   - "Already Reverse": NCHL (or our own system reversal)
                #     later sent the money back out — a DR elsewhere in
                #     the statement chases the original CR's own trailing
                #     ISO id (core.services.is_already_reversed(), the
                #     same mechanism used to confirm a manual reversal).
                #     This is also a complete, accounted-for outcome —
                #     not a problem to flag — just a different one, so
                #     it's labeled distinctly rather than lumped in with
                #     a plain clean success.
                if is_already_reversed(index, ref_id, source):
                    stat.reconciled += 1
                    result.need_reversal.append(
                        NeedReversalRow(
                            source=f"{label} - Settled then Reversed",
                            status="Reconciled",
                            debtor=str(debtor_name or ""),
                            creditor=str(creditor_name or ""),
                            member_txn_id=member_txn_id,
                            ref_id=ref_id,
                            amount=amount,
                            detail=f"Settled successfully on {debtor_bank.display_name}'s statement (CR+DR confirmed), then a later entry shows the amount was reversed back out — already reverse, accounted for.",
                        )
                    )
                else:
                    stat.reconciled += 1
            else:
                stat.flagged += 1
                reason = (
                    f"CR (collection) found on {debtor_bank.display_name} but the settlement DR hasn't "
                    "posted / matched yet — common for T+1 batch settlement, re-check against the next day's statement."
                    if has_cr
                    else f"No CR found on {debtor_bank.display_name} for this reference id."
                )
                flagged_list.append(
                    FlaggedRow(
                        network=label,
                        debtor=str(debtor_name or ""),
                        creditor=str(creditor_name or ""),
                        member_txn_id=member_txn_id,
                        ref_id=ref_id,
                        amount=amount,
                        reason=reason,
                    )
                )

    return result
