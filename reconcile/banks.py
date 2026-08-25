"""The 18 banks that make up our SCT network universe.

Two of them (Global IME Bank, Prabhu Bank) are *our own* issuer banks —
every transaction's Debtor Bank is (almost) always one of these two. The
other 16 are member banks money gets sent *to* (Creditor Bank) over the
SCT network directly. NCHL_NETWORK / KHALTI_NETWORK transactions land on
banks *outside* this list of 18 (Nabil, NIC Asia, Siddhartha, ...) — we
have no statement access to those at all, so those two networks are
verified purely against our own issuer bank's statement instead (see
services.py).

`key` is also the upload form field name (see forms.py:
`statement_<key>`), so keep it a simple identifier.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Bank:
    key: str
    display_name: str
    # Upper-cased substrings matched against the Debtor Bank / Creditor
    # Bank columns of the transaction report. First match wins, so keep
    # these specific enough not to collide with each other.
    keywords: tuple[str, ...]
    # Whether we currently receive a daily statement export for this bank
    # at all (see the prompt: 13 of 18 today). This is only the *default*
    # expectation shown on the upload form — what actually counts as
    # "missing" for a given run is whatever file wasn't actually uploaded.
    available_by_default: bool
    is_issuer: bool = False


BANKS: tuple[Bank, ...] = (
    Bank("global", "Global IME Bank", ("GLOBAL",), True, is_issuer=True),
    Bank("prabhu", "Prabhu Bank", ("PRABHU",), True, is_issuer=True),
    Bank("lumbini", "Lumbini Bikas Bank", ("LUMBINI",), True),
    Bank("kamana", "Kamana Sewa Bikas Bank", ("KAMANA",), True),
    Bank("laxmisunrise", "Laxmi Sunrise Bank", ("LAXMI SUNRISE",), True),
    Bank("jyoti", "Jyoti Bikas Bank", ("JYOTI",), True),
    Bank("everest", "Everest Bank", ("EVEREST",), True),
    Bank("adb", "Agricultural Development Bank", ("AGRICULTUR",), True),
    Bank("machapuchhre", "Machhapuchchhre Bank", ("MACHHAPUCHCHHRE", "MACHAPUCHHRE", "MACHHAPUCHHRE"), True),
    Bank("icfc", "ICFC Finance", ("ICFC",), True),
    Bank("rastriya", "Rastriya Banijya Bank", ("RASTRIYA",), True),
    Bank("nepalfinance", "Nepal Finance", ("NEPAL FINANCE",), True),
    Bank("sangrila", "Shangri-La Development Bank", ("SHANGRI",), True),
    Bank("garima", "Garima Bikas Bank", ("GARIMA",), True),
    Bank("green", "Green Development Bank", ("GREEN DEVELOPMENT",), False),
    Bank("manjushree", "Manjushree Finance", ("MANJUSHREE",), False),
    Bank("saptakoshi", "Saptakoshi Development Bank", ("SAPTAKOSHI",), False),
    Bank("sindhu", "Sindhu Bikas Bank", ("SINDHU",), False),
)

BANKS_BY_KEY: dict[str, Bank] = {b.key: b for b in BANKS}

ISSUER_KEYS = tuple(b.key for b in BANKS if b.is_issuer)


def identify_bank(name) -> Bank | None:
    """Match a raw 'Debtor Bank' / 'Creditor Bank' string (e.g.
    'GLOBAL IME BANK LIMITED') to one of our 18 known banks, or None if
    it's some other bank entirely (i.e. an NCHL/Khalti-only destination
    we don't hold a statement relationship with)."""
    if not name:
        return None
    upper = str(name).strip().upper()
    for bank in BANKS:
        if any(kw in upper for kw in bank.keywords):
            return bank
    return None
