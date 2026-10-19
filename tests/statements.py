"""Synthetic bank statements shaped like PostFinance, Swisscard and Wise PDFs.

All names, numbers and amounts are made up. Each builder returns (pdf_bytes, expected) where
expected is the list of (date, amount, currency, description fragment) the parser must find.
Run `python tests/statements.py sample/` to write the sample PDFs for manual testing.
"""
import io
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4


def chf(v, sep="'"):
    """Swiss format: 1'234.50 (or with another thousands separator, e.g. a space)"""
    s = "{:,.2f}".format(abs(v)).replace(",", sep)
    return ("-" if v < 0 else "") + s


def en(v):
    s = "{:,.2f}".format(abs(v))
    return ("-" if v < 0 else "") + s


# ---------------------------------------------------------------- PostFinance

PF_ROWS = [  # (date, description lines, amount: + credit / - debit)
    ("01.09.26", ["GUTSCHRIFT AUFTRAGGEBER: ACME AG ZUERICH", "MITTEILUNGEN: LOHN SEPTEMBER"], 7450.00),
    ("01.09.26", ["DAUERAUFTRAG", "IMMO VERWALTUNG AG MIETE"], -2100.00),
    ("03.09.26", ["KAUF/DIENSTLEISTUNG VOM 03.09.2026", "KARTEN NR. XXXX1234", "MIGROS M ZUERICH"], -87.35),
    ("04.09.26", ["TWINT ZAHLUNG AN ANNA MUELLER", "+41791234567"], -60.00),
    ("05.09.26", ["KAUF/DIENSTLEISTUNG VOM 05.09.2026", "KARTEN NR. XXXX1234", "TOTALENERGIES TANKSTELLE BERN"], -95.20),
    ("08.09.26", ["LASTSCHRIFT CSS VERSICHERUNG AG", "PRAEMIE SEPTEMBER"], -420.00),
    ("10.09.26", ["SWISSCARD AECS GMBH", "KARTENABRECHNUNG"], -812.40),
    ("12.09.26", ["UEBERWEISUNG AN WISE EUROPE SA"], -500.00),
    ("15.09.26", ["KAUF/DIENSTLEISTUNG VOM 15.09.2026", "KARTEN NR. XXXX1234", "COOP-1234 WINTERTHUR"], -54.10),
    ("18.09.26", ["AXA VERSICHERUNGEN AG", "MOTORFAHRZEUG JAHRESPRAEMIE"], -1240.00),
    ("20.09.26", ["KAUF/DIENSTLEISTUNG VOM 20.09.2026", "KARTEN NR. XXXX1234", "FRESSNAPF ZUERICH"], -43.90),
    ("22.09.26", ["ZAHNARZTPRAXIS DR. MEIER", "RECHNUNG 2026-118"], -280.00),
    ("25.09.26", ["KAUF/DIENSTLEISTUNG VOM 25.09.2026", "KARTEN NR. XXXX1234", "SBB CFF FFS ZUERICH HB"], -31.00),
    ("28.09.26", ["SALT MOBILE SA", "RECHNUNG SEPTEMBER"], -49.95),
]
PF_OPENING = 4500.00


