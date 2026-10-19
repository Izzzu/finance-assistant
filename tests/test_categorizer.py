import categorizer
import db
import fx
import pipeline
import statements


def cid(conn, name: str) -> int:
    """A category's id; the default categories always exist."""
    i = db.category_id(conn, name)
    assert i is not None, name
    return i


def scan_all(conn, settings=None):
    settings = settings or db.get_settings(conn)
    for name, fn in [("pf.pdf", statements.postfinance), ("sc.pdf", statements.swisscard), ("w.pdf", statements.wise)]:
        pipeline.ingest(conn, name, fn()[0], settings)
    fx.convert(conn, settings)
    return categorizer.run(conn, settings)


def by_desc(conn, frag):
    return conn.execute("SELECT t.*, c.name AS cat, s.name AS sug FROM transactions t "
                        "LEFT JOIN categories c ON c.id=t.category_id LEFT JOIN categories s ON s.id=t.suggestion_id "
                        "WHERE t.description LIKE ?", ("%" + frag + "%",)).fetchall()


def test_pipeline_rules_ai_and_review(conn, fake_llm, fake_ecb):
    summary = scan_all(conn)
    assert summary["llm_error"] is None
    # default rules: own transfers
    for frag in ("SWISSCARD AECS", "Ihre Zahlung", "WISE EUROPE", "Converted"):
        assert all(r["cat"] == db.TRANSFER and r["status"] == "rule" for r in by_desc(conn, frag)), frag
    # confident AI answers are applied
    assert by_desc(conn, "MIGROS")[0]["cat"] == "Grocery"
    assert by_desc(conn, "AXA")[0]["cat"] == "Yearly spendings"
    # unsure AI answers become questions with the suggestion kept
    amazon = by_desc(conn, "AMAZON")[0]
    assert amazon["status"] == "review" and amazon["sug"] == "Online shopping"
    # person-to-person payments are always asked, even if the AI were confident
    assert by_desc(conn, "ANNA")[0]["status"] == "review"


def test_same_merchant_asked_once_per_pass(conn, fake_llm, fake_ecb):
    scan_all(conn)  # double-check is on by default: every batch is asked twice
    descs = [it["description"] for batch in fake_llm for it in batch]
    assert len(descs) == 2 * len(set(descs))


def test_double_check_disagreement_goes_to_review(conn, fake_ecb, monkeypatch):
    calls = []

    def flaky(settings, cats, examples, items):
        calls.append(1)
        cat = "Grocery" if len(calls) % 2 else "Other shopping"  # answer flips between passes
        return [{"id": it["id"], "merchant": "X", "category": cat, "confidence": 0.95} for it in items]
    monkeypatch.setattr(categorizer, "_ask_llm", flaky)
    conn.execute("DELETE FROM rules")
    scan_all(conn)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE status='llm'").fetchone()[0] == 0


def test_ollama_offline_everything_goes_to_review(conn, fake_ecb, monkeypatch):
    import llm

    def down(*a, **k):
        raise llm.LLMError("Local LLM not reachable at http://127.0.0.1:11434", "unreachable")
    monkeypatch.setattr(categorizer, "_ask_llm", down)
    summary = scan_all(conn)
    assert summary["llm_error"] == "The local AI isn't running. Please start Ollama and try again."
    left = conn.execute("SELECT COUNT(*) FROM transactions WHERE status IN ('new','llm')").fetchone()[0]
    assert left == 0


def test_malformed_llm_answer_is_safe(conn, fake_ecb, monkeypatch):
    monkeypatch.setattr(categorizer, "_ask_llm", lambda *a: [{"id": 0, "category": "Made up", "confidence": "x"}, "junk"])
    scan_all(conn)
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE status='llm'").fetchone()[0] == 0


def test_transfer_pairing_exact_amounts_only(conn, fake_ecb, monkeypatch):
    monkeypatch.setattr(categorizer, "_ask_llm", lambda *a: [])
    # remove default rules so pairing has to find the Swisscard/PostFinance and Wise top-up pairs itself
    conn.execute("DELETE FROM rules")
    scan_all(conn)
    tid = cid(conn, db.TRANSFER)
    paired = {r["description"][:20] for r in conn.execute(
        "SELECT description FROM transactions WHERE suggestion_id=? AND status='review'", (tid,))}
    assert any("SWISSCARD" in d for d in paired) and any("Ihre Zahlung" in d for d in paired)
    assert any("WISE" in d for d in paired) and any("Topped up" in d for d in paired)
    # -60.00 TWINT and +59.90 Zalando refund must NOT be paired
    assert not any("TWINT" in d or "ZALANDO" in d for d in paired)


def test_remember_rule_applies_but_never_overwrites_user(conn, fake_llm, fake_ecb):
    scan_all(conn)
    sport = cid(conn, "Sport")
    groc = cid(conn, "Grocery")
    coop = by_desc(conn, "COOP")[0]
    categorizer.assign(conn, [coop["id"]], groc)  # user sets it by hand
    migros = by_desc(conn, "MIGROS")[0]
    # a broad rule must not touch the manual choice on Coop
    tok = categorizer.assign(conn, [migros["id"]], sport, remember=True, pattern="kauf dienstleistung zuerich")
    assert by_desc(conn, "COOP")[0]["category_id"] == groc
    categorizer.undo(conn, tok)
    assert by_desc(conn, "MIGROS")[0]["category_id"] == migros["category_id"]
    assert not conn.execute("SELECT 1 FROM rules WHERE pattern='kauf dienstleistung zuerich'").fetchone()


