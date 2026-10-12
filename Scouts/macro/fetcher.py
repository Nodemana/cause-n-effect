"""Fetch macro series through shared.nethttp. Pass a client in tests.

FRED needs FRED_API_KEY and EIA needs EIA_API_KEY. A missing key skips that
source. Keys stay in the query string the API requires and are stripped from
errors. They are not written to the database.
"""

import json
import os
import urllib.parse
from datetime import date
from pathlib import Path

from shared.nethttp import Client, EndpointError

from . import parsers

HISTORY_START = "2000-01-01"
MACRO_INTERVAL = 1.0
GFS_PAGE = (
    "https://www.abs.gov.au/statistics/economy/government/"
    "government-finance-statistics-australia/latest-release"
)
GSCPI_URL = (
    "https://www.newyorkfed.org/medialibrary/research/interactives/data/"
    "gscpi/gscpi_interactive_data.csv"
)
_KEY_ENV = {"fred": "FRED_API_KEY", "eia": "EIA_API_KEY"}


class MissingKey(Exception):
    def __init__(self, name):
        super().__init__(f"{name} is unset")
        self.name = name


def macro_client():
    """One second between requests to the same host, via shared.nethttp."""
    return Client("cause-n-effect (macro research)", min_interval=MACRO_INTERVAL)


def read_keys():
    return {name: os.environ.get(name, "").strip() for name in ("FRED_API_KEY", "EIA_API_KEY")}


def redact(text, secrets):
    cleaned = text or ""
    for secret in secrets:
        if not secret:
            continue
        cleaned = cleaned.replace(secret, "[redacted]")
        quoted = urllib.parse.quote(secret, safe="")
        if quoted != secret:
            cleaned = cleaned.replace(quoted, "[redacted]")
    return cleaned


class Guarded:
    """Hide API keys if a request URL lands in an error."""

    def __init__(self, client, secrets):
        self.client = client
        self.secrets = [secret for secret in secrets if secret]

    def get(self, url, accept="application/json"):
        try:
            return self.client.get(url, accept=accept)
        except EndpointError as exc:
            raise EndpointError(redact(str(exc), self.secrets)) from None


def load_observations(spec, client, keys, fixture_dir, today):
    env_name = _KEY_ENV.get(spec["provider"])
    try:
        if fixture_dir:
            path = Path(fixture_dir) / spec["fixture"]
            return parsers.parse(spec, path.read_bytes())
        if env_name and not (keys or {}).get(env_name):
            raise MissingKey(env_name)
        payload = _download(spec, client, keys, today)
        return parsers.parse(spec, payload)
    except ValueError as exc:
        raise EndpointError(redact(str(exc), _secret_values(keys))) from None


def fred_url(remote_id, api_key, start=HISTORY_START):
    query = urllib.parse.urlencode(
        {
            "series_id": remote_id,
            "api_key": api_key,
            "file_type": "json",
            "observation_start": start,
        }
    )
    return f"https://api.stlouisfed.org/fred/series/observations?{query}"


def bls_url(remote_id, start_year, end_year):
    series = urllib.parse.quote(remote_id, safe="")
    return (
        "https://api.bls.gov/publicAPI/v2/timeseries/data/"
        f"{series}?startyear={start_year}&endyear={end_year}"
    )


def bls_years(today):
    """Ten calendar years, the public BLS window that does not need a key."""
    end = today.year
    return end - 9, end


def abs_url(dataflow, data_key, start):
    return (
        "https://data.api.abs.gov.au/rest/data/"
        f"{dataflow}/{data_key}?startPeriod={urllib.parse.quote(start)}&format=csv"
    )


def rba_url(table):
    return f"https://www.rba.gov.au/statistics/tables/csv/{table}-data.csv"


def eia_url(route, remote_id, api_key, start, offset, length=5000):
    query = urllib.parse.urlencode(
        [
            ("api_key", api_key),
            ("frequency", "daily"),
            ("data[0]", "value"),
            ("facets[series][]", remote_id),
            ("start", start),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
            ("offset", str(offset)),
            ("length", str(length)),
        ]
    )
    return f"https://api.eia.gov/v2/{route}/data/?{query}"


def _download(spec, client, keys, today):
    provider = spec["provider"]
    if provider == "fred":
        url = fred_url(spec["remote_id"], keys["FRED_API_KEY"])
        return _body(client, url, "application/json")
    if provider == "bls":
        start_year, end_year = bls_years(today or date.today())
        url = bls_url(spec["remote_id"], start_year, end_year)
        return _body(client, url, "application/json")
    if provider == "abs":
        url = abs_url(spec["dataflow"], spec["data_key"], spec["start"])
        return _body(client, url, "text/csv")
    if provider == "rba":
        return _body(client, rba_url(spec["table"]), "text/csv")
    if provider == "nyfed":
        return _body(client, GSCPI_URL, "text/csv")
    if provider == "eia":
        return _eia_pages(spec, client, keys["EIA_API_KEY"])
    if provider == "gfs":
        page = _body(client, GFS_PAGE, "text/html").decode("utf-8", errors="replace")
        url = parsers.gfs_xlsx_url(page)
        return _body(
            client,
            url,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    raise ValueError(f"unknown provider {provider}")


def _eia_pages(spec, client, api_key, length=5000):
    offset = 0
    combined = None
    bucket = []
    total = None
    while offset <= 20000:
        url = eia_url(spec["route"], spec["remote_id"], api_key, HISTORY_START, offset, length)
        payload = json.loads(_body(client, url, "application/json"))
        response = payload.get("response")
        if not isinstance(response, dict) or response.get("data") is None:
            return json.dumps(payload).encode("utf-8")
        points = response.get("data") or []
        if combined is None:
            combined = payload
        bucket.extend(points)
        if total is None and response.get("total") not in (None, ""):
            total = int(response["total"])
        offset += length
        if not points or len(points) < length or (total is not None and offset >= total):
            break
    combined["response"]["data"] = bucket
    return json.dumps(combined).encode("utf-8")


def _body(client, url, accept):
    body, _content_type = client.get(url, accept=accept)
    return body


def _secret_values(keys):
    if not keys:
        return []
    return [keys.get("FRED_API_KEY"), keys.get("EIA_API_KEY")]
