"""Categorization: your rules -> transfer pairing -> local LLM -> To review.

Statuses: new -> rule | llm | review -> user.
Rules and the LLM never touch transactions you categorized yourself (status 'user').
"""
from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Collection, Iterable, Sequence
from sqlite3 import Connection, Row
from typing import TypedDict

import db
import llm
from domain import JSON, Category, RuleUndo, Settings, TxnSnapshot, UndoToken
from textutil import is_generic, matches, merchant_key, normalize

BATCH = 15
class RunSummary(TypedDict):
    transfer_pairs: int
    llm_error: str | None


class LLMItem(TypedDict):
    """One merchant group sent to the model."""
    id: int
    description: str
    amount: float
    currency: str


ProgressFn = Callable[[int, int], None]
"""progress(done, total)"""

PERSON_HINTS = ("twint", "sent money", "received money", "zahlung an", "zahlung von")


# ---------------------------------------------------------------- preparation and rules

def prepare(conn: Connection) -> None:
    for r in conn.execute("SELECT id, description FROM transactions WHERE norm IS NULL").fetchall():
        n = normalize(r["description"])
        conn.execute("UPDATE transactions SET norm=?, merchant_key=? WHERE id=?", (n, merchant_key(n), r["id"]))


def rules(conn: Connection) -> list[Row]:
    rows: list[Row] = conn.execute("SELECT id, pattern, category_id FROM rules").fetchall()
    return sorted(rows, key=lambda r: -len(str(r["pattern"])))


def match_rule(norm: str, rule_list: Iterable[Row]) -> Row | None:
    for r in rule_list:  # longest pattern first
        if matches(r["pattern"], norm):
            return r
    return None


def apply_rules(conn: Connection, statuses: Sequence[str] = ("new", "review", "llm", "rule"),
                only_pattern: str | None = None) -> list[int]:
    """Re-apply rules to all non-user transactions (optionally only those matching one pattern)."""
    rl = rules(conn)
    q = "SELECT id, norm FROM transactions WHERE status IN (%s)" % ",".join("?" * len(statuses))
    changed: list[int] = []
    for t in conn.execute(q, statuses).fetchall():
        if only_pattern and not matches(only_pattern, t["norm"] or ""):
            continue
        r = match_rule(t["norm"] or "", rl)
        if r:
            conn.execute("UPDATE transactions SET category_id=?, status='rule', suggestion_id=NULL, "
                         "confidence=NULL WHERE id=?", (r["category_id"], t["id"]))
            changed.append(t["id"])
    return changed


# ---------------------------------------------------------------- transfers between own accounts

def pair_transfers(conn: Connection) -> int:
    """Same amount, opposite sign, different accounts, within 5 days -> suggest 'Transfer'."""
    tid = db.category_id(conn, db.TRANSFER)
    rows = conn.execute("SELECT id, account, date, amount, currency, amount_chf FROM transactions "
                        "WHERE status='new' AND amount_chf IS NOT NULL ORDER BY date").fetchall()
    used: set[int] = set()
    pairs = 0
    for a in rows:
        if a["id"] in used or a["amount_chf"] >= 0:
            continue
        da = datetime.date.fromisoformat(a["date"])
        for b in rows:
            if b["id"] in used or b["account"] == a["account"] or b["amount_chf"] <= 0:
                continue
            if a["currency"] == b["currency"]:
                if abs(abs(a["amount"]) - b["amount"]) > 0.005:  # same currency: must be exact
                    continue
            elif abs(abs(a["amount_chf"]) - b["amount_chf"]) > 0.015 * b["amount_chf"]:
                continue  # different currencies: allow for exchange-rate differences
            if abs((datetime.date.fromisoformat(b["date"]) - da).days) > 5:
                continue
            for x in (a["id"], b["id"]):
                conn.execute("UPDATE transactions SET status='review', suggestion_id=?, confidence=0.9 "
                             "WHERE id=?", (tid, x))
            used |= {a["id"], b["id"]}
            pairs += 1
            break
    return pairs


# ---------------------------------------------------------------- local LLM

def _schema(names: list[str]) -> JSON:
    return {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["id", "merchant", "category", "confidence"],
        "properties": {"id": {"type": "integer"}, "merchant": {"type": "string"},
                       "category": {"type": "string", "enum": names},
                       "confidence": {"type": "number"}}}}}}


SYSTEM = (
    "You are a careful personal accountant in Switzerland. Assign each bank transaction to exactly one "
    "category from the given list, using the category descriptions.\n"
    "- amount < 0 is money spent, amount > 0 is money received. Income categories (Salary, Bonus, Other income) and 'Refunds' only for money received.\n"
    "- merchant: short clean name of the shop, company or person (e.g. 'Migros', 'Galaxus', 'Anna Müller'), "
    "without city, card numbers or reference numbers.\n"
    "- confidence from 0 to 1. Use 0.9 or more only when the merchant is well known and the category is "
    "obvious. Use 0.5 or less for payments to/from private persons (TWINT, 'sent money'), unknown names, "
    "and shops that sell many different kinds of things.\n"
    "- Never invent a category. Answer with JSON only."
)


