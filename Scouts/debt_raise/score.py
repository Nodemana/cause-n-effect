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
        "min_deal_usd": float(payload.get("min_deal_usd", DEFAULT_MIN_DEAL_USD)),
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


# Deal size ignores boilerplate under this amount unless the caller overrides it.
DEFAULT_MIN_DEAL_USD = 50_000_000

_PRINCIPAL = re.compile(r"aggregate principal amount", re.I)
_OFFERING = re.compile(r"\boffering\b", re.I)
_NOISE_BEFORE = re.compile(
    r"(?:less than|at least|greater than|more than|up to|minimum denomination|"
    r"offering price(?: of)?|discount of|commission of|fee of)\s*$",
    re.I,
)
_NOISE_AFTER = re.compile(r"^\s*(?:or less|or fewer|or more|per note|per share)\b", re.I)
_STATED_TOTAL = re.compile(
    r"total aggregate principal|\bin the aggregate\s*$",
    re.I,
)
_IN_THE_AGGREGATE = re.compile(r"^\s*in the aggregate\b", re.I)


def first_amount(text):
    """First stated money amount. A later parenthetical conversion is left out."""
    found = money_amounts(text)
    if not found:
        return None
    return _public_amount(found[0])


def money_amounts(text):
    """Every money amount in document order, with offsets into the plain text."""
    plain = plain_text(text)
    found = []
    for match in _MONEY.finditer(plain):
        scale = _SCALE.get((match.group("scale") or "").casefold(), 1)
        number = float(match.group("num").replace(",", ""))
        found.append(
            {
                "value": number * scale,
                "currency": _CURRENCY[(match.group("cur") or "").casefold()],
                "text": match.group(0).strip(),
                "start": match.start(),
                "end": match.end(),
            }
        )
    return found


def deal_amount(text, min_deal=DEFAULT_MIN_DEAL_USD):
    """Deal size from amounts tied to the offering, not the first dollar figure.

    Amounts next to "aggregate principal amount" win over amounts that only sit
    near "offering". A stated total is kept when the filing gives one. Otherwise
    tranche principal amounts are summed. A parenthetical currency conversion is
    not a second tranche. When nothing is tied to those phrases, the first
    stated amount is used so a facility can still be read.
    """
    plain = plain_text(text)
    amounts = [
        item
        for item in money_amounts(plain)
        if not _parenthetical(plain, item["start"]) and not _noise_amount(plain, item)
    ]
    if not amounts:
        return None
    principal = [item for item in amounts if _near(plain, item, _PRINCIPAL)]
    offering = [item for item in amounts if _near(plain, item, _OFFERING)]
    pool = principal or offering
    if not pool:
        return _public_amount(amounts[0])
    picked = _combine_deal(plain, pool, min_deal)
    return _public_amount(picked) if picked else None


def _combine_deal(plain, pool, min_deal):
    """Stated total, else the sum of the tranches, else the largest tied amount."""
    floor = float(min_deal)
    totals = [item for item in pool if _stated_total(plain, item)]
    candidates = totals or pool
    if any(item["value"] >= floor for item in candidates):
        candidates = [item for item in candidates if item["value"] >= floor]
    candidates = _one_currency(candidates)
    if not candidates:
        return None
    if totals:
        return max(candidates, key=lambda item: item["value"])
    if len(candidates) == 1:
        return candidates[0]
    largest = max(candidates, key=lambda item: item["value"])
    rest = [item for item in candidates if item is not largest]
    rest_sum = sum(item["value"] for item in rest)
    if abs(largest["value"] - rest_sum) <= 1:
        return largest
    return {
        "value": sum(item["value"] for item in candidates),
        "currency": candidates[0]["currency"],
        "text": " + ".join(item["text"] for item in candidates),
        "start": candidates[0]["start"],
        "end": candidates[-1]["end"],
    }


def _one_currency(pool):
    currencies = {item["currency"] for item in pool}
    if len(currencies) <= 1:
        return pool
    usd = [item for item in pool if item["currency"] == "USD"]
    if usd:
        return usd
    chosen = max(pool, key=lambda item: item["value"])["currency"]
    return [item for item in pool if item["currency"] == chosen]


def _public_amount(item):
    return {
        "value": item["value"],
        "currency": item["currency"],
        "text": item["text"],
    }


def _near(plain, item, pattern, radius=80):
    window = plain[max(0, item["start"] - radius) : item["end"] + radius]
    return pattern.search(window) is not None


def _parenthetical(plain, start):
    index = start - 1
    while index >= 0 and plain[index] == " ":
        index -= 1
    return index >= 0 and plain[index] == "("


def _noise_amount(plain, item):
    """Covenant floors, fees, and per-note prices are not the deal size."""
    prefix = plain[max(0, item["start"] - 40) : item["start"]]
    suffix = plain[item["end"] : item["end"] + 20]
    if _NOISE_BEFORE.search(prefix):
        return True
    return _NOISE_AFTER.search(suffix) is not None


def _stated_total(plain, item):
    before = plain[max(0, item["start"] - 110) : item["start"]]
    after = plain[item["end"] : item["end"] + 24]
    if _STATED_TOTAL.search(before):
        return True
    return _IN_THE_AGGREGATE.search(after) is not None


def figure_kind(text):
    folded = plain_text(text).casefold()
    if "bond" in folded or "senior note" in folded or re.search(r"\bnotes\b", folded):
        return "bond", "bond_amount"
    if "facility" in folded or "credit agreement" in folded or "credit facility" in folded:
        return "facility", "facility_amount"
    if "loan" in folded:
        return "loan", "loan_amount"
    return "", "proceeds_amount"
