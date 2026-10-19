"""Shared types used across the app.

Values that go out as JSON to the browser are built from these TypedDicts; the database layer
returns sqlite3.Row objects, which are read field by field into these types.
"""
from __future__ import annotations

from typing import Any, Literal

from typing_extensions import TypedDict  # pydantic needs this TypedDict on Python < 3.12

Settings = dict[str, str]
"""App settings as stored in the `settings` table (all values are strings)."""

JSON = dict[str, Any]
"""A JSON object as sent to / received from the browser or the local model."""

BalanceCheck = Literal["ok", "diff", "none"]


class ReadError(ValueError):
    """A problem with a statement file. Its message is written for the user and shown in the UI."""


class StatementTxn(TypedDict):
    """One transaction as read from a statement file, before it is stored."""
    date: str              # YYYY-MM-DD
    description: str
    amount: float          # negative = money out
    currency: str
    balance: float | None  # running balance printed on that row
    source: str            # llm | csv


class ReadResult(TypedDict):
    """What a statement reader (PDF or CSV) returns."""
    bank: str
    account: str
    transactions: list[StatementTxn]
    period_from: str | None
    period_to: str | None
    opening: float | None
    closing: float | None
    liability: bool        # credit card: the balance is what you owe
    balance_check: BalanceCheck
    balance_diff: float | None
    warnings: list[str]


class FileStatus(TypedDict, total=False):
    """Per-file status of a scan job, shown on the Upload screen."""
    name: str
    status: str            # waiting | reading | done | error
    detail: str | None
    error: str
    import_id: int | None
    bank: str
    account: str
    period_from: str | None
    period_to: str | None
    balance_check: BalanceCheck
    balance_diff: float | None
    warnings: list[str]
    added: int
    duplicates: int


class IngestResult(TypedDict):
    """Result of storing one statement file (see pipeline.ingest)."""
    import_id: int | None
    bank: str
    account: str
    period_from: str | None
    period_to: str | None
    balance_check: BalanceCheck
    balance_diff: float | None
    warnings: list[str]
    added: int
    duplicates: int
    detail: str | None


class CsvMapping(TypedDict):
    """Which CSV column holds what (column indexes, or None)."""
    date: int | None
    date_format: str | None
    value_date: int | None
    value_date_format: str | None
    balance: int | None
    description: list[int]
    amount: int | None
    debit: int | None
    credit: int | None
    currency: int | None
    invert: bool


class CsvAnalysis(TypedDict):
    headers: list[str]
    rows: list[list[str]]
    delimiter: str
    mapping: CsvMapping


class TxnSnapshot(TypedDict):
    """A transaction's categorization before a change (for Undo)."""
    id: int
    category_id: int | None
    status: str
    suggestion_id: int | None
    confidence: float | None


class RuleUndo(TypedDict):
    id: int
    prev_category_id: int | None


class UndoToken(TypedDict):
    txns: list[TxnSnapshot]
    rule: RuleUndo | None
    not_remembered: bool


class Category(TypedDict):
    id: int
    name: str
    kind: str              # expense | income | transfer
    period: str            # monthly | yearly
    description: str | None
