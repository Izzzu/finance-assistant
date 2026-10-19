"""TEST HELPER ONLY: the former rule-based statement reader (per-layout rules).

Pipeline:
  1. pdfplumber words with x/y positions -> lines (keeps the column layout)
  2. detect bank profile (PostFinance / Swisscard / Wise / generic)
  3. find the table header -> x-position of each column (credit, debit, amount, balance, ...)
  4. a transaction = an "anchor" line with an amount in an amount column, plus the following
     description lines; its date is on the anchor line or on the next line (Wise)
  5. verify: opening + movements = closing, and/or running balance continuity
  6. pages where the layout parser finds nothing but which contain amounts -> local LLM fallback
The PDF bytes are only held in memory and never written to disk.
"""
import datetime
import io
import json
import re
from statistics import median

import pdfplumber

from importer import parse_amount
from textutil import MONTHS, fold, last4

AMOUNT_RE = re.compile(r"^[-+]?\(?\d{1,3}(?:['’`]\d{3})*(?:,\d{3})*[.,]\d{2}\)?-?$|^[-+]?\d+[.,]\d{2}-?$")
CURRENCIES = {"CHF", "EUR", "USD", "GBP", "PLN", "SEK", "NOK", "DKK", "CZK", "HUF", "JPY", "CAD", "AUD"}

HEADER_ROLES = {
    "date": ["datum", "date", "data", "transaktionsdatum", "transaction date"],
    "valuta": ["valuta", "valeur", "value date", "value", "buchungsdatum", "buchung", "booking date"],
    "text": ["text", "beschreibung", "description", "details", "buchungsdetails", "testo", "libelle"],
    "credit": ["gutschrift", "gutschriften", "credit", "credits", "accredito", "accrediti", "incoming", "eingang",
               "eingange", "einzahlung", "einzahlungen"],
    "debit": ["lastschrift", "lastschriften", "belastung", "belastungen", "debit", "debits", "addebito",
              "addebiti", "outgoing", "ausgang", "ausgange", "auszahlung", "auszahlungen"],
    "amount": ["betrag", "amount", "montant", "importo"],
    "balance": ["saldo", "balance", "solde"],
}
AMOUNT_ROLES = ("credit", "debit", "amount", "balance")

OPENING_KW = ["anfangssaldo", "saldovortrag", "saldo alt", "alter saldo", "opening balance",
              "previous balance", "saldo vorherige", "saldo letzte rechnung", "solde initial",
              "solde precedent", "saldo iniziale", "saldo precedente"]
CLOSING_KW = ["schlusssaldo", "endsaldo", "saldo neu", "neuer saldo", "closing balance", "new balance",
              "solde final", "nouveau solde", "saldo finale", "nuovo saldo"]
NEUTRAL_BAL_KW = ["kontostand", "balance on", "saldo per", "saldo am", "solde au"]
# Lines that are never transactions (page furniture, subtotals).
SKIP_KW = ["ubertrag", "uebertrag", "report", "zwischentotal", "zwischensumme", "total", "summe",
           "carried forward", "brought forward", "seite", "page ", "minimum payment", "mindestzahlung",
           "zu bezahlen", "faellig", "fallig", "a reporter"]
CREDIT_WORDS = ["zahlung", "gutschrift", "payment", "credit", "rueckerstattung", "ruckerstattung",
                "refund", "remboursement", "rimborso"]

PROFILES = {
    # prefer_value_date: the Value column is filled on every row and is when the money moved
    # (for card payments: the purchase day), while the booking date is printed once per day.
    "postfinance": {"name": "PostFinance", "liability": False, "card_sign": False, "prefer_value_date": True},
    "swisscard": {"name": "Swisscard", "liability": True, "card_sign": True},
    "wise": {"name": "Wise", "liability": False, "card_sign": False},
    "generic": {"name": "Bank", "liability": False, "card_sign": False},
}


class Line:
    __slots__ = ("page", "top", "bottom", "words", "text", "ftext")

    def __init__(self, page, words):
        self.page = page
        self.words = sorted(words, key=lambda w: w["x0"])
        self.top = min(w["top"] for w in words)
        self.bottom = max(w["bottom"] for w in words)
        self.text = " ".join(w["text"] for w in self.words)
        self.ftext = fold(self.text)


# ---------- low-level helpers ----------