def _ask_llm(settings: Settings, cats: list[Category], examples: list[dict[str, str]],
             items: list[LLMItem]) -> list[JSON]:
    payload: JSON = {
        "categories": [{"name": c["name"], "description": c["description"]} for c in cats],
        "examples_from_user": examples,
        "transactions": items,
    }
    msgs: list[llm.Message] = [{"role": "system", "content": SYSTEM},
                               {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    items_out: list[JSON] = llm.chat_json(settings, msgs, _schema([c["name"] for c in cats])).get("items", [])
    return items_out


def llm_categorize(conn: Connection, settings: Settings, progress: ProgressFn | None = None) -> None:
    cats = db.categories(conn)
    by_name = {c["name"]: c for c in cats}
    threshold = float(settings.get("confidence_threshold", "0.8"))
    examples = [{"text": r["pattern"], "category": r["name"]} for r in conn.execute(
        "SELECT r.pattern, c.name FROM rules r JOIN categories c ON c.id=r.category_id "
        "ORDER BY r.id DESC LIMIT 40")]

    rows = conn.execute("SELECT id, description, amount, amount_chf, currency, norm, merchant_key "
                        "FROM transactions WHERE status='new'").fetchall()
    groups: dict[str, list[Row]] = {}
    for r in rows:
        key = "%s|%s" % (r["merchant_key"], "in" if r["amount"] > 0 else "out")
        groups.setdefault(key, []).append(r)

    todo: list[tuple[str, list[Row]]] = []
    for key, g in groups.items():
        c = conn.execute("SELECT merchant, category_id, confidence FROM llm_cache WHERE key=?", (key,)).fetchone()
        if c:
            _apply(conn, g, c["merchant"], c["category_id"], c["confidence"], threshold, by_name)
        else:
            todo.append((key, g))

    done = 0
    total = len(todo)
    if progress:
        progress(0, total)
    for start in range(0, total, BATCH):
        chunk = todo[start:start + BATCH]
        items = [LLMItem(id=i, description=str(g[0]["description"])[:200],
                         amount=g[0]["amount_chf"] if g[0]["amount_chf"] is not None else g[0]["amount"],
                         currency=settings.get("currency", "CHF") if g[0]["amount_chf"] is not None else g[0]["currency"])
                 for i, (_, g) in enumerate(chunk)]
        answers: dict[int, JSON]
        try:
            answers = _answers(_ask_llm(settings, cats, examples, items))
            if settings.get("double_check", "1") == "1" and answers:
                # same question, items in reverse order: an answer that flips is a guess -> ask the user
                second = _answers(_ask_llm(settings, cats, examples, list(reversed(items))))
                for i, a in answers.items():
                    b = second.get(i)
                    if not b or b.get("category") != a.get("category"):
                        a["confidence"] = min(_conf(a), 0.5)
        except llm.LLMError:
            raise
        except Exception:
            answers = {}  # malformed answer: everything in this batch goes to review
        for i, (key, g) in enumerate(chunk):
            a = answers.get(i)
            cat = by_name.get(str(a.get("category"))) if a else None
            if not cat or not a:
                _to_review(conn, g)
                continue
            conf = _conf(a)
            merchant = str(a.get("merchant") or "")[:80]
            conn.execute("INSERT OR REPLACE INTO llm_cache(key, merchant, category_id, confidence) VALUES(?,?,?,?)",
                         (key, merchant, cat["id"], conf))
            _apply(conn, g, merchant, cat["id"], conf, threshold, by_name)
        done += len(chunk)
        conn.commit()
        if progress:
            progress(done, total)


def _answers(raw: list[JSON]) -> dict[int, JSON]:
    return {int(a["id"]): a for a in raw if isinstance(a.get("id"), int)}


def _conf(a: JSON) -> float:
    try:
        return max(0.0, min(1.0, float(a.get("confidence", 0))))
    except (TypeError, ValueError):
        return 0.0


def _to_review(conn: Connection, group: Iterable[Row]) -> None:
    for r in group:
        conn.execute("UPDATE transactions SET status='review' WHERE id=?", (r["id"],))


def _apply(conn: Connection, group: Iterable[Row], merchant: str, cat_id: int, conf: float, threshold: float,
           by_name: dict[str, Category]) -> None:
    kind = next((c["kind"] for c in by_name.values() if c["id"] == cat_id), "expense")
    for r in group:
        c = conf
        if any(h in (r["norm"] or "") or h in r["description"].lower() for h in PERSON_HINTS):
            c = min(c, 0.5)  # person-to-person payments are always asked about
        if kind == "income" and r["amount"] < 0:
            c = min(c, 0.3)  # money out can't be income
        if c >= threshold:
            conn.execute("UPDATE transactions SET merchant=?, category_id=?, status='llm', confidence=?, "
                         "suggestion_id=NULL WHERE id=?", (merchant, cat_id, c, r["id"]))
        else:
            conn.execute("UPDATE transactions SET merchant=?, category_id=NULL, status='review', confidence=?, "
                         "suggestion_id=? WHERE id=?", (merchant, c, cat_id, r["id"]))


def run(conn: Connection, settings: Settings, progress: ProgressFn | None = None) -> RunSummary:
    """Full pipeline for all 'new' transactions. Returns a short summary."""
    prepare(conn)
    apply_rules(conn, statuses=("new",))
    pairs = pair_transfers(conn)
    error: str | None = None
    try:
        llm_categorize(conn, settings, progress)
    except llm.LLMError as e:
        error = e.user_message
    # anything still new (LLM unreachable) is simply asked about
    conn.execute("UPDATE transactions SET status='review' WHERE status='new'")
    return RunSummary(transfer_pairs=pairs, llm_error=error)


# ---------------------------------------------------------------- user actions

def suggest_pattern(conn: Connection, txn_id: int) -> str:
    t = conn.execute("SELECT norm, merchant, merchant_key FROM transactions WHERE id=?", (txn_id,)).fetchone()
    if not t:
        return ""
    m = normalize(t["merchant"] or "")
    if m and matches(m, t["norm"] or ""):
        return m
    return str(t["merchant_key"]) if t["merchant_key"] != "unknown" else str(t["norm"] or "")


def similar(conn: Connection, pattern: str, exclude_ids: Collection[int] = ()) -> list[int]:
    p = normalize(pattern)
    if not p:
        return []
    rows = conn.execute("SELECT id, norm FROM transactions WHERE status != 'user'").fetchall()
    return [int(r["id"]) for r in rows if r["id"] not in exclude_ids and matches(p, r["norm"] or "")]


def _snapshot(conn: Connection, ids: Collection[int]) -> list[TxnSnapshot]:
    if not ids:
        return []
    q = "SELECT id, category_id, status, suggestion_id, confidence FROM transactions WHERE id IN (%s)"
    return [TxnSnapshot(id=r["id"], category_id=r["category_id"], status=r["status"],
                        suggestion_id=r["suggestion_id"], confidence=r["confidence"])
            for r in conn.execute(q % ",".join("?" * len(ids)), list(ids))]


def assign(conn: Connection, txn_ids: Iterable[int], category_id: int, remember: bool = False,
           pattern: str | None = None) -> UndoToken:
    """Set the category for the given transactions; optionally remember a rule and apply it.

    Returns an undo token: the previous state of every changed row and of the rule.
    """
    ids = [int(i) for i in txn_ids]
    rule_pattern = normalize(pattern or "") if remember else ""
    if rule_pattern and is_generic(rule_pattern):
        rule_pattern = ""  # e.g. "mobile no" would match every TWINT payment: don't remember it
    affected: set[int] = set(ids) | (set(similar(conn, rule_pattern)) if rule_pattern else set[int]())
    token = UndoToken(txns=_snapshot(conn, affected), rule=None, not_remembered=bool(remember and not rule_pattern))
    for i in ids:
        conn.execute("UPDATE transactions SET category_id=?, status='user', suggestion_id=NULL WHERE id=?",
                     (category_id, i))
    if rule_pattern:
        old = conn.execute("SELECT id, category_id FROM rules WHERE pattern=?", (rule_pattern,)).fetchone()
        if old:
            token["rule"] = RuleUndo(id=old["id"], prev_category_id=old["category_id"])
            conn.execute("UPDATE rules SET category_id=? WHERE id=?", (category_id, old["id"]))
        else:
            cur = conn.execute("INSERT INTO rules(pattern, category_id, created_at) VALUES(?,?,datetime('now'))",
                               (rule_pattern, category_id))
            token["rule"] = RuleUndo(id=cur.lastrowid or 0, prev_category_id=None)
        apply_rules(conn, only_pattern=rule_pattern)
    return token


def undo(conn: Connection, token: UndoToken) -> None:
    for t in token["txns"]:
        conn.execute("UPDATE transactions SET category_id=?, status=?, suggestion_id=?, confidence=? WHERE id=?",
                     (t["category_id"], t["status"], t["suggestion_id"], t["confidence"], t["id"]))
    r = token["rule"]
    if r:
        if r["prev_category_id"] is None:
            conn.execute("DELETE FROM rules WHERE id=?", (r["id"],))
        else:
            conn.execute("UPDATE rules SET category_id=? WHERE id=?", (r["prev_category_id"], r["id"]))
