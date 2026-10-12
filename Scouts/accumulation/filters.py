"""Deterministic screens. Ambiguous filings are withheld for the agent."""

from datetime import date, timedelta
import re

from Scouts.accumulation.model import norm_acn, norm_cik, norm_name, position_dict

# Item 4 language that asserts a control purpose. Boilerplate denials are not a match.
ACTIVIST_PHRASES = (
    "board representation",
    "board seat",
    "nominate",
    "proxy contest",
    "solicit proxies",
    "tender offer",
    "merger",
    "strategic alternative",
    "sale of the issuer",
    "sale of the company",
    "change in control",
    "control of the issuer",
    "influence control",
    "seek control",
    "acquire control",
    "going private",
    "take private",
    "extraordinary corporate transaction",
    "replace management",
    "change the board",
)

_DENIALS = (
    "no plan",
    "no present plan",
    "no current plan",
    "do not have any plan",
    "does not have any plan",
    "do not currently",
    "does not currently",
    "no intention",
    "not acquired with the purpose",
    "not acquired for the purpose",
    "without the purpose",
    "without a purpose",
    "no proposal",
)

_FILTER_ORDER = (
    "stake_above_threshold",
    "repeat_filer",
    "sector_concentration",
    "recent_window",
)
_FLAG_ORDER = (
    "unparseable_stake",
    "unparseable_date",
    "unknown_sector",
    "conflicting_data",
    "switch_13g_to_13d",
    "activist_purpose",
)
# These go to the agent even when no quantitative filter matched.
_ALWAYS_REVIEW = {
    "unparseable_stake",
    "unparseable_date",
    "conflicting_data",
    "switch_13g_to_13d",
    "activist_purpose",
}


class ScreenConfig:
    def __init__(
        self,
        min_stake_pct=5.0,
        recent_days=90,
        repeat_filer_min_targets=2,
        sector_min_positions=2,
        require_known_sector=True,
        as_of=None,
        activist_phrases=ACTIVIST_PHRASES,
    ):
        if min_stake_pct < 0:
            raise ValueError("min_stake_pct must be >= 0")
        if recent_days < 0:
            raise ValueError("recent_days must be >= 0")
        if repeat_filer_min_targets < 1 or sector_min_positions < 1:
            raise ValueError("pattern minimums must be >= 1")
        self.min_stake_pct = float(min_stake_pct)
        self.recent_days = int(recent_days)
        self.repeat_filer_min_targets = int(repeat_filer_min_targets)
        self.sector_min_positions = int(sector_min_positions)
        self.require_known_sector = bool(require_known_sector)
        self.as_of = as_of
        self.activist_phrases = tuple(activist_phrases)

    def as_of_date(self):
        return self.as_of or date.today()

    def to_dict(self):
        return {
            "min_stake_pct": self.min_stake_pct,
            "recent_days": self.recent_days,
            "repeat_filer_min_targets": self.repeat_filer_min_targets,
            "sector_min_positions": self.sector_min_positions,
            "require_known_sector": self.require_known_sector,
            "as_of": self.as_of_date().isoformat(),
            "activist_phrases": list(self.activist_phrases),
        }


def screen(positions, config=None, sectors=None):
    """Split positions into auto-included candidates and agent-review flags.

    A candidate is inside the recent window, is not a ceasing notice, has no
    review flag, and matches at least one of: stake at or above the threshold,
    a filer appearing on enough distinct targets, or a filer accumulating
    inside one sector. Flagged positions are omitted from candidates.
    """
    config = config or ScreenConfig()
    sectors = sectors or {}
    as_of = config.as_of_date()
    prepared = []
    for position in positions:
        sector = lookup_sector(position, sectors)
        prepared.append((position, sector, _anchor(position)))

    switches = _switch_pairs(prepared)
    target_counts, sector_counts = _pattern_counts(prepared, as_of, config)

    candidates = []
    review = []
    for position, sector, anchor in prepared:
        flags = _flags(position, sector, switches, config)
        if anchor is None:
            flags.add("unparseable_date")
        pair = (position.filer_key(), position.target_key())
        if (
            pair in switches
            and position.form_family == "13G"
            and _in_window(anchor, as_of, config.recent_days)
        ):
            flags.add("switch_13g_to_13d")
        matched = _matched(position, sector, anchor, flags, target_counts, sector_counts, as_of, config)
        in_window = "recent_window" in matched
        substantive = [name for name in matched if name != "recent_window"]
        # History outside the window stays off both lists. It is only context
        # for switch detection. Undated filings are the exception: the window
        # cannot be applied, so the agent sees them.
        surface = in_window or "unparseable_date" in flags
        ambiguous = bool(flags & _ALWAYS_REVIEW)
        if flags and surface and (substantive or ambiguous):
            if position.ceased and not ambiguous:
                continue
            review.append(_review_row(position, sector, flags, matched))
            continue
        if position.ceased or not in_window or not substantive:
            continue
        candidates.append((position, position_dict(position, _order(matched, _FILTER_ORDER), sector)))

    candidates.sort(key=lambda item: (-(item[0].stake_pct or -1), item[0].filer, item[0].target_name, item[0].document_url))
    review.sort(key=lambda row: (row["flags"], row["position"]["filer"], row["position"]["target"]["name"], row["position"]["document_url"]))
    return {
        "scout": "accumulation",
        "schema": 1,
        "as_of": as_of.isoformat(),
        "config": config.to_dict(),
        "candidates": [row for _, row in candidates],
        "review": review,
    }


