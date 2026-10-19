import pytest

import categorizer
import db
import reports
from test_categorizer import by_desc, scan_all


@pytest.fixture
def scanned(conn, fake_llm, fake_ecb):
    scan_all(conn)
    # answer all questions with the suggestion, like "Accept all"
    for g in reports.review_groups(conn):
        if g["suggestion_id"]:
            categorizer.assign(conn, g["txn_ids"], g["suggestion_id"])
    return conn


def test_monthly_excludes_yearly_and_transfers(scanned):
    r = reports.monthly(scanned, "2026-09")
    names = [c["category"] for c in r["categories"]]
    assert "Yearly spendings" not in names and db.TRANSFER not in names
    assert r["yearly_paid"] == 1240.00
    assert [t["description"][:3] for t in r["yearly"]] == ["AXA"]
    shares = [c["share"] for c in r["categories"] if c["share"] is not None]
    assert sum(shares) == pytest.approx(100, abs=0.5)
    assert r["net"] == pytest.approx(r["income"] + r["unreviewed_income"] - r["monthly_costs"] - r["yearly_paid"], abs=0.01)


def test_twelve_month_average(scanned):
    r = reports.monthly(scanned, "2026-09")
    assert r["avg_months"] == 1  # August has data (Swisscard)
    eat = next(c for c in r["categories"] if c["category"] == "Eating Out")
    aug = reports.monthly(scanned, "2026-08")
    aug_eat = next(c for c in aug["categories"] if c["category"] == "Eating Out")
    assert eat["avg"] == aug_eat["amount"]


def test_spread_yearly_costs(scanned):
    r = reports.monthly(scanned, "2026-09", spread=True)
    assert r["spread"] == round(1240 / 12, 2)
    assert r["cost_of_living"] == pytest.approx(r["monthly_costs"] + r["spread"])


def test_yearly_report(scanned):
    y = reports.yearly(scanned, "2026")
    assert y["yearly_total"] == 1240.00 and y["set_aside"] == round(1240 / 12, 2)
    assert y["active_months"] == 2
    sep = y["per_month"]["2026-09"]
    m = reports.monthly(scanned, "2026-09")
    assert sep["monthly_costs"] == pytest.approx(m["monthly_costs"])


def test_account_filter(scanned):
    r = reports.monthly(scanned, "2026-09", account="Wise")
    assert all(t["account"] == "Wise" for c in r["categories"] for t in c["transactions"])


def test_search_by_text_and_amount(scanned):
    assert any("FRESSNAPF" in t["description"] for t in reports.search(scanned, "fressnapf"))
    assert any(t["amount"] == -43.90 for t in reports.search(scanned, "43.90"))


def test_export_has_all_rows(scanned):
    csv_text = reports.export_csv(scanned)
    n = scanned.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert len(csv_text.strip().splitlines()) == n + 1


def test_income_section(scanned):
    r = reports.monthly(scanned, "2026-09")
    lines = {l["category"]: l for l in r["income_lines"]}
    assert lines["Salary"]["amount"] == 7450.00
    assert any("ACME" in t["description"] for t in lines["Salary"]["transactions"])
    assert sum(l["amount"] for l in r["income_lines"]) == pytest.approx(r["income"] + r["unreviewed_income"])
    assert sum(l["share"] for l in r["income_lines"] if l["share"]) == pytest.approx(100, abs=0.5)
    # transfers are listed on their own and never counted as income
    assert r["transfers"] and all(t["category"] == db.TRANSFER for t in r["transfers"])
    assert not any(t["category"] == db.TRANSFER for l in r["income_lines"] for t in l["transactions"])


def test_yearly_income_table(scanned):
    y = reports.yearly(scanned, "2026")
    assert y["income_matrix"]["2026-09"]["Salary"] == 7450.00
    assert sum(y["income_totals"].values()) == pytest.approx(y["income"])


def test_merchant_names_consistent_between_lists(scanned):
    r = reports.monthly(scanned, "2026-09")
    top = {m["name"] for m in r["top_merchants"]}
    assert all(len(t["name"]) <= 60 for t in r["largest"])
    # the same merchant gets the same name everywhere
    names = {}
    for c in r["categories"]:
        for t in c["transactions"]:
            key = scanned.execute("SELECT merchant_key FROM transactions WHERE id=?", (t["id"],)).fetchone()[0]
            names.setdefault(key, set()).add(t["name"])
    assert all(len(v) == 1 for v in names.values())
    largest_names = {t["name"] for t in r["largest"]}
    assert largest_names & top  # big expenses are among the top merchants, under the same name


def test_pretty_name_for_rule_rows():
    assert reports._pretty("css versicherung praemie") == "CSS Versicherung Praemie"
    assert reports.display_name({"merchant_key": "migros zuerich", "description": "KAUF/DIENSTLEISTUNG VOM ..."}) == "Migros Zuerich"
