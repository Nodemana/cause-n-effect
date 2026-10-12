"""First stated money amount in a headline or filing description."""

import re

_MONEY = re.compile(
    r"(?P<cur>US\$|A\$|NZ\$|NOK|SEK|USD|AUD|EUR|GBP|€|£|\$)\s*"
    r"(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<scale>billion|million|thousand|bn|mn|m\b)?",
    re.I,
)
_SCALE = {
    "billion": 1_000_000_000,
    "bn": 1_000_000_000,
    "million": 1_000_000,
    "mn": 1_000_000,
    "m": 1_000_000,
    "thousand": 1_000,
}
_CURRENCY = {
    "us$": "USD",
    "usd": "USD",
    "a$": "AUD",
    "aud": "AUD",
    "nz$": "NZD",
    "nok": "NOK",
    "sek": "SEK",
    "eur": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "£": "GBP",
}


def first_amount(text, dollar="USD"):
    """Return value, currency, and the literal text. None when no amount is stated.

    `dollar` is the currency of a bare `$`. SEC prose uses USD. An ASX headline uses AUD.
    """
    match = _MONEY.search(text or "")
    if not match:
        return None
    scale = _SCALE.get((match.group("scale") or "").casefold(), 1)
    number = float(match.group("num").replace(",", ""))
    token = match.group("cur")
    if token == "$":
        currency = dollar
    else:
        currency = _CURRENCY.get(token.casefold(), token)
    return {
        "value": number * scale,
        "currency": currency,
        "text": match.group(0).strip(),
    }
