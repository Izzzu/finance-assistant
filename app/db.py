"""SQLite storage. Everything lives in data/finance.db on this machine only."""
from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from domain import Category, Settings

ROOT = Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    return Path(os.environ.get("FINANCE_DATA_DIR") or ROOT / "data")


def db_path() -> Path:
    return data_dir() / "finance.db"


# (name, kind, period, description) - the description is given to the local LLM.
# period decides the report: "monthly" or "yearly". Income/transfer categories are not spending.
DEFAULT_CATEGORIES = [
    ("Grocery", "expense", "monthly", "Supermarkets and food stores (Migros, Coop, Denner, Aldi, Lidl)"),
    ("Eating Out", "expense", "monthly", "Restaurants, cafes, bars, take-away, food delivery (e.g. Tibits, Hiltl, Starbucks, McDonald's, Uber Eats)"),
    ("Entertainment", "expense", "monthly", "Escape rooms, cinema (Pathé, Kitag), concerts, events, museums, games"),
    ("Transport", "expense", "monthly", "Public transport (SBB, ZVV, BLS, tickets), car fuel (Shell, BP, TotalEnergies), parking, taxi, Uber, car service"),
    ("Online shopping", "expense", "monthly", "Online stores such as Amazon, Galaxus, Zalando, AliExpress"),
    ("Other shopping", "expense", "monthly", "Any in-store shopping not covered by another category"),
    ("Travel", "expense", "monthly", "Flights, train tickets abroad, hotels, Airbnb, holiday bookings"),
    ("Beauty", "expense", "monthly", "Beauty and cosmetic salons, nails, hairdresser"),
    ("Bills", "expense", "monthly", "Recurring monthly payments: rent, health insurance (CSS, Helsana, Swica, Sanitas), mobile/phone/internet (Swisscom, Salt, Sunrise), electricity, streaming and app subscriptions (Netflix, Spotify, iCloud)"),
    ("Sport", "expense", "monthly", "Sport classes, gym memberships"),
    ("House", "expense", "monthly", "House maintenance, renovations, repairs, craftsmen, DIY/hardware stores, cleaning lady"),
    ("Pets", "expense", "monthly", "Anything for pets: pet food, pet shops (e.g. Fressnapf), vet, cat sitter"),
    ("Health", "expense", "monthly", "Doctor and dentist invoices, pharmacy, therapy, glasses (health insurance premiums go to Bills)"),
    ("Yearly spendings", "expense", "yearly", "Bills paid once a year: taxes, car insurance, house insurance, other yearly bills"),
    ("Salary", "income", "monthly", "Regular salary or wage from an employer (Lohn, Gehalt, salaire), including the 13th month salary"),
    ("Bonus", "income", "monthly", "Bonus payments, profit share, performance or sign-on bonus from an employer"),
    ("Other income", "income", "monthly", "Any other money received that is not salary, a bonus, a refund or a transfer between own accounts: side income, interest, dividends, money from friends or family, government payments"),
    ("Refunds", "income", "monthly", "Refunds and returns from merchants"),
    ("Transfer between my accounts", "transfer", "monthly", "Moving money between own accounts, credit card bill payments, Wise top-ups, currency conversions, savings"),
]

TRANSFER = "Transfer between my accounts"

# Patterns (normalized words) that are transfers between your own accounts.
DEFAULT_RULES = [
    ("swisscard", TRANSFER),
    ("ihre zahlung", TRANSFER),
    ("zahlung danke", TRANSFER),
    ("payment thank you", TRANSFER),
    ("topped up", TRANSFER),
    ("converted", TRANSFER),
    ("wise europe", TRANSFER),
    ("wise payments", TRANSFER),
]

