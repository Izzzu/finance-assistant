"""HTTP API + static UI. Local mode: bound to 127.0.0.1 by start.py.

Security (see docs/ARCHITECTURE.md §5):
- Host header must be localhost/127.0.0.1/[::1] (blocks DNS-rebinding attacks from other sites)
- every non-GET /api call needs the X-Local-Accountant header; browsers can't add custom headers
  cross-site without a CORS preflight, which this server never approves
- strict Content-Security-Policy: the page can only load and talk to itself
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import sqlite3
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import categorizer
import db
import fx
import llm
import pipeline
import reports
from domain import JSON, Category, TxnSnapshot, UndoToken
from textutil import merchant_key, normalize

STATIC = Path(__file__).parent / "static"
MAX_FILE = 25 * 1024 * 1024
MAX_FILES = 40
CSRF_HEADER = "x-local-accountant"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    db.init()
    yield


log = logging.getLogger("local_accountant.server")

app = FastAPI(title="Local Accountant", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def _allowed_hosts() -> set[str]:
    extra = {h.strip().lower() for h in os.environ.get("LOCAL_ACCOUNTANT_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return LOCAL_HOSTS | extra


@app.middleware("http")
async def security(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    host = (request.headers.get("host") or "").lower()
    hostname = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    if hostname not in _allowed_hosts():
        return PlainTextResponse("Forbidden host", status_code=403)
    if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD"):
        if request.headers.get(CSRF_HEADER) != "1":
            return PlainTextResponse("Missing request header", status_code=403)
    resp = await call_next(request)
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Cache-Control"] = "no-store"
    return resp


def conn() -> AbstractContextManager[sqlite3.Connection]:
    return db.db()


# ---------------------------------------------------------------- status & scan

@app.get("/api/status")
def status() -> JSON:
    with conn() as c:
        s = db.get_settings(c)
        review = c.execute("SELECT COUNT(*) FROM transactions WHERE status='review'").fetchone()[0]
        n = c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        months = reports.months(c)
        accounts = reports.accounts(c)
    try:
        models = llm.list_models(s["llm_url"])
        missing = [m for m in {s["llm_model"], s.get("parser_model") or s["llm_model"]} if m not in models]
        llm_ok = not missing
        llm_msg = None if llm_ok else "The AI chosen in Settings isn't available on this computer."
        if missing:
            log.warning("models not installed in Ollama: %s (ollama pull %s)", ", ".join(missing), missing[0])
    except llm.LLMError as e:
        log.warning("%s", e)
        models, llm_ok, llm_msg = [], False, e.user_message
    return {"llm_ok": llm_ok, "llm_message": llm_msg, "models": models, "model": s["llm_model"],
            "currency": s["currency"], "to_review": review, "transactions": n, "months": months,
            "accounts": accounts}


@app.post("/api/scan")
async def scan(files: List[UploadFile] = File(...)) -> JSON:
    if len(files) > MAX_FILES:
        raise HTTPException(400, "Too many files (max %d)" % MAX_FILES)
    payload: list[tuple[str, bytes]] = []
    for f in files:
        data = await f.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise HTTPException(400, "%s is larger than 25 MB" % f.filename)
        name = os.path.basename(f.filename or "statement")
        if not (data[:5] == b"%PDF-" or name.lower().endswith((".csv", ".txt"))):
            raise HTTPException(400, "%s is not a PDF or CSV file" % name)
        payload.append((name, data))
    return {"job_id": pipeline.start(payload)}


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> pipeline.Job:
    j = pipeline.JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "Unknown job")
    return j


@app.post("/api/recategorize")
def recategorize() -> JSON:
    """Re-run AI categorization for items still To review (e.g. after Ollama was offline)."""
    with conn() as c:
        c.execute("UPDATE transactions SET status='new' WHERE status='review' AND suggestion_id IS NULL")
    return {"job_id": pipeline.start([])}


# ---------------------------------------------------------------- reports

@app.get("/api/report/month/{month}")
def report_month(month: str, account: Optional[str] = None, spread: bool = False) -> JSON:
    with conn() as c:
        return reports.monthly(c, month, account, spread)


@app.get("/api/report/year/{year}")
def report_year(year: str, account: Optional[str] = None) -> JSON:
    with conn() as c:
        return reports.yearly(c, year, account)


@app.get("/api/report/all")
def report_all(account: Optional[str] = None) -> JSON:
    with conn() as c:
        return reports.alltime(c, account)


@app.get("/api/review")
def review(period: Optional[str] = None, account: Optional[str] = None) -> list[JSON]:
    with conn() as c:
        return reports.review_groups(c, period, account)


@app.get("/api/search")
def search(q: str) -> list[JSON]:
    with conn() as c:
        return reports.search(c, q)


class NotesIn(BaseModel):
    month: str
    account: Optional[str] = None


@app.post("/api/notes")
def notes(body: NotesIn) -> JSON:
    with conn() as c:
        s = db.get_settings(c)
        try:
            return {"text": reports.notes(c, s, body.month, body.account)}
        except llm.LLMError as e:
            log.warning("notes: %s", e)
            raise HTTPException(503, e.user_message)


class AskIn(BaseModel):
    question: str
    account: Optional[str] = None


@app.post("/api/ask")
def ask(body: AskIn) -> JSON:
    with conn() as c:
        s = db.get_settings(c)
        try:
            return {"text": reports.ask(c, s, body.question[:500], body.account)}
        except llm.LLMError as e:
            log.warning("ask: %s", e)
            raise HTTPException(503, e.user_message)


@app.get("/api/export.csv")
def export() -> Response:
    with conn() as c:
        data = reports.export_csv(c)
    return Response(data, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=transactions.csv"})


# ---------------------------------------------------------------- categorizing by the user

class AssignIn(BaseModel):
    txn_ids: List[int]
    category_id: int
    remember: bool = False
    pattern: Optional[str] = None


@app.post("/api/assign")
def assign(body: AssignIn) -> JSON:
    with conn() as c:
        if not c.execute("SELECT 1 FROM categories WHERE id=?", (body.category_id,)).fetchone():
            raise HTTPException(400, "Unknown category")
        token = categorizer.assign(c, body.txn_ids, body.category_id, body.remember, body.pattern)
        return {"undo": token, "changed": len(token["txns"]), "not_remembered": token.get("not_remembered", False)}


class SimilarIn(BaseModel):
    txn_id: int


@app.post("/api/similar")
def similar(body: SimilarIn) -> JSON:
    """Suggested 'remember' pattern for a transaction and how many others it would update."""
    with conn() as c:
        p = categorizer.suggest_pattern(c, body.txn_id)
        return {"pattern": p, "count": len(categorizer.similar(c, p, exclude_ids={body.txn_id}))}


class PatternIn(BaseModel):
    pattern: str


@app.post("/api/similar/count")
def similar_count(body: PatternIn) -> JSON:
    with conn() as c:
        return {"count": len(categorizer.similar(c, body.pattern))}


class UndoIn(BaseModel):
    undo: UndoToken


@app.post("/api/undo")
def undo(body: UndoIn) -> JSON:
    with conn() as c:
        categorizer.undo(c, body.undo)
    return {"ok": True}


class AcceptIn(BaseModel):
    period: Optional[str] = None
    account: Optional[str] = None


@app.post("/api/review/accept-all")
def accept_all(body: AcceptIn) -> JSON:
    """Accept every AI suggestion in the To review list (without creating rules)."""
    with conn() as c:
        groups = reports.review_groups(c, body.period, body.account)
        snapshots: list[TxnSnapshot] = []
        for g in groups:
            if g["suggestion_id"]:
                snapshots += categorizer.assign(c, g["txn_ids"], g["suggestion_id"])["txns"]
        merged = UndoToken(txns=snapshots, rule=None, not_remembered=False)
        return {"undo": merged, "changed": len(snapshots)}


# ---------------------------------------------------------------- categories, rules, files, settings

@app.get("/api/categories")
def categories() -> list[Category]:
    with conn() as c:
        return db.categories(c)


class CategoryIn(BaseModel):
    id: Optional[int] = None
    name: str
    kind: str = "expense"
    period: str = "monthly"
    description: str = ""


@app.post("/api/categories")
def save_category(body: CategoryIn) -> JSON:
    if body.kind not in ("expense", "income", "transfer") or body.period not in ("monthly", "yearly"):
        raise HTTPException(400, "Please choose a type and a report for the category.")
    name = body.name.strip()[:60]
    if not name:
        raise HTTPException(400, "Name is required")
    with conn() as c:
        try:
            if body.id:
                c.execute("UPDATE categories SET name=?, kind=?, period=?, description=? WHERE id=?",
                          (name, body.kind, body.period, body.description[:300], body.id))
                cid = body.id
            else:
                cid = c.execute("INSERT INTO categories(name, kind, period, description) VALUES(?,?,?,?)",
                                (name, body.kind, body.period, body.description[:300])).lastrowid
        except Exception:
            raise HTTPException(400, "A category with this name already exists")
        c.execute("DELETE FROM llm_cache")  # new/changed categories: let the AI decide again
        return {"id": cid}


@app.delete("/api/categories/{cid}")
def delete_category(cid: int) -> JSON:
    with conn() as c:
        used = c.execute("SELECT COUNT(*) FROM transactions WHERE category_id=?", (cid,)).fetchone()[0]
        if used:
            raise HTTPException(400, "Category is used by %d transactions; move them first" % used)
        c.execute("DELETE FROM categories WHERE id=?", (cid,))
        c.execute("DELETE FROM llm_cache")
    return {"ok": True}


@app.get("/api/rules")
def list_rules() -> list[JSON]:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT r.id, r.pattern, r.category_id, c.name AS category, r.created_at FROM rules r "
            "JOIN categories c ON c.id=r.category_id ORDER BY r.pattern")]


@app.delete("/api/rules/{rid}")
def delete_rule(rid: int) -> JSON:
    with conn() as c:
        c.execute("DELETE FROM rules WHERE id=?", (rid,))
    return {"ok": True}


@app.get("/api/imports")
def imports() -> list[JSON]:
    with conn() as c:
        out: list[JSON] = []
        for r in c.execute("SELECT * FROM imports ORDER BY id DESC"):
            d: JSON = dict(r)
            d["warnings"] = json.loads(d["warnings"] or "[]")
            out.append(d)
        return out


@app.delete("/api/imports/{iid}")
def delete_import(iid: int) -> JSON:
    with conn() as c:
        c.execute("DELETE FROM imports WHERE id=?", (iid,))
    return {"ok": True}


class TxnEdit(BaseModel):
    date: Optional[str] = None
    description: Optional[str] = None
    amount: Optional[float] = None


def _import_status(c: sqlite3.Connection, import_id: int | None) -> JSON | None:
    if not import_id:
        return None
    r = c.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
    if not r:
        return None
    d: JSON = dict(r)
    d["warnings"] = json.loads(d["warnings"] or "[]")
    return d


@app.put("/api/transactions/{tid}")
def edit_transaction(tid: int, body: TxnEdit) -> JSON:
    """Correct a row the statement reader got wrong. The original amount is kept for reference."""
    with conn() as c:
        t = c.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone()
        if not t:
            raise HTTPException(404, "Unknown transaction")
        s = db.get_settings(c)
        date, desc, amount = t["date"], t["description"], t["amount"]
        if body.date is not None:
            try:
                date = datetime.date.fromisoformat(body.date).isoformat()
            except ValueError:
                raise HTTPException(400, "Please enter a valid date.")
        if body.description is not None:
            desc = body.description.strip()[:300] or desc
        if body.amount is not None:
            if body.amount == 0:
                raise HTTPException(400, "Amount cannot be 0 (delete the row instead)")
            amount = round(body.amount, 2)
        if t["currency"] == s.get("currency", "CHF"):
            amount_chf, rate = amount, 1.0
        elif t["fx_rate"]:
            amount_chf, rate = round(amount * t["fx_rate"], 2), t["fx_rate"]
        else:
            amount_chf, rate = None, None
        norm = normalize(desc)
        orig = t["orig_amount"] if t["orig_amount"] is not None else (t["amount"] if amount != t["amount"] else None)
        c.execute("UPDATE transactions SET date=?, description=?, amount=?, amount_chf=?, fx_rate=?, norm=?, "
                  "merchant_key=?, orig_amount=?, edited=1 WHERE id=?",
                  (date, desc, amount, amount_chf, rate, norm, merchant_key(norm), orig, tid))
        pipeline.recheck(c, t["import_id"], delta=amount - t["amount"])
        return {"ok": True, "import": _import_status(c, t["import_id"])}


@app.delete("/api/transactions/{tid}")
def delete_transaction(tid: int) -> JSON:
    """Remove a row that isn't a real transaction (e.g. a misread summary line)."""
    with conn() as c:
        t = c.execute("SELECT import_id, amount FROM transactions WHERE id=?", (tid,)).fetchone()
        if not t:
            raise HTTPException(404, "Unknown transaction")
        c.execute("DELETE FROM transactions WHERE id=?", (tid,))
        pipeline.recheck(c, t["import_id"], delta=-t["amount"])
        return {"ok": True, "import": _import_status(c, t["import_id"])}


