"""Bank and credit-card statement PDFs -> transactions, for any layout. No bank-specific rules.

  1. Docling reads the PDF (layout + table structure, using the PDF's own text layer) -> Markdown per page
  2. a local LLM (Ollama) turns each page's Markdown into JSON transactions
  3. generic checks, the same for every bank:
     - every amount must be printed in the PDF's text layer (rows the PDF doesn't contain are dropped)
     - opening + movements = closing, or each running balance follows from the row before

Docling's models are downloaded once into .models/ and then used offline; the PDF itself never
leaves this computer.
"""
from __future__ import annotations

import datetime
import hashlib
import io
import json
import logging
import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import pdfplumber

import llm
from domain import JSON, BalanceCheck, ReadError, ReadResult, Settings, StatementTxn
from importer import parse_amount
from textutil import last4

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

MODELS_DIR = Path(os.environ.get("LOCAL_ACCOUNTANT_MODELS") or Path(__file__).resolve().parent.parent / ".models") / "docling"
DEFAULT_PARSER_MODEL = "qwen3:8b"
log = logging.getLogger("local_accountant.pdf")

SCHEMA: JSON = {
    "type": "object", "required": ["statement", "transactions"],
    "properties": {
        "statement": {
            "type": "object",
            "required": ["bank", "statement_type", "currency", "opening_balance", "closing_balance"],
            "properties": {
                "bank": {"type": ["string", "null"]},
                "statement_type": {"type": "string", "enum": ["bank_account", "credit_card"]},
                "currency": {"type": ["string", "null"]},
                "opening_balance": {"type": ["number", "null"]},
                "closing_balance": {"type": ["number", "null"]}}},
        "transactions": {"type": "array", "items": {
            "type": "object",
            "required": ["date", "description", "money_in", "money_out", "balance", "currency"],
            "properties": {
                "date": {"type": "string"},
                "description": {"type": "string"},
                "money_in": {"type": ["number", "null"]},
                "money_out": {"type": ["number", "null"]},
                "balance": {"type": ["number", "null"]},
                "currency": {"type": ["string", "null"]}}}}},
}

INSTRUCTIONS = """You read one page of a bank or credit-card statement (converted to Markdown) and return its
transactions as JSON.
- One entry per transaction. Every table row that has an amount (in a credit, debit, in, out or amount
  column) is its own transaction, EVEN IF ITS DATE CELL IS EMPTY - an empty date only means "same day as
  the row above". Never merge two rows: a transaction has money_in OR money_out, never both.
- A description often spans several lines within one row: join those lines.
- date: YYYY-MM-DD. If there is a Value / Valuta column, use that date. On credit-card statements use the
  transaction (purchase) date. If a row has no date printed, use the previous row's date.
- money_in / money_out: positive numbers, exactly as printed. Salary, refunds, money received and card
  payments received are money_in; purchases and payments are money_out. On credit-card statements purchases
  are money_out, and amounts marked CR / credit / payment received are money_in.
- balance: the running balance printed on that row, else null. currency: the row's currency, else null.
- Opening/previous balance, closing/new balance, carried/brought forward lines, totals and summaries are NOT
  transactions. Put opening and closing balance into "statement" if the page shows them.
- statement.bank: the bank or card issuer's name. statement_type: credit_card or bank_account.
- Copy digits exactly. Never invent anything. If the page has no transactions, return an empty list."""

NUM_RE = re.compile(r"\d{1,3}(?:[ '’, ]\d{3})+[.,]\d{2}|\d+[.,]\d{2}")


@dataclass(frozen=True)
class Page:
    """One PDF page: Docling's Markdown and the PDF's own text layer."""
    number: int
    markdown: str
    text: str


class PageTrace(TypedDict):
    """What happened on one page (shown by `start.py --read`)."""
    page: int
    markdown: str
    answer: str | None
    error: str | None


class BalanceRow(TypedDict):
    amount: float
    balance: float | None
    currency: str


Progress = Callable[[int, int, str], None]
"""progress(page, pages, stage)"""


# ------------------------------------------------------------------ 1. Docling

def ensure_models(log: Callable[[str], None] = print) -> None:
    """Download Docling's layout and table models once (internet needed only this first time)."""
    marker = MODELS_DIR / ".complete"
    if marker.exists():
        return
    from docling.utils.model_downloader import download_models
    log("Downloading Docling's document models (one time, ~200 MB) ...")
    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    download_models(output_dir=MODELS_DIR, with_layout=True, with_tableformer=True, with_code_formula=False,
                    with_picture_classifier=False, with_rapidocr=False)
    marker.write_text(datetime.datetime.now().isoformat())


