"""
QIF parser tests against synthetic QIF fixture strings.

No real bank files or disk databases are needed. Fixtures cover the common
date formats and account types so the full parse → reconcile pipeline can be
exercised in isolation from any I/O.
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from parsers.qif import parse, reconcile, extract_account_type, extract_date_range
from persistence.database import init_schema
from persistence.repositories import (
    AccountRepository, CategoryRepository, VendorRuleRepository,
    TransactionRepository, ReviewQueueRepository, ExchangeRateRepository,
)
from services.ingestion import ingest_transactions


# ── Fixtures ──────────────────────────────────────────────────────────────────

# Standard US bank checking export — MM/DD/YYYY dates
QIF_BANK = """\
!Type:Bank
D01/05/2026
T3500.00
PACME PAYROLL
MDIRECT DEPOSIT
N
^
D01/10/2026
T-120.00
PGROCERY STORE
^
D01/15/2026
T-45.50
PGAS STATION
MFUEL
N1001
^
"""

# Credit card export — MM/DD/YY (legacy 2-digit year)
QIF_CCARD_LEGACY_YEAR = """\
!Type:CCard
D01/20/26
T-89.99
PAMAZON
MONLINE PURCHASE
^
D01/25/26
T-15.00
PNETFLIX
^
"""

# UK bank export — DD/MM/YYYY date format (hyphen separator)
QIF_BANK_UK = """\
!Type:Bank
D05-01-2026
T1200.00
PSALARY
^
D10-01-2026
T-250.00
PRENT
^
"""

# File with !Account metadata section before transactions
QIF_WITH_ACCOUNT_BLOCK = """\
!Option:AutoSwitch
!Account
NMy Checking
TBank
^
!Type:Bank
D02/01/2026
T500.00
PDEPOSIT
^
D02/15/2026
T-75.00
PUTILITIES
^
"""

# Apostrophe date format (Quicken legacy MM/DD'YY)
QIF_APOSTROPHE_DATE = """\
!Type:Bank
D03/10'26
T-50.00
PCOFFEE SHOP
^
"""


# ── Shared DB fixture ─────────────────────────────────────────────────────────

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    yield c
    c.close()


# ── parse() tests ─────────────────────────────────────────────────────────────

def test_parse_basic_bank():
    txns = parse(QIF_BANK)
    assert len(txns) == 3


def test_parse_date_mdy4():
    txns = parse(QIF_BANK)
    assert txns[0].date == "2026-01-05"
    assert txns[1].date == "2026-01-10"
    assert txns[2].date == "2026-01-15"


def test_parse_amounts():
    txns = parse(QIF_BANK)
    assert txns[0].transaction_amount == pytest.approx(3500.00)
    assert txns[1].transaction_amount == pytest.approx(-120.00)
    assert txns[2].transaction_amount == pytest.approx(-45.50)


def test_parse_description_payee_only():
    txns = parse(QIF_BANK)
    assert txns[1].description == "GROCERY STORE"


def test_parse_description_payee_and_memo():
    txns = parse(QIF_BANK)
    # When payee and memo differ both should appear
    assert txns[0].description == "ACME PAYROLL DIRECT DEPOSIT"
    assert txns[2].description == "GAS STATION FUEL"


def test_parse_description_memo_same_as_payee():
    # When memo == payee, description should not be duplicated
    qif = "!Type:Bank\nD01/01/2026\nT-10.00\nPSTARBUCKS\nMSTARBUCKS\n^\n"
    txns = parse(qif)
    assert txns[0].description == "STARBUCKS"


def test_parse_reference_in_raw_source():
    txns = parse(QIF_BANK)
    assert txns[2].raw_source_lines == ["1001"]


def test_parse_no_reference():
    txns = parse(QIF_BANK)
    assert txns[1].raw_source_lines == []


def test_parse_currency_kwarg():
    txns = parse(QIF_BANK, currency="GBP")
    for t in txns:
        assert t.transaction_currency == "GBP"
        assert t.settlement_currency == "GBP"


def test_parse_fx_rate_and_balance_always_none():
    txns = parse(QIF_BANK)
    for t in txns:
        assert t.fx_rate is None
        assert t.balance_after is None


def test_parse_both_currencies_match():
    txns = parse(QIF_BANK)
    for t in txns:
        assert t.transaction_currency == t.settlement_currency
        assert t.transaction_amount == t.settlement_amount


def test_parse_legacy_2digit_year():
    txns = parse(QIF_CCARD_LEGACY_YEAR)
    assert txns[0].date == "2026-01-20"
    assert txns[1].date == "2026-01-25"


def test_parse_apostrophe_date():
    txns = parse(QIF_APOSTROPHE_DATE)
    assert txns[0].date == "2026-03-10"


def test_parse_uk_hyphen_date():
    txns = parse(QIF_BANK_UK)
    assert txns[0].date == "2026-01-05"
    assert txns[1].date == "2026-01-10"


def test_parse_account_block_skipped():
    txns = parse(QIF_WITH_ACCOUNT_BLOCK)
    assert len(txns) == 2
    assert txns[0].description == "DEPOSIT"
    assert txns[1].description == "UTILITIES"


def test_parse_empty_file():
    assert parse("") == []


def test_parse_no_caret_at_end():
    # File ending without a trailing ^ should still yield the last record
    qif = "!Type:Bank\nD01/01/2026\nT100.00\nPSOMETHING\n"
    txns = parse(qif)
    assert len(txns) == 1
    assert txns[0].transaction_amount == pytest.approx(100.00)


# ── reconcile() tests ─────────────────────────────────────────────────────────

def test_reconcile_always_clean():
    txns = parse(QIF_BANK)
    result = reconcile(txns)
    assert result.is_clean is True


def test_reconcile_no_stated_balance():
    result = reconcile([])
    assert result.stated_balance is None
    assert result.computed_balance is None
    assert result.diff is None


# ── extract_account_type() tests ──────────────────────────────────────────────

def test_extract_account_type_bank():
    assert extract_account_type(QIF_BANK) == "Bank"


def test_extract_account_type_ccard():
    assert extract_account_type(QIF_CCARD_LEGACY_YEAR) == "CCard"


def test_extract_account_type_skips_account_block():
    # !Account section should not be returned as the account type
    assert extract_account_type(QIF_WITH_ACCOUNT_BLOCK) == "Bank"


def test_extract_account_type_unknown():
    assert extract_account_type("") is None


# ── extract_date_range() tests ────────────────────────────────────────────────

def test_extract_date_range_basic():
    earliest, latest = extract_date_range(QIF_BANK)
    assert earliest == "2026-01-05"
    assert latest == "2026-01-15"


def test_extract_date_range_empty():
    earliest, latest = extract_date_range("")
    assert earliest is None
    assert latest is None


def test_extract_date_range_single_txn():
    qif = "!Type:Bank\nD06/30/2026\nT-10.00\nPSOMETHING\n^\n"
    earliest, latest = extract_date_range(qif)
    assert earliest == latest == "2026-06-30"


# ── Integration: parse → ingest_transactions ──────────────────────────────────

@pytest.fixture
def ingestion_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    init_schema(c)
    c.execute("INSERT INTO owners (name) VALUES ('Test User')")
    c.execute("""
        INSERT INTO accounts (account_code, display_name, account_type, currency, owner_id)
        VALUES ('TEST_CHECKING', 'Test Checking', 'checking', 'USD', 1)
    """)
    c.execute("INSERT INTO categories (name, money_type) VALUES ('Miscellaneous', 'expense')")
    c.commit()
    return c


def test_ingestion_integration(ingestion_conn):
    conn = ingestion_conn
    acct_id = conn.execute("SELECT id FROM accounts WHERE account_code='TEST_CHECKING'").fetchone()["id"]

    txns = parse(QIF_BANK)
    inserted_ids, skipped = ingest_transactions(
        txns, acct_id, "test.qif",
        TransactionRepository(conn), VendorRuleRepository(conn),
        CategoryRepository(conn), ReviewQueueRepository(conn), ExchangeRateRepository(conn),
        skip_statement_check=True,
    )
    assert len(inserted_ids) == 3
    assert skipped == 0


def test_ingestion_deduplication(ingestion_conn):
    conn = ingestion_conn
    acct_id = conn.execute("SELECT id FROM accounts WHERE account_code='TEST_CHECKING'").fetchone()["id"]

    txns = parse(QIF_BANK)
    ingest_transactions(
        txns, acct_id, "test.qif",
        TransactionRepository(conn), VendorRuleRepository(conn),
        CategoryRepository(conn), ReviewQueueRepository(conn), ExchangeRateRepository(conn),
        skip_statement_check=True,
    )
    # Re-importing same data should produce all skipped
    inserted_ids2, skipped2 = ingest_transactions(
        txns, acct_id, "test.qif",
        TransactionRepository(conn), VendorRuleRepository(conn),
        CategoryRepository(conn), ReviewQueueRepository(conn), ExchangeRateRepository(conn),
        skip_statement_check=True,
    )
    assert len(inserted_ids2) == 0
    assert skipped2 == 3
