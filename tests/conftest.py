import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "app"), str(ROOT / "tests")]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    import db
    db.init()
    return tmp_path


@pytest.fixture
def conn(data_dir):
    import db
    c = db.connect()
    yield c
    c.close()


@pytest.fixture
def fake_llm(monkeypatch):
    """Deterministic stand-in for Ollama: categorizes by keywords, like a well-behaved model."""
    import categorizer
    calls = []
    table = [("migros", "Grocery", 0.95), ("coop", "Grocery", 0.95), ("galaxus", "Online shopping", 0.95),
             ("tibits", "Eating Out", 0.92), ("hiltl", "Eating Out", 0.93), ("css", "Bills", 0.95),
             ("salt", "Bills", 0.9), ("axa", "Yearly spendings", 0.9), ("fressnapf", "Pets", 0.95),
             ("zahnarzt", "Health", 0.95), ("sbb", "Transport", 0.97), ("totalenergies", "Transport", 0.9),
             ("acme", "Salary", 0.9), ("immo", "Bills", 0.9), ("coiffeur", "Beauty", 0.92),
             ("escape", "Entertainment", 0.9), ("spotify", "Bills", 0.85), ("ryanair", "Travel", 0.95),
             ("cafe", "Eating Out", 0.9), ("amazon", "Online shopping", 0.6), ("zalando", "Refunds", 0.85),
             ("anna", "Transfer between my accounts", 0.4)]

    def ask(settings, cats, examples, items):
        calls.append(items)
        out = []
        for it in items:
            d = it["description"].lower()
            hit = next(((c, conf) for k, c, conf in table if k in d), ("Other shopping", 0.3))
            out.append({"id": it["id"], "merchant": it["description"].split()[0].title(),
                        "category": hit[0], "confidence": hit[1]})
        return out

    monkeypatch.setattr(categorizer, "_ask_llm", ask)
    return calls


@pytest.fixture
def fake_ecb(monkeypatch):
    import fx
    original = fx.fetch_ecb
    csv_text = "Date,USD,CHF,\n" + "".join("2026-09-%02d,1.10,0.94,\n" % d for d in range(1, 31))
    monkeypatch.setattr(fx, "fetch_ecb", lambda: csv_text)
    return original


# ---------------------------------------------------------------- statement reader: real Docling, simulated AI

@pytest.fixture(scope="session", autouse=True)
def docling_models():
    """Docling's models (downloaded once into .models/); tests then run Docling offline."""
    import os
    import pdf_import
    pdf_import.ensure_models()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")


class FakeParserAI:
    """Stands in for the local AI in pdf_import: answers each page with the JSON the real model should give.

    The 'right answer' comes from the former rule-based reader (tests/reference_rules.py), which reads
    the synthetic statements exactly. Docling and all checks still run for real.
    """

    def __init__(self):
        self.data = None
        self.calls = 0
        self.override = None  # fn(page, answer_dict) -> answer_dict, to simulate mistakes

    def answer(self, page):
        import reference_rules
        r = reference_rules.parse_pdf(self.data)
        rows = [t for t in r["transactions"] if t["page"] + 1 == page.number]
        last = max((t["page"] for t in r["transactions"]), default=0) + 1
        obj = {"statement": {
                   "bank": r["bank"], "currency": None,
                   "statement_type": "credit_card" if r["bank"] == "Swisscard" else "bank_account",
                   "opening_balance": r["opening"] if page.number == 1 else None,
                   "closing_balance": r["closing"] if page.number == last else None},
               "transactions": [{"date": t["date"], "description": t["description"],
                                 "money_in": t["amount"] if t["amount"] > 0 else None,
                                 "money_out": -t["amount"] if t["amount"] < 0 else None,
                                 "balance": t["balance"], "currency": t["currency"]} for t in rows]}
        return self.override(page, obj) if self.override else obj


@pytest.fixture(autouse=True)
def fake_parser(request, monkeypatch):
    if request.node.get_closest_marker("live"):
        yield None
        return
    import json
    import pdf_import
    fake = FakeParserAI()
    real_read = pdf_import.read_pages

    def read_pages(data):
        fake.data = data
        return real_read(data)

    def llm_page(settings, page, n_pages, last_date):
        fake.calls += 1
        return json.dumps(fake.answer(page))

    monkeypatch.setattr(pdf_import, "read_pages", read_pages)
    monkeypatch.setattr(pdf_import, "llm_page", llm_page)
    yield fake