_converter: DocumentConverter | None = None
_cache: dict[str, list[Page]] = {}  # sha256 -> pages; a re-scan of the same file doesn't re-run Docling


def _docling() -> DocumentConverter:
    global _converter
    if _converter is None:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        ensure_models()
        opts = PdfPipelineOptions(artifacts_path=str(MODELS_DIR), do_ocr=False, do_table_structure=True)
        _converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})
    return _converter


def _text_layer(data: bytes) -> list[str]:
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages: list[Any] = list(pdf.pages)  # pdfplumber has no type hints
        return [str(p.extract_text() or "") for p in pages]


def read_pages(data: bytes) -> list[Page]:
    """PDF bytes -> [Page] with Docling's Markdown and the text layer of each page."""
    key = hashlib.sha256(data).hexdigest()
    if key in _cache:
        return _cache[key]
    texts = _text_layer(data)
    if not any(t.strip() for t in texts):
        raise ReadError("This PDF is a scanned image. Scanned statements can't be read yet.")
    from docling.datamodel.base_models import DocumentStream
    doc = _docling().convert(DocumentStream(name="statement.pdf", stream=io.BytesIO(data))).document
    pages = [Page(n, doc.export_to_markdown(page_no=n), texts[n - 1] if n - 1 < len(texts) else "")
             for n in sorted(doc.pages)]
    if len(_cache) > 16:
        _cache.clear()
    _cache[key] = pages
    return pages


# ------------------------------------------------------------------ 2. LLM -> JSON

def llm_page(settings: Settings, page: Page, n_pages: int, last_date: str | None) -> str:
    """Ask the local model for one page's transactions. Returns the model's raw JSON text."""
    hint = " The last transaction date on the previous page was %s." % last_date if last_date else ""
    prompt = "%s\n\nThis is page %d of %d.%s\n\n----- PAGE (Markdown) -----\n%s" % (
        INSTRUCTIONS, page.number, n_pages, hint, page.markdown[:20000])
    return llm.chat(settings, [{"role": "user", "content": prompt}], SCHEMA, timeout=900,
                    model=settings.get("parser_model") or DEFAULT_PARSER_MODEL,
                    options={"num_ctx": 16384, "num_predict": 6000})


def _iso(s: object) -> str | None:
    text = str(s or "").strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y"):
        try:
            return datetime.datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _text(v: object) -> str:
    return "" if v is None else str(v)


# ------------------------------------------------------------------ 3. checks

def text_numbers(text: str) -> set[float]:
    """Every amount printed in a text, as absolute values rounded to cents."""
    out: set[float] = set()
    for m in NUM_RE.findall(text):
        v = parse_amount(m)
        if v is not None:
            out.add(round(abs(v), 2))
    return out


def _continuity(rows: Sequence[BalanceRow]) -> tuple[int, int] | None:
    """(rows whose running balance doesn't follow from the previous row, rows checked); best direction."""
    best: tuple[int, int] | None = None
    for order in (list(rows), list(reversed(rows))):
        bad, checked = 0, 0
        prev: float | None = None
        for r in order:
            balance = r["balance"]
            if balance is None:
                continue
            if prev is not None:
                checked += 1
                if abs(prev + r["amount"] - balance) > 0.01:
                    bad += 1
            prev = balance
        if checked and (best is None or bad < best[0]):
            best = (bad, checked)
    return best


def running_balance_errors(rows: Sequence[BalanceRow]) -> int | None:
    """How many rows' running balance doesn't follow from the row before (per currency).

    None if the rows carry no running balances, so there is nothing to check.
    """
    by_cur: dict[str, list[BalanceRow]] = {}
    for r in rows:
        by_cur.setdefault(r["currency"], []).append(r)
    results = [c for c in (_continuity(g) for g in by_cur.values()) if c]
    return sum(bad for bad, _ in results) if results else None


def _verify(rows: Sequence[StatementTxn], opening: float | None, closing: float | None, liability: bool,
            warnings: list[str]) -> tuple[BalanceCheck, float | None]:
    if opening is not None and closing is not None and len({r["currency"] for r in rows}) <= 1:
        total = round(sum(r["amount"] for r in rows), 2)
        diff = round(opening + total - closing, 2)
        if liability:  # credit card: the balance is what you owe
            diff_l = round(opening - total - closing, 2)
            if abs(diff_l) < abs(diff):
                diff = diff_l
        if abs(diff) < 0.01:
            return "ok", 0.0
        warnings.append("Balances don't match: opening %.2f + movements %.2f ≠ closing %.2f "
                        "(difference %.2f). A row may be missing or misread." % (opening, total, closing, diff))
        return "diff", diff
    bad = running_balance_errors(rows)
    if bad is not None:
        if bad == 0:
            return "ok", 0.0
        warnings.append("%d row(s) don't match the running balance; a row may be missing or misread." % bad)
        return "diff", None
    return "none", None


