"""Statement reader: Docling (real) -> AI (simulated, see conftest.FakeParserAI) -> generic checks (real)."""
import io
import json

import pytest

import llm
import pdf_import
import statements

S = {"llm_url": "http://127.0.0.1:11434", "llm_model": "qwen3:8b", "parser_model": "qwen3:8b"}


def _check(fn, bank):
    data, expected = fn()
    r = pdf_import.parse_pdf(data, S)
    got = sorted((t["date"], t["amount"], t["currency"]) for t in r["transactions"])
    assert got == sorted((d, round(a, 2), c) for d, a, c, _ in expected)
    assert r["bank"] == bank and r["balance_check"] == "ok", r["warnings"]
    return r


def test_docling_reads_the_statement():
    pages = pdf_import.read_pages(statements.postfinance()[0])
    assert len(pages) == 1
    assert "7'450.00" in pages[0].markdown and "MIGROS" in pages[0].markdown
    assert "7'450.00" in pages[0].text  # the PDF's own text layer, used to verify amounts


@pytest.mark.parametrize("fn,bank", [(statements.postfinance, "PostFinance"), (statements.swisscard, "Swisscard"),
                                     (statements.wise, "Wise"), (statements.postfinance_en_daily, "PostFinance")])
def test_correct_answers_pass_all_checks(fn, bank):
    r = _check(fn, bank)
    assert r["account"].startswith(bank)


def test_credit_card_balance_check_uses_amount_owed():
    r = _check(statements.swisscard, "Swisscard")
    assert r["liability"] is True


def test_rows_the_pdf_does_not_contain_are_dropped(fake_parser):
    def invent(page, obj):
        obj["transactions"].append({"date": "2026-09-30", "description": "INVENTED", "money_in": None,
                                    "money_out": 999.99, "balance": None, "currency": None})
        obj["transactions"][0]["balance"] = 123456.78  # a balance that isn't printed
        return obj
    fake_parser.override = invent
    r = pdf_import.parse_pdf(statements.postfinance()[0], S)
    assert not any(t["description"] == "INVENTED" for t in r["transactions"])
    assert any("isn't on the statement" in w for w in r["warnings"])
    assert r["transactions"][0]["balance"] is None
    assert r["balance_check"] == "ok"


def test_merged_rows_are_caught_by_the_checks(fake_parser):
    def merge(page, obj):  # the real model's typical mistake: two rows become one with in AND out
        a, b = obj["transactions"][0], obj["transactions"][1]
        a["money_out"], a["description"] = b["money_out"], a["description"] + " " + b["description"]
        del obj["transactions"][1]
        return obj
    fake_parser.override = merge
    r = pdf_import.parse_pdf(statements.postfinance()[0], S)
    assert r["balance_check"] == "diff"


def test_missing_row_is_detected():
    data, _ = statements.postfinance(drop=4)
    r = pdf_import.parse_pdf(data, S)
    assert r["balance_check"] == "diff" and r["balance_diff"] == pytest.approx(95.20)


def test_one_unreadable_page_does_not_stop_the_others(fake_parser):
    def fail_page2(page, obj):
        if page.number == 2:
            raise llm.LLMError("The local model's answer was cut off", "cut_off")
        return obj
    fake_parser.override = fail_page2
    r = pdf_import.parse_pdf(statements.wise()[0], S)
    assert {t["currency"] for t in r["transactions"]} == {"CHF"}
    assert any("Page 2 couldn't be read" in w for w in r["warnings"])


def test_ollama_down_is_an_error(fake_parser):
    def down(page, obj):
        raise llm.LLMError("Local LLM not reachable at http://127.0.0.1:11434", "unreachable")
    fake_parser.override = down
    with pytest.raises(llm.LLMError):
        pdf_import.parse_pdf(statements.postfinance()[0], S)


def test_dates_are_normalized_and_carried(fake_parser):
    def odd_dates(page, obj):
        obj["transactions"][0]["date"] = "01.09.2026"
        obj["transactions"][1]["date"] = ""  # no date -> previous row's date
        return obj
    fake_parser.override = odd_dates
    r = pdf_import.parse_pdf(statements.postfinance()[0], S)
    assert [t["date"] for t in r["transactions"][:2]] == ["2026-09-01", "2026-09-01"]


def test_scanned_pdf_without_text_is_rejected():
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.rect(100, 100, 300, 300, fill=1)
    c.save()
    with pytest.raises(ValueError, match="scanned image"):
        pdf_import.parse_pdf(buf.getvalue(), S)


def test_progress_is_reported():
    seen = []
    pdf_import.parse_pdf(statements.wise()[0], S, progress=lambda page, pages, stage: seen.append(stage))
    assert seen == ["Reading the statement…", "Reading page 1 of 2…", "Reading page 2 of 2…"]


def test_reader_report_and_redaction(tmp_path):
    import read_pdf
    f = tmp_path / "s.pdf"
    f.write_bytes(statements.postfinance()[0])
    full, red = io.StringIO(), io.StringIO()
    read_pdf.run(str(f), redact_mode=False, out=full, settings=S)
    read_pdf.run(str(f), redact_mode=True, out=red, settings=S)
    assert "what Docling read" in full.getvalue() and "raw JSON" in full.getvalue()
    assert "MIGROS" in full.getvalue() and "7450" in full.getvalue().replace("'", "")
    text = red.getvalue()
    for secret in ["MIGROS", "ACME", "ANNA", "7'450", "7450", "6789", "4'500"]:
        assert secret.lower() not in text.lower(), secret
    assert "money_in" in text and "balance check: ok" in text  # structure stays readable
