"""Official ASX and US prices. No network."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pipeline.flow as flow  # noqa: E402
from pipeline.agent import AgentResultError  # noqa: E402
from pipeline.flow import ingest_run, run_pipeline  # noqa: E402
from pipeline.marketcap import resolve_market_cap  # noqa: E402
from pipeline.prices import (  # noqa: E402
    BROWSER_USER_AGENT,
    ingest_price_result,
    insert_price,
    markit_client,
    parse_markit_header,
    refresh_markit,
    result_path,
    trading_days_old,
    write_needed,
)
from shared.nethttp import EndpointError  # noqa: E402
from shared.pipeline_store import connect  # noqa: E402
from shared.store import save_run  # noqa: E402

SWEEP_AT = "2026-10-12T18:00:00Z"


class PriceTests(unittest.TestCase):
    def setUp(self):
        self._utc_now = flow.utc_now
        flow.utc_now = lambda: SWEEP_AT

    def tearDown(self):
        flow.utc_now = self._utc_now

    def test_history_closes_are_stored_and_a_missing_close_stays_blank(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = str(Path(tmp) / "agent_queue")
            conn = connect(path)
            conn.close()
            run_id = "2026-10-12-prices"
            needed = write_needed(queue, run_id, [{"exchange": "US", "ticker": "LMT"}], "2026-10-12")
            payload = json.loads(needed.read_text(encoding="utf-8"))
            self.assertEqual(payload["history"]["prior_year_close"], "last close of the prior calendar year")
            self.assertIn("close_1y", payload["history"])
            result = needed.with_name("prices.result.json")
            result.write_text(
                json.dumps(
                    [
                        {
                            "ticker": "LMT",
                            "conid": 1,
                            "last": 450,
                            "top_status": "DELAYED",
                            "as_of": "2026-10-12",
                            "prior_year_close": 400,
                            "close_1y": 375,
                            "low_52w": 360,
                            "high_52w": 480,
                        },
                        {
                            "ticker": "NOC",
                            "conid": 2,
                            "last": 500,
                            "top_status": "DELAYED",
                            "as_of": "2026-10-12",
                        },
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            conn = connect(path)
            try:
                ingest_price_result(conn, queue, run_id, SWEEP_AT)
                lmt = conn.execute(
                    "SELECT prior_year_close, close_1y, low_52w, high_52w FROM prices WHERE ticker = 'LMT'"
                ).fetchone()
                noc = conn.execute(
                    "SELECT price, prior_year_close, close_1y, low_52w, high_52w FROM prices WHERE ticker = 'NOC'"
                ).fetchone()
            finally:
                conn.close()
        self.assertEqual(lmt["prior_year_close"], 400)
        self.assertEqual(lmt["close_1y"], 375)
        self.assertEqual(lmt["low_52w"], 360)
        self.assertEqual(lmt["high_52w"], 480)
        self.assertEqual(noc["price"], 500)
        self.assertIsNone(noc["prior_year_close"])
        self.assertIsNone(noc["close_1y"])
        self.assertIsNone(noc["low_52w"])
        self.assertIsNone(noc["high_52w"])
        header = parse_markit_header(
            {"priceLast": 10, "marketCap": 20, "yearLow": 8, "yearHigh": 12, "currency": "USD"}
        )
        self.assertEqual(header["low_52w"], 8)
        self.assertEqual(header["high_52w"], 12)
        bare = parse_markit_header({"priceLast": 10, "marketCap": 20})
        self.assertIsNone(bare["low_52w"])
        self.assertIsNone(bare["high_52w"])

    def test_three_trading_days_skips_the_weekend(self):
        self.assertEqual(trading_days_old("2026-10-07", SWEEP_AT), 3)
        self.assertEqual(trading_days_old("2026-10-06", SWEEP_AT), 4)
        self.assertEqual(trading_days_old("2026-10-09", SWEEP_AT), 1)
        self.assertEqual(trading_days_old("2026-10-10", SWEEP_AT), 1)
        self.assertEqual(trading_days_old("2026-10-12", SWEEP_AT), 0)

    def test_markit_client_uses_a_browser_agent_and_no_email(self):
        client = markit_client()
        self.assertTrue(client.ua.startswith("Mozilla/5.0"))
        self.assertEqual(client.ua, BROWSER_USER_AGENT)
        self.assertNotIn("@", client.ua)

    def test_markit_failure_leaves_the_ticker_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = connect(path)
            try:
                figure = type(
                    "Figure",
                    (),
                    {"ticker": "BHP.AX", "source_type": "asx_announcement", "scout": "capex", "figure_type": "x"},
                )()

                class Fake:
                    def json(self, url):
                        raise EndpointError("HTTP 404 for " + url)

                with self.assertLogs("pipeline.prices", level="WARNING") as caught:
                    refresh_markit(conn, [figure], Fake(), SWEEP_AT)
                cap, reason = resolve_market_cap(conn, "BHP.AX", now=SWEEP_AT)
                stored = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            finally:
                conn.close()
        self.assertIsNone(cap)
        self.assertEqual(reason, "market cap unverified")
        self.assertEqual(stored, 0)
        self.assertTrue(any("BHP" in line for line in caught.output))
        self.assertFalse(any("@" in line for line in caught.output))

    def test_ibkr_cap_is_shares_times_price_and_bad_quotes_are_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = connect(path)
            try:
                save_run(conn, "capex", "2026-10-12T14:30:52Z", [_shares("AMZN", 1_000_000_000)])
                quote_path = result_path(str(Path(tmp) / "agent_queue"), "price-run")
                quote_path.parent.mkdir(parents=True, exist_ok=True)
                quote_path.write_text(
                    json.dumps(
                        {
                            "ticker": "AMZN",
                            "conid": 265598,
                            "last": 12,
                            "top_status": "REALTIME",
                            "as_of": "2026-10-12",
                        }
                    ),
                    encoding="utf-8",
                )
                ingest_price_result(conn, str(Path(tmp) / "agent_queue"), "price-run", SWEEP_AT)
                fresh, fresh_reason = resolve_market_cap(conn, "AMZN", now=SWEEP_AT)
                stored_cap = conn.execute("SELECT market_cap, conid FROM prices").fetchone()
                insert_price(
                    conn,
                    ticker="OLD",
                    exchange="US",
                    price=10,
                    currency="USD",
                    market_cap=10_000_000_000,
                    source="ibkr",
                    status="DELAYED",
                    fetched_at="2026-10-06T18:00:00Z",
                    as_of="2026-10-06",
                )
                save_run(conn, "capex", "2026-10-12T14:31:00Z", [_shares("OLD")])
                stale, stale_reason = resolve_market_cap(conn, "OLD", now=SWEEP_AT)
                insert_price(
                    conn,
                    ticker="CLOSED",
                    exchange="US",
                    price=10,
                    currency="USD",
                    market_cap=10_000_000_000,
                    source="ibkr",
                    status="CLOSED",
                    fetched_at=SWEEP_AT,
                    as_of="2026-10-12",
                )
                save_run(conn, "capex", "2026-10-12T14:32:00Z", [_shares("CLOSED")])
                closed, closed_reason = resolve_market_cap(conn, "CLOSED", now=SWEEP_AT)
            finally:
                conn.close()
        self.assertEqual(fresh, 12_000_000_000)
        self.assertIn("official price", fresh_reason)
        self.assertEqual(stored_cap["market_cap"], 12_000_000_000)
        self.assertEqual(stored_cap["conid"], "265598")
        self.assertIsNone(stale)
        self.assertEqual(stale_reason, "market cap unverified")
        self.assertIsNone(closed)
        self.assertEqual(closed_reason, "market cap unverified")

    def test_run_queues_us_prices_and_ingest_rejects_a_missing_or_bad_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = str(Path(tmp) / "agent_queue")
            conn = connect(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T14:30:52Z",
                    [
                        _figure("AMZN"),
                        _prior("AMZN"),
                        _figure("BHP.AX", source_type="asx_announcement"),
                        _shares("AMZN"),
                    ],
                )
            finally:
                conn.close()
            result = run_pipeline(path, queue_dir=queue, now=SWEEP_AT)
            self.assertEqual(result["stage"], "prices")
            needed_path = Path(queue) / result["run_id"] / "prices" / "needed.json"
            needed = json.loads(needed_path.read_text(encoding="utf-8"))
            self.assertEqual([item["ticker"] for item in needed["tickers"]], ["AMZN"])
            with self.assertRaises(AgentResultError) as missing:
                ingest_run(path, result["run_id"])
            self.assertIn("missing result file", str(missing.exception))
            self.assertIn("prices.result.json", str(missing.exception))
            result_path = needed_path.with_name("prices.result.json")
            result_path.write_text('{"last": 10}\n', encoding="utf-8")
            with self.assertRaises(AgentResultError) as malformed:
                ingest_run(path, result["run_id"])
            self.assertIn("missing key", str(malformed.exception))
            conn = connect(path)
            try:
                stage = conn.execute("SELECT stage FROM pipeline_runs").fetchone()["stage"]
                prices = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(stage, "prices")
        self.assertEqual(prices, 0)

    def test_ingest_stores_a_delayed_quote_and_gates_use_the_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = str(Path(tmp) / "agent_queue")
            conn = connect(path)
            try:
                save_run(
                    conn,
                    "debt-raise",
                    "2026-10-12T14:33:00Z",
                    [_debt("SMALL", 400_000_000), _debt("OK", 800_000_000), _shares("SMALL", 1_000), _shares("OK")],
                )
            finally:
                conn.close()
            result = run_pipeline(path, queue_dir=queue, now=SWEEP_AT)
            result_path = Path(queue) / result["run_id"] / "prices" / "prices.result.json"
            result_path.write_text(
                json.dumps(
                    [
                        {"ticker": "SMALL", "conid": 1, "last": 10, "top_status": "DELAYED", "as_of": "2026-10-09"},
                        {"ticker": "OK", "conid": 2, "last": 10, "top_status": "REALTIME", "as_of": "2026-10-12"},
                        {"ticker": "OK", "conid": 3, "last": 10, "top_status": "FROZEN", "extra": True},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(AgentResultError):
                ingest_run(path, result["run_id"])
            result_path.write_text(
                json.dumps(
                    [
                        {"ticker": "SMALL", "conid": 1, "last": 10, "top_status": "DELAYED", "as_of": "2026-10-09"},
                        {"ticker": "OK", "conid": 2, "last": 10, "top_status": "REALTIME", "as_of": "2026-10-12"},
                        {"ticker": "SKIP", "conid": 3, "last": 10, "top_status": "CLOSED", "as_of": "2026-10-12"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            advanced = ingest_run(path, result["run_id"])
            self.assertEqual(advanced["stage"], "significance")
            conn = connect(path)
            try:
                reasons = {
                    row["ticker"]: row["reason"]
                    for row in conn.execute(
                        "SELECT ticker, reason FROM lead_rejects WHERE rejection_stage = 'deterministic'"
                    )
                }
                ok_cap = conn.execute(
                    "SELECT market_cap, status, currency FROM prices WHERE ticker = 'OK'"
                ).fetchone()
                small_cap = conn.execute("SELECT market_cap FROM prices WHERE ticker = 'SMALL'").fetchone()
            finally:
                conn.close()
        self.assertIn("min_market_cap", reasons["SMALL"])
        self.assertIn("is below", reasons["SMALL"])
        self.assertNotIn("SMALL", {advanced["stage"]})
        self.assertEqual(small_cap["market_cap"], 10_000)
        self.assertEqual(ok_cap["status"], "REALTIME")
        self.assertEqual(ok_cap["currency"], "USD")
        self.assertEqual(ok_cap["market_cap"], 10_000_000_000)
        self.assertNotIn("OK", reasons)

    def test_raise_vs_company_uses_the_real_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = connect(path)
            try:
                save_run(
                    conn,
                    "debt-raise",
                    "2026-10-12T14:33:00Z",
                    [_debt("DEBT", 400_000_000), _shares("DEBT")],
                )
                insert_price(
                    conn,
                    ticker="DEBT",
                    exchange="US",
                    price=10,
                    currency="USD",
                    market_cap=10_000_000_000,
                    source="ibkr",
                    status="DELAYED",
                    fetched_at=SWEEP_AT,
                    as_of="2026-10-12",
                )
            finally:
                conn.close()
            from pipeline.load import load_latest_figures
            from pipeline.rubric_gate import evaluate

            conn = connect(path)
            try:
                figure = next(
                    item
                    for item in load_latest_figures(conn, as_of=SWEEP_AT)
                    if item.ticker == "DEBT" and item.figure_type == "facility_amount"
                )
                outcome = evaluate(conn, figure, "debt_raise", now=SWEEP_AT)
            finally:
                conn.close()
        failed = [item for item in outcome.results if item["name"] == "raise_vs_company"]
        self.assertEqual(outcome.status, "filtered")
        self.assertTrue(failed)
        self.assertIn("below", failed[0]["evidence"])

    def test_sources_do_not_name_an_unofficial_feed(self):
        for relative in (
            "pipeline/prices.py",
            "pipeline/marketcap.py",
            "pipeline/flow.py",
            "pipeline/cli.py",
            "shared/nethttp.py",
        ):
            text = (REPO / relative).read_text(encoding="utf-8").casefold()
            self.assertNotIn("yahoo", text)
            self.assertNotIn("interactivebrokers", text)


def _shares(ticker, value=1_000_000_000):
    return {
        "ticker": ticker,
        "company": ticker,
        "figure_type": "EntityCommonStockSharesOutstanding",
        "instrument": "shares",
        "period": "2026-10-01",
        "value": value,
        "value_text": str(value),
        "currency": "USD",
        "seen_at": "2026-10-12",
        "announced_on": "2026-10-01",
        "source_url": f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-shares.htm",
        "source_type": "filing",
        "verified": 1,
    }


def _prior(ticker):
    row = _figure(ticker)
    row["figure_type"] = "PaymentsToAcquirePropertyPlantAndEquipment"
    row["period"] = "2025-07-01/2025-09-30"
    row["value"] = 1_000_000_000
    row["value_text"] = "1000000000"
    return row


def _figure(ticker, source_type="filing"):
    return {
        "ticker": ticker,
        "company": ticker,
        "figure_type": "PaymentsToAcquireProductiveAssets",
        "instrument": "quarter",
        "period": "2026-07-01/2026-09-30",
        "value": 5_000_000_000,
        "value_text": "5000000000",
        "currency": "USD",
        "seen_at": "2026-10-12",
        "announced_on": "2026-10-01",
        "source_url": f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}.htm",
        "source_type": source_type,
        "verified": 1,
    }


def _debt(ticker, value):
    row = _figure(ticker)
    row["figure_type"] = "facility_amount"
    row["instrument"] = "facility"
    row["period"] = "2026-10-01"
    row["value"] = value
    row["value_text"] = str(value)
    return row


if __name__ == "__main__":
    unittest.main()
