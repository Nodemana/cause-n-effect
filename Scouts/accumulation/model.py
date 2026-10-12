"""Documents in, positions out. No network and no secrets."""

from dataclasses import dataclass, field
import re


def norm_name(value):
    return " ".join((value or "").casefold().split())


def norm_cik(value):
    digits = re.sub(r"\D", "", value or "")
    if not digits:
        return None
    return digits.zfill(10)


def norm_acn(value):
    compact = re.sub(r"\s+", "", value or "")
    return compact or None


@dataclass
class Document:
    """One official filing body already retrieved through nethttp, or a fixture."""

    source: str
    body: str
    url: str
    form_type: str | None = None
    filing_date: str | None = None
    accession: str | None = None
    ticker: str | None = None


@dataclass
class Position:
    """One filer/target stake parsed from a single filing.

    Joint SEC filings stay one position. The reporting person whose CIK matches
    the submission filer is primary; otherwise the largest reported stake is.
    Co-filers are named and are not turned into extra positions, because their
    share counts usually describe the same shares.
    """

    source: str
    form_type: str
    form_family: str
    amendment: bool
    filer: str
    target_name: str
    document_url: str
    filer_id: str | None = None
    co_filers: list = field(default_factory=list)
    target_id: str | None = None
    ticker: str | None = None
    stake_pct: float | None = None
    share_count: int | None = None
    event_date: str | None = None
    filing_date: str | None = None
    purpose_text: str | None = None
    accession: str | None = None
    ceased: bool = False
    flags: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def filer_key(self):
        if self.filer_id and self.source == "sec":
            cik = norm_cik(self.filer_id)
            if cik:
                return "cik:" + cik
        if self.filer:
            return "name:" + norm_name(self.filer)
        return ""

    def target_key(self):
        if self.target_id and self.source == "sec":
            cik = norm_cik(self.target_id)
            if cik:
                return "cik:" + cik
        if self.target_id and self.source == "asx":
            acn = norm_acn(self.target_id)
            if acn:
                return "acn:" + acn
        if self.ticker:
            return "ticker:" + self.ticker.upper()
        if self.target_name:
            return "name:" + norm_name(self.target_name)
        return ""


def position_dict(position, matched_filters, sector):
    return {
        "source": position.source,
        "form_type": position.form_type,
        "form_family": position.form_family,
        "amendment": position.amendment,
        "filer": position.filer,
        "filer_id": position.filer_id,
        "co_filers": list(position.co_filers),
        "target": {
            "name": position.target_name,
            "id": position.target_id,
            "ticker": position.ticker,
        },
        "sector": sector,
        "stake_pct": position.stake_pct,
        "share_count": position.share_count,
        "event_date": position.event_date,
        "filing_date": position.filing_date,
        "purpose_text": position.purpose_text,
        "ceased": position.ceased,
        "document_url": position.document_url,
        "accession": position.accession,
        "matched_filters": list(matched_filters),
    }
