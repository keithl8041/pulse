"""
QIF (Quicken Interchange Format) statement parser.

QIF is a line-based text format exported by many UK and US banks.
Each record is separated by a caret (^) line. Records are preceded by a
!Type: header that names the account type (Bank, CCard, Cash, etc.).

Key differences from OFX:
- No machine-readable account number → last4 is never available.
- No opening or closing balance fields → reconcile() always returns clean
  with no stated balance (nothing to check).
- Dates appear in several formats depending on the exporting application
  (MM/DD/YYYY, MM/DD/YY, MM/DD'YY, DD/MM/YYYY, YYYY-MM-DD).
- Credit-card sign convention is the same as OFX: positive = credit,
  negative = debit (we do not flip signs).
"""
import re
from typing import List, Optional, Tuple

from domain.models import RawTransaction, ReconciliationResult


# ── Date parsing ──────────────────────────────────────────────────────────────

_DATE_FORMATS = [
    # MM/DD/YYYY  (most US banks)
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "mdy4"),
    # MM/DD'YY or MM/DD/YY  (legacy Quicken 2-digit year)
    (re.compile(r"^(\d{1,2})[/'](\d{1,2})[/'](\d{2})$"), "mdy2"),
    # DD/MM/YYYY  (UK / European banks)
    (re.compile(r"^(\d{1,2})-(\d{1,2})-(\d{4})$"), "dmy4"),
    (re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$"), "mdy4"),   # duplicate for ordering
    # YYYY-MM-DD  (ISO)
    (re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"), "iso"),
]


def _parse_date(raw: str) -> Optional[str]:
    """Return ISO YYYY-MM-DD from a QIF D-line value, or None on failure."""
    s = raw.strip()
    # Remove any trailing time component
    s = s.split(" ")[0]

    # MM/DD/YYYY
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        mm, dd, yyyy = m.group(1), m.group(2), m.group(3)
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"

    # MM/DD'YY or MM/DD/YY  (Quicken legacy)
    m = re.match(r"^(\d{1,2})[/'](\d{1,2})[/''](\d{2})$", s)
    if m:
        mm, dd, yy = m.group(1), m.group(2), int(m.group(3))
        yyyy = 2000 + yy if yy < 50 else 1900 + yy
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"

    # DD-MM-YYYY  (some UK exports use hyphens)
    m = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", s)
    if m:
        dd, mm, yyyy = m.group(1), m.group(2), m.group(3)
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"

    # YYYY-MM-DD  (ISO)
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

    return None


def _parse_amount(raw: str) -> float:
    """Parse a QIF amount string, stripping commas and currency symbols."""
    return float(raw.strip().replace(",", "").replace("$", "").replace("£", "").replace("€", ""))


# ── Parser ────────────────────────────────────────────────────────────────────

def parse(text: str, currency: str = "USD", **_context) -> List[RawTransaction]:
    """
    Parse a QIF file's text content into canonical RawTransactions.

    The currency kwarg sets both transaction_currency and settlement_currency
    (QIF carries a single amount in the account's own currency, like OFX).
    Pass currency="GBP" for UK bank exports.
    """
    lines = text.splitlines()
    results: List[RawTransaction] = []

    in_account_block = False  # inside a !Account metadata section
    in_transaction_block = False  # inside a !Type:XXX transaction section

    # Current record accumulator
    date: Optional[str] = None
    amount: Optional[float] = None
    payee: Optional[str] = None
    memo: Optional[str] = None
    ref: Optional[str] = None

    def flush():
        nonlocal date, amount, payee, memo, ref
        if date is None or amount is None:
            date = amount = payee = memo = ref = None
            return
        description = payee or memo or ""
        if payee and memo and memo != payee:
            description = f"{payee} {memo}"
        results.append(RawTransaction(
            date=date,
            description=description,
            transaction_currency=currency,
            transaction_amount=amount,
            settlement_currency=currency,
            settlement_amount=amount,
            fx_rate=None,
            balance_after=None,
            raw_source_lines=[ref] if ref else [],
        ))
        date = amount = payee = memo = ref = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Section control headers
        if stripped.startswith("!Type:"):
            flush()
            typ = stripped[6:].strip().lower()
            # "account" is a metadata section, not a transaction section
            in_account_block = (typ == "account")
            in_transaction_block = not in_account_block
            continue

        if stripped == "!Account":
            flush()
            in_account_block = True
            in_transaction_block = False
            continue

        if stripped.startswith("!"):
            # Other directives (Option:AutoSwitch, etc.) — skip
            continue

        if in_account_block:
            # Inside a !Account block, ^ ends the block — check for next !Type
            if stripped == "^":
                in_account_block = False
            continue

        if not in_transaction_block:
            continue

        if stripped == "^":
            flush()
            continue

        field, value = stripped[0], stripped[1:]

        if field == "D":
            date = _parse_date(value)
        elif field in ("T", "U"):
            try:
                amount = _parse_amount(value)
            except ValueError:
                pass
        elif field == "P":
            payee = value.strip()
        elif field == "M":
            memo = value.strip()
        elif field == "N":
            ref = value.strip()
        # Other fields (L=category, C=cleared, A=address, etc.) are ignored

    flush()  # catch final record if file doesn't end with ^
    return results


def reconcile(transactions: List[RawTransaction], **_context) -> ReconciliationResult:
    """
    QIF provides no balance figures, so reconciliation always returns clean
    with no stated balance — nothing to check, nothing to record.
    """
    return ReconciliationResult(
        computed_balance=None,
        stated_balance=None,
        diff=None,
    )


# ── Web-layer helpers ─────────────────────────────────────────────────────────

def extract_account_type(text: str) -> Optional[str]:
    """Return the raw !Type value from the first transaction block, e.g. 'Bank' or 'CCard'."""
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("!Type:"):
            typ = s[6:].strip()
            if typ.lower() != "account":
                return typ
    return None


def extract_date_range(text: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Return (earliest_date, latest_date) as ISO strings by scanning all D lines.
    Used by detect_qif() to build a human-readable period label.
    """
    dates = []
    for line in text.splitlines():
        s = line.strip()
        if s and s[0] == "D":
            parsed = _parse_date(s[1:])
            if parsed:
                dates.append(parsed)
    if not dates:
        return None, None
    return min(dates), max(dates)
