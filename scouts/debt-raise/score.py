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


def first_amount(text):
    """First stated money amount. A later parenthetical conversion is left out."""
    plain = plain_text(text)
    match = _MONEY.search(plain)
    if not match:
        return None
    scale = _SCALE.get((match.group("scale") or "").casefold(), 1)
    number = float(match.group("num").replace(",", ""))
    return {
        "value": number * scale,
        "currency": _CURRENCY[(match.group("cur") or "").casefold()],
        "text": match.group(0).strip(),
    }


def figure_kind(text):
    folded = plain_text(text).casefold()
    if "bond" in folded or "senior note" in folded or re.search(r"\bnotes\b", folded):
        return "bond", "bond_amount"
    if "facility" in folded or "credit agreement" in folded or "credit facility" in folded:
        return "facility", "facility_amount"
    if "loan" in folded:
        return "loan", "loan_amount"
    return "", "proceeds_amount"
