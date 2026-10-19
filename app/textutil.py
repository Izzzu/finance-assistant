"""Text normalization shared by importers, rules and the categorizer."""
from __future__ import annotations

import re
import unicodedata

# Words that carry no information about *who* was paid. Removed before matching/grouping.
# "zahlung"/"payment" are kept on purpose: default rules such as "ihre zahlung" need them.
NOISE = set("""
card cards purchase pos transaction transactions trans debit credit visa mastercard
maestro contactless online www com ch de eu net ltd llc inc gmbh ag sa sarl srl co
kauf dienstleistung dienstleistungen vom karten karte kartennummer nr einkauf belastung
gutschrift lastschrift auftrag dauerauftrag ueberweisung uberweisung e-banking ebanking
achat paiement carte versement sales services service mobile
issued by of on to from the and ending in an fur pour per
chf eur usd gbp pln sek nok dkk czk huf jpy cad aud
""".split())

MONTHS = {
    "jan": 1, "january": 1, "januar": 1, "janvier": 1, "gennaio": 1,
    "feb": 2, "february": 2, "februar": 2, "fevrier": 2, "febbraio": 2,
    "mar": 3, "march": 3, "marz": 3, "maerz": 3, "mars": 3, "marzo": 3,
    "apr": 4, "april": 4, "avril": 4, "aprile": 4,
    "may": 5, "mai": 5, "maggio": 5,
    "jun": 6, "june": 6, "juni": 6, "juin": 6, "giugno": 6,
    "jul": 7, "july": 7, "juli": 7, "juillet": 7, "luglio": 7,
    "aug": 8, "august": 8, "aout": 8, "agosto": 8,
    "sep": 9, "sept": 9, "september": 9, "septembre": 9, "settembre": 9,
    "oct": 10, "october": 10, "okt": 10, "oktober": 10, "octobre": 10, "ottobre": 10,
    "nov": 11, "november": 11, "novembre": 11,
    "dec": 12, "december": 12, "dez": 12, "dezember": 12, "decembre": 12, "dicembre": 12,
}


def fold(s: str) -> str:
    """Lowercase and strip accents (ł -> l, ß -> ss) for matching."""
    s = s.lower().replace("ł", "l").replace("ß", "ss")
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


# Bank phrases that say *how* you paid, not *whom*. Removed as whole phrases.
BOILERPLATE = [
    "twint receive money from", "twint send money to", "twint money received from", "twint money sent to",
    "receive money from", "send money to", "from mobile no", "to mobile no", "mobile no",
    "apple pay", "google pay", "samsung pay", "sales services of", "sales service of",
    "purchase service of", "purchase services of", "card no",
]
_BOILERPLATE_RE = re.compile(r"\b(%s)\b" % "|".join(re.escape(p) for p in sorted(BOILERPLATE, key=len, reverse=True)))


def normalize(desc: str | None) -> str:
    """'KAUF/DIENSTLEISTUNG VOM 03.09.2026 KARTEN NR. XXXX1234 MIGROS M ZUERICH' -> 'migros zuerich'."""
    s = re.sub(r"[^a-z0-9]+", " ", fold(desc or ""))
    s = re.sub(r"\s+", " ", s)
    keep_twint = " twint " in " %s " % s
    s = _BOILERPLATE_RE.sub(" ", s)
    if keep_twint and "twint" not in s:
        s = "twint " + s  # keep the payment method word for rules like "twint", but not as the key
    words = [w for w in s.split() if w not in NOISE and len(w) > 1 and not re.search(r"\d", w)
             and not re.fullmatch(r"x+", w)]
    return " ".join(words)


# Generic payment words: kept for rules, but skipped when grouping by merchant/person.
KEY_SKIP = {"zahlung", "payment", "payments", "twint", "ihre", "sent", "money", "received", "receive", "send"}


def is_generic(pattern: str) -> bool:
    """True if a rule pattern only contains payment boilerplate (it would match far too much)."""
    words = normalize(pattern).split()
    return not words or all(w in KEY_SKIP or w in NOISE or len(w) <= 2 for w in words)


def merchant_key(norm: str) -> str:
    words = [w for w in norm.split() if w not in KEY_SKIP] or norm.split()
    return " ".join(words[:3]) if words else "unknown"


def matches(pattern: str, norm: str) -> bool:
    """Whole-word containment: 'coop' matches 'coop city' but not 'cooperative'."""
    return bool(pattern) and (" %s " % pattern) in (" %s " % norm)


def last4(text: str) -> str:
    """Last 4 characters of the first IBAN or card number in text, never the full number."""
    m = re.search(r"\b[A-Z]{2}\d{2}(?:\s?[0-9A-Z]{4}){3,7}(?:\s?[0-9A-Z]{1,3})?\b", text)
    if m:
        return re.sub(r"\s", "", m.group(0))[-4:]
    m = re.search(r"(?:\d{4}|[X*•]{4})[\sX*•\d]{6,}?(\d{4})\b", text)
    return m.group(1) if m else ""