@app.get("/api/imports/{iid}/transactions")
def import_transactions(iid: int) -> JSON:
    with conn() as c:
        rows = c.execute(
            "SELECT t.*, c.name AS category, c.kind, c.period, s.name AS suggestion FROM transactions t "
            "LEFT JOIN categories c ON c.id=t.category_id LEFT JOIN categories s ON s.id=t.suggestion_id "
            "WHERE t.import_id=? ORDER BY t.date, t.id", (iid,)).fetchall()
        return {"import": _import_status(c, iid), "transactions": reports.as_transactions(c, rows)}


class ShareIn(BaseModel):
    share: float


@app.post("/api/transactions/{tid}/share")
def set_share(tid: int, body: ShareIn) -> JSON:
    """Your part of a shared cost: 0.5 = split half, 1 = all yours. Only your part counts in reports."""
    if not 0 < body.share <= 1:
        raise HTTPException(400, "Share must be between 0 and 100%")
    with conn() as c:
        if not c.execute("SELECT 1 FROM transactions WHERE id=?", (tid,)).fetchone():
            raise HTTPException(404, "Unknown transaction")
        c.execute("UPDATE transactions SET share=? WHERE id=?", (round(body.share, 4), tid))
    return {"ok": True}


@app.get("/api/settings")
def get_settings() -> dict[str, str]:
    with conn() as c:
        return db.get_settings(c)