def postfinance(drop=None, rows=PF_ROWS, opening=PF_OPENING, misprint=None, sep="'", lang="de",
                no_date=(), no_value_date=()):
    """drop: index of a row to leave out; misprint: {index: wrongly printed amount};
    no_date / no_value_date: indexes of rows printed without a booking date / value date."""
    misprint = misprint or {}
    chfs = lambda v: chf(v, sep)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    shown = [r for i, r in enumerate(rows) if i != drop]

    def page_header(first):
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, H - 50, "PostFinance")
        c.setFont("Helvetica", 9)
        if first:
            c.drawString(40, H - 70, "Account statement Private account" if lang == "en" else "Kontoauszug Privatkonto")
            c.drawString(40, H - 82, "IBAN CH93 0900 0000 1234 5678 9")
            c.drawString(40, H - 94, "Currency CHF" if lang == "en" else "Währung CHF")
            c.drawString(40, H - 106, "Periode 01.09.2026 - 30.09.2026")
        c.setFont("Helvetica-Bold", 8.5)
        y = H - 140
        if lang == "en":  # left-aligned headers above right-aligned amounts
            c.drawString(40, y, "Date")
            c.drawString(95, y, "Details")
            c.drawString(300, y, "Credit")
            c.drawString(372, y, "Debit")
            c.drawString(445, y, "Value")
            c.drawString(505, y, "Balance")
        else:
            c.drawString(40, y, "Datum")
            c.drawString(95, y, "Text")
            c.drawRightString(365, y, "Gutschrift")
            c.drawRightString(435, y, "Lastschrift")
            c.drawString(445, y, "Valuta")
            c.drawRightString(555, y, "Saldo")
        c.setFont("Helvetica", 8.5)
        return y - 18

    y = page_header(True)
    c.drawString(40, y, "31.08.26")
    c.drawString(95, y, "Opening balance" if lang == "en" else "Kontostand")
    c.drawRightString(555, y, chfs(opening))
    y -= 16
    bal = opening
    for i, (d, lines, amt) in enumerate(rows):
        bal = round(bal + amt, 2)
        if i == drop:
            continue  # simulate a row the parser never saw, but keep the true balances
        if y < 120:
            c.drawString(95, y - 4, "Carried forward" if lang == "en" else "Übertrag")
            c.drawRightString(555, y - 4, chfs(round(bal - amt, 2)))
            c.drawString(40, 40, "Seite 1/2   PostFinance AG, Mingerstrasse 20, 3030 Bern")
            c.showPage()
            y = page_header(False)
            c.drawString(95, y, "Brought forward" if lang == "en" else "Übertrag")
            c.drawRightString(555, y, chfs(round(bal - amt, 2)))
            y -= 16
        if i not in no_date:
            c.drawString(40, y, d)
        c.drawString(95, y, lines[0])
        c.drawRightString(365 if amt > 0 else 435, y, chfs(abs(misprint.get(i, amt))))
        if i not in no_value_date:
            c.drawString(445, y, d)
        c.drawRightString(555, y, chfs(bal))
        for extra in lines[1:]:
            y -= 11
            c.drawString(95, y, extra)
        y -= 16
    c.drawString(40, y, "30.09.26")
    c.drawString(95, y, "Closing balance" if lang == "en" else "Kontostand")
    c.drawRightString(555, y, chfs(bal))
    c.drawString(40, 40, "Seite 2/2   PostFinance AG, Mingerstrasse 20, 3030 Bern")
    c.save()
    exp = [("2026-%s-%s" % (d[3:5], d[:2]), amt, "CHF", lines[-1].split()[0])
           for i, (d, lines, amt) in enumerate(rows) if i != drop]
    return buf.getvalue(), exp


# ---------------------------------------------------------------- Swisscard

SC_ROWS = [  # (transaction date, description lines, amount as printed: purchases positive)
    ("10.09.2026", ["Ihre Zahlung - Danke"], -812.40),
    ("16.08.2026", ["GALAXUS.CH ZUERICH"], 249.90),
    ("19.08.2026", ["TIBITS ZUERICH"], 48.50),
    ("22.08.2026", ["AMAZON.DE LUXEMBOURG", "EUR 45.00 Kurs 0.9411"], 42.35),
    ("27.08.2026", ["ESCAPE ROOM ADVENTURE BERN"], 120.00),
    ("02.09.2026", ["COIFFEUR BELLA ZUERICH"], 85.00),
    ("05.09.2026", ["SPOTIFY P1234 STOCKHOLM"], 12.95),
    ("09.09.2026", ["ZALANDO RUECKERSTATTUNG BERLIN"], -59.90),
]
SC_OPENING = 812.40