def _date_at(tokens, i):
    """Parse a date starting at tokens[i]. Returns (date, n_tokens) or (None, 0)."""
    t = tokens[i].rstrip(",")
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.datetime.strptime(t, fmt).date(), 1
        except ValueError:
            pass
    # "12 September 2026", "12. Sept. 2026", "Sep 12, 2026"
    if i + 2 < len(tokens):
        a, b, c = tokens[i].rstrip(".,"), fold(tokens[i + 1]).rstrip(".,"), tokens[i + 2].rstrip(",")
        if a.isdigit() and b in MONTHS and re.fullmatch(r"\d{4}", c):
            try:
                return datetime.date(int(c), MONTHS[b], int(a)), 3
            except ValueError:
                return None, 0
        a2, b2 = fold(tokens[i]).rstrip(".,"), tokens[i + 1].rstrip(",")
        if a2 in MONTHS and b2.isdigit() and re.fullmatch(r"\d{4}", c):
            try:
                return datetime.date(int(c), MONTHS[a2], int(b2)), 3
            except ValueError:
                return None, 0
    return None, 0


def _is_amount(tok):
    return bool(AMOUNT_RE.match(tok)) and parse_amount(tok) is not None


_NUM_HEAD = re.compile(r"^[-+]?\d{1,3}['’`]?$")              # "1", "-12", "1'"
_NUM_MID = re.compile(r"^['’`]?\d{3}['’`]?$")                # "234" in "1 234 567.00"
_NUM_LAST = re.compile(r"^['’`]?\d{3}[.,]\d{2}-?$")          # "200.00"


def _merge_split_numbers(words):
    """Rejoin amounts that the PDF split at the thousands separator: '1' '200.00' -> '1200.00'.

    Only merges when the pieces are very close together (a space or thin space) and the run
    ends in a decimal group, so '1 200.00' is joined but 'Seite 1 200' or '12 Sept' is not.
    """
    def close(a, b):
        return b["x0"] - a["x1"] <= 0.6 * max(a["bottom"] - a["top"], 1) and abs(b["top"] - a["top"]) <= 2

    out, i = [], 0
    while i < len(words):
        w = words[i]
        end = None
        if _NUM_HEAD.match(w["text"]):
            j = i + 1
            while j < len(words) and close(words[j - 1], words[j]):
                if _NUM_LAST.match(words[j]["text"]):
                    end = j
                    break
                if not _NUM_MID.match(words[j]["text"]):
                    break
                j += 1
        if end is None:
            out.append(w)
            i += 1
        else:
            run = words[i:end + 1]
            out.append(dict(w, text="".join(x["text"] for x in run), x1=run[-1]["x1"]))
            i = end + 1
    return out


def _lines_from_page(page, pno):
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2.5, keep_blank_chars=False)
    words.sort(key=lambda w: (round(w["top"]), w["x0"]))
    lines, cur = [], []
    for w in words:
        if cur and abs(w["top"] - cur[0]["top"]) > 2.5:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return [Line(pno, _merge_split_numbers(sorted(ws, key=lambda w: w["x0"]))) for ws in lines]


def detect_bank(text):
    t = fold(text)
    if "postfinance" in t:
        return "postfinance"
    if "swisscard" in t:
        return "swisscard"
    if "wise.com" in t or "transferwise" in t or re.search(r"\bwise (europe|payments)\b", t):
        return "wise"
    return "generic"


def _header(line):
    """If the line is a table header, return {role: (x0, x1)}."""
    roles = {}
    toks = line.words
    for i, w in enumerate(toks):
        f = fold(w["text"]).strip(":()[]")
        two = (f + " " + fold(toks[i + 1]["text"])) if i + 1 < len(toks) else ""
        for role, kws in HEADER_ROLES.items():
            if role in roles:
                continue
            if f in kws or two in kws:
                x1 = toks[i + 1]["x1"] if two in kws else w["x1"]
                roles[role] = (w["x0"], x1)
                break
    amount_like = sum(r in roles for r in AMOUNT_ROLES)
    if len(roles) >= 3 and amount_like >= 1 and not any(_is_amount(w["text"]) for w in toks):
        return roles
    return None


def _role_of(word, header):
    """Which amount column a number belongs to.

    Amounts are right-aligned, so we look at the number's right edge. A column runs from where
    its header starts to where the next amount header starts. This works whether headers are
    left-aligned ("Credit" above the left part of the column) or right-aligned ("Gutschrift"
    ending where the numbers end). Numbers left of the first amount column belong to none.
    """
    cols = sorted((header[r][0], r) for r in AMOUNT_ROLES if r in header)
    if not cols:
        return None
    x1 = word["x1"]
    if x1 < cols[0][0] - 15:
        return None
    for i, (start, role) in enumerate(cols):
        end = cols[i + 1][0] if i + 1 < len(cols) else 1e9
        if x1 < end:
            return role
    return cols[-1][1]