# ------------------------------------------------------------------ the whole thing

def parse_pdf(data: bytes, settings: Settings, default_currency: str = "CHF", progress: Progress | None = None,
              trace: list[PageTrace] | None = None) -> ReadResult:
    """Statement PDF -> transactions, statement balances, balance check and warnings.

    progress(page, pages, stage) reports what's happening; trace receives one PageTrace per page,
    so the raw Docling output and model answers can be shown.
    """
    n = 0

    def step(page: int, stage: str) -> None:
        if progress:
            progress(page, n, stage)

    step(0, "Reading the statement…")
    pages = read_pages(data)
    n = len(pages)
    full_text = "\n".join(p.text for p in pages)
    warnings: list[str] = []
    out: list[StatementTxn] = []
    bank_name: str | None = None
    statement_currency: str | None = None
    is_credit_card = False
    opening: float | None = None
    closing: float | None = None
    last_date: str | None = None
    printed = text_numbers(full_text)  # every amount printed anywhere in the PDF

    for page in pages:
        step(page.number, "Reading page %d of %d…" % (page.number, n))
        try:
            answer = llm_page(settings, page, n, last_date)
            obj: JSON = json.loads(answer)
        except (llm.LLMError, ValueError) as e:
            if isinstance(e, llm.LLMError) and e.kind in ("unreachable", "refused"):
                raise
            log.warning("page %d of a statement could not be read: %s", page.number, e)
            warnings.append("Page %d couldn't be read, so its transactions may be missing." % page.number)
            if trace is not None:
                trace.append(PageTrace(page=page.number, markdown=page.markdown, answer=None, error=str(e)))
            continue
        if trace is not None:
            trace.append(PageTrace(page=page.number, markdown=page.markdown, answer=answer, error=None))

        st: JSON = obj.get("statement") or {}
        bank_name = bank_name or (_text(st.get("bank")).strip()[:40] or None)
        is_credit_card = is_credit_card or st.get("statement_type") == "credit_card"
        statement_currency = statement_currency or (_text(st.get("currency")).upper()[:3] or None)
        if opening is None:
            opening = _num(st.get("opening_balance"))
        closing = _num(st.get("closing_balance")) if _num(st.get("closing_balance")) is not None else closing

        rows: list[JSON] = obj.get("transactions") or []
        for t in rows:
            amount = round((_num(t.get("money_in")) or 0) - (_num(t.get("money_out")) or 0), 2)
            description = re.sub(r"\s+", " ", _text(t.get("description"))).strip()[:300]
            if not amount:
                continue
            if round(abs(amount), 2) not in printed:  # the model can't add money that isn't printed
                log.warning("page %d: dropped a row whose amount %.2f isn't printed in the PDF", page.number, amount)
                warnings.append("Page %d: skipped a row that isn't on the statement (%s, %.2f)."
                                % (page.number, description[:40], amount))
                continue
            date = _iso(t.get("date")) or last_date
            if date is None:
                warnings.append("Page %d: skipped a row without a date (%s)." % (page.number, description[:40]))
                continue
            last_date = date
            balance = _num(t.get("balance"))
            if balance is not None and round(abs(balance), 2) not in printed:
                balance = None  # a balance that isn't printed was made up
            currency = _text(t.get("currency")).upper()[:3] or statement_currency or default_currency
            out.append(StatementTxn(date=date, description=description or "(no description)", amount=amount,
                                    currency=currency, balance=balance, source="llm"))

    if opening is not None and round(abs(opening), 2) not in printed:
        warnings.append("Couldn't confirm the statement's opening balance.")
        opening = None
    if closing is not None and round(abs(closing), 2) not in printed:
        warnings.append("Couldn't confirm the statement's closing balance.")
        closing = None

    check, diff = _verify(out, opening, closing, is_credit_card, warnings)
    bank = bank_name or "Bank"
    account_end = last4(full_text)
    dates = sorted(r["date"] for r in out)
    return ReadResult(bank=bank, account=bank + (" …" + account_end if account_end else ""), transactions=out,
                      period_from=dates[0] if dates else None, period_to=dates[-1] if dates else None,
                      opening=opening, closing=closing, liability=is_credit_card, balance_check=check,
                      balance_diff=diff, warnings=warnings)
