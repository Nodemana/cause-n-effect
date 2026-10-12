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


# Under US$1,000,000 is a denomination, a par figure, or a fee, not a deal.
_MIN_DEAL = 1_000_000
# Structured notes of a few million are real deals above that floor. The
# pipeline drops them unless the caller lowers this store floor.
DEFAULT_MIN_DEAL_USD = 50_000_000
_BEFORE = 120
_AFTER = 80
_AGGREGATE = re.compile(r"aggregate principal(?:\s+amount)?", re.I)
_OFFERING_PHRASE = re.compile(r"aggregate offering(?:\s+price)?|offering amount", re.I)
_OFFERING_OF = re.compile(
    r"\boffering of(?:\s+(?:approximately|about|up to))?\s*$",
    re.I,
)
_COVER_TOTAL = re.compile(r"\btotal(?:\s+amount)?(?:\s+of)?\s*$", re.I)
_REJECT = re.compile(
    r"\bdenominations?\b|\bintegral multiples?\b|\bpar value\b|\bper notes?\b|"
    r"\bfees?\b|\bunderwriting discounts?\b|\bcommissions?\b",
    re.I,
)
_CONVERSION_OPEN = re.compile(
    r"\s*\(\s*(?:approximately\s+|about\s+|or\s+)?$",
    re.I,
)
_CONVERSION_CLOSE = re.compile(r"(?:\s+[A-Za-z]+){0,4}\s*\)")
_SENTENCE = re.compile(r"\.\s+")
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


def _amount_from(match):
    scale = _SCALE.get((match.group("scale") or "").casefold(), 1)
    number = float(match.group("num").replace(",", ""))
    return {
        "value": number * scale,
        "currency": _CURRENCY[(match.group("cur") or "").casefold()],
        "text": match.group(0).strip(),
    }


def _nearest(before, after, pattern):
    best = None
    for found in pattern.finditer(before):
        dist = len(before) - found.end()
        if best is None or dist < best:
            best = dist
    for found in pattern.finditer(after):
        dist = found.start()
        if best is None or dist < best:
            best = dist
    return best


def _labeled_wins(dist, reject_at):
    return dist is not None and (reject_at is None or dist <= reject_at)


def _conversion_indexes(plain, matches):
    """Parenthetical currency conversions sit beside the amount they restate."""
    skip = set()
    for index, match in enumerate(matches[:-1]):
        nxt = matches[index + 1]
        between = plain[match.end() : nxt.start()]
        after = plain[nxt.end() : nxt.end() + 24]
        if _CONVERSION_OPEN.fullmatch(between) and _CONVERSION_CLOSE.match(after):
            skip.add(index + 1)
    return skip


def _sentence_before(gap):
    return _SENTENCE.split(gap)[-1][-_BEFORE:]


def _sentence_after(gap):
    return _SENTENCE.split(gap, maxsplit=1)[0][:_AFTER]


def _classify(plain, match, prev_end, next_start):
    amount = _amount_from(match)
    amount["start"] = match.start()
    amount["end"] = match.end()
    if amount["value"] < _MIN_DEAL:
        return None
    # Labels bind inside the current sentence, so the next sentence's
    # "aggregate principal amount" is not attached to the previous figure.
    before = _sentence_before(plain[prev_end : match.start()])
    after = _sentence_after(plain[match.end() : next_start])
    if _NOISE_BEFORE.search(before[-40:]) or _NOISE_AFTER.match(after):
        return None
    reject_at = _nearest(before, after, _REJECT)
    aggregate_at = _nearest(before, after, _AGGREGATE)
    offering_at = _nearest(before, after, _OFFERING_PHRASE)
    if _OFFERING_OF.search(before):
        offering_at = 0 if offering_at is None else min(offering_at, 0)
    cover_at = 0 if _COVER_TOTAL.search(before) else None
    labeled = [dist for dist in (aggregate_at, offering_at, cover_at) if dist is not None]
    prefer_at = min(labeled) if labeled else None
    if reject_at is not None and (prefer_at is None or reject_at < prefer_at):
        return None
    if _labeled_wins(aggregate_at, reject_at):
        tier = 3
    elif _labeled_wins(offering_at, reject_at):
        tier = 2
    elif _labeled_wins(cover_at, reject_at):
        tier = 1
    else:
        tier = 0
    amount["tier"] = tier
    return amount