# Starter memory: well-known Swiss merchants. Shown and deletable under Settings -> Remembered rules.
STARTER_RULES = [
    ("migros", "Grocery"), ("coop", "Grocery"), ("denner", "Grocery"), ("aldi", "Grocery"), ("lidl", "Grocery"),
    ("migrolino", "Grocery"), ("coop pronto", "Grocery"), ("volg", "Grocery"), ("spar", "Grocery"),
    ("galaxus", "Online shopping"), ("digitec", "Online shopping"), ("zalando", "Online shopping"),
    ("aliexpress", "Online shopping"), ("brack", "Online shopping"),
    ("ikea", "House"), ("jumbo", "House"), ("hornbach", "House"), ("bauhaus", "House"), ("obi", "House"),
    ("netflix", "Bills"), ("spotify", "Bills"), ("swisscom", "Bills"), ("salt mobile", "Bills"),
    ("sunrise", "Bills"), ("helsana", "Bills"), ("css versicherung", "Bills"), ("sanitas", "Bills"),
    ("swica", "Bills"), ("serafe", "Yearly spendings"), ("steueramt", "Yearly spendings"),
    ("sbb", "Transport"), ("zvv", "Transport"), ("bls", "Transport"), ("mobility", "Transport"),
    ("fielmann", "Health"), ("apotheke", "Health"), ("amavita", "Health"), ("sunstore", "Health"),
    ("fressnapf", "Pets"), ("qualipet", "Pets"), ("fitnesspark", "Sport"), ("ticketcorner", "Entertainment"),
    ("pathe", "Entertainment"),
]

