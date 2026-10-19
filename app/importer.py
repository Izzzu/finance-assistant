"""Parse bank statement CSV exports of (almost) any shape.

Detects encoding, delimiter, preamble/header row, which columns hold the date,
description and amount (or separate debit/credit columns), and date/number formats.
"""
from __future__ import annotations

import csv
import datetime
import io
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Sequence

from domain import CsvAnalysis, CsvMapping, ReadError, StatementTxn

DATE_FORMATS = [
    "%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d",
    "%Y.%m.%d", "%d.%m.%y", "%d/%m/%y", "%m/%d/%y", "%d %b %Y", "%d %B %Y",
    "%b %d, %Y", "%d-%b-%Y", "%d-%b-%y", "%Y%m%d",
]

KEYWORDS = {
    "date": ["transaction date", "transaktionsdatum", "data transakcji", "data operacji", "booking date",
             "buchungsdatum", "buchungstag",
             "date", "data", "datum", "fecha", "datum transakce"],
    "amount": ["amount", "kwota", "betrag", "importe", "montant", "castka", "wartosc"],
    "value_date": ["value date", "valuta", "wertstellung", "date valeur", "valeur", "value"],
    "debit": ["debit", "lastschrift", "belastung", "addebito", "withdrawal", "money out", "paid out",
              "outgoing", "obciazenia", "wyplyw", "soll", "ausgang"],
    "credit": ["credit", "gutschrift", "accredito", "deposit", "money in", "paid in", "incoming",
               "uznania", "wplyw", "haben", "eingang"],
    "currency": ["currency", "waluta", "wahrung", "devise", "moneda", "mena"],
    "description": ["description", "buchungsdetails", "avisierungstext", "text", "opis", "title", "tytul", "details", "merchant", "payee", "name",
                    "nazwa", "counterparty", "kontrahent", "odbiorca", "nadawca", "recipient",
                    "verwendungszweck", "empfanger", "beguenstigter", "auftraggeber", "narrative",
                    "reference", "memo", "concepto", "libelle", "lokalizacja", "location"],
}
NOT_AMOUNT = ["balance", "saldo", "solde", "kontostand", "po transakcji", "po operacji", "date", "datum", "valuta"]
NOT_DESC = ["number", "numer", "iban", "nr rachunku", "account no", "bic", "swift"]


def fold(s: str) -> str:
    """Lowercase and strip accents (ł -> l too) for keyword matching."""
    s = s.lower().replace("ł", "l")
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1250"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1")


def parse_amount(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip()
    for ch in ("\xa0", "\u202f", " ", "'", "\u2019", "`"):
        s = s.replace(ch, "")
    if not s:
        return None
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1]
    s = re.sub(r"[^\d,.\-+]", "", s)  # drop currency symbols / codes
    if s.endswith("-"):
        neg, s = True, s[:-1]
    if s.startswith("-"):
        neg, s = (not neg), s[1:]
    elif s.startswith("+"):
        s = s[1:]
    if not s or not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        return None
    if re.fullmatch(r"\d{1,2}[./]\d{1,2}[./]\d{2,4}", s):  # 01.09.2026 is a date, not 1'092'026
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        parts = s.split(",")
        if len(parts) > 2 or len(parts[-1]) == 3:
            s = s.replace(",", "")
        else:
            s = s.replace(",", ".")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_date(s: str | None, fmt: str) -> datetime.date | None:
    s = (s or "").strip()
    if not s:
        return None
    for cand in (s, s.split("T")[0], s.split(" ")[0]):
        try:
            return datetime.datetime.strptime(cand, fmt).date()
        except ValueError:
            continue
    return None


def looks_like_date(s: str | None) -> bool:
    s = (s or "").strip()
    if len(s) < 6 or not re.search(r"\d", s):
        return False
    return any(parse_date(s, f) for f in DATE_FORMATS)


def detect_date_format(values: Sequence[str]) -> str | None:
    values = [v for v in values if v and v.strip()]
    if not values:
        return None
    for fmt in DATE_FORMATS:
        ok = sum(1 for v in values if parse_date(v, fmt))
        if ok >= 0.9 * len(values):
            return fmt
    return None


def read_rows(text: str) -> tuple[list[list[str]], str]:
    lines = [l for l in text.splitlines() if l.strip()][:60]
    best, best_score = ",", -1
    for d in (";", ",", "\t", "|"):
        lengths = [len(r) for r in csv.reader(io.StringIO("\n".join(lines)), delimiter=d)]
        if not lengths:
            continue
        length, freq = Counter(lengths).most_common(1)[0]
        score = freq * 100 + length if length >= 3 else 0
        if score > best_score:
            best, best_score = d, score
    rows: list[list[str]] = []
    for r in csv.reader(io.StringIO(text), delimiter=best):
        r = [c.strip() for c in r]
        if any(r):
            rows.append(r)
    return rows, best


def find_header(rows: Sequence[Sequence[str]]) -> int | None:
    for i, row in enumerate(rows[:40]):
        nonempty = [c for c in row if c]
        if len(nonempty) < 3:
            continue
        if any(looks_like_date(c) for c in row):
            continue
        if sum(parse_amount(c) is not None for c in nonempty) > len(nonempty) / 3:
            continue
        nxt = rows[i + 1:i + 4]
        if nxt and any(any(looks_like_date(c) for c in r) for r in nxt):
            return i
    return None


def _find(headers: Sequence[str], keys: Sequence[str], exclude: Sequence[str] = (),
          used: Collection[int | None] = ()) -> int | None:
    for k in keys:
        for i, h in enumerate(headers):
            if i in used or any(x in h for x in exclude):
                continue
            if k in h:
                return i
    return None


