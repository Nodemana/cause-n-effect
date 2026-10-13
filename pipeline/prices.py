"""Official prices.

ASX names are fetched here from the Markit company header. The price and the
market cap in that payload are AUD. The payload's currency field is the
company's reporting currency and is ignored.

US names are not fetched here. Interactive Brokers is reached by the
operator's agent. `run` writes prices/needed.json. The agent writes
prices.result.json. `ingest` keeps a quote whose top_status is REALTIME or
DELAYED and whose as_of is within three trading days. Anything else leaves
that ticker unverified. Market cap for a US quote is SEC shares times the
price, computed in marketcap.py.

No unofficial feed is called.
"""

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

from pipeline.agent import AgentResultError
from pipeline.load import classify, exchange_for
from shared.nethttp import ASX_MIN_INTERVAL, Client, EndpointError

log = logging.getLogger(__name__)

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
MARKIT_HEADER = "https://asx.api.markitdigital.com/asx-research/1.0/companies/{code}/header"
USABLE_STATUS = {"REALTIME", "DELAYED"}
STALE_AFTER_TRADING_DAYS = 3
QUOTE_REQUIRED = {"ticker", "conid", "last", "top_status", "as_of"}
QUOTE_OPTIONAL = {"prior_year_close", "close_1y", "low_52w", "high_52w", "closes"}
QUOTE_KEYS = QUOTE_REQUIRED | QUOTE_OPTIONAL


def markit_client(retries=4, backoff_base=2.0, backoff_cap=60.0):
    """Markit client. Browser User-Agent, ASX spacing, no contact email."""
    return Client(
        BROWSER_USER_AGENT,
        ASX_MIN_INTERVAL,
        require_contact=False,
        retries=retries,
        backoff_base=backoff_base,
        backoff_cap=backoff_cap,
    )


def considered_figures(figures, limit):
    """Lead figures the sweep will rate, after --limit, before the gates."""
    ordered = [figure for figure in figures if classify(figure.scout, figure.figure_type) is not None]
    if limit is not None:
        ordered = ordered[: int(limit)]
    return ordered


def markit_code(ticker):
    text = (ticker or "").strip().upper()
    if text.endswith(".AX"):
        text = text[:-3]
    return text


def markit_header_url(code):
    return MARKIT_HEADER.format(code=quote(code, safe=""))


def parse_markit_header(payload):
    """Return price and market cap in AUD.

    `currency` on the payload is the reporting currency. It is not the
    currency of priceLast or marketCap.
    """
    data = payload
    if isinstance(payload, dict) and "priceLast" not in payload and "marketCap" not in payload:
        nested = payload.get("data")
        if isinstance(nested, dict):
            data = nested
    if not isinstance(data, dict):
        raise ValueError("markit header is not an object")
    price = data.get("priceLast")
    cap = data.get("marketCap")
    if not _positive(price) or not _positive(cap):
        raise ValueError("markit header is missing priceLast or marketCap")
    low = data.get("yearLow")
    high = data.get("yearHigh")
    if not (_positive(low) and _positive(high) and float(low) <= float(high)):
        low = None
        high = None
    return {
        "price": float(price),
        "market_cap": float(cap),
        "low_52w": None if low is None else float(low),
        "high_52w": None if high is None else float(high),
    }


def fetch_markit_header(client, code):
    return parse_markit_header(client.json(markit_header_url(code)))


def refresh_markit(conn, figures, client, now):
    """Store a fresh Markit quote for each ASX ticker that does not have one.

    A failed header leaves that ticker unverified. The sweep continues.
    """
    if client is None:
        return
    seen = set()
    for figure in figures:
        if exchange_for(figure) != "ASX":
            continue
        ticker = figure.ticker
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        if latest_usable_price(conn, ticker, now) is not None:
            continue
        code = markit_code(ticker)
        if not code:
            continue
        try:
            parsed = fetch_markit_header(client, code)
        except (EndpointError, ValueError, TypeError):
            log.warning("markit header failed for %s", code)
            continue
        insert_price(
            conn,
            ticker=ticker,
            exchange="ASX",
            price=parsed["price"],
            currency="AUD",
            market_cap=parsed["market_cap"],
            source="asx_markit",
            status="DELAYED",
            fetched_at=now,
            as_of=str(now)[:10],
            low_52w=parsed.get("low_52w"),
            high_52w=parsed.get("high_52w"),
        )


def ibkr_needed(conn, figures, now):
    """US tickers in this sweep that do not have a fresh usable quote."""
    asx = set()
    for figure in figures:
        if exchange_for(figure) == "ASX" and figure.ticker:
            asx.add(figure.ticker)
    needed = []
    seen = set()
    for figure in figures:
        ticker = figure.ticker
        if not ticker or ticker in asx or ticker in seen:
            continue
        seen.add(ticker)
        if latest_usable_price(conn, ticker, now) is not None:
            continue
        needed.append({"exchange": "US", "ticker": ticker})
    needed.sort(key=lambda item: item["ticker"])
    return needed


def needed_path(queue_dir, run_id):
    return Path(queue_dir) / run_id / "prices" / "needed.json"


