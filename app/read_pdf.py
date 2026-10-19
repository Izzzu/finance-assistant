"""Try the statement reader on a PDF or CSV and see what it found. Runs only on this computer.

    python3 start.py --read statement.pdf            # full report, for your eyes only
    python3 start.py --read statement.pdf --redact   # names, merchants and amounts masked, to share
    python3 start.py --read export.csv               # which columns were recognized, rows read

Nothing is saved and nothing is sent anywhere. For PDFs it prints Docling's Markdown and the local
AI's raw JSON for every page, then the checks and the extracted transactions.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import pdf_import
from domain import Settings
from textutil import fold

# Words that describe the statement's structure, not you. Kept readable in --redact mode.
KEEP = set("""
date datum details text description beschreibung credit debit gutschrift lastschrift gutschriften lastschriften
belastung belastungen value valuta balance saldo amount betrag currency wahrung chf eur usd gbp
opening closing previous new carried brought forward ubertrag kontostand total subtotal page seite of von
account statement kontoauszug private privatkonto iban period periode transaction transactions booking
purchase service from card no nr kauf dienstleistung vom karten twint credit debit direct standing order
transfer payment zahlung sender recipient message mitteilungen auftraggeber salary lohn incoming outgoing
postfinance swisscard wise ag gmbh sa in to for the and
amounts row above left columns column ignored expected next line furniture no an found header table width
statement type null markdown what read answer raw json checks money
""".split())


def redact(text: str) -> str:
    def word(m: re.Match[str]) -> str:
        w = m.group(0)
        return w if fold(w) in KEEP else re.sub(r"[^\W\d_]", lambda c: "X" if c.group(0).isupper() else "x", w)
    text = re.sub(r"[^\W\d_]+", word, text)
    return re.sub(r"\d", "9", text)


def _plain(text: str) -> str:
    return text


def _printer(out: TextIO) -> Callable[..., None]:
    def p(*parts: object) -> None:
        print(*parts, file=out)
    return p


def run_csv(data: bytes, redact_mode: bool, out: TextIO) -> int:
    import importer
    show = redact if redact_mode else _plain
    p = _printer(out)
    a = importer.analyze(data)
    m = a["mapping"]
    rows, skipped = importer.build(a["rows"], m, "CHF")
    p("=" * 100)
    p("CSV reader report%s" % ("  (REDACTED)" if redact_mode else "  (contains your data - use --redact to share)"))
    p("delimiter: %r   columns: %s" % (a["delimiter"], " | ".join(a["headers"])))
    def name(i: int | None) -> str:
        return a["headers"][i] if i is not None and i < len(a["headers"]) else "-"

    p("date: %s (%s)   value date: %s   description: %s" % (
        name(m["date"]), m["date_format"], name(m["value_date"]), ", ".join(name(i) for i in m["description"])))
    p("amount: %s   credit: %s   debit: %s   balance: %s" % (name(m["amount"]), name(m["credit"]),
                                                            name(m["debit"]), name(m["balance"])))
    p("rows read: %d   rows skipped (no date or amount): %d" % (len(rows), skipped))
    p("-" * 100)
    for t in rows:
        p("%s  %12s  %12s  %s" % (show(t["date"]), show("%.2f" % t["amount"]),
                                 show("" if t["balance"] is None else "%.2f" % t["balance"]), show(t["description"])[:70]))
    return 0


def _settings() -> Settings:
    """The app's settings (model names) if a database exists, else the defaults."""
    import db
    s = dict(db.DEFAULT_SETTINGS)
    if db.db_path().exists():
        with db.db() as c:
            s.update(db.get_settings(c))
    return s


def _progress(page: int, pages: int, stage: str) -> None:
    print("...", stage, file=sys.stderr, flush=True)


def run(path: str, redact_mode: bool = False, out: TextIO = sys.stdout, settings: Settings | None = None) -> int:
    data = Path(path).read_bytes()
    if not data.startswith(b"%PDF-"):
        return run_csv(data, redact_mode, out)
    settings = settings or _settings()
    show = redact if redact_mode else _plain
    p = _printer(out)
    trace: list[pdf_import.PageTrace] = []
    try:
        r = pdf_import.parse_pdf(data, settings, trace=trace, progress=_progress)
    except Exception as e:
        p("ERROR:", e)
        return 1
    p("=" * 100)
    p("Statement reader report%s" % ("  (REDACTED: letters -> x, digits -> 9; structure words kept)" if redact_mode else
                                      "  (contains your data - do not share; use --redact for a shareable version)"))
    p("parser model: %s" % (settings.get("parser_model") or pdf_import.DEFAULT_PARSER_MODEL))
    for t in trace:
        p("=" * 100)
        p("PAGE %d - what Docling read (Markdown)" % t["page"])
        p("-" * 100)
        p(show(t["markdown"]))
        p("-" * 100)
        p("PAGE %d - the local AI's answer (raw JSON)" % t["page"])
        p("-" * 100)
        p(show(t["answer"]) if t["answer"] is not None else "(no answer: %s)" % t["error"])
    p("=" * 100)
    p("CHECKS")
    p("bank: %s   account: %s   period: %s .. %s" % (show(r["bank"]), show(r["account"]), show(str(r["period_from"])),
                                                    show(str(r["period_to"]))))
    p("balance check: %s   difference: %s   opening: %s   closing: %s" % (
        r["balance_check"], show(str(r["balance_diff"])), show(str(r["opening"])), show(str(r["closing"]))))
    for w in r["warnings"]:
        p("warning:", show(w))
    p("=" * 100)
    p("EXTRACTED TRANSACTIONS (%d)" % len(r["transactions"]))
    p("-" * 100)
    for txn in r["transactions"]:
        bal = "" if txn["balance"] is None else "%.2f" % txn["balance"]
        p("%s  %12s %s %12s  %s" % (show(txn["date"]), show("%.2f" % txn["amount"]), txn["currency"], show(bal),
                                    show(txn["description"])[:70]))
    return 0


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    return run(args[0], "--redact" in argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