def swisscard():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, H - 50, "Swisscard AECS GmbH")
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 70, "Monatsrechnung Cashback Cards")
    c.drawString(40, H - 82, "Kartennummer XXXX XXXX XXXX 4321")
    c.drawString(40, H - 94, "Rechnungsdatum 15.09.2026")
    y = H - 130
    c.drawString(210, y, "Saldo vorherige Rechnung")
    c.drawRightString(555, y, chf(SC_OPENING))
    y -= 24
    c.setFont("Helvetica-Bold", 8.5)
    c.drawString(40, y, "Transaktionsdatum")
    c.drawString(125, y, "Buchungsdatum")
    c.drawString(210, y, "Beschreibung")
    c.drawRightString(555, y, "Betrag CHF")
    c.setFont("Helvetica", 8.5)
    y -= 18
    owed = SC_OPENING
    for d, lines, amt in SC_ROWS:
        owed = round(owed + amt, 2)
        c.drawString(40, y, d)
        c.drawString(125, y, d)
        c.drawString(210, y, lines[0])
        c.drawRightString(555, y, chf(amt))
        for extra in lines[1:]:
            y -= 11
            c.drawString(210, y, extra)
        y -= 16
    y -= 8
    c.drawString(210, y, "Neuer Saldo")
    c.drawRightString(555, y, chf(owed))
    y -= 14
    c.drawString(210, y, "Total zu bezahlen bis 10.10.2026")
    c.drawRightString(555, y, chf(owed))
    c.drawString(40, 40, "Seite 1/1")
    c.save()
    exp = [("%s-%s-%s" % (d[6:], d[3:5], d[:2]), -amt, "CHF", lines[0].split()[0])
           for d, lines, amt in SC_ROWS]
    return buf.getvalue(), exp


# ---------------------------------------------------------------- Wise

WISE = {  # currency -> (opening, chronological rows (date, description, amount))
    "CHF": (120.00, [
        ("12 September 2026", "Topped up from PostFinance", 500.00),
        ("14 September 2026", "Converted 200.00 CHF to 212.40 EUR", -200.00),
        ("16 September 2026", "Card transaction of 31.50 CHF issued by Hiltl Zurich", -31.50),
    ]),
    "EUR": (10.00, [
        ("14 September 2026", "Converted 200.00 CHF to 212.40 EUR", 212.40),
        ("18 September 2026", "Card transaction of 89.00 EUR issued by Ryanair Dublin", -89.00),
        ("20 September 2026", "Card transaction of 25.00 EUR issued by Cafe de Flore Paris", -25.00),
    ]),
}
MONTH_NUM = {"September": "09"}


def wise():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    exp = []
    for cur, (opening, rows) in WISE.items():
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, H - 50, "Wise")
        c.setFont("Helvetica", 9)
        c.drawString(40, H - 66, "Wise Europe SA, Rue du Trône 100, 1050 Brussels · wise.com")
        c.setFont("Helvetica-Bold", 11)
        c.drawString(40, H - 92, "%s statement" % cur)
        c.setFont("Helvetica", 9)
        c.drawString(40, H - 106, "1 September 2026 - 30 September 2026")
        y = H - 136
        c.setFont("Helvetica-Bold", 8.5)
        c.drawString(40, y, "Description")
        c.drawRightString(400, y, "Incoming")
        c.drawRightString(470, y, "Outgoing")
        c.drawRightString(555, y, "Balance")
        c.setFont("Helvetica", 8.5)
        y -= 18
        bal, balances = opening, []
        for d, desc, amt in rows:
            bal = round(bal + amt, 2)
            balances.append(bal)
        for (d, desc, amt), b in reversed(list(zip(rows, balances))):  # newest first
            c.drawString(40, y, desc)
            c.drawRightString(400 if amt > 0 else 470, y, en(amt))
            c.drawRightString(555, y, en(b))
            y -= 11
            c.setFillGray(0.4)
            c.drawString(40, y, "%s   Transaction: CARD-%d" % (d, abs(int(amt * 100))))
            c.setFillGray(0)
            y -= 18
            day, month, year = d.split()
            exp.append(("%s-%s-%02d" % (year, MONTH_NUM[month], int(day)), amt, cur, desc.split()[0]))
        c.drawString(40, 40, "Page 1 of 1")
        c.showPage()
    c.save()
    return buf.getvalue(), exp


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "sample")
    out.mkdir(parents=True, exist_ok=True)
    for name, fn in [("postfinance-2026-09.pdf", postfinance), ("swisscard-2026-09.pdf", swisscard),
                     ("wise-2026-09.pdf", wise)]:
        (out / name).write_bytes(fn()[0])
        print("wrote", out / name)