def test_remember_updates_similar_and_future_scans(conn, fake_llm, fake_ecb):
    scan_all(conn)
    sport = cid(conn, "Sport")
    anna = by_desc(conn, "ANNA")[0]
    pattern = categorizer.suggest_pattern(conn, anna["id"])
    assert pattern  # e.g. "anna" / "anna mueller"
    categorizer.assign(conn, [anna["id"]], sport, remember=True, pattern="anna mueller")
    # a new statement with another payment to Anna is categorized by the rule, without asking
    conn.execute("INSERT INTO transactions(account, date, description, amount, currency, amount_chf, hash) "
                 "VALUES('PF','2026-10-02','TWINT ZAHLUNG AN ANNA MUELLER',-60,'CHF',-60,'x1')")
    categorizer.run(conn, db.get_settings(conn))
    r = by_desc(conn, "ANNA")
    assert all(x["category_id"] == sport for x in r)
    assert r[-1]["status"] == "rule"


def test_reimport_adds_nothing(conn, fake_llm, fake_ecb):
    s = db.get_settings(conn)
    first = pipeline.ingest(conn, "pf.pdf", statements.postfinance()[0], s)
    again = pipeline.ingest(conn, "pf.pdf", statements.postfinance()[0], s)
    assert first["added"] == 14 and again["added"] == 0 and again["duplicates"] == 14


def test_identical_rows_same_day_are_both_kept(conn):
    rows = [("03.09.26", ["KAUF COFFEE BAR"], -4.50), ("03.09.26", ["KAUF COFFEE BAR"], -4.50)]
    r = pipeline.ingest(conn, "x.pdf", statements.postfinance(rows=rows)[0], db.get_settings(conn))
    assert r["added"] == 2


def test_fx_conversion_sources(conn, fake_ecb):
    s = db.get_settings(conn)
    pipeline.ingest(conn, "w.pdf", statements.wise()[0], s)
    fx.convert(conn, s)
    hiltl = by_desc(conn, "Hiltl")[0]
    assert hiltl["fx_source"] == "same" and hiltl["amount_chf"] == -31.50
    ryan = by_desc(conn, "Ryanair")[0]
    assert ryan["fx_source"] == "ecb" and ryan["amount_chf"] == round(-89.00 * 0.94, 2)


def test_fx_weekend_uses_previous_business_day():
    import datetime
    rates = {datetime.date(2026, 9, 18): {"EUR": 1.0, "CHF": 0.94, "USD": 1.1}}  # Friday
    assert fx.ecb_rate(rates, "EUR", "CHF", datetime.date(2026, 9, 20)) == 0.94  # Sunday
    usd = fx.ecb_rate(rates, "USD", "CHF", datetime.date(2026, 9, 19))
    assert usd is not None and round(usd, 4) == round(0.94 / 1.1, 4)


def test_stated_amount_beats_rates():
    assert fx.stated_amount("Card transaction of 25.00 CHF issued by X", "CHF") == 25.0
    assert fx.stated_amount("Card transaction of 25.00 EUR issued by X", "CHF") is None


def test_migration_income_split_keeps_existing_data(tmp_path, monkeypatch):
    """A database created before Salary/Bonus/Other income existed is upgraded in place."""
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    db.init()
    with db.db() as c:  # simulate the old layout: one 'Income' category with a transaction and a rule
        c.execute("DELETE FROM categories WHERE name IN ('Bonus', 'Other income')")
        c.execute("UPDATE categories SET name='Income' WHERE name='Salary'")
        c.execute("DELETE FROM settings WHERE key='migration:income-split'")
        iid = db.category_id(c, "Income")
        c.execute("INSERT INTO transactions(account, date, description, amount, currency, category_id, status, hash) "
                  "VALUES('PF','2026-09-01','LOHN ACME',7450,'CHF',?,'user','h')", (iid,))
        c.execute("INSERT INTO rules(pattern, category_id) VALUES('acme', ?)", (iid,))
    db.init()  # app start
    with db.db() as c:
        names = [x["name"] for x in db.categories(c) if x["kind"] == "income"]
        assert {"Salary", "Bonus", "Other income", "Refunds"} <= set(names) and "Income" not in names
        sid = db.category_id(c, "Salary")
        assert c.execute("SELECT category_id FROM transactions WHERE hash='h'").fetchone()[0] == sid
        assert c.execute("SELECT category_id FROM rules WHERE pattern='acme'").fetchone()[0] == sid
    db.init()  # running again changes nothing
    with db.db() as c:
        assert c.execute("SELECT COUNT(*) FROM categories WHERE kind='income'").fetchone()[0] == 4


def test_twint_senders_get_separate_keys_and_generic_rules_are_refused(conn, fake_llm, fake_ecb):
    s = db.get_settings(conn)
    pipeline.ingest(conn, "pf-en.pdf", statements.postfinance_en_daily()[0], s)
    fx.convert(conn, s)
    categorizer.run(conn, s)
    keys = {r["merchant_key"] for r in conn.execute("SELECT merchant_key FROM transactions WHERE description LIKE 'TWINT%'")}
    assert keys == {"muster anna", "beispiel marc"}
    apple = by_desc(conn, "APPLE PAY")[0]
    assert apple["merchant_key"].startswith("mc musterladen")
    tw = by_desc(conn, "MUSTER")[0]
    tok = categorizer.assign(conn, [tw["id"]], cid(conn, "Sport"), remember=True, pattern="mobile no")
    assert tok["not_remembered"] and not conn.execute("SELECT 1 FROM rules WHERE pattern='mobile no'").fetchone()
    assert by_desc(conn, "BEISPIEL")[0]["category_id"] != cid(conn, "Sport")  # other sender untouched
