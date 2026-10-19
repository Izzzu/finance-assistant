"""Currency conversion to the main currency (CHF).

Order of preference for each transaction:
  1. same currency                          -> rate 1
  2. amount stated on the statement          -> e.g. Wise "Card transaction of 31.50 CHF"
  3. ECB euro reference rates (public file)  -> CHF cross rate for that date (or the business day before)
  4. manual rate from Settings

The ECB download is the app's only outgoing internet request. It fetches the WHOLE public
history file (no dates, amounts or anything else is sent) and caches it for a day.
Exchange rates: European Central Bank euro foreign exchange reference rates;
CHF cross rates calculated by Local Accountant.
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import re
import time
import urllib.request
import zipfile
from pathlib import Path
from sqlite3 import Connection

import db
from domain import Settings
from importer import parse_amount

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Rates = dict[datetime.date, dict[str, float]]
"""{date: {currency: units per 1 EUR}}"""


def _cache() -> Path:
    return db.data_dir() / "ecb-eurofxref-hist.csv"


def fetch_ecb() -> str:
    """Download the public ECB history file. Replaced by a stub in tests."""
    req = urllib.request.Request(ECB_URL, headers={"User-Agent": "LocalAccountant"})
    with _opener.open(req, timeout=30) as r:
        z = zipfile.ZipFile(io.BytesIO(r.read()))
    return z.read(z.namelist()[0]).decode("utf-8")


def load_rates(download: bool = True) -> Rates:
    """{date: {currency: units per 1 EUR}} or {} if unavailable."""
    path = _cache()
    fresh = path.exists() and time.time() - path.stat().st_mtime < 86400
    if download and not fresh:
        try:
            path.write_text(fetch_ecb(), encoding="utf-8")
        except Exception:
            pass  # offline: fall back to the cached copy or manual rates
    if not path.exists():
        return {}
    rates: Rates = {}
    for row in csv.DictReader(io.StringIO(path.read_text(encoding="utf-8"))):
        try:
            d = datetime.date.fromisoformat(row.pop("Date").strip())
        except (KeyError, ValueError):
            continue
        rates[d] = {k.strip(): float(v) for k, v in row.items() if k and v and v.strip() not in ("", "N/A")}
        rates[d]["EUR"] = 1.0
    return rates


def ecb_rate(rates: Rates, cur: str, main: str, date: datetime.date) -> float | None:
    """Units of `main` per 1 unit of `cur`, using the last published rate on or before `date`."""
    for back in range(8):
        r = rates.get(date - datetime.timedelta(days=back))
        if r and cur in r and main in r:
            return r[main] / r[cur]
    return None


def stated_amount(description: str | None, main: str) -> float | None:
    """'Card transaction of 31.50 CHF issued by ...' -> 31.5 (the amount in the main currency)."""
    m = re.search(r"\bof\s+([\d'’,.]+)\s+%s\b" % main, description or "", re.I)
    value = parse_amount(m.group(1)) if m else None
    return abs(value) if value else None


def convert(conn: Connection, settings: Settings) -> int:
    """Fill amount_chf for all transactions that don't have it yet. Returns #rows left without a rate."""
    main = settings.get("currency", "CHF")
    rows = conn.execute("SELECT id, date, amount, currency, description FROM transactions "
                        "WHERE amount_chf IS NULL").fetchall()
    if not rows:
        return 0
    manual: dict[str, float] = json.loads(settings.get("fx_manual") or "{}")
    need_ecb = any(r["currency"] != main for r in rows)
    rates: Rates = load_rates(download=settings.get("fx_download", "1") == "1") if need_ecb else {}
    missing = 0
    for r in rows:
        amt: float = r["amount"]
        cur: str = r["currency"]
        rate: float | None = None
        src: str | None = None
        if cur == main:
            rate, src = 1.0, "same"
        else:
            st = stated_amount(r["description"], main)
            if st:
                rate, src = st / abs(amt), "statement"
            else:
                rate = ecb_rate(rates, cur, main, datetime.date.fromisoformat(r["date"]))
                src = "ecb" if rate else None
                if rate is None and manual.get(cur):
                    rate, src = float(manual[cur]), "manual"
        if rate is None:
            missing += 1
            continue
        conn.execute("UPDATE transactions SET amount_chf=?, fx_rate=?, fx_source=? WHERE id=?",
                     (round(amt * rate, 2), round(rate, 6), src, r["id"]))
    return missing