def _balance_line_value(line):
    nums = [w for w in line.words if _is_amount(w["text"])]
    return parse_amount(nums[-1]["text"]) if nums else None


# ---------- main parser ----------

def parse_pdf(data, llm_fallback=None, default_currency="CHF", trace=None):
    """Parse statement bytes. Returns dict with bank, account, transactions, check, warnings.

    llm_fallback(page_text) -> list of {date, description, amount} is used for pages the
    layout parser cannot read; every amount it returns must literally occur on the page.
    trace: optional list; receives (line, decision, detail) for every line (see read_pdf.py).
    """
    def note(line, decision, detail=""):
        if trace is not None:
            trace.append((line, decision, detail))

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        pages = [_lines_from_page(p, i) for i, p in enumerate(pdf.pages)]
        page_widths = [p.width for p in pdf.pages]
    all_lines = [l for p in pages for l in p]
    full_text = "\n".join(l.text for l in all_lines)
    if not all_lines:
        return {"error": "This PDF has no text layer (scanned image). OCR is not enabled yet."}

    letterhead = []  # lines above the first table header: where the bank names itself
    for l in pages[0]:
        if _header(l):
            break
        letterhead.append(l.text)
    head = "\n".join(letterhead[:15])
    bank = detect_bank(head)
    if bank == "generic":
        bank = detect_bank(full_text)
    prof = PROFILES[bank]
    account = prof["name"] + (" …" + last4(full_text) if last4(full_text) else "")
    heights = [l.bottom - l.top for l in all_lines] or [10]
    lh = median(heights)

    warnings = []
    opening = closing = None
    neutral = []
    txns = []
    header = None
    currency = default_currency
    pages_with_rows = set()

    for pno, lines in enumerate(pages):
        cur = None  # transaction being built
        prev_bottom = None
        for li, line in enumerate(lines):
            h = _header(line)
            if h:
                header = h
                cur = None
                note(line, "HEADER", ", ".join("%s@%d-%d" % (r, x0, x1) for r, (x0, x1) in h.items()))
                continue
            f = line.ftext
            # section currency, e.g. "EUR statement", "Kontoauszug in CHF", "Währung CHF"
            codes = [w["text"] for w in line.words if w["text"] in CURRENCIES]
            if codes and not any(_is_amount(w["text"]) for w in line.words) and re.search(
                    r"statement|balance|konto|auszug|wahrung|currency|devise|valuta|account", f):
                currency = codes[0]
                note(line, "CURRENCY", currency)
                continue
            # balances (never transactions)
            if any(k in f for k in OPENING_KW):
                v = _balance_line_value(line)
                if v is not None and opening is None:
                    opening = v
                cur = None
                note(line, "OPENING", "" if v is None else "%.2f" % v)
                continue
            if any(k in f for k in CLOSING_KW):
                v = _balance_line_value(line)
                if v is not None:
                    closing = v
                cur = None
                note(line, "CLOSING", "" if v is None else "%.2f" % v)
                continue
            if any(k in f for k in NEUTRAL_BAL_KW):
                v = _balance_line_value(line)
                if v is not None:
                    neutral.append(v)
                cur = None
                note(line, "BALANCE", "" if v is None else "%.2f" % v)
                continue
            toks = [w["text"] for w in line.words]
            d, nd = _date_at(toks, 0) if toks else (None, 0)
            # page furniture / subtotals; a dated line is a real row ("TotalEnergies" is fuel)
            if not nd and any(re.search(r"(^|\s)" + re.escape(k.strip()) + r"(?![a-z])", f) for k in SKIP_KW):
                cur = None
                note(line, "SKIP", "page furniture / subtotal")
                continue
            nums, ignored = [], []
            for i, w in enumerate(line.words):
                if i < nd or not _is_amount(w["text"]):
                    continue
                if header:
                    role = _role_of(w, header)
                    if role is None:
                        ignored.append("%s@%d: left of the amount columns" % (w["text"], w["x0"]))
                        continue
                else:
                    role = None
                    if w["x0"] < page_widths[pno] * 0.40:  # no header: amounts live in the right part
                        ignored.append("%s@%d: left of 40%% page width (no table header found)" % (w["text"], w["x0"]))
                        continue
                nums.append((w, role))

            if nums:  # anchor line -> new transaction
                desc_words = []
                for i, w in enumerate(line.words):
                    if i < nd or any(w is n[0] for n in nums):
                        continue
                    if w["text"] in CURRENCIES and w["x0"] >= page_widths[pno] * 0.40:
                        continue
                    if header and w["x0"] > min(
                            (header[r][0] for r in AMOUNT_ROLES if r in header), default=1e9) - 5:
                        continue  # valuta dates etc. right of the description column
                    if _date_at([w["text"]], 0)[0] and i > 0:
                        continue
                    desc_words.append(w["text"])
                # value date (Valuta): the right-most other date on the line. Some statements leave the
                # booking date empty (e.g. on incoming payments) and only print the value date.
                vstart = header["valuta"][0] - 20 if header and "valuta" in header else 0
                vdates = [_date_at([w["text"]], 0)[0] for i, w in enumerate(line.words)
                          if i >= nd and w["x0"] >= vstart]
                vdate = next((x for x in reversed(vdates) if x), None)
                cur = {"date": d, "value_date": vdate, "desc": [" ".join(desc_words)], "nums": nums,
                       "currency": currency, "page": pno, "line": line.text}
                txns.append(cur)
                pages_with_rows.add(pno)
                note(line, "ROW", "amounts " + ", ".join("%s=%s" % (r or "?", w["text"]) for w, r in nums)
                     + ("" if d else " (no date at start of line: value date, the next line or the row above is used)")
                     + ("; ignored " + "; ".join(ignored) if ignored else ""))
            elif cur is not None and prev_bottom is not None and line.top - prev_bottom < 1.6 * lh:
                text = line.text
                if cur["date"] is None and nd:
                    cur["date"] = d
                    text = " ".join(toks[nd:])
                if text.strip():
                    cur["desc"].append(text.strip())
                note(line, "CONT", "description of the row above" + ("; ignored " + "; ".join(ignored) if ignored else ""))
            else:
                cur = None
                note(line, "IGNORED" if ignored else "-", "; ".join(ignored) if ignored else
                     ("no amount in an amount column" if nd else ""))
            prev_bottom = line.bottom if cur is not None else None

    # ---------- turn raw rows into signed amounts ----------
    out = []
    has_explicit_sign = any(n[0]["text"].startswith("-") or n[0]["text"].endswith("-")
                            for t in txns for n in t["nums"])
    last_date = None
    for t in txns:
        if prof.get("prefer_value_date") and t["value_date"]:
            t["date"] = t["value_date"]
        if t["date"] is None:
            # no booking date: use the value date, else the date of the row above (banks often print
            # the date only on the first row of each day)
            t["date"] = t["value_date"] or last_date
        if t["date"] is None:
            warnings.append("Skipped a row without a date: %s" % t["line"][:80])
            continue
        last_date = t["date"]
        vals = {}
        for w, role in t["nums"]:
            vals.setdefault(role, parse_amount(w["text"]))
        bal = vals.pop("balance", None)
        if None in vals and bal is None and len(t["nums"]) >= 2:
            # no header: the right-most number is the running balance
            bal = parse_amount(t["nums"][-1][0]["text"])
            vals[None] = parse_amount(t["nums"][0][0]["text"])
        if "credit" in vals or "debit" in vals:
            amt = abs(vals.get("credit") or 0) - abs(vals.get("debit") or 0)
        else:
            amt = vals.get("amount", vals.get(None))
        if amt is None:
            continue
        desc = re.sub(r"\bTransaction:\s*\S+", "", " ".join(t["desc"]))
        desc = re.sub(r"\s+", " ", desc).strip(" -")
        if prof["card_sign"] and "credit" not in vals and "debit" not in vals:
            # credit cards list purchases as positive numbers; credits carry a minus or credit words
            if amt < 0 or any(w in fold(desc) for w in CREDIT_WORDS):
                amt = abs(amt)
            else:
                amt = -abs(amt)
        out.append({"date": t["date"].isoformat(), "description": desc or "(no description)",
                    "amount": round(amt, 2), "currency": t["currency"], "balance": bal,
                    "page": t["page"], "source": "layout",
                    "_unsigned": not has_explicit_sign and "credit" not in vals and "debit" not in vals
                    and not prof["card_sign"]})

    _fix_signs_from_balances(out)

    # ---------- LLM fallback for unreadable pages ----------
    for pno, lines in enumerate(pages):
        if pno in pages_with_rows:
            continue
        n_amount_lines = sum(1 for l in lines if any(_is_amount(w["text"]) for w in l.words))
        if n_amount_lines >= 3:
            page_text = "\n".join(l.text for l in lines)
            if llm_fallback is None:
                warnings.append("Page %d has amounts but no readable table; it was not imported." % (pno + 1))
                continue
            try:
                rows = llm_fallback(page_text)
            except Exception as e:
                warnings.append("Page %d: local LLM fallback failed (%s)." % (pno + 1, e))
                continue
            for r in rows:
                ok = _amount_on_page(r.get("amount"), page_text)
                d = _parse_iso(r.get("date"))
                if not ok or d is None:
                    warnings.append("Page %d: discarded an LLM row that is not on the page." % (pno + 1))
                    continue
                out.append({"date": d.isoformat(), "description": str(r.get("description", ""))[:300],
                            "amount": round(float(r["amount"]), 2), "currency": currency,
                            "balance": None, "page": pno, "source": "llm", "_unsigned": False})

    if opening is None and len(neutral) >= 2:
        opening = neutral[0]
    if closing is None and len(neutral) >= 2:
        closing = neutral[-1]
    check, diff = _verify(out, opening, closing, prof["liability"], warnings)
    for r in out:
        r.pop("_unsigned", None)
    dates = sorted(r["date"] for r in out)
    return {"bank": prof["name"], "account": account, "transactions": out,
            "period_from": dates[0] if dates else None, "period_to": dates[-1] if dates else None,
            "opening": opening, "closing": closing, "balance_check": check, "balance_diff": diff,
            "liability": prof["liability"], "warnings": warnings}


