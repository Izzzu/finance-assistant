import datetime

import pytest

from importer import detect_date_format, parse_amount
from textutil import last4, matches, merchant_key, normalize


@pytest.mark.parametrize("raw,want", [
    ("1'234.50", 1234.50), ("1’234.50", 1234.50), ("-12.00", -12.0), ("12.00-", -12.0), ("(12.00)", -12.0),
    ("1 234,56", 1234.56), ("1.234,56", 1234.56), ("1,234.56", 1234.56), ("CHF 99.90", 99.90), ("0.05", 0.05),
    ("", None), ("abc", None), ("2026-09-01", None),
])
def test_amounts(raw, want):
    assert parse_amount(raw) == want


def test_ambiguous_date_column_uses_whole_column():
    assert detect_date_format(["03/09/2026", "25/09/2026"]) == "%d/%m/%Y"
    assert detect_date_format(["09/03/2026", "09/25/2026"]) == "%m/%d/%Y"


def test_normalize_strips_boilerplate_and_numbers():
    assert normalize("KAUF/DIENSTLEISTUNG VOM 03.09.2026 KARTEN NR. XXXX1234 MIGROS M ZUERICH") == "migros zuerich"
    assert merchant_key(normalize("TWINT Zahlung an Anna Müller +41791234567")) == "anna muller"
    assert merchant_key(normalize("Sent money to Anna Muller")) == "anna muller"


def test_rules_match_whole_words_only():
    assert matches("coop", "coop city winterthur")
    assert not matches("coop", "cooperative bank")
    assert matches("ihre zahlung", "ihre zahlung danke")


def test_account_numbers_are_masked():
    assert last4("IBAN CH93 0076 2011 6238 5295 7") == "2957"
    assert last4("Kartennummer XXXX XXXX XXXX 4321") == "4321"


def test_dates_are_not_amounts():
    assert parse_amount("01.09.2026") is None and parse_amount("1.9.26") is None


POSTFINANCE_CSV_EN = """Date from:;="01.09.2026"
Date to:;="30.09.2026"
Entry type:;="All bookings"
Account:;="CH9309000000123456789"
Currency:;="CHF"

Date;Notification text;Credit in CHF;Debit in CHF;Value;Balance in CHF
01.09.2026;"DIRECT DEBIT IMMO VERWALTUNG AG RENT";;-2100.00;01.09.2026;2900.00
;"CREDIT SENDER: ACME AG ZURICH MESSAGE: SALARY SEPTEMBER";7450.00;;01.09.2026;10350.00
03.09.2026;"PURCHASE/SERVICE FROM 03.09.2026 CARD NO. XXXX1234 MIGROS M ZURICH";;-87.35;03.09.2026;10262.65
;"TWINT CREDIT FROM PETRA MUSTER";45.00;;06.09.2026;10307.65
;"TWINT CREDIT FROM ANA LIMA";20.00;;;10327.65

Disclaimer:
"This is not a document created by PostFinance Ltd."
"""


def test_postfinance_english_csv_with_empty_dates():
    from importer import analyze, build
    a = analyze(POSTFINANCE_CSV_EN.encode("utf-8"))
    m = a["mapping"]
    assert m["credit"] is not None and m["debit"] is not None and m["value_date"] is not None
    assert a["headers"][m["credit"]] == "Credit in CHF" and a["headers"][m["debit"]] == "Debit in CHF"
    assert m["amount"] is None and a["headers"][m["value_date"]] == "Value"
    rows, skipped = build(a["rows"], m, "CHF")
    got = [(t["date"], t["amount"]) for t in rows]
    assert got == [("2026-09-01", -2100.00), ("2026-09-01", 7450.00), ("2026-09-03", -87.35),
                   ("2026-09-06", 45.00), ("2026-09-06", 20.00)]
    assert "ACME" in rows[1]["description"]