def _one_value(group):
    return len({round(item["value"], 2) for item in group}) == 1


def _choose(candidates, plain, min_deal):
    if not candidates:
        return None, True
    for tier in (3, 2, 1):
        group = [item for item in candidates if item["tier"] == tier]
        if not group:
            continue
        if tier == 3:
            combined = _combine_principal(group, plain, min_deal)
            if combined is None:
                return None, True
            return combined, False
        if _one_value(group):
            return group[0], False
        return None, True
    if _one_value(candidates):
        return candidates[0], False
    return None, True


def _combine_principal(group, plain, min_deal):
    """Stated total, else the sum of the tranches, else the largest principal.

    A figure under `min_deal` is left out of the sum when a larger principal
    amount is present, so a covenant floor is not added to the deal.
    """
    floor = float(min_deal)
    totals = [item for item in group if _stated_total(plain, item)]
    candidates = totals or list(group)
    if any(item["value"] >= floor for item in candidates):
        candidates = [item for item in candidates if item["value"] >= floor]
    if not candidates:
        return None
    candidates = _one_currency(candidates)
    if totals:
        return max(candidates, key=lambda item: item["value"])
    if _one_value(candidates) or len(candidates) == 1:
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


def _stated_total(plain, item):
    before = plain[max(0, item["start"] - 110) : item["start"]]
    after = plain[item["end"] : item["end"] + 24]
    if _STATED_TOTAL.search(before):
        return True
    return _IN_THE_AGGREGATE.search(after) is not None


def pick_amount(text, min_deal=DEFAULT_MIN_DEAL_USD):
    """Return (amount or None, needs_review).

    Prefer an aggregate principal amount, then "offering of $X" or an aggregate
    offering, then a cover-page total. Several aggregate-principal tranches are
    summed, or a stated total is kept. Amounts near denominations, integral
    multiples, par value, per note, or a fee are ignored, as is anything under
    US$1,000,000. needs_review is true when money was stated but no single
    plausible deal size is clear, so the caller stores nothing.
    """
    plain = plain_text(text).replace("\u00ad", "").replace("\u200b", "")
    matches = list(_MONEY.finditer(plain))
    if not matches:
        return None, False
    skipped = _conversion_indexes(plain, matches)
    candidates = []
    for index, match in enumerate(matches):
        if index in skipped:
            continue
        prev_end = matches[index - 1].end() if index else 0
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(plain)
        parsed = _classify(plain, match, prev_end, next_start)
        if parsed is not None:
            candidates.append(parsed)
    amount, needs_review = _choose(candidates, plain, min_deal)
    if amount is not None:
        amount = {
            "value": amount["value"],
            "currency": amount["currency"],
            "text": amount["text"],
        }
    return amount, needs_review


def first_amount(text):
    """Deal size, or None when the text does not state one.

    A later parenthetical currency conversion is left out. Denominations,
    integral multiples, par value, per-note prices, fees, and amounts under
    US$1,000,000 are not a deal.
    """
    amount, _needs_review = pick_amount(text)
    return amount


def deal_amount(text, min_deal=DEFAULT_MIN_DEAL_USD):
    """Deal size from aggregate principal, or None when the text does not state one.

    Tranche principal amounts are summed unless the filing states a total.
    A figure under `min_deal` is left out of that sum when a larger principal
    amount is present. A deal that is itself under the floor is still returned
    so the caller can log it instead of storing it.
    """
    amount, _needs_review = pick_amount(text, min_deal=min_deal)
    return amount


def figure_kind(text):
    folded = plain_text(text).casefold()
    if "bond" in folded or "senior note" in folded or re.search(r"\bnotes\b", folded):
        return "bond", "bond_amount"
    if "facility" in folded or "credit agreement" in folded or "credit facility" in folded:
        return "facility", "facility_amount"
    if "loan" in folded:
        return "loan", "loan_amount"
    return "", "proceeds_amount"
