"""Accuracy evaluation of the REAL local model (not part of the automated test run).

    .venv/bin/python tests/eval_llm.py            # uses the model from Settings (default qwen3:8b)
    .venv/bin/python tests/eval_llm.py llama3.1:8b-instruct-q8_0
    .venv/bin/python tests/eval_llm.py --ai-only --no-double-check   # the raw model, no starter rules

Measures what matters for this app:
  precision of auto-assigned  - of the items the AI did NOT ask about, how many are right (target >= 95%)
  review rate                 - how often you would be asked
All descriptions are made up; nothing leaves this computer.
"""
import sys
import tempfile
import time
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent.parent / "app")]
import os  # noqa: E402

os.environ["FINANCE_DATA_DIR"] = tempfile.mkdtemp()
import categorizer  # noqa: E402
import db  # noqa: E402

# (description, amount CHF, expected category or None if a question is the right outcome)
CASES = [
    ("KAUF/DIENSTLEISTUNG VOM 03.09.2026 KARTEN NR. XXXX1234 MIGROS M ZUERICH", -87.35, "Grocery"),
    ("KAUF/DIENSTLEISTUNG VOM 15.09.2026 KARTEN NR. XXXX1234 COOP-1234 WINTERTHUR", -54.10, "Grocery"),
    ("DENNER AG ZUERICH", -23.40, "Grocery"), ("ALDI SUISSE 45 DIETIKON", -61.20, "Grocery"),
    ("LIDL SCHWEIZ 0123 BERN", -38.95, "Grocery"), ("MIGROLINO SHOP ZUERICH", -12.60, "Grocery"),
    ("TIBITS ZUERICH", -48.50, "Eating Out"), ("Card transaction of 31.50 CHF issued by Hiltl Zurich", -31.50, "Eating Out"),
    ("STARBUCKS ZUERICH HB", -7.80, "Eating Out"), ("MCDONALDS RESTAURANT BERN", -15.40, "Eating Out"),
    ("UBER EATS HELP.UBER.COM", -34.90, "Eating Out"), ("RESTAURANT ZUR LINDE BADEN", -96.00, "Eating Out"),
    ("ESCAPE ROOM ADVENTURE BERN", -120.00, "Entertainment"), ("PATHE KINO ZUERICH", -38.00, "Entertainment"),
    ("TICKETCORNER AG RUEMLANG", -149.00, "Entertainment"), ("KUNSTHAUS ZUERICH", -26.00, "Entertainment"),
    ("SBB CFF FFS ZUERICH HB", -31.00, "Transport"), ("SBB MOBILE APP", -8.80, "Transport"),
    ("ZVV TICKETAUTOMAT", -4.60, "Transport"), ("TOTALENERGIES TANKSTELLE BERN", -95.20, "Transport"),
    ("SHELL STATION WINTERTHUR", -88.10, "Transport"), ("PARKHAUS URANIA ZUERICH", -14.00, "Transport"),
    ("GALAXUS.CH ZUERICH", -249.90, "Online shopping"), ("ZALANDO SE BERLIN", -89.95, "Online shopping"),
    ("DIGITEC GALAXUS AG", -129.00, "Online shopping"), ("ALIEXPRESS.COM", -18.40, "Online shopping"),
    ("H&M ZUERICH BAHNHOFSTRASSE", -59.90, "Other shopping"), ("MANOR AG BASEL", -74.30, "Other shopping"),
    ("IKEA SPREITENBACH", -212.00, "House"), ("JUMBO DIY DIETLIKON", -44.80, "House"),
    ("PUTZFEE GMBH REINIGUNG", -160.00, "House"), ("SWISS INTERNATIONAL AIR LINES", -412.00, "Travel"),
    ("Card transaction of 89.00 EUR issued by Ryanair Dublin", -83.66, "Travel"), ("BOOKING.COM AMSTERDAM", -320.00, "Travel"),
    ("AIRBNB PAYMENTS", -540.00, "Travel"), ("COIFFEUR BELLA ZUERICH", -85.00, "Beauty"),
    ("NAILS & BEAUTY STUDIO WINTERTHUR", -65.00, "Beauty"), ("KOSMETIKSTUDIO AURA", -110.00, "Beauty"),
    ("LASTSCHRIFT CSS VERSICHERUNG AG PRAEMIE SEPTEMBER", -420.00, "Bills"),
    ("HELSANA VERSICHERUNGEN AG PRAEMIE", -398.50, "Bills"), ("SALT MOBILE SA RECHNUNG SEPTEMBER", -49.95, "Bills"),
    ("SWISSCOM (SCHWEIZ) AG RECHNUNG", -79.00, "Bills"), ("NETFLIX.COM", -18.90, "Bills"),
    ("SPOTIFY P1234 STOCKHOLM", -12.95, "Bills"), ("DAUERAUFTRAG IMMO VERWALTUNG AG MIETE", -2100.00, "Bills"),
    ("EWZ ELEKTRIZITAETSWERK ZUERICH", -68.40, "Bills"), ("INJOY FITNESS ZUERICH ABO", -89.00, "Sport"),
    ("YOGA STUDIO LOTUS KURS", -150.00, "Sport"), ("MIGROS FITNESSPARK", -95.00, "Sport"),
    ("FRESSNAPF ZUERICH", -43.90, "Pets"), ("TIERARZTPRAXIS DR. HUBER", -180.00, "Pets"),
    ("ZAHNARZTPRAXIS DR. MEIER RECHNUNG 2026-118", -280.00, "Health"), ("AMAVITA APOTHEKE ZUERICH", -32.50, "Health"),
    ("PRAXIS DR. MED. KELLER", -150.00, "Health"), ("FIELMANN AG BRILLEN", -240.00, "Health"),
    ("AXA VERSICHERUNGEN AG MOTORFAHRZEUG JAHRESPRAEMIE", -1240.00, "Yearly spendings"),
    ("KANTONALES STEUERAMT ZUERICH STEUERN 2025", -3800.00, "Yearly spendings"),
    ("MOBILIAR HAUSRAT JAHRESPRAEMIE", -310.00, "Yearly spendings"),
    ("GUTSCHRIFT AUFTRAGGEBER: ACME AG ZUERICH MITTEILUNGEN: LOHN SEPTEMBER", 7450.00, "Salary"),
    ("GUTSCHRIFT AUFTRAGGEBER: ACME AG ZUERICH MITTEILUNGEN: BONUS 2026", 5000.00, "Bonus"),
    ("ZINSGUTSCHRIFT SPARKONTO", 12.40, "Other income"),
    # these should be questions (person-to-person / ambiguous)
    ("TWINT ZAHLUNG AN ANNA MUELLER +41791234567", -60.00, None),
    ("Sent money to Marco Rossi", -120.00, None),
    ("TWINT ZAHLUNG VON LUCA BERNASCONI", 45.00, None),
]