def _parse_iso(s):
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def _amount_on_page(amount, page_text):
    try:
        a = abs(float(amount))
    except (TypeError, ValueError):
        return False
    for tok in re.findall(r"[\d'’`.,]+", page_text):
        v = parse_amount(tok)
        if v is not None and abs(abs(v) - a) < 0.005:
            return True
    return False


def _fix_signs_from_balances(rows):
    """Unsigned amounts (no +/- and no credit/debit columns): take the sign from balance changes."""
    groups = {}
    for r in rows:
        groups.setdefault(r["currency"], []).append(r)
    for g in groups.values():
        for order in (g, list(reversed(g))):
            prev = None
            for r in order:
                if prev is not None and r["_unsigned"] and r["balance"] is not None and prev["balance"] is not None:
                    delta = round(r["balance"] - prev["balance"], 2)
                    if abs(abs(delta) - abs(r["amount"])) < 0.01:
                        r["amount"] = delta
                        r["_unsigned"] = False
                prev = r


def _continuity(rows):
    """Number of rows whose running balance doesn't follow from the previous row (best direction)."""
    best = None
    for order in (rows, list(reversed(rows))):
        bad, prev, checked = 0, None, 0
        for r in order:
            if r["balance"] is None:
                continue
            if prev is not None:
                checked += 1
                if abs(prev + r["amount"] - r["balance"]) > 0.01:
                    bad += 1
            prev = r["balance"]
        if checked and (best is None or bad < best[0]):
            best = (bad, checked)
    return best


