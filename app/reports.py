"""Reports. All sums are in the main currency (amount_chf); spending is shown as positive numbers.

Monthly report: monthly-period categories only (+ items still To review); yearly-period
categories are listed separately as "yearly costs paid this month".
Yearly report: yearly costs, 12-month table of monthly costs, set-aside suggestion.
"""
from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Mapping, Sequence
from sqlite3 import Connection, Row
from typing import Any

import db
import llm
from categorizer import suggest_pattern
from domain import JSON, Category, Settings

Rec = dict[str, Any]
"""A transaction row as a dict (database columns + category, share, display name)."""

TO_REVIEW = "To review"
INCOME_REVIEW = "To review (money in)"


def _where(prefix: str | None = None, account: str | None = None) -> tuple[str, list[str]]:
    sql: list[str] = ["t.amount_chf IS NOT NULL"]
    args: list[str] = []
    if prefix:
        sql.append("t.date LIKE ?")
        args.append(prefix + "%")
    if account:
        sql.append("t.account = ?")
        args.append(account)
    return " AND ".join(sql), args


def _pretty(key: str | None) -> str:
    """'css versicherung praemie' -> 'CSS Versicherung Praemie' (short words like AG/SBB upper-case)."""
    return " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in (key or "").split())


def merchant_names(conn: Connection) -> dict[str, str]:
    """One display name per merchant: the name the AI gave most often for that merchant key."""
    best: dict[str, str] = {}
    for r in conn.execute("SELECT merchant_key, merchant, COUNT(*) n FROM transactions WHERE merchant IS NOT NULL "
                          "AND merchant != '' GROUP BY merchant_key, merchant ORDER BY n DESC"):
        best.setdefault(r["merchant_key"], r["merchant"])
    return best


def display_name(d: Mapping[str, Any], names: Mapping[str, str] | None = None) -> str:
    key: str | None = d.get("merchant_key")
    name: str | None = (names or {}).get(key or "") or d.get("merchant")
    if not name and key and key != "unknown":
        return _pretty(key)
    if not name:
        return str(d.get("description") or "")[:40]
    return _pretty(name.lower()) if name.isupper() else name  # "IMMO VERWALTUNG AG" -> "Immo Verwaltung AG"


def _mine(r: Row, names: Mapping[str, str] | None = None) -> Rec:
    """Row as a dict where amount_chf is YOUR part (e.g. half of a bill split with someone).

    The full amount stays in full_chf; statement balance checks always use the full amount.
    """
    d: Rec = dict(r)
    d["name"] = display_name(d, names)
    share = d.get("share")
    share = 1.0 if share is None else share
    d["share"] = share
    d["full_chf"] = d["amount_chf"]
    if d["amount_chf"] is not None and share != 1:
        d["amount_chf"] = round(d["amount_chf"] * share, 2)
    return d


def _rows(conn: Connection, prefix: str | None = None, account: str | None = None, extra: str = "") -> list[Rec]:
    w, a = _where(prefix, account)
    names = merchant_names(conn)
    return [_mine(r, names) for r in conn.execute(
        "SELECT t.*, c.name AS category, c.kind, c.period, s.name AS suggestion "
        "FROM transactions t LEFT JOIN categories c ON c.id=t.category_id "
        "LEFT JOIN categories s ON s.id=t.suggestion_id WHERE %s %s ORDER BY t.date, t.id" % (w, extra), a
    ).fetchall()]


def _txn(r: Rec) -> JSON:
    return {"id": r["id"], "date": r["date"], "description": r["description"], "merchant": r["merchant"],
            "account": r["account"], "amount": r["amount"], "currency": r["currency"],
            "amount_chf": r["amount_chf"], "fx_source": r["fx_source"], "category_id": r["category_id"],
            "category": r["category"], "status": r["status"], "confidence": r["confidence"],
            "suggestion_id": r["suggestion_id"], "suggestion": r["suggestion"],
            "import_id": r["import_id"], "edited": bool(r["edited"]), "orig_amount": r["orig_amount"],
            "share": r["share"], "full_chf": r["full_chf"], "name": r["name"]}


def _bucket(r: Rec) -> str:
    """'monthly' | 'yearly' | 'income' | 'transfer' | 'review_out' | 'review_in'"""
    if r["category_id"] is None:
        return "review_out" if r["amount_chf"] < 0 else "review_in"
    if r["kind"] == "expense":
        return str(r["period"])
    return str(r["kind"])


