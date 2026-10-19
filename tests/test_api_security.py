import re
import socket
import time

import pytest

import fx
import llm
import statements

H = {"x-local-accountant": "1"}


@pytest.fixture
def client(data_dir, fake_llm, fake_ecb, monkeypatch):
    monkeypatch.setenv("LOCAL_ACCOUNTANT_ALLOWED_HOSTS", "testserver")
    monkeypatch.setattr(llm, "list_models", lambda url: ["qwen3:8b"])
    from fastapi.testclient import TestClient
    import server
    with TestClient(server.app) as c:
        yield c


def _scan(client):
    files = [("files", (n, fn()[0], "application/pdf")) for n, fn in
             [("pf.pdf", statements.postfinance), ("sc.pdf", statements.swisscard), ("w.pdf", statements.wise)]]
    jid = client.post("/api/scan", files=files, headers=H).json()["job_id"]
    for _ in range(1200):  # up to 60 s: Docling reads the PDFs
        j = client.get("/api/jobs/" + jid).json()
        if j["done"]:
            return j
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def test_full_flow(client):
    j = _scan(client)
    assert [f["status"] for f in j["files"]] == ["done"] * 3
    assert all(f["balance_check"] == "ok" for f in j["files"])
    review = client.get("/api/review").json()
    assert review
    r = client.post("/api/review/accept-all", json={}, headers=H).json()
    assert r["changed"] > 0
    assert all(not g["suggestion_id"] for g in client.get("/api/review").json())
    client.post("/api/undo", json={"undo": r["undo"]}, headers=H)
    assert client.get("/api/review").json()
    m = client.get("/api/report/month/2026-09").json()
    assert m["yearly_paid"] == 1240.0
    imp = client.get("/api/imports").json()
    client.delete("/api/imports/%d" % imp[0]["id"], headers=H)
    assert len(client.get("/api/imports").json()) == 2