def guess_mapping(headers: Sequence[str], data: Sequence[Sequence[str]]) -> CsvMapping:
    h = [fold(x) for x in headers]
    ncols = len(headers)

    def col(i: int | None) -> list[str]:
        return [r[i] if i is not None and i < len(r) else "" for r in data[:200]]

    date = _find(h, KEYWORDS["date"])
    if date is None or not detect_date_format(col(date)):
        date = next((i for i in range(ncols) if detect_date_format(col(i))), None)
    used = {date}

    currency = _find(h, KEYWORDS["currency"], used=used)
    if currency is not None:
        used.add(currency)

    # running balance column (used to verify the import), never an amount
    balance = _find(h, ["balance", "saldo", "solde", "kontostand"], used=used)
    if balance is not None:
        vals = [v for v in col(balance) if v]
        if vals and sum(parse_amount(v) is not None for v in vals) >= 0.9 * len(vals):
            used.add(balance)
        else:
            balance = None

    # value date (Valuta): fallback for rows whose booking date is empty
    value_date = _find(h, KEYWORDS["value_date"], used=used)
    if value_date is not None and detect_date_format(col(value_date)):
        used.add(value_date)
    else:
        value_date = None

    # separate Credit / Debit columns win over a generic "amount" column
    amount = None
    debit = _find(h, KEYWORDS["debit"], exclude=NOT_AMOUNT, used=used)
    credit = _find(h, KEYWORDS["credit"], exclude=NOT_AMOUNT, used=used | {debit})
    if debit is None or credit is None:
        debit = credit = None
        amount = _find(h, KEYWORDS["amount"], exclude=NOT_AMOUNT, used=used)
    if amount is None and debit is None:
        for i in range(ncols):
            if i in used or any(x in h[i] for x in NOT_AMOUNT) or detect_date_format(col(i)):
                continue
            vals = [v for v in col(i) if v]
            if vals and sum(parse_amount(v) is not None for v in vals) >= 0.9 * len(vals):
                amount = i
                break
    used |= {amount, debit, credit}

    desc = [i for i, x in enumerate(h)
            if i not in used and any(k in x for k in KEYWORDS["description"])
            and not any(n in x for n in NOT_DESC)]
    if not desc:
        lens = [(sum(len(v) for v in col(i)), i) for i in range(ncols) if i not in used]
        if lens:
            desc = [max(lens)[1]]

    return {
        "date": date,
        "date_format": detect_date_format(col(date)) if date is not None else None,
        "value_date": value_date,
        "balance": balance,
        "value_date_format": detect_date_format(col(value_date)) if value_date is not None else None,
        "description": desc,
        "amount": amount,
        "debit": debit,
        "credit": credit,
        "currency": currency,
        "invert": False,
    }


def analyze(raw: bytes) -> CsvAnalysis:
    """Return headers, data rows and a guessed column mapping for a statement file."""
    text = decode(raw)
    rows, delim = read_rows(text)
    if not rows:
        raise ReadError("The file looks empty.")
    hi = find_header(rows)
    if hi is None:
        start = next((i for i, r in enumerate(rows) if any(looks_like_date(c) for c in r)), 0)
        width = max(len(r) for r in rows[start:start + 20])
        headers = ["Column %d" % (i + 1) for i in range(width)]
        data = rows[start:]
    else:
        headers = rows[hi]
        data = rows[hi + 1:]
    data = [r for r in data if len(r) >= max(2, len(headers) // 2)]
    return {"headers": headers, "rows": data, "delimiter": delim,
            "mapping": guess_mapping(headers, data)}


def _cell(r: Sequence[str], i: int | None) -> str:
    return r[i] if i is not None and i < len(r) else ""


def build(rows: Sequence[Sequence[str]], m: CsvMapping, default_currency: str) -> tuple[list[StatementTxn], int]:
    """Turn rows into transactions using a mapping. Returns (transactions, skipped_rows)."""
    out: list[StatementTxn] = []
    skipped = 0
    fmt = m.get("date_format")
    vfmt = m.get("value_date_format")
    last_date: datetime.date | None = None
    for r in rows:
        d = parse_date(_cell(r, m.get("date")), fmt) if fmt else None
        if m.get("amount") is not None:
            amt = parse_amount(_cell(r, m["amount"]))
        else:
            deb = parse_amount(_cell(r, m.get("debit")))
            cre = parse_amount(_cell(r, m.get("credit")))
            amt = None if deb is None and cre is None else (cre or 0) - abs(deb or 0)
        desc = " ".join(_cell(r, i) for i in m.get("description") or [] if _cell(r, i))
        desc = re.sub(r"\s+", " ", desc).strip()
        # the value date (Valuta) is when the money moved and is filled on every row; the booking
        # date is often printed only once per day. Fallback: booking date, then the row above.
        vd = parse_date(_cell(r, m.get("value_date")), vfmt) if vfmt else None
        if amt:
            d = vd or d or last_date
        if d is None or amt is None or amt == 0:
            skipped += 1
            continue
        last_date = d
        if m.get("invert"):
            amt = -amt
        cur = _cell(r, m.get("currency")).upper()[:3] or default_currency
        bal = parse_amount(_cell(r, m.get("balance"))) if m.get("balance") is not None else None
        out.append(StatementTxn(date=d.isoformat(), description=desc or "(no description)", amount=round(amt, 2),
                                currency=cur, balance=bal, source="csv"))
    return out, skipped