def lookup_sector(position, sectors):
    if not sectors:
        return None
    keys = []
    if position.target_id and position.source == "sec":
        cik = norm_cik(position.target_id)
        if cik:
            keys.append(cik)
    if position.target_id and position.source == "asx":
        acn = norm_acn(position.target_id)
        if acn:
            keys.append(acn)
    if position.ticker:
        keys.append(position.ticker.upper())
    if position.target_name:
        keys.append(norm_name(position.target_name))
    for key in keys:
        if key in sectors:
            return sectors[key]
    return None


def has_activist_purpose(text, phrases=ACTIVIST_PHRASES):
    """True when a clause states a control purpose and does not deny it."""
    if not text:
        return False
    clauses = re.split(r"[.;]|\bbut\b", text, flags=re.I)
    for clause in clauses:
        folded = " ".join(clause.casefold().split())
        if not folded:
            continue
        if any(phrase in folded for phrase in phrases) and not any(denial in folded for denial in _DENIALS):
            return True
    return False


def _flags(position, sector, switches, config):
    flags = set(position.flags)
    pair = (position.filer_key(), position.target_key())
    if pair in switches and pair[0] and pair[1] and position.form_family == "13D":
        flags.add("switch_13g_to_13d")
    if position.form_family == "13D" and has_activist_purpose(position.purpose_text, config.activist_phrases):
        flags.add("activist_purpose")
    if (
        config.require_known_sector
        and sector is None
        and not position.ceased
        and position.form_family != "605"
    ):
        flags.add("unknown_sector")
    return flags


def _matched(position, sector, anchor, flags, target_counts, sector_counts, as_of, config):
    matched = []
    if _in_window(anchor, as_of, config.recent_days):
        matched.append("recent_window")
    if position.ceased:
        return matched
    if position.stake_pct is not None and position.stake_pct >= config.min_stake_pct:
        matched.append("stake_above_threshold")
    filer = position.filer_key()
    if filer and target_counts.get(filer, 0) >= config.repeat_filer_min_targets:
        matched.append("repeat_filer")
    if sector and filer and sector_counts.get((filer, sector), 0) >= config.sector_min_positions:
        matched.append("sector_concentration")
    # unknown_sector withholds auto-inclusion, but it is not itself a reason
    # to put a below-threshold one-off filing on the review list.
    if "unknown_sector" in flags and not any(
        name in matched for name in ("stake_above_threshold", "repeat_filer", "sector_concentration")
    ):
        flags.discard("unknown_sector")
    return matched


def _pattern_counts(prepared, as_of, config):
    targets = {}
    sectors = {}
    for position, sector, anchor in prepared:
        if position.ceased or not _in_window(anchor, as_of, config.recent_days):
            continue
        filer = position.filer_key()
        target = position.target_key()
        if not filer or not target:
            continue
        targets.setdefault(filer, set()).add(target)
        if sector:
            sectors.setdefault((filer, sector), set()).add(target)
    return (
        {filer: len(names) for filer, names in targets.items()},
        {key: len(names) for key, names in sectors.items()},
    )


def _switch_pairs(prepared):
    """Filer/target pairs whose first 13D comes after a 13G."""
    groups = {}
    for position, _sector, anchor in prepared:
        if position.form_family not in ("13D", "13G"):
            continue
        key = (position.filer_key(), position.target_key())
        if not key[0] or not key[1]:
            continue
        groups.setdefault(key, []).append((anchor or date.min, position))
    switched = set()
    for key, rows in groups.items():
        rows.sort(key=lambda item: (item[0], 0 if item[1].form_family == "13G" else 1, item[1].document_url))
        seen_g = False
        seen_d = False
        for _anchor, position in rows:
            if position.form_family == "13G" and not seen_d:
                seen_g = True
            elif position.form_family == "13D":
                if seen_g and not seen_d:
                    switched.add(key)
                seen_d = True
    return switched


def _review_row(position, sector, flags, matched):
    ordered_flags = _order(flags, _FLAG_ORDER)
    notes = list(position.notes)
    if "switch_13g_to_13d" in flags:
        notes.append("13G-to-13D switch; agent decides whether the change in intent matters")
    if "activist_purpose" in flags:
        notes.append("Item 4 purpose language matches the activist phrase list")
    if "unknown_sector" in flags:
        notes.append("no sector mapping for this target")
    return {
        "flags": ordered_flags,
        "notes": notes,
        "position": position_dict(position, _order(matched, _FILTER_ORDER), sector),
    }


def _anchor(position):
    for raw in (position.filing_date, position.event_date):
        if not raw:
            continue
        try:
            return date.fromisoformat(raw)
        except ValueError:
            continue
    return None


def _in_window(anchor, as_of, recent_days):
    if anchor is None:
        return False
    start = as_of - timedelta(days=recent_days)
    return start <= anchor <= as_of


def _order(values, order):
    rank = {name: index for index, name in enumerate(order)}
    return sorted(values, key=lambda name: rank.get(name, len(order)))