class SettingsIn(BaseModel):
    llm_url: Optional[str] = None
    llm_model: Optional[str] = None
    parser_model: Optional[str] = None
    confidence_threshold: Optional[float] = None
    currency: Optional[str] = None
    fx_download: Optional[bool] = None
    fx_manual: Optional[dict[str, float]] = None
    double_check: Optional[bool] = None


@app.post("/api/settings")
def save_settings(body: SettingsIn) -> JSON:
    upd: dict[str, str] = {}
    if body.llm_url is not None:
        try:
            llm.ensure_local(body.llm_url)
        except llm.LLMError as e:
            raise HTTPException(400, e.user_message)
        upd["llm_url"] = body.llm_url
    if body.llm_model is not None:
        upd["llm_model"] = body.llm_model
    if body.parser_model is not None:
        upd["parser_model"] = body.parser_model
    if body.confidence_threshold is not None:
        upd["confidence_threshold"] = str(max(0.0, min(1.0, body.confidence_threshold)))
    if body.currency is not None:
        upd["currency"] = body.currency.upper()[:3]
    if body.fx_download is not None:
        upd["fx_download"] = "1" if body.fx_download else "0"
    if body.double_check is not None:
        upd["double_check"] = "1" if body.double_check else "0"
    if body.fx_manual is not None:
        upd["fx_manual"] = json.dumps({k.upper()[:3]: float(v) for k, v in body.fx_manual.items() if v})
    with conn() as c:
        for k, v in upd.items():
            c.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?,?)", (k, v))
        if "fx_manual" in upd or "fx_download" in upd:
            fx.convert(c, db.get_settings(c))
    return {"ok": True}


# ---------------------------------------------------------------- UI

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC / "index.html"))


@app.exception_handler(Exception)
async def _errors(request: Request, exc: Exception) -> JSONResponse:
    log.error("unexpected error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"detail": "Something went wrong. Please try again."}, status_code=500)