def _month_add(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m += n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return "%04d-%02d" % (y, m)


def months(conn: Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT DISTINCT substr(date,1,7) FROM transactions ORDER BY 1")]


def accounts(conn: Connection) -> list[str]:
    return [str(r[0]) for r in conn.execute("SELECT DISTINCT account FROM transactions ORDER BY 1")]


def _spend_by_category(rows: Sequence[Rec]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        b = _bucket(r)
        if b == "monthly":
            out[r["category"]] = out.get(r["category"], 0) - r["amount_chf"]
        elif b == "review_out":
            out[TO_REVIEW] = out.get(TO_REVIEW, 0) - r["amount_chf"]
    return out


def _income_by_category(rows: Sequence[Rec]) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows:
        b = _bucket(r)
        if b == "income":
            out[r["category"]] = out.get(r["category"], 0) + r["amount_chf"]
        elif b == "review_in":
            out[INCOME_REVIEW] = out.get(INCOME_REVIEW, 0) + r["amount_chf"]
    return out


def _lines(values: dict[str, float], hist: dict[str, float], n_hist: int, rows: Sequence[Rec],
           belongs: Callable[[Rec, str], bool], cats: Mapping[str, Category]) -> list[JSON]:
    """Category lines with share of total, 12-month average and the transactions behind them."""
    base = sum(v for v in values.values() if v > 0)
    out: list[JSON] = []
    for name, v in sorted(values.items(), key=lambda kv: -kv[1]):
        avg = hist.get(name, 0) / n_hist if n_hist else None
        out.append({
            "category": name, "category_id": cats[name]["id"] if name in cats else None,
            "amount": round(v, 2),
            "share": round(100 * v / base, 1) if base > 0 and v > 0 else None,
            "avg": round(avg, 2) if avg is not None else None,
            "vs_avg": round(100 * (v - avg) / avg, 1) if avg else None,
            "transactions": [_txn(r) for r in rows if belongs(r, name)],
        })
    return out


def monthly(conn: Connection, month: str, account: str | None = None, spread: bool = False) -> JSON:
    rows = _rows(conn, month, account)
    cats = {c["name"]: c for c in db.categories(conn)}
    spend = _spend_by_category(rows)

    # 12-month average (months before this one that have any data)
    hist_months = [m for m in months(conn) if _month_add(month, -12) <= m < month]
    hist: dict[str, float] = {}
    hist_in: dict[str, float] = {}
    for m in hist_months:
        mrows = _rows(conn, m, account)
        for k, v in _spend_by_category(mrows).items():
            hist[k] = hist.get(k, 0) + v
        for k, v in _income_by_category(mrows).items():
            hist_in[k] = hist_in.get(k, 0) + v

    yearly_rows = [r for r in rows if _bucket(r) == "yearly"]
    yearly_total = -sum(r["amount_chf"] for r in yearly_rows)
    income = sum(r["amount_chf"] for r in rows if _bucket(r) == "income")
    review_in = sum(r["amount_chf"] for r in rows if _bucket(r) == "review_in")
    monthly_costs = sum(spend.values())

    spread_line: float | None = None
    if spread:
        window = _rows(conn, None, account, "AND substr(t.date,1,7) BETWEEN '%s' AND '%s'"
                       % (_month_add(month, -11), month))
        spread_line = round(-sum(r["amount_chf"] for r in window if _bucket(r) == "yearly") / 12, 2)

    shown_total = monthly_costs + (spread_line or 0)
    share_base = sum(v for v in spend.values() if v > 0) + (spread_line or 0)  # refunds don't distort shares
    lines: list[JSON] = []
    for name, v in sorted(spend.items(), key=lambda kv: -kv[1]):
        avg = hist.get(name, 0) / len(hist_months) if hist_months else None
        lines.append({
            "category": name, "category_id": cats[name]["id"] if name in cats else None,
            "amount": round(v, 2),
            "share": round(100 * v / share_base, 1) if share_base > 0 and v > 0 else None,
            "avg": round(avg, 2) if avg is not None else None,
            "vs_avg": round(100 * (v - avg) / avg, 1) if avg else None,
            "transactions": [_txn(r) for r in rows if
                             (r["category"] == name and _bucket(r) == "monthly") or
                             (name == TO_REVIEW and _bucket(r) == "review_out")],
        })
    if spread_line:
        lines.append({"category": "Yearly spendings (1/12)", "category_id": None, "amount": spread_line,
                      "share": round(100 * spread_line / share_base, 1), "avg": None, "vs_avg": None,
                      "transactions": [], "spread": True})

    net = income + review_in - monthly_costs - yearly_total
    outflows = [r for r in rows if _bucket(r) in ("monthly", "yearly", "review_out") and r["amount_chf"] < 0]
    merchants: dict[str, tuple[str, float]] = {}  # grouped by merchant key, so one shop never shows under two spellings
    for r in outflows:
        k = str(r["merchant_key"]) if r["merchant_key"] not in (None, "unknown") else str(r["name"])
        name, total = merchants.get(k, (r["name"], 0))
        merchants[k] = (name, total - r["amount_chf"])
    income_lines = _lines(
        _income_by_category(rows), hist_in, len(hist_months), rows,
        lambda r, name: (_bucket(r) == "income" and r["category"] == name) or
                        (name == INCOME_REVIEW and _bucket(r) == "review_in"), cats)
    return {
        "month": month, "account": account,
        "income": round(income, 2), "unreviewed_income": round(review_in, 2),
        "monthly_costs": round(monthly_costs, 2), "yearly_paid": round(yearly_total, 2),
        "net": round(net, 2),
        "savings_rate": round(100 * net / (income + review_in), 1) if income + review_in > 0 else None,
        "spread": spread_line, "cost_of_living": round(shown_total, 2),
        "avg_months": len(hist_months),
        "categories": lines,
        "yearly": [_txn(r) for r in yearly_rows],
        "income_lines": income_lines,
        "transfers": [_txn(r) for r in rows if _bucket(r) == "transfer"],
        "top_merchants": [{"name": n, "amount": round(v, 2)} for n, v in
                          sorted(merchants.values(), key=lambda nv: -nv[1])[:5]],
        "largest": [_txn(r) for r in sorted(outflows, key=lambda r: r["amount_chf"])[:5]],
        "to_review": sum(1 for r in rows if r["status"] == "review"),
        "without_rate": _without_rate(conn, month, account),
    }


def _without_rate(conn: Connection, prefix: str, account: str | None) -> int:
    q, a = "SELECT COUNT(*) FROM transactions WHERE amount_chf IS NULL AND date LIKE ?", [prefix + "%"]
    if account:
        q += " AND account=?"
        a.append(account)
    return int(conn.execute(q, a).fetchone()[0])


def yearly(conn: Connection, year: str, account: str | None = None) -> JSON:
    rows = _rows(conn, year, account)
    last = _rows(conn, str(int(year) - 1), account)
    cats = [c for c in db.categories(conn) if c["kind"] == "expense" and c["period"] == "monthly"]

    yearly_rows = [r for r in rows if _bucket(r) == "yearly"]
    ytotal = -sum(r["amount_chf"] for r in yearly_rows)
    ylast = -sum(r["amount_chf"] for r in last if _bucket(r) == "yearly")
    by_cat: dict[str, list[JSON]] = {}
    for r in yearly_rows:
        by_cat.setdefault(r["category"], []).append(_txn(r))

    month_keys = ["%s-%02d" % (year, m) for m in range(1, 13)]
    matrix: dict[str, dict[str, float]] = {}
    income_matrix: dict[str, dict[str, float]] = {}
    per_month: dict[str, JSON] = {}
    for mk in month_keys:
        mrows = [r for r in rows if str(r["date"]).startswith(mk)]
        s = _spend_by_category(mrows)
        matrix[mk] = {k: round(v, 2) for k, v in s.items()}
        income_matrix[mk] = {k: round(v, 2) for k, v in _income_by_category(mrows).items()}
        per_month[mk] = {
            "income": round(sum(r["amount_chf"] for r in mrows if _bucket(r) in ("income", "review_in")), 2),
            "monthly_costs": round(sum(s.values()), 2),
            "yearly": round(-sum(r["amount_chf"] for r in mrows if _bucket(r) == "yearly"), 2),
            "has_data": bool(mrows),
        }
        per_month[mk]["net"] = round(per_month[mk]["income"] - per_month[mk]["monthly_costs"]
                                     - per_month[mk]["yearly"], 2)
    active = [mk for mk in month_keys if per_month[mk]["has_data"]]
    names = [c["name"] for c in cats] + [TO_REVIEW]
    names = [n for n in names if any(matrix[mk].get(n) for mk in month_keys)]
    col_tot = {n: round(sum(matrix[mk].get(n, 0) for mk in month_keys), 2) for n in names}
    income: float = sum(per_month[mk]["income"] for mk in month_keys)
    in_names = [c["name"] for c in db.categories(conn) if c["kind"] == "income"] + [INCOME_REVIEW]
    in_names = [n for n in in_names if any(income_matrix[mk].get(n) for mk in month_keys)]
    in_tot = {n: round(sum(income_matrix[mk].get(n, 0) for mk in month_keys), 2) for n in in_names}
    mcosts: float = sum(per_month[mk]["monthly_costs"] for mk in month_keys)
    net = income - mcosts - ytotal
    return {
        "year": year, "account": account,
        "yearly_costs": [{"category": k, "total": round(-sum(t["amount_chf"] for t in v), 2), "transactions": v}
                         for k, v in by_cat.items()],
        "yearly_total": round(ytotal, 2), "yearly_last_year": round(ylast, 2),
        "yearly_vs_last": round(100 * (ytotal - ylast) / ylast, 1) if ylast else None,
        "set_aside": round(ytotal / 12, 2),
        "months": month_keys, "categories": names, "matrix": matrix, "per_month": per_month,
        "category_totals": col_tot,
        "category_avg": {n: round(col_tot[n] / len(active), 2) if active else 0 for n in names},
        "income_categories": in_names, "income_matrix": income_matrix, "income_totals": in_tot,
        "income_avg": {n: round(in_tot[n] / len(active), 2) if active else 0 for n in in_names},
        "income": round(income, 2), "monthly_costs": round(mcosts, 2), "net": round(net, 2),
        "savings_rate": round(100 * net / income, 1) if income > 0 else None,
        "active_months": len(active),
        "to_review": sum(1 for r in rows if r["status"] == "review"),
    }


def alltime(conn: Connection, account: str | None = None) -> JSON:
    years = sorted({m[:4] for m in months(conn)})
    out: list[JSON] = []
    for y in years:
        rep = yearly(conn, y, account)
        out.append({"year": y, "income": rep["income"], "monthly_costs": rep["monthly_costs"],
                    "yearly_costs": rep["yearly_total"], "net": rep["net"], "savings_rate": rep["savings_rate"],
                    "categories": rep["category_totals"]})
    return {"years": out}


def review_groups(conn: Connection, prefix: str | None = None, account: str | None = None) -> list[JSON]:
    w, a = _where(prefix, account)
    w = w.replace("t.amount_chf IS NOT NULL", "1=1")
    rows = conn.execute(
        "SELECT t.*, s.name AS suggestion FROM transactions t LEFT JOIN categories s ON s.id=t.suggestion_id "
        "WHERE t.status='review' AND %s ORDER BY t.date" % w, a).fetchall()
    groups: dict[tuple[str | None, int | None, bool], list[Row]] = {}
    for r in rows:
        k = (r["merchant_key"], r["suggestion_id"], r["amount"] > 0)
        groups.setdefault(k, []).append(r)
    out: list[JSON] = []
    for (_key, sid, _incoming), g in groups.items():
        out.append({
            "label": display_name(dict(g[0])),
            "count": len(g),
            "total": round(sum((r["amount_chf"] if r["amount_chf"] is not None else r["amount"]) for r in g), 2),
            "suggestion_id": sid, "suggestion": g[0]["suggestion"], "confidence": g[0]["confidence"],
            "txn_ids": [r["id"] for r in g],
            "pattern": suggest_pattern(conn, g[0]["id"]),
            "samples": [{"date": r["date"], "description": r["description"], "amount": r["amount"],
                         "currency": r["currency"], "account": r["account"]} for r in g[:5]],
        })
    out.sort(key=lambda x: -abs(x["total"]))
    return out


def search(conn: Connection, q: str, limit: int = 200) -> list[JSON]:
    like = "%" + q.strip() + "%"
    args: list[str | float] = [like, like]
    extra = ""
    try:
        v = float(q.replace("'", "").replace(",", "."))
        extra = " OR abs(abs(t.amount) - ?) < 0.005"
        args.append(abs(v))
    except ValueError:
        pass
    rows = conn.execute(
        "SELECT t.*, c.name AS category, c.kind, c.period, s.name AS suggestion FROM transactions t "
        "LEFT JOIN categories c ON c.id=t.category_id LEFT JOIN categories s ON s.id=t.suggestion_id "
        "WHERE t.description LIKE ? OR t.merchant LIKE ?%s ORDER BY t.date DESC LIMIT %d" % (extra, limit),
        args).fetchall()
    return as_transactions(conn, rows)


def as_transactions(conn: Connection, rows: Sequence[Row]) -> list[JSON]:
    """Database rows (with category/suggestion columns joined) -> transactions as sent to the browser."""
    names = merchant_names(conn)
    return [_txn(_mine(r, names)) for r in rows]


# ---------------------------------------------------------------- local-LLM writing

def notes(conn: Connection, settings: Settings, month: str, account: str | None = None) -> str:
    rep = monthly(conn, month, account)
    compact: JSON = {
        "month": month, "currency": settings.get("currency", "CHF"),
        "income": rep["income"], "monthly_costs": rep["monthly_costs"], "yearly_paid": rep["yearly_paid"],
        "net": rep["net"], "savings_rate_percent": rep["savings_rate"],
        "categories": [{k: c[k] for k in ("category", "amount", "share", "avg", "vs_avg")} for c in rep["categories"]],
        "yearly_items": [{"merchant": t["merchant"] or t["description"][:40], "amount": -t["amount_chf"]}
                         for t in rep["yearly"]],
        "top_merchants": rep["top_merchants"],
    }
    msgs: list[llm.Message] = [{"role": "system", "content":
             "You are a friendly, practical personal accountant. Write a short monthly review (max 150 words, "
             "3-5 bullet points): where the money went, categories notably above or below their 12-month average, "
             "and one or two concrete suggestions. Use only the numbers given; do not invent any. Plain text."},
            {"role": "user", "content": json.dumps(compact, ensure_ascii=False)}]
    return llm.chat(settings, msgs, timeout=300)


def ask(conn: Connection, settings: Settings, question: str, account: str | None = None) -> str:
    years_out: list[JSON] = []
    data: JSON = {"currency": settings.get("currency", "CHF"), "today": datetime.date.today().isoformat(),
            "years": years_out}
    for y in sorted({m[:4] for m in months(conn)}):
        rep = yearly(conn, y, account)
        years_out.append({
            "year": y, "income": rep["income"], "monthly_costs": rep["monthly_costs"],
            "yearly_costs": rep["yearly_total"], "net": rep["net"],
            "monthly_costs_by_month_and_category": {m: v for m, v in rep["matrix"].items() if v},
            "yearly_costs": [{"category": c["category"], "total": c["total"],
                              "items": [{"date": t["date"], "merchant": t["merchant"] or t["description"][:40],
                                         "amount": -t["amount_chf"]} for t in c["transactions"]]}
                             for c in rep["yearly_costs"]],
        })
    msgs: list[llm.Message] = [{"role": "system", "content":
             "You are a personal accountant answering questions about the user's spending. Answer briefly "
             "and precisely using only the data provided (amounts are totals per month and category). "
             "If the data cannot answer the question, say so."},
            {"role": "user", "content": "DATA:\n" + json.dumps(data, ensure_ascii=False) + "\n\nQUESTION: " + question}]
    return llm.chat(settings, msgs, timeout=300)


def export_csv(conn: Connection) -> str:
    import csv
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "account", "description", "merchant", "amount", "currency", "amount_chf", "fx_source",
                "your_share", "your_amount_chf", "category", "report", "status"])
    for r in _rows(conn) + [_mine(x) for x in conn.execute(
            "SELECT t.*, c.name AS category, c.kind, c.period, NULL AS suggestion FROM transactions t "
            "LEFT JOIN categories c ON c.id=t.category_id WHERE t.amount_chf IS NULL").fetchall()]:
        w.writerow([r["date"], r["account"], r["description"], r["merchant"] or "", r["amount"], r["currency"],
                    r["full_chf"] if r["full_chf"] is not None else "", r["fx_source"] or "",
                    r["share"], r["amount_chf"] if r["amount_chf"] is not None else "",
                    r["category"] or "", r["period"] or "", r["status"]])
    return buf.getvalue()