def _verify(rows, opening, closing, liability, warnings):
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
    by_cur = {}
    for r in rows:
        by_cur.setdefault(r["currency"], []).append(r)
    results = [c for c in (_continuity(g) for g in by_cur.values()) if c]
    if results:
        bad = sum(b for b, _ in results)
        if bad == 0:
            return "ok", 0.0
        warnings.append("%d row(s) don't match the running balance; a row may be missing or misread." % bad)
        return "diff", None
    return "none", None


# ---------- LLM fallback ----------

FALLBACK_SCHEMA = {
    "type": "object",
    "properties": {"rows": {"type": "array", "items": {
        "type": "object",
        "properties": {"date": {"type": "string"}, "description": {"type": "string"},
                       "amount": {"type": "number"}},
        "required": ["date", "description", "amount"]}}},
    "required": ["rows"],
}


def make_llm_fallback(settings):
    import llm

    def fallback(page_text):
        msgs = [
            {"role": "system", "content":
                "You extract bank statement transactions from page text. Return JSON only. "
                "Dates as YYYY-MM-DD. Amount negative for money going out, positive for money in. "
                "Copy amounts exactly as printed. Skip balances, totals and headers. "
                "If there are no transactions, return an empty list."},
            {"role": "user", "content": page_text[:12000]},
        ]
        return llm.chat_json(settings, msgs, FALLBACK_SCHEMA, timeout=300).get("rows", [])

    return fallback
