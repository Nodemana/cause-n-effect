"""Compact currency text for stored figures. No network."""

_SYMBOLS = {
    "USD": "$",
    "AUD": "A$",
    "EUR": "€",
    "GBP": "£",
    "CAD": "C$",
    "NZD": "NZ$",
}


def format_money(value, currency=None):
    """A dollar amount with its currency, scaled when it reaches millions.

    240316 AUD is `A$240,316`. 28500000000 USD is `$28.5B`.
    """
    code = (currency or "USD").upper()
    prefix = _SYMBOLS.get(code, f"{code} ")
    number = float(value)
    sign = "-" if number < 0 else ""
    amount = abs(number)
    if amount >= 1_000_000_000:
        body = _scaled(amount / 1_000_000_000) + "B"
    elif amount >= 1_000_000:
        body = _scaled(amount / 1_000_000) + "M"
    elif abs(amount - round(amount)) < 0.005:
        body = f"{int(round(amount)):,}"
    else:
        body = f"{amount:,.2f}"
    return f"{sign}{prefix}{body}"


def _scaled(number):
    rounded = round(number, 1)
    if abs(rounded - round(rounded)) < 1e-9:
        return str(int(round(rounded)))
    return f"{rounded:.1f}"
