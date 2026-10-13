"""Scout rubric gate: pass, fail, and needs_price. No network."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pipeline.flow as flow  # noqa: E402
from pipeline.flow import ingest_run, run_pipeline  # noqa: E402
from pipeline.load import load_latest_figures  # noqa: E402
from pipeline.prefilter import prefilter  # noqa: E402
from pipeline.rubric_gate import evaluate  # noqa: E402
from shared.pipeline_store import connect  # noqa: E402
from shared.store import save_run  # noqa: E402

SWEEP_AT = "2026-10-12T18:00:00Z"


class RubricGateTests(unittest.TestCase):
    def setUp(self):
        self._utc_now = flow.utc_now
        flow.utc_now = lambda: SWEEP_AT

    def tearDown(self):
        flow.utc_now = self._utc_now

    def test_pass_fail_and_needs_price(self):
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
                        _capex("PASS", "2026-07-01/2026-09-30", 5_000_000_000),
                        _capex("PASS", "2025-07-01/2025-09-30", 1_000_000_000, figure_type="PaymentsToAcquirePropertyPlantAndEquipment"),
                        _shares("PASS"),
                        _capex("FLAT", "2026-07-01/2026-09-30", 1_050_000_000),
                        _capex("FLAT", "2025-07-01/2025-09-30", 1_000_000_000, figure_type="PaymentsToAcquirePropertyPlantAndEquipment"),
                        _shares("FLAT"),
                        _capex("WAIT", "2026-07-01/2026-09-30", 5_000_000_000),
                        _capex("WAIT", "2025-07-01/2025-09-30", 1_000_000_000, figure_type="PaymentsToAcquirePropertyPlantAndEquipment"),
                        _shares("WAIT"),
                        _capex("GRANT", "2026-07-01/2026-09-30", 5_000_000_000),
                    ],
                )
                from pipeline.prices import insert_price

                for ticker in ("PASS", "FLAT"):
                    insert_price(
                        conn,
                        ticker=ticker,
                        exchange="US",
                        price=10,
                        currency="USD",
                        market_cap=10_000_000_000,
                        source="ibkr",
                        status="DELAYED",
                        fetched_at=SWEEP_AT,
                        as_of="2026-10-12",
                    )
                screened, _rejects, _used = prefilter(
                    conn, load_latest_figures(conn, as_of=SWEEP_AT), now=SWEEP_AT
                )
                by_ticker = {candidate.figure.ticker: candidate for candidate in screened}
                passed = evaluate(conn, by_ticker["PASS"].figure, "capex", now=SWEEP_AT)
                failed = evaluate(conn, by_ticker["FLAT"].figure, "capex", now=SWEEP_AT)
                waiting = evaluate(conn, by_ticker["WAIT"].figure, "capex", now=SWEEP_AT)
            finally:
                conn.close()
            result = run_pipeline(path, queue_dir=queue, now=SWEEP_AT)
            needed = json.loads((Path(queue) / result["run_id"] / "prices" / "needed.json").read_text(encoding="utf-8"))
            conn = connect(path)
            try:
                rows = {
                    row["ticker"]: row
                    for row in conn.execute("SELECT ticker, status, summary FROM leads")
                }
                flat = conn.execute(
                    """
                    SELECT c.name, r.passed, r.measured_value, r.evidence, r.rubric_id, r.rubric_version
                    FROM lead_criteria_results AS r
                    JOIN rubric_criteria AS c ON c.criterion_id = r.criterion_id
                    JOIN leads AS l ON l.lead_id = r.lead_id
                    WHERE l.ticker = 'FLAT' AND c.name = 'capex_growth_yoy'
                    """
                ).fetchone()
                tasks = list((Path(queue) / result["run_id"] / "significance").glob("*.task.json"))
            finally:
                conn.close()
            self.assertEqual(passed.status, "pass")
            self.assertTrue(all(item["passed"] for item in passed.results))
            self.assertEqual(failed.status, "filtered")
            self.assertIn("capex_growth_yoy", {item["name"] for item in failed.results if not item["passed"]})
            self.assertEqual(waiting.status, "needs_price")
            self.assertTrue(any(item["needs_price"] and item["name"] == "min_market_cap" for item in waiting.results))
            self.assertEqual(result["stage"], "prices")
            self.assertEqual([item["ticker"] for item in needed["tickers"]], ["WAIT"])
            self.assertEqual(rows["FLAT"]["status"], "filtered")
            self.assertEqual(rows["WAIT"]["status"], "needs_price")
            self.assertNotIn("PASS", rows)
            self.assertEqual(flat["passed"], 0)
            self.assertIsNotNone(flat["rubric_id"])
            self.assertEqual(flat["rubric_version"], 1)
            self.assertTrue(flat["measured_value"])
            self.assertIn("below", flat["evidence"])
            self.assertEqual(tasks, [])

            quote = Path(queue) / result["run_id"] / "prices" / "prices.result.json"
            quote.write_text(
                json.dumps(
                    [{"ticker": "WAIT", "conid": 1, "last": 10, "top_status": "DELAYED", "as_of": "2026-10-12"}]
                )
                + "\n",
                encoding="utf-8",
            )
            advanced = ingest_run(path, result["run_id"], queue_dir=queue)
            self.assertEqual(advanced["stage"], "significance")
            conn = connect(path)
            try:
                statuses = {
                    row["ticker"]: row["status"]
                    for row in conn.execute("SELECT ticker, status FROM leads")
                }
                queued = list((Path(queue) / result["run_id"] / "significance").glob("*.task.json"))
            finally:
                conn.close()
            self.assertNotIn("WAIT", statuses)
            self.assertEqual(statuses["FLAT"], "filtered")
            self.assertGreaterEqual(len(queued), 1)

    def test_raise_below_the_stored_cap_is_filtered(self):
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
                from pipeline.prices import insert_price

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
                candidates, _rejects, _used = prefilter(
                    conn, load_latest_figures(conn, as_of=SWEEP_AT), now=SWEEP_AT
                )
                outcome = evaluate(conn, candidates[0].figure, "debt_raise", now=SWEEP_AT)
            finally:
                conn.close()
        self.assertEqual(outcome.status, "filtered")
        failed = [item for item in outcome.results if item["name"] == "raise_vs_company"][0]
        self.assertFalse(failed["passed"])
        self.assertIn("below", failed["evidence"])
        self.assertEqual(failed["rubric_version"], 1)

    def test_macro_has_no_rubric_gate(self):
        figure = type("Figure", (), {"ticker": "BRENT", "figure_type": "macro", "value_text": "", "instrument": "", "period": "", "source_url": "", "source_type": "filing", "value": 1})()
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = connect(path)
            try:
                outcome = evaluate(conn, figure, "macro", now=SWEEP_AT)
            finally:
                conn.close()
        self.assertEqual(outcome.status, "pass")
        self.assertEqual(outcome.results, [])


def _capex(ticker, period, value, figure_type="PaymentsToAcquireProductiveAssets"):
    return {
        "ticker": ticker,
        "company": ticker,
        "figure_type": figure_type,
        "instrument": "quarter",
        "period": period,
        "value": value,
        "value_text": str(value),
        "currency": "USD",
        "seen_at": "2026-10-12",
        "announced_on": "2026-10-01",
        "source_url": f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}.htm",
        "source_type": "filing",
        "verified": 1,
    }


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


def _debt(ticker, value):
    row = _capex(ticker, "2026-10-01", value, figure_type="facility_amount")
    row["instrument"] = "facility for the plant project"
    row["value_text"] = f"{value} project"
    return row


if __name__ == "__main__":
    unittest.main()