def result_path(queue_dir, run_id):
    return Path(queue_dir) / run_id / "prices" / "prices.result.json"


def write_needed(queue_dir, run_id, tickers, as_of, signals=None):
    path = needed_path(queue_dir, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "as_of": as_of,
        "history": {
            "close_1y": "close 52 weeks before as_of, from historical bars",
            "closes": "daily closes from closes_from through as_of, from historical bars",
            "high_52w": "highest price in the 52 weeks before as_of",
            "low_52w": "lowest price in the 52 weeks before as_of",
            "prior_year_close": "last close of the prior calendar year",
        },
        "run_id": run_id,
        "signals": list(signals or []),
        "tickers": tickers,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def prices_outstanding(queue_dir, run_id):
    """The needed.json path when the agent has not written a result yet."""
    if result_path(queue_dir, run_id).is_file():
        return []
    path = needed_path(queue_dir, run_id)
    if path.is_file():
        return [path]
    return [path]


def ingest_price_result(conn, queue_dir, run_id, now):
    """Validate prices.result.json and store usable US quotes.

    A missing file or a broken object fails the stage. A quote whose
    top_status is not REALTIME or DELAYED, whose last is not a positive
    number, or whose as_of is older than three trading days is unverified
    and is not stored. Other tickers in the same file are still stored.
    """
    path = result_path(queue_dir, run_id)
    if not path.is_file():
        raise AgentResultError(f"prices: missing result file {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AgentResultError(f"prices: result is not JSON ({exc})") from None
    quotes = _parse_quotes(payload)
    for quote in quotes:
        if quote["top_status"] not in USABLE_STATUS:
            continue
        if not _positive(quote.get("last")):
            continue
        as_of = quote.get("as_of") or ""
        if trading_days_old(as_of, now) > STALE_AFTER_TRADING_DAYS:
            continue
        price = float(quote["last"])
        from pipeline.marketcap import latest_shares

        shares = latest_shares(conn, quote["ticker"])
        market_cap = shares * price if shares is not None else None
        insert_price(
            conn,
            ticker=quote["ticker"],
            exchange="US",
            price=price,
            currency="USD",
            market_cap=market_cap,
            source="ibkr",
            status=quote["top_status"],
            fetched_at=now,
            conid=_conid_text(quote.get("conid")),
            as_of=str(as_of)[:10],
            prior_year_close=_optional_close(quote.get("prior_year_close")),
            close_1y=_optional_close(quote.get("close_1y")),
            low_52w=_optional_close(quote.get("low_52w")),
            high_52w=_optional_close(quote.get("high_52w")),
        )
        if "closes" in quote:
            replace_closes(conn, quote["ticker"], quote.get("closes") or [], now)


def insert_price(
    conn,
    *,
    ticker,
    exchange,
    price,
    currency,
    market_cap,
    source,
    status,
    fetched_at,
    conid=None,
    as_of=None,
    prior_year_close=None,
    close_1y=None,
    low_52w=None,
    high_52w=None,
):
    with conn:
        cursor = conn.execute(
            """
            INSERT INTO prices (
                ticker, exchange, price, currency, market_cap, source, status,
                fetched_at, conid, as_of, prior_year_close, close_1y, low_52w, high_52w
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ticker,
                exchange,
                price,
                currency,
                market_cap,
                source,
                status,
                fetched_at,
                conid,
                as_of,
                prior_year_close,
                close_1y,
                low_52w,
                high_52w,
            ),
        )
        return cursor.lastrowid


def latest_usable_price(conn, ticker, now):
    """Newest stored quote that is still a usable official price."""
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prices'"
    ).fetchone()
    if not found:
        return None
    rows = conn.execute(
        """
        SELECT price_id, ticker, exchange, price, currency, market_cap, source,
               status, fetched_at, conid, as_of, prior_year_close, close_1y,
               low_52w, high_52w
        FROM prices
        WHERE ticker = ?
        ORDER BY fetched_at DESC, price_id DESC
        """,
        (ticker,),
    ).fetchall()
    anchor = now
    for row in rows:
        if _usable(row, anchor):
            return row
    return None


def trading_days_old(as_of, now):
    """Weekdays after as_of up to and including now. Weekends are not counted."""
    try:
        start = date.fromisoformat(str(as_of)[:10])
        end = date.fromisoformat(str(now)[:10])
    except ValueError:
        return 10**6
    if end <= start:
        return 0
    days = 0
    cursor = start
    while cursor < end:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5:
            days += 1
    return days


def _usable(row, now):
    if (row["status"] or "") not in USABLE_STATUS:
        return False
    when = row["as_of"] or row["fetched_at"]
    if trading_days_old(when, now) > STALE_AFTER_TRADING_DAYS:
        return False
    source = row["source"]
    if source == "asx_markit":
        cap = row["market_cap"]
        return cap is not None and float(cap) > 0
    if source == "ibkr":
        if (row["currency"] or "").upper() != "USD":
            return False
        return _positive(row["price"])
    return False


def _parse_quotes(payload):
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise AgentResultError("prices: result must be a list of quotes")
    quotes = []
    errors = []
    for index, item in enumerate(payload):
        label = f"prices[{index}]"
        item_errors = []
        if not isinstance(item, dict):
            errors.append(f"{label}: quote must be an object")
            continue
        extra = sorted(set(item) - QUOTE_KEYS)
        missing = sorted(QUOTE_REQUIRED - set(item))
        for key in extra:
            item_errors.append(f"{label}: unexpected key {key}")
        for key in missing:
            item_errors.append(f"{label}: missing key {key}")
        ticker = item.get("ticker")
        if "ticker" in item and (not isinstance(ticker, str) or not ticker.strip()):
            item_errors.append(f"{label}: ticker must be a non-empty string")
        if "top_status" in item and not isinstance(item.get("top_status"), str):
            item_errors.append(f"{label}: top_status must be a string")
        if "as_of" in item and not isinstance(item.get("as_of"), str):
            item_errors.append(f"{label}: as_of must be a string")
        if "conid" in item and not _conid_ok(item.get("conid")):
            item_errors.append(f"{label}: conid must be a string or a number")
        if "last" in item and item.get("last") is not None and not _number(item.get("last")):
            item_errors.append(f"{label}: last must be a number or null")
        for key in sorted(QUOTE_OPTIONAL - {"closes"}):
            if key not in item or item.get(key) is None:
                continue
            if not _positive(item.get(key)):
                item_errors.append(f"{label}: {key} must be a positive number or null")
        if "closes" in item and item.get("closes") is not None:
            item_errors.extend(_close_errors(label, item.get("closes")))
        errors.extend(item_errors)
        if not item_errors:
            quotes.append(item)
    if errors:
        raise AgentResultError("\n".join(errors))
    return quotes


def _conid_ok(value):
    if isinstance(value, str):
        return True
    return _number(value)


def _conid_text(value):
    if value is None:
        return None
    return str(value)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _positive(value):
    return _number(value) and float(value) > 0


def _close_errors(label, closes):
    if not isinstance(closes, list):
        return [f"{label}: closes must be a list"]
    errors = []
    for index, item in enumerate(closes):
        prefix = f"{label}: closes[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        extra = sorted(set(item) - {"date", "close"})
        for key in extra:
            errors.append(f"{prefix}: unexpected key {key}")
        if not isinstance(item.get("date"), str) or len(item.get("date") or "") < 10:
            errors.append(f"{prefix}: date must be a YYYY-MM-DD string")
        else:
            try:
                date.fromisoformat(item["date"][:10])
            except ValueError:
                errors.append(f"{prefix}: date must be a YYYY-MM-DD string")
        if not _positive(item.get("close")):
            errors.append(f"{prefix}: close must be a positive number")
    return errors


def replace_closes(conn, ticker, closes, fetched_at):
    """Replace the stored daily closes for one ticker.

    An empty list clears the old bars. The caller has already checked each close.
    """
    key = str(ticker or "").strip().upper()
    if not key:
        return
    with conn:
        conn.execute("DELETE FROM price_closes WHERE upper(ticker) = ?", (key,))
        for item in closes or []:
            conn.execute(
                """
                INSERT INTO price_closes (ticker, obs_date, close, source, fetched_at)
                VALUES (?, ?, ?, 'ibkr', ?)
                """,
                (key, str(item["date"])[:10], float(item["close"]), fetched_at),
            )


def load_closes(conn, ticker):
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'price_closes'"
    ).fetchone()
    if not found:
        return []
    rows = conn.execute(
        """
        SELECT obs_date, close FROM price_closes
        WHERE upper(ticker) = ?
        ORDER BY obs_date
        """,
        (str(ticker or "").strip().upper(),),
    ).fetchall()
    return [{"date": row["obs_date"], "close": row["close"]} for row in rows]


def has_closes(conn, ticker):
    return bool(load_closes(conn, ticker))


def _optional_close(value):
    if not _positive(value):
        return None
    return float(value)


def quotes_for(conn, tickers, now):
    """Usable official quotes keyed by upper-case ticker.

    A ticker with no fresh quote is omitted. The caller prints n/a.
    """
    found = {}
    for ticker in tickers:
        key = str(ticker or "").strip().upper()
        if not key or key in found:
            continue
        row = _quote_row(conn, key, now)
        if row is None:
            continue
        found[key] = {
            "price": row["price"],
            "currency": row["currency"],
            "as_of": (row["as_of"] or str(row["fetched_at"] or "")[:10]),
            "prior_year_close": row["prior_year_close"],
            "close_1y": row["close_1y"],
            "low_52w": row["low_52w"],
            "high_52w": row["high_52w"],
        }
    return found


def _quote_row(conn, ticker, now):
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'prices'"
    ).fetchone()
    if not found:
        return None
    rows = conn.execute(
        """
        SELECT price_id, ticker, exchange, price, currency, market_cap, source,
               status, fetched_at, conid, as_of, prior_year_close, close_1y,
               low_52w, high_52w
        FROM prices
        WHERE upper(ticker) = ?
        ORDER BY fetched_at DESC, price_id DESC
        """,
        (ticker.upper(),),
    ).fetchall()
    for row in rows:
        if _usable(row, now):
            return row
    return None