def test_foreign_host_is_rejected(client):
    assert client.get("/api/status", headers={"host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"host": "attacker.local:8765"}).status_code == 403


def test_write_without_custom_header_is_rejected(client):
    assert client.post("/api/assign", json={"txn_ids": [1], "category_id": 1}).status_code == 403
    assert client.delete("/api/rules/1").status_code == 403
    files = [("files", ("a.pdf", b"%PDF-1.4", "application/pdf"))]
    assert client.post("/api/scan", files=files).status_code == 403


def test_security_headers(client):
    r = client.get("/")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in r.headers


def test_upload_type_is_checked(client):
    files = [("files", ("evil.exe", b"MZ\x90\x00", "application/octet-stream"))]
    assert client.post("/api/scan", files=files, headers=H).status_code == 400


@pytest.mark.parametrize("url", ["https://api.openai.com", "http://192.168.1.10:11434", "http://example.com:11434",
                                 "http://10.0.0.5:11434"])
def test_non_local_llm_is_refused(client, url):
    assert client.post("/api/settings", json={"llm_url": url}, headers=H).status_code == 400
    with pytest.raises(llm.LLMError):
        llm.ensure_local(url)


def test_local_llm_urls_are_allowed():
    for url in ("http://127.0.0.1:11434", "http://localhost:11434", "http://[::1]:11434"):
        llm.ensure_local(url)


def test_server_mode_allowlist_only_private(monkeypatch):
    monkeypatch.setenv("LOCAL_ACCOUNTANT_LLM_HOSTS", "ollama,10.0.0.5,ollama.example.com")
    llm.ensure_local("http://ollama:11434")
    llm.ensure_local("http://10.0.0.5:11434")
    with pytest.raises(llm.LLMError):
        llm.ensure_local("http://ollama.example.com:11434")  # public DNS names never allowed


def test_no_data_leaves_the_machine(client, fake_ecb, monkeypatch):
    """Full scan + reports with all non-loopback connections blocked; ECB request carries no data."""
    real_connect = socket.socket.connect
    attempts = []

    def guarded(sock, address):
        host = address[0] if isinstance(address, tuple) else address
        if host not in ("127.0.0.1", "::1", "localhost"):
            attempts.append(address)
            raise OSError("blocked by test")
        return real_connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    seen = []

    class Recorder:
        def open(self, req, timeout=None):
            seen.append(req)
            raise OSError("offline")

    monkeypatch.setattr(fx, "_opener", Recorder())
    monkeypatch.setattr(fx, "fetch_ecb", fake_ecb)  # the real download function, against the recorder
    _scan(client)
    client.get("/api/report/month/2026-09")
    client.get("/api/report/year/2026")
    assert attempts == []
    assert len(seen) == 1
    for req in seen:  # the only outgoing request: a plain GET of the public ECB file
        assert req.full_url == fx.ECB_URL and req.data is None and "?" not in req.full_url



def _scan_files(client, files):
    jid = client.post("/api/scan", files=files, headers=H).json()["job_id"]
    for _ in range(1200):  # up to 60 s: Docling reads the PDFs
        j = client.get("/api/jobs/" + jid).json()
        if j["done"]:
            return j
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def test_correcting_a_misread_row_fixes_the_balance_check(client):
    # the statement printed 1'200.00 but the reader got 200.00 (simulated misread)
    rows = [("02.09.26", ["AXA VERSICHERUNGEN AG"], -1200.00), ("04.09.26", ["MIGROS"], -87.35)]
    pdf = statements.postfinance(rows=rows, opening=5000.00, misprint={0: -200.00})[0]
    j = _scan_files(client, [("files", ("pf.pdf", pdf, "application/pdf"))])
    f = j["files"][0]
    assert f["balance_check"] == "diff" and f["balance_diff"] == 1000.00
    d = client.get("/api/imports/%d/transactions" % f["import_id"]).json()
    axa = next(t for t in d["transactions"] if "AXA" in t["description"])
    assert axa["amount"] == -200.00
    r = client.put("/api/transactions/%d" % axa["id"], json={"amount": -1200.00}, headers=H).json()
    assert r["import"]["balance_check"] == "ok"
    d = client.get("/api/imports/%d/transactions" % f["import_id"]).json()
    axa = next(t for t in d["transactions"] if "AXA" in t["description"])
    assert axa["amount"] == -1200.00 and axa["amount_chf"] == -1200.00
    assert axa["edited"] and axa["orig_amount"] == -200.00
    # reports use the corrected amount
    m = client.get("/api/report/month/2026-09").json()
    assert any(t["amount_chf"] == -1200.00 for c in m["categories"] for t in c["transactions"]) or \
        any(t["amount_chf"] == -1200.00 for t in m["yearly"])


def test_edit_validation_and_delete(client):
    _scan(client)
    t = client.get("/api/search?q=fressnapf").json()[0]
    assert client.put("/api/transactions/%d" % t["id"], json={"amount": 0}, headers=H).status_code == 400
    assert client.put("/api/transactions/%d" % t["id"], json={"date": "31.09.2026"}, headers=H).status_code == 400
    assert client.put("/api/transactions/%d" % t["id"], json={"amount": -1}).status_code == 403  # no CSRF header
    r = client.put("/api/transactions/%d" % t["id"], json={"description": "FRESSNAPF WINTERTHUR"}, headers=H).json()
    assert r["import"]["balance_check"] == "ok"  # description changes don't affect balances
    r = client.delete("/api/transactions/%d" % t["id"], headers=H).json()
    assert r["import"]["balance_check"] == "diff" and r["import"]["balance_diff"] == 43.90


def test_recheck_for_files_scanned_before_balances_were_stored(conn):
    import pipeline
    conn.execute("INSERT INTO imports(id, filename, bank, account, balance_check, balance_diff, warnings) "
                 "VALUES(7,'old.pdf','PostFinance','PF','diff',1000.0,'[]')")
    pipeline.recheck(conn, 7, delta=-1000.0)  # user corrected -200 -> -1200
    r = conn.execute("SELECT balance_check, balance_diff FROM imports WHERE id=7").fetchone()
    assert r["balance_check"] == "ok" and r["balance_diff"] == 0


def test_running_balance_file_rechecks_after_correction(client):
    """Wise-style statement (no opening/closing, only a running balance): fixing the row turns it ✅."""
    pdf = statements.wise()[0]
    j = _scan_files(client, [("files", ("w.pdf", pdf, "application/pdf"))])
    f = j["files"][0]
    assert f["balance_check"] == "ok"
    d = client.get("/api/imports/%d/transactions" % f["import_id"]).json()
    ryan = next(t for t in d["transactions"] if "Ryanair" in t["description"])
    bad = client.put("/api/transactions/%d" % ryan["id"], json={"amount": -9.00}, headers=H).json()
    assert bad["import"]["balance_check"] == "diff"  # a wrong amount breaks the running balance
    good = client.put("/api/transactions/%d" % ryan["id"], json={"amount": -89.00}, headers=H).json()
    assert good["import"]["balance_check"] == "ok"


def test_split_half_counts_only_your_part(client):
    _scan(client)
    rent = client.get("/api/search?q=IMMO").json()[0]
    before = client.get("/api/report/month/2026-09").json()
    assert client.post("/api/transactions/%d/share" % rent["id"], json={"share": 0.5}, headers=H).status_code == 200
    after = client.get("/api/report/month/2026-09").json()
    assert after["monthly_costs"] == pytest.approx(before["monthly_costs"] - 1050.00)
    assert after["net"] == pytest.approx(before["net"] + 1050.00)
    row = next(t for c in after["categories"] for t in c["transactions"] if t["id"] == rent["id"])
    assert row["amount_chf"] == -1050.00 and row["full_chf"] == -2100.00 and row["share"] == 0.5
    # the statement's balance check still uses the full amount
    imp = client.get("/api/imports/%d/transactions" % rent["import_id"]).json()["import"]
    assert imp["balance_check"] == "ok"
    # undo: back to the full amount; invalid shares are refused
    client.post("/api/transactions/%d/share" % rent["id"], json={"share": 1}, headers=H)
    assert client.get("/api/report/month/2026-09").json()["monthly_costs"] == pytest.approx(before["monthly_costs"])
    assert client.post("/api/transactions/%d/share" % rent["id"], json={"share": 0}, headers=H).status_code == 400
    assert client.post("/api/transactions/%d/share" % rent["id"], json={"share": 1.5}, headers=H).status_code == 400
    assert client.post("/api/transactions/%d/share" % rent["id"], json={"share": 0.5}).status_code == 403


def test_postfinance_csv_scan_is_balance_checked(client):
    from test_parsing import POSTFINANCE_CSV_EN
    j = _scan_files(client, [("files", ("export.csv", POSTFINANCE_CSV_EN.encode(), "text/csv"))])
    f = j["files"][0]
    assert f["status"] == "done" and f["added"] == 5 and f["balance_check"] == "ok"
    m = client.get("/api/report/month/2026-09").json()
    assert m["income"] + m["unreviewed_income"] == pytest.approx(7450.00 + 45.00 + 20.00)
    broken = POSTFINANCE_CSV_EN.replace(";-87.35;", ";-8.35;")  # misread / wrong row
    j = _scan_files(client, [("files", ("export2.csv", broken.replace("ACME", "ACME2").encode(), "text/csv"))])
    assert j["files"][0]["balance_check"] == "diff"


TECHNICAL = re.compile(
    r"docling|\bllm\b|json|https?://|127\.0\.0\.1|localhost|qwen|traceback|exception|errno|urlopen|markdown", re.I)


def test_no_technical_details_reach_the_ui(client, monkeypatch, fake_parser):
    """Everything the UI can show must be plain language (details belong in the server log)."""
    import pdf_import
    import reports
    from fastapi.testclient import TestClient
    import server
    settings = {"llm_url": "http://127.0.0.1:11434", "llm_model": "qwen3:8b", "parser_model": "qwen3:8b"}
    shown: list[str] = []

    # 1. scan progress texts and page warnings
    def page2_fails(page, obj):
        if page.number == 2:
            raise llm.LLMError("Local model answer was cut off; invalid JSON from qwen3:8b", "cut_off")
        return obj
    fake_parser.override = page2_fails
    r = pdf_import.parse_pdf(statements.wise()[0], settings, progress=lambda p, n, stage: shown.append(stage))
    shown += r["warnings"]

    # 2. the local AI is down while scanning
    def down(page, obj):
        raise llm.LLMError("Local LLM not reachable at http://127.0.0.1:11434 (<urlopen error [Errno 61]>)", "unreachable")
    fake_parser.override = down
    j = _scan_files(client, [("files", ("pf.pdf", statements.postfinance()[0], "application/pdf"))])
    shown += [f.get("error") or "" for f in j["files"]] + [j["error"] or ""]

    # 3. a damaged file
    fake_parser.override = None
    j = _scan_files(client, [("files", ("broken.pdf", b"%PDF-1.4 this is not really a pdf", "application/pdf"))])
    shown += [f.get("error") or "" for f in j["files"]]

    # 4. status pill / tooltip when the AI is unavailable
    def no_ollama(url):
        raise llm.LLMError("Local LLM not reachable at %s (Connection refused)" % url, "unreachable")
    monkeypatch.setattr(llm, "list_models", no_ollama)
    shown.append(client.get("/api/status").json()["llm_message"] or "")

    # 5. an unexpected server error
    def boom(*a, **k):
        raise RuntimeError("sqlite3.OperationalError: no such column x (Traceback ...)")
    monkeypatch.setattr(reports, "monthly", boom)
    with TestClient(server.app, raise_server_exceptions=False) as c2:
        shown.append(c2.get("/api/report/month/2026-09", headers={"host": "testserver"}).json()["detail"])

    assert all(s.strip() for s in shown[:3]), shown
    leaks = [s for s in shown if TECHNICAL.search(s)]
    assert not leaks, leaks
