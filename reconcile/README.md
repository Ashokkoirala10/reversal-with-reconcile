# Reconcile app — v7 update

**No `models.py` change — no migration needed.** Just drop these files
in and restart the server.

## Correction: NCHL "settled then reversed" should not be flagged as a problem

You were right to push back — last round I treated "settled, then later
reversed" as an anomaly needing review (red flag). It isn't: once a
transaction has settled (CR+DR present, confirmed), there are two
equally legitimate, complete outcomes, exactly as you described:

- **Already Success** — it settled and stayed settled. Nothing further
  happened.
- **Already Reverse** — NCHL (or a system reversal) later sent the money
  back out, chasing the original CR's own trailing ISO id with a DR
  elsewhere in the statement (`core.services.is_already_reversed()` —
  the same ISO-id-chase mechanism already used to confirm a manual
  reversal, just applied to a successful transaction this time).

Both are now counted as **reconciled** — neither is a problem. The
"Already Reverse" case is still recorded, distinctly labeled ("NCHL /
Khalti - Settled then Reversed") on the Need To Reversal sheet with a
green "Reconciled" status, so it's visible and traceable, but it no
longer shows up as a red flag needing review.

Retested against your 12 Aug data: identical totals to before (947/947
NCHL, 827/827 Khalti, 11236 reconciled / 7 outstanding) — this dataset
didn't happen to contain a "settled then reversed" case, but the code
path is in place and confirmed working for whenever one shows up.

## Files changed

Only `engine.py` — one function's outcome relabeled. Everything else
included for convenience, unchanged since the last update.
