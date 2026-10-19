"""LIVE: the real statement reader (Docling + your local Ollama model) on all synthetic statements.

    uv run pytest -m live -s        # needs Ollama running with the parser model (default qwen3:8b)

Prints, per statement, how many rows were found correctly and whether the statement's own balances
confirm them. Three layouts have never been seen by anyone tuning this app.
"""
import time
from collections import Counter

import pytest

import db
import pdf_import
import statements
import unseen_layouts

FILES = {
    "PostFinance (DE)": statements.postfinance,
    "PostFinance (EN, real layout)": statements.postfinance_en_daily,
    "Swisscard": statements.swisscard,
    "Wise (2 currencies)": statements.wise,
    "UNSEEN neo-bank": unseen_layouts.neobank,
    "UNSEEN cantonal bank": unseen_layouts.cantonal,
    "UNSEEN credit card": unseen_layouts.creditcard,
}


@pytest.mark.live
def test_live_reader_on_all_synthetic_statements():
    settings = dict(db.DEFAULT_SETTINGS)
    results = []
    for name, fn in FILES.items():
        data, expected = fn()
        t0 = time.time()
        r = pdf_import.parse_pdf(data, settings)
        got = Counter((t["date"], t["amount"]) for t in r["transactions"])
        want = Counter((d, round(a, 2)) for d, a, *_ in expected)
        right = sum((got & want).values())
        results.append((name, right, sum(want.values()), sum(got.values()), r["balance_check"], time.time() - t0))
        print("%-32s %2d/%-2d correct  %2d found  balance %-5s %4.0f s" % results[-1], flush=True)
        for w in r["warnings"]:
            print("      warning:", w)
    perfect = sum(1 for _, right, want, got, check, _ in results if right == want == got and check == "ok")
    print("\n%d/%d statements read perfectly and confirmed by their own balances" % (perfect, len(results)))
    assert perfect >= len(results) - 2  # leaves room for a model's bad day; warnings above show what went wrong