DEFAULT_SETTINGS = {
    "llm_url": "http://127.0.0.1:11434",
    "llm_model": "qwen3:8b",      # categorizing, notes, Q&A
    "parser_model": "qwen3:8b",   # turning statement PDFs into transactions
    "confidence_threshold": "0.8",
    "currency": "CHF",
    "fx_download": "1",
    "fx_manual": "{}",
    "double_check": "1",  # ask the model twice; disagreement -> To review   # {"EUR": 0.94} CHF per 1 unit, used when ECB rates are off/missing
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL DEFAULT 'expense',    -- expense | income | transfer
  period TEXT NOT NULL DEFAULT 'monthly',  -- monthly | yearly: which report it belongs to
  description TEXT                         -- what belongs here; guides the local LLM
);
CREATE TABLE IF NOT EXISTS imports(
  id INTEGER PRIMARY KEY,
  filename TEXT, bank TEXT, account TEXT,
  period_from TEXT, period_to TEXT,
  balance_check TEXT,          -- ok | diff | none
  balance_diff REAL,
  opening REAL, closing REAL,  -- statement balances, kept so edits can be re-checked
  liability INTEGER DEFAULT 0, -- 1 = credit card (balance is what you owe)
  warnings TEXT,               -- JSON list of messages
  imported_at TEXT,
  added INTEGER, skipped INTEGER
);
CREATE TABLE IF NOT EXISTS transactions(
  id INTEGER PRIMARY KEY,
  import_id INTEGER REFERENCES imports(id) ON DELETE CASCADE,
  account TEXT,
  date TEXT NOT NULL,              -- YYYY-MM-DD
  description TEXT NOT NULL,
  amount REAL NOT NULL,            -- negative = money out, in `currency`
  currency TEXT NOT NULL,
  amount_chf REAL,                 -- converted to the main currency (NULL = no rate yet)
  fx_rate REAL,
  fx_source TEXT,                  -- same | statement | ecb | manual
  norm TEXT,                       -- normalized description used for matching
  merchant_key TEXT,               -- groups similar transactions for review
  merchant TEXT,                   -- clean merchant name (from the local LLM)
  category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'new',  -- new | rule | llm | review | user
  suggestion_id INTEGER,           -- LLM suggestion when it was not confident
  confidence REAL,
  source TEXT DEFAULT 'layout',    -- layout | llm | csv: how the row was extracted
  balance REAL,                    -- running balance printed on the statement (for re-checks)
  share REAL DEFAULT 1,            -- your part of the cost, e.g. 0.5 when split half with someone
  orig_amount REAL,                -- amount as first read from the statement, if you corrected it
  edited INTEGER DEFAULT 0,        -- 1 = date/description/amount corrected by you
  hash TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_txn_date ON transactions(date);
CREATE INDEX IF NOT EXISTS ix_txn_status ON transactions(status);
CREATE INDEX IF NOT EXISTS ix_txn_key ON transactions(merchant_key);
CREATE TABLE IF NOT EXISTS rules(
  id INTEGER PRIMARY KEY,
  pattern TEXT UNIQUE NOT NULL,    -- normalized words; matches whole words in a description
  category_id INTEGER NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS llm_cache(
  key TEXT PRIMARY KEY, merchant TEXT, category_id INTEGER, confidence REAL
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db() -> Generator[sqlite3.Connection, None, None]:
    conn = connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _owner_only(path: Path, is_dir: bool) -> None:
    """chmod 700/600 on macOS/Linux. On Windows, user profile folders are already per-user."""
    if os.name == "nt":
        return
    os.chmod(str(path), stat.S_IRWXU if is_dir else stat.S_IRUSR | stat.S_IWUSR)


def init() -> None:
    d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    _owner_only(d, True)
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        if not c.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
            c.executemany("INSERT INTO categories(name, kind, period, description) VALUES(?,?,?,?)",
                          DEFAULT_CATEGORIES)
            for pattern, cat in DEFAULT_RULES + STARTER_RULES:
                c.execute("INSERT OR IGNORE INTO rules(pattern, category_id, created_at) "
                          "SELECT ?, id, datetime('now') FROM categories WHERE name=?", (pattern, cat))
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (k, v))
        _migrate(c)
    _owner_only(db_path(), False)


def _add_column(c: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if column not in {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}:
        c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, decl))


def _migrate(c: sqlite3.Connection) -> None:
    """One-time upgrades of existing databases. Each step runs once and is recorded in settings."""
    for table, column, decl in [("imports", "opening", "REAL"), ("imports", "closing", "REAL"),
                                ("imports", "liability", "INTEGER DEFAULT 0"),
                                ("transactions", "orig_amount", "REAL"), ("transactions", "balance", "REAL"),
                                ("transactions", "share", "REAL DEFAULT 1"),
                                ("transactions", "edited", "INTEGER DEFAULT 0")]:
        _add_column(c, table, column, decl)
    done = {r["key"] for r in c.execute("SELECT key FROM settings WHERE key LIKE 'migration:%'")}
    if "migration:income-split" not in done:
        # 'Income' becomes 'Salary' (same id, so categorized transactions and rules follow);
        # 'Bonus' and 'Other income' are added.
        salary = next(d for n, _, _, d in DEFAULT_CATEGORIES if n == "Salary")
        if c.execute("SELECT 1 FROM categories WHERE name='Income' AND kind='income'").fetchone() and \
                not c.execute("SELECT 1 FROM categories WHERE name='Salary'").fetchone():
            c.execute("UPDATE categories SET name='Salary', description=? WHERE name='Income'", (salary,))
        for name, kind, period, desc in DEFAULT_CATEGORIES:
            if name in ("Salary", "Bonus", "Other income"):
                c.execute("INSERT OR IGNORE INTO categories(name, kind, period, description) VALUES(?,?,?,?)",
                          (name, kind, period, desc))
        c.execute("DELETE FROM llm_cache")  # let the AI choose among the new categories
        c.execute("INSERT INTO settings(key, value) VALUES('migration:income-split', datetime('now'))")
    if "migration:keys-v2" not in done:
        # merchant keys now ignore payment boilerplate ("TWINT RECEIVE MONEY FROM ... MOBILE NO."),
        # so each person/shop gets its own key. Recompute for stored rows; amounts are untouched.
        from textutil import merchant_key, normalize
        for r in c.execute("SELECT id, description FROM transactions").fetchall():
            n = normalize(r["description"])
            c.execute("UPDATE transactions SET norm=?, merchant_key=? WHERE id=?", (n, merchant_key(n), r["id"]))
        c.execute("DELETE FROM llm_cache")
        c.execute("INSERT INTO settings(key, value) VALUES('migration:keys-v2', datetime('now'))")


def get_settings(conn: sqlite3.Connection) -> Settings:
    return {str(r["key"]): str(r["value"]) for r in conn.execute("SELECT key, value FROM settings")}


def categories(conn: sqlite3.Connection) -> list[Category]:
    return [Category(id=r["id"], name=r["name"], kind=r["kind"], period=r["period"], description=r["description"])
            for r in conn.execute(
        "SELECT id, name, kind, period, description FROM categories ORDER BY "
        "CASE kind WHEN 'expense' THEN 0 WHEN 'income' THEN 1 ELSE 2 END, "
        "CASE period WHEN 'yearly' THEN 1 ELSE 0 END, id")]


def category_id(conn: sqlite3.Connection, name: str) -> int | None:
    r = conn.execute("SELECT id FROM categories WHERE name=?", (name,)).fetchone()
    return r["id"] if r else None