def main():
    db.init()
    with db.db() as conn:
        s = db.get_settings(conn)
        models = [a for a in sys.argv[1:] if not a.startswith("--")]
        if models:
            s["llm_model"] = models[0]
        for i, (d, a, _) in enumerate(CASES):
            conn.execute("INSERT INTO transactions(account, date, description, amount, currency, amount_chf, hash) "
                         "VALUES('eval','2026-09-01',?,?,'CHF',?,?)", (d, a, a, "h%d" % i))
        if "--ai-only" in sys.argv:
            conn.execute("DELETE FROM rules")  # measure the AI alone, without starter/default rules
        if "--no-double-check" in sys.argv:
            s["double_check"] = "0"
        t0 = time.time()
        categorizer.run(conn, s)
        secs = time.time() - t0
        rows = conn.execute("SELECT t.description, t.status, t.confidence, c.name cat, g.name sug FROM transactions t "
                            "LEFT JOIN categories c ON c.id=t.category_id LEFT JOIN categories g ON g.id=t.suggestion_id "
                            "ORDER BY t.id").fetchall()
    auto = right_auto = asked = should_ask_asked = sugg_right = 0
    print("%-55s %-18s %-18s %s" % ("description", "expected", "got", "status"))
    for (d, a, exp), r in zip(CASES, rows):
        got = r["cat"] or r["sug"]
        if r["status"] in ("llm", "rule"):  # categorized without asking you
            auto += 1
            right_auto += got == exp
        else:
            asked += 1
            should_ask_asked += exp is None
            sugg_right += exp is not None and got == exp
        mark = "" if (r["status"] == "review" and exp is None) or got == exp else "  ✗"
        print("%-55s %-18s %-18s %s %.2f%s" % (d[:55], exp or "(ask)", got or "-", r["status"], r["confidence"] or 0, mark))
    n_ask = sum(1 for c in CASES if c[2] is None)
    print("\nmodel %s · %d cases · %.0f s" % (s["llm_model"], len(CASES), secs))
    print("precision of auto-assigned : %d/%d = %.0f%%  (target ≥ 95%%)" % (right_auto, auto, 100 * right_auto / max(auto, 1)))
    print("review rate                : %d/%d = %.0f%%" % (asked, len(CASES), 100 * asked / len(CASES)))
    print("person payments asked      : %d/%d" % (should_ask_asked, n_ask))
    print("suggestion right when asked: %d/%d" % (sugg_right, asked - should_ask_asked))


if __name__ == "__main__":
    main()
