"""Use-of-proceeds specificity from the phrase rules in scoring.json."""

import json
import re
from pathlib import Path

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
    "$": "USD",
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
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "scoring.json"

BUCKETS = ("undecided", "0", "1", "2", "3", "4", "5+")


class Result:
    def __init__(self, score, decided, matched, excerpt):
        self.score = score
        self.decided = decided
        self.matched = list(matched)
        self.excerpt = excerpt


def load_config(path=CONFIG_PATH):
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rules = []
    for rule in payload["rules"]:
        rules.append(
            {
                "id": rule["id"],
                "weight": int(rule["weight"]),
                "pattern": re.compile(rule["pattern"], re.I),
            }
        )
    return {
        "threshold": float(payload["threshold"]),
        "mid_min": float(payload["mid_min"]),
        "mid_max": float(payload["mid_max"]),
        "rules": rules,
    }


def score_text(text, rules):
    """Highest matching weight. No match, or empty text, is undecided."""
    plain = plain_text(text)
    if not plain:
        return Result(None, False, [], "")
    matched = []
    for rule in rules:
        found = rule["pattern"].search(plain)
        if found:
            matched.append((rule, found))
    if not matched:
        return Result(None, False, [], "")
    best_rule, best_match = max(matched, key=lambda item: item[0]["weight"])
    return Result(
        best_rule["weight"],
        True,
        [rule["id"] for rule, _match in matched],
        excerpt(plain, best_match),
    )


def needs_full_document(result, threshold):
    """Full read when the cheap score is above the threshold or not decided."""
    if not result.decided or result.score is None:
        return True
    return result.score > threshold


def histogram(scores):
    counts = {bucket: 0 for bucket in BUCKETS}
    for score in scores:
        counts[bucket_for(score)] += 1
    return counts


def bucket_for(score):
    if score is None:
        return "undecided"
    if score >= 5:
        return "5+"
    return str(int(score))


def plain_text(text):
    raw = text or ""
    raw = re.sub(r"(?is)<script\b.*?>.*?</script>", " ", raw)
    raw = re.sub(r"(?is)<style\b.*?>.*?</style>", " ", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def excerpt(plain, match, window=80):
    start = max(0, match.start() - window)
    end = min(len(plain), match.end() + window)
    return redact(plain[start:end].strip())


def redact(text):
    return _EMAIL.sub("[redacted]", text or "")


_AGGREGATE = re.compile(
    r"aggregate (?:principal |offering )?(?:amount|price)|aggregate principal|offering amount",
    re.I,
)
_DENOMINATION = re.compile(
    r"denominat|integral multiple|minimum (?:denomination|purchase)",
    re.I,
)
_PAR = re.compile(r"\bpar (?:value|amount)\b", re.I)
_FEE = re.compile(r"\b(?:registration|filing|commission)\s+fee\b|\bsec fee\b", re.I)


def _amount_from(match):
    scale = _SCALE.get((match.group("scale") or "").casefold(), 1)
    number = float(match.group("num").replace(",", ""))
    return {
        "value": number * scale,
        "currency": _CURRENCY[(match.group("cur") or "").casefold()],
        "text": match.group(0).strip(),
    }


def _amount_window(plain, match, radius=40):
    return plain[max(0, match.start() - radius) : match.end() + radius]


def _skip_amount(plain, match):
    """Denominations, par values, fees, and sub-dollar figures are not the raise."""
    amount = _amount_from(match)
    if amount["value"] < 1:
        return True
    window = _amount_window(plain, match)
    if _PAR.search(window) or _DENOMINATION.search(window):
        return True
    if _FEE.search(window) and not _AGGREGATE.search(window):
        return True
    return False


def first_amount(text):
    """Stated money amount. Prefer the aggregate offering over the first hit.

    Skips $2,000/$1,000 denomination minimums, par values such as $0.01 and
    $0.0001, registration fees, and numbers with no currency. A later
    parenthetical conversion is left out when an earlier amount is kept.
    """
    plain = plain_text(text)
    kept = [match for match in _MONEY.finditer(plain) if not _skip_amount(plain, match)]
    if not kept:
        return None
    preferred = [match for match in kept if _AGGREGATE.search(_amount_window(plain, match))]
    return _amount_from(preferred[0] if preferred else kept[0])


def figure_kind(text):
    folded = plain_text(text).casefold()
    if "bond" in folded or "senior note" in folded or re.search(r"\bnotes\b", folded):
        return "bond", "bond_amount"
    if "facility" in folded or "credit agreement" in folded or "credit facility" in folded:
        return "facility", "facility_amount"
    if "loan" in folded:
        return "loan", "loan_amount"
    return "", "proceeds_amount"