# ---------------------------------------------------------------- PostFinance (English, real layout)

PF_EN_DAYS = [  # (date or None when the bank doesn't repeat it, description lines, amount, value date, balance shown)
    ("19.09.26", ["APPLE PAY SALES/SERVICES OF", "18.09.2026", "CARD NO. XXXX1234", "MC MUSTERLADEN, EINKAUFSZ",
                  "MUSTERDORF (CH)"], -37.60, "18.09.26", False),
    (None, ["TWINT RECEIVE MONEY FROM", "19.09.2026", "FROM MOBILE NO.", "+41790000000", "MUSTER, ANNA"],
     18.50, "19.09.26", True),
    ("25.09.26", ["CREDIT", "SENDER: ACME AG", "ZURICH", "MESSAGE: SALARY SEPTEMBER"], 7450.00, "25.09.26", False),
    (None, ["TWINT RECEIVE MONEY FROM", "25.09.2026", "FROM MOBILE NO.", "+41790000001", "BEISPIEL, MARC"],
     1200.00, "25.09.26", False),
    (None, ["PURCHASE/SERVICE OF", "25.09.2026", "CARD NO. XXXX1234", "MIGROS M ZURICH"], -87.35, "25.09.26", True),
]
PF_EN_OPENING = 6543.21


def postfinance_en_daily():
    """English PostFinance layout as seen on real statements: date and balance only once per day,
    multi-line descriptions with their own date line, space as thousands separator.
    Only the layout is copied; every name, merchant and amount is made up."""
    sp = lambda v: chf(v, " ")
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, H - 50, "PostFinance")
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 70, "Account statement")
    c.drawString(40, H - 82, "IBAN CH93 0900 0000 1234 5678 9")
    c.drawString(40, H - 94, "Currency CHF")
    y = H - 130
    c.setFont("Helvetica-Bold", 8.5)
    for x, t in [(14, "Date"), (83, "Details"), (300, "Credit"), (385, "Debit"), (437, "Value"), (520, "Balance")]:
        c.drawString(x, y, t)
    c.setFont("Helvetica", 9)
    y -= 18
    c.drawString(83, y, "Opening balance")
    c.drawRightString(567, y, sp(PF_EN_OPENING))
    y -= 8
    bal = PF_EN_OPENING
    for d, lines, amt, vd, show_bal in PF_EN_DAYS:
        bal = round(bal + amt, 2)
        c.line(14, y, 575, y)
        y -= 14
        if d:
            c.drawString(14, y, d)
        c.drawString(83, y, lines[0])
        c.drawRightString(335 if amt > 0 else 422, y, sp(abs(amt)))
        c.drawString(437, y, vd)
        if show_bal:
            c.drawRightString(567, y, sp(bal))
        for extra in lines[1:]:
            y -= 13.5
            c.drawString(83, y, extra)
        y -= 6
    c.line(14, y, 575, y)
    y -= 16
    c.drawString(83, y, "Closing balance")
    c.drawRightString(567, y, sp(bal))
    c.save()
    exp = [("20%s-%s-%s" % (vd[6:8], vd[3:5], vd[:2]), amt, "CHF", lines[0].split()[0])
           for d, lines, amt, vd, _ in PF_EN_DAYS]  # the value date is the transaction date
    return buf.getvalue(), exp
