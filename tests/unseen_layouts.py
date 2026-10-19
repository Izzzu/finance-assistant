"""Synthetic statements in layouts nothing in this app was ever tuned for (made-up data).

Used to test whether a method generalizes. All data is made up.
Each builder returns (pdf_bytes, expected) with expected = [(date, amount, currency, fragment)].
"""
import io

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

W, H = A4


def _chf(v, sep="'"):
    return "{:,.2f}".format(abs(v)).replace(",", sep)


# --------------------------------------------------------------- 1. neo-bank (money out / money in, text dates)
NEO = [("3 Sep 2026", "Migros Zurich HB", -87.35), ("4 Sep 2026", "Salary ACME AG", 7450.00),
       ("6 Sep 2026", "Transfer from Anna Muster", 45.00), ("9 Sep 2026", "Galaxus.ch", -249.90),
       ("12 Sep 2026", "SBB Mobile ticket Zurich - Bern", -51.00), ("15 Sep 2026", "Netflix.com", -18.90),
       ("21 Sep 2026", "Coffee shop Kafi Zurich", -6.50), ("28 Sep 2026", "Rent October Immo AG", -2100.00)]


def neobank():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, H - 60, "NeoBank Account Statement")
    c.setFont("Helvetica", 9)
    c.drawString(50, H - 78, "CHF Statement · Generated on 1 Oct 2026")
    c.drawString(50, H - 92, "Balance summary")
    opening = 1200.00
    total_in = sum(a for _, _, a in NEO if a > 0)
    total_out = -sum(a for _, _, a in NEO if a < 0)
    closing = round(opening + total_in - total_out, 2)
    y = H - 110
    for label, v in [("Opening balance", opening), ("Money out", total_out), ("Money in", total_in), ("Closing balance", closing)]:
        c.drawString(60, y, label)
        c.drawRightString(260, y, "CHF " + _chf(v, ","))
        y -= 13
    y -= 20
    c.setFont("Helvetica-Bold", 9)
    for x, t in [(50, "Date"), (140, "Description"), (390, "Money out"), (460, "Money in"), (530, "Balance")]:
        c.drawString(x, y, t)
    c.setFont("Helvetica", 9)
    y -= 16
    bal = opening
    for d, desc, a in NEO:
        bal = round(bal + a, 2)
        c.drawString(50, y, d)
        c.drawString(140, y, desc)
        c.drawRightString(440 if a < 0 else 510, y, "CHF " + _chf(a, ","))
        c.drawRightString(570, y, "CHF " + _chf(bal, ","))
        y -= 18
    c.save()
    months = {"Sep": "09"}
    exp = [("2026-%s-%02d" % (months[d.split()[1]], int(d.split()[0])), a, "CHF", desc.split()[0]) for d, desc, a in NEO]
    return buf.getvalue(), exp


# --------------------------------------------------------------- 2. cantonal bank: day headings, one signed column
KB = [("Mittwoch, 03.09.2026", [("Einkauf Coop Pronto Bern", -23.40), ("Einkauf Apotheke Bahnhof", -32.50)]),
      ("Donnerstag, 04.09.2026", [("Lohnzahlung ACME AG", 7450.00)]),
      ("Montag, 08.09.2026", [("Dauerauftrag Miete Immo AG", -2100.00), ("TWINT von Muster Anna", 45.00),
                               ("Einkauf IKEA Spreitenbach", -212.00)]),
      ("Freitag, 19.09.2026", [("Krankenkasse Helsana Praemie", -398.50)])]


def cantonal():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(45, H - 55, "Kantonalbank Muster · Kontoauszug")
    c.setFont("Helvetica", 9)
    c.drawString(45, H - 72, "Privatkonto CH12 0000 0000 1111 2222 3  ·  01.09.2026 - 30.09.2026")
    y = H - 105
    c.setFont("Helvetica-Bold", 9)
    for x, t in [(45, "Buchungstext"), (380, "Betrag CHF"), (480, "Saldo CHF")]:
        c.drawString(x, y, t)
    c.setFont("Helvetica", 9)
    y -= 16
    bal = 3000.00
    c.drawString(45, y, "Saldovortrag")
    c.drawRightString(540, y, _chf(bal))
    y -= 20
    exp = []
    for day, items in KB:
        c.setFont("Helvetica-Bold", 9)
        c.drawString(45, y, day)
        c.setFont("Helvetica", 9)
        y -= 14
        d = day.split(", ")[1]
        for desc, a in items:
            bal = round(bal + a, 2)
            c.drawString(60, y, desc)
            c.drawRightString(430, y, ("-" if a < 0 else "+") + _chf(a))
            c.drawRightString(540, y, _chf(bal))
            exp.append(("%s-%s-%s" % (d[6:10], d[3:5], d[0:2]), a, "CHF", desc.split()[0]))
            y -= 14
        y -= 8
    c.drawString(45, y, "Saldo per 30.09.2026")
    c.drawRightString(540, y, _chf(bal))
    c.save()
    return buf.getvalue(), exp


# --------------------------------------------------------------- 3. credit card: location + original currency columns
CC = [("02.09.2026", "AMAZON EU SARL", "LUXEMBOURG", "EUR 45.00", 42.35),
      ("05.09.2026", "SPOTIFY AB", "STOCKHOLM", "", 12.95),
      ("11.09.2026", "RESTAURANT HILTL", "ZURICH", "", 64.80),
      ("14.09.2026", "BOOKING.COM", "AMSTERDAM", "EUR 320.00", 301.15),
      ("20.09.2026", "PAYMENT RECEIVED - THANK YOU", "", "", -900.00),
      ("27.09.2026", "SHELL TANKSTELLE", "WINTERTHUR", "", 88.10)]


def creditcard():
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(40, H - 55, "CardCo Monthly Statement")
    c.setFont("Helvetica", 9)
    c.drawString(40, H - 72, "Card number **** **** **** 9876   Statement date 30.09.2026")
    previous = 900.00
    new = round(previous + sum(a for *_, a in CC), 2)
    c.drawString(40, H - 92, "Previous balance")
    c.drawRightString(250, H - 92, _chf(previous))
    c.drawString(40, H - 105, "New balance")
    c.drawRightString(250, H - 105, _chf(new))
    y = H - 140
    c.setFont("Helvetica-Bold", 8.5)
    for x, t in [(40, "Date"), (110, "Merchant"), (290, "Location"), (390, "Original"), (500, "CHF")]:
        c.drawString(x, y, t)
    c.setFont("Helvetica", 8.5)
    y -= 16
    for d, m, loc, orig, a in CC:
        c.drawString(40, y, d)
        c.drawString(110, y, m)
        c.drawString(290, y, loc)
        c.drawString(390, y, orig)
        c.drawRightString(560, y, _chf(a) + (" CR" if a < 0 else ""))
        y -= 15
    c.save()
    # spending is negative for the user; the card payment is money in
    exp = [("%s-%s-%s" % (d[6:], d[3:5], d[:2]), -a, "CHF", m.split()[0]) for d, m, loc, orig, a in CC]
    return buf.getvalue(), exp
