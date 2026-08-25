"""Constants shared by core's and reconcile's dashboard/report code.

Kept in their own module (rather than living in core/views.py, where they
used to be) purely so reconcile/dashboard.py can import them without
pulling in all of core/views.py — and so core/views.py can, in turn,
import reconcile/dashboard.py to render the two apps' numbers on one
combined dashboard page without a circular import between the two.
"""

MONTH_NAMES = [
    (1, "January"), (2, "February"), (3, "March"), (4, "April"),
    (5, "May"), (6, "June"), (7, "July"), (8, "August"),
    (9, "September"), (10, "October"), (11, "November"), (12, "December"),
]

REASON_ORDER = [
    "Timeout",
    "Insufficient fund",
    "Card issuer timeout",
    "Response timeout",
    "Transaction amount exceeded",
    "Other",
]
