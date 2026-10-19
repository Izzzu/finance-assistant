"""Scan job: files -> transactions (deduplicated) -> CHF amounts -> categories.

Runs in a background thread; the UI polls the job for per-file progress.
Uploaded file bytes live only in memory for the duration of the job.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import logging
import sqlite3
import threading
import uuid
from collections import Counter
from collections.abc import Callable
from typing import TypedDict

import categorizer
import db
import fx
import importer
import pdf_import
from domain import JSON, BalanceCheck, FileStatus, IngestResult, ReadError, ReadResult, Settings, StatementTxn
from llm import LLMError


class JobProgress(TypedDict):
    done: int
    total: int


class Job(TypedDict):
    id: str
    stage: str              # queued | converting | categorizing | done
    done: bool
    error: str | None
    files: list[FileStatus]
    progress: JobProgress
    summary: JSON | None


log = logging.getLogger("local_accountant.scan")

JOBS: dict[str, Job] = {}


def user_message(e: Exception) -> str:
    """What the UI shows for an error while scanning; the technical detail goes to the log."""
    if isinstance(e, ReadError):
        return str(e)
    if isinstance(e, LLMError):
        return e.user_message
    log.exception("scan failed")
    return "This file couldn't be read. It may be damaged or in a format that isn't supported."
_lock = threading.Lock()  # one scan at a time keeps SQLite writes simple


def start(files: list[tuple[str, bytes]]) -> str:
    """files: list of (filename, bytes). Returns the job id."""
    job = Job(id=uuid.uuid4().hex[:12], stage="queued", done=False, error=None,
              files=[FileStatus(name=n, status="waiting") for n, _ in files],
              progress=JobProgress(done=0, total=0), summary=None)
    JOBS[job["id"]] = job
    threading.Thread(target=_run, args=(job, files), daemon=True).start()
    return job["id"]


def _run(job: Job, files: list[tuple[str, bytes]]) -> None:
    with _lock:
        try:
            with db.db() as conn:
                settings = db.get_settings(conn)
                for i, (name, data) in enumerate(files):
                    f = job["files"][i]
                    f["status"] = "reading"

                    def show(text: str, f: FileStatus = f) -> None:
                        f["detail"] = text

                    try:
                        res = ingest(conn, name, data, settings, progress=show)
                        f["import_id"], f["bank"], f["account"] = res["import_id"], res["bank"], res["account"]
                        f["period_from"], f["period_to"] = res["period_from"], res["period_to"]
                        f["balance_check"], f["balance_diff"] = res["balance_check"], res["balance_diff"]
                        f["warnings"], f["added"], f["duplicates"] = res["warnings"], res["added"], res["duplicates"]
                        f["detail"] = None
                        f["status"] = "done"
                    except Exception as e:  # one bad file must not stop the others
                        f["status"] = "error"
                        f["error"] = user_message(e)
                    conn.commit()
                job["stage"] = "converting"
                missing = fx.convert(conn, settings)
                conn.commit()
                job["stage"] = "categorizing"

                def progress(done: int, total: int) -> None:
                    job["progress"] = JobProgress(done=done, total=total)

                summary: JSON = dict(categorizer.run(conn, settings, progress))
                summary["without_rate"] = missing
                job["summary"] = summary
        except Exception as e:
            job["error"] = user_message(e)
        finally:
            job["stage"] = "done"
            job["done"] = True


def _running_balance_warning(bad: int) -> str:
    return "%d row(s) don't match the running balance; a row may be missing or misread." % bad


def recheck(conn: sqlite3.Connection, import_id: int, delta: float = 0.0) -> None:
    """Re-run the opening + movements = closing check for one file, e.g. after you corrected a row.

    delta: how much the file's total changed with this correction. Files scanned before balances
    were stored only have their old difference, which is then adjusted by delta. Files checked
    only by running balance (no difference recorded) keep their status.
    """
    imp = conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
    if not imp:
        return
    rows = conn.execute("SELECT amount, currency, balance FROM transactions WHERE import_id=? ORDER BY id",
                        (import_id,)).fetchall()
    liability = bool(imp["liability"]) or imp["bank"] == "Swisscard"
    old_warnings: list[str] = json.loads(imp["warnings"] or "[]")
    if imp["opening"] is None and any(r["balance"] is not None for r in rows):
        # statement with a running balance column: every row must follow from the previous one
        bad = pdf_import.running_balance_errors(
            [pdf_import.BalanceRow(amount=r["amount"], balance=r["balance"], currency=r["currency"]) for r in rows]) or 0
        warnings = [w for w in old_warnings if "running balance" not in w]
        if bad:
            warnings.append(_running_balance_warning(bad))
        conn.execute("UPDATE imports SET balance_check=?, balance_diff=NULL, warnings=?, added=? WHERE id=?",
                     ("diff" if bad else "ok", json.dumps(warnings), len(rows), import_id))
        return
    diff: float
    if imp["opening"] is not None and imp["closing"] is not None:
        if len({r["currency"] for r in rows}) > 1:
            return
        total = round(sum(float(r["amount"]) for r in rows), 2)
        diff = round(imp["opening"] + total - imp["closing"], 2)
        if liability:
            alt = round(imp["opening"] - total - imp["closing"], 2)
            diff = alt if abs(alt) < abs(diff) else diff
    elif imp["balance_diff"] is not None and imp["balance_check"] in ("ok", "diff"):
        diff = round(imp["balance_diff"] + (-delta if liability else delta), 2)
    else:
        return
    ok = abs(diff) < 0.01
    warnings = [w for w in old_warnings if not w.startswith("Balances don't match")]
    if not ok:
        warnings.append("Balances don't match: difference %.2f. A row may be missing or misread." % diff)
    conn.execute("UPDATE imports SET balance_check=?, balance_diff=?, warnings=?, added=? WHERE id=?",
                 ("ok" if ok else "diff", 0.0 if ok else diff, json.dumps(warnings), len(rows), import_id))


def _hash(account: str, t: StatementTxn, n: int) -> str:
    base = "%s|%s|%.2f|%s|%s|%d" % (account, t["date"], t["amount"], t["currency"], t["description"], n)
    return hashlib.sha256(base.encode()).hexdigest()


def _read_csv(name: str, data: bytes, main: str) -> ReadResult:
    a = importer.analyze(data)
    rows, skipped = importer.build(a["rows"], a["mapping"], main)
    dates = sorted(t["date"] for t in rows)
    warnings = ["%d rows skipped (no date or amount)" % skipped] if skipped else []
    check: BalanceCheck = "none"
    bad = pdf_import.running_balance_errors(rows)  # verify with the CSV's balance column, if any
    if bad is not None:
        check = "diff" if bad else "ok"
        if bad:
            warnings.append(_running_balance_warning(bad))
    bank = "PostFinance" if b"postfinance" in data.lower()[:4000] or "postfinance" in name.lower() else "CSV"
    return ReadResult(bank=bank, account=name.rsplit(".", 1)[0], transactions=rows, opening=None, closing=None,
                      liability=False, period_from=dates[0] if dates else None,
                      period_to=dates[-1] if dates else None, balance_check=check, balance_diff=None,
                      warnings=warnings)


def ingest(conn: sqlite3.Connection, name: str, data: bytes, settings: Settings,
           progress: Callable[[str], None] | None = None) -> IngestResult:
    """Parse one file and store its transactions. Returns the file status for the UI.

    progress(text) is called with what's happening, e.g. "AI reading page 2/3".
    """
    main = settings.get("currency", "CHF")
    if data[:5] == b"%PDF-":
        def on_page(page: int, pages: int, stage: str) -> None:
            if progress:
                progress(stage)
        r = pdf_import.parse_pdf(data, settings, default_currency=main, progress=on_page)
    elif name.lower().endswith((".csv", ".txt")):
        r = _read_csv(name, data, main)
    else:
        raise ReadError("This file type isn't supported. Please upload PDF or CSV statements.")

    if not r["transactions"]:
        raise ReadError("No transactions were found in this file.")
    cur = conn.execute(
        "INSERT INTO imports(filename, bank, account, period_from, period_to, balance_check, balance_diff, "
        "opening, closing, liability, warnings, imported_at, added, skipped) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,0)",
        (name, r["bank"], r["account"], r["period_from"], r["period_to"], r["balance_check"],
         r["balance_diff"], r["opening"], r["closing"], 1 if r["liability"] else 0,
         json.dumps(r["warnings"]), datetime.datetime.now().isoformat(timespec="seconds")))
    import_id = cur.lastrowid
    seen: Counter[tuple[str, float, str, str]] = Counter()
    added = dupes = 0
    for t in r["transactions"]:
        key = (t["date"], t["amount"], t["currency"], t["description"])
        seen[key] += 1
        h = _hash(r["account"], t, seen[key])
        try:
            conn.execute(
                "INSERT INTO transactions(import_id, account, date, description, amount, currency, balance, "
                "source, hash) VALUES(?,?,?,?,?,?,?,?,?)",
                (import_id, r["account"], t["date"], t["description"], t["amount"], t["currency"] or main,
                 t["balance"], t["source"], h))
            added += 1
        except sqlite3.IntegrityError:  # already imported earlier
            dupes += 1
    conn.execute("UPDATE imports SET added=?, skipped=? WHERE id=?", (added, dupes, import_id))
    if added == 0:
        conn.execute("DELETE FROM imports WHERE id=?", (import_id,))
    return IngestResult(import_id=import_id if added else None, bank=r["bank"], account=r["account"],
                      period_from=r["period_from"], period_to=r["period_to"],
                      balance_check=r["balance_check"], balance_diff=r["balance_diff"],
                      warnings=r["warnings"], added=added, duplicates=dupes, detail=None)
