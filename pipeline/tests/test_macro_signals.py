"""Macro signals from stored observations. No network and no model."""

import json
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipeline.fixture import run_with_fixture  # noqa: E402
from pipeline.macro_signals import ONE_MONTH_PCT, THREE_MONTH_PCT, detect  # noqa: E402
from shared.pipeline_store import connect  # noqa: E402
from shared.store import save_run  # noqa: E402

SWEEP_AT = "2026-10-12T18:00:00Z"
LATEST = date(2026, 10, 6)
BRENT_URL = "https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm"
WTI_URL = "https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm"


class MacroSignalTests(unittest.TestCase):
    def test_brent_move_and_brent_wti_spread_become_macro_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = str(Path(tmp) / "agent_queue")
            conn = connect(path)
            try:
                _macro_tables(conn)
                _seed_oil(conn)
                _seed_related(conn)
                signals = {row["ticker"]: row for row in detect(conn, SWEEP_AT)}
            finally:
                conn.close()
            brent = signals["BRENT"]
            self.assertEqual(brent["points"]["latest"]["value"], 125.44)
            self.assertEqual(brent["points"]["latest"]["obs_date"], "2026-10-06")
            self.assertEqual(brent["points"]["1m"]["value"], 102.24)
            self.assertEqual(brent["points"]["1m"]["obs_date"], "2026-09-06")
            self.assertEqual(brent["points"]["3m"]["value"], 71.78)
            self.assertEqual(brent["points"]["3m"]["obs_date"], "2026-07-06")
            self.assertGreaterEqual(abs(brent["changes"]["1m_pct"]), ONE_MONTH_PCT)
            self.assertGreaterEqual(abs(brent["changes"]["3m_pct"]), THREE_MONTH_PCT)
            self.assertAlmostEqual(brent["changes"]["1m_pct"], (125.44 - 102.24) / 102.24 * 100.0)
            self.assertAlmostEqual(brent["changes"]["3m_pct"], (125.44 - 71.78) / 71.78 * 100.0)
            self.assertEqual(brent["points"]["latest"]["source_url"], BRENT_URL)
            self.assertNotIn("WTI", signals)
            spread = signals["BRENT-WTI"]
            self.assertAlmostEqual(spread["value"], 125.44 - 96.24)
            self.assertGreater(spread["value"], spread["range_high"])
            legs = {(item.get("role"), item["series_id"]): item for item in spread["series"]}
            self.assertEqual(legs[(None, "BRENT")]["value"], 125.44)
            self.assertEqual(legs[(None, "WTI")]["value"], 96.24)
            self.assertEqual(legs[("1m", "WTI")]["value"], 92.69)
            self.assertEqual(legs[("1m", "BRENT")]["value"], 102.24)
            self.assertEqual(legs[(None, "WTI")]["source_url"], WTI_URL)
            result = run_with_fixture(path, queue_dir=queue, now=SWEEP_AT)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["stage"], "legate_selection")
            tasks = [json.loads(path.read_text(encoding="utf-8")) for path in (Path(queue) / result["run_id"] / "significance").glob("*.task.json")]
            blob = "\n".join(task["prompt"] for task in tasks)
            self.assertIn("125.44", blob)
            self.assertIn("102.24", blob)
            self.assertIn("71.78", blob)
            self.assertIn("96.24", blob)
            self.assertIn("92.69", blob)
            self.assertIn(BRENT_URL, blob)
            self.assertIn("macro signal", blob)
            rules = next(task["rules"] for task in tasks if task["subject"]["lead_type"] == "macro")
            self.assertIn("likely effects", rules)
            self.assertIn("winning tickers", rules)
            self.assertIn("losing tickers", rules)
            conn = connect(path)
            try:
                rows = conn.execute(
                    """
                    SELECT ticker, lead_type, exchange, source_url, summary, source_scout
                    FROM leads
                    WHERE lead_type = 'macro'
                    ORDER BY ticker
                    """
                ).fetchall()
                theses = {
                    row["ticker"]: row["thesis_text"]
                    for row in conn.execute(
                        """
                        SELECT l.ticker, t.thesis_text
                        FROM leads AS l
                        JOIN theses AS t ON t.lead_id = l.lead_id
                        WHERE l.lead_type = 'macro'
                        """
                    )
                }
            finally:
                conn.close()
            _assert_brent_related(self, queue, result["run_id"])
            _assert_brent_report(self, result["report"])
        self.assertEqual([row["ticker"] for row in rows], ["BRENT", "BRENT-WTI"])
        self.assertTrue(all(row["exchange"] == "MACRO" for row in rows))
        self.assertTrue(all(row["source_scout"] == "macro" for row in rows))
        self.assertTrue(all(row["source_url"] == BRENT_URL for row in rows))
        self.assertIn("125.44", rows[0]["summary"])
        self.assertIn("29.20", rows[1]["summary"])
        self.assertIn("Likely effects", theses["BRENT"])
        self.assertIn("Winning tickers", theses["BRENT"])
        self.assertIn("losing tickers", theses["BRENT"])

    def test_a_quiet_move_and_a_missing_table_emit_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = connect(path)
            try:
                self.assertEqual(detect(conn, SWEEP_AT), [])
                _macro_tables(conn)
                conn.execute(
                    """
                    INSERT INTO macro_series (
                        series_id, name, source, source_url, unit, frequency, stale_after_hours
                    ) VALUES ('DGS10', '10-year', 'FRED', 'https://fred.stlouisfed.org/series/DGS10', 'percent', 'daily', 24)
                    """
                )
                conn.executemany(
                    """
                    INSERT INTO macro_observations (series_id, obs_date, value, fetched_at)
                    VALUES ('DGS10', ?, ?, '2026-10-12T00:00:00Z')
                    """,
                    [("2026-09-01", 3.9), ("2026-10-01", 4.2)],
                )
                conn.commit()
                self.assertEqual(detect(conn, SWEEP_AT), [])
            finally:
                conn.close()


def _macro_tables(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_series (
            series_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source TEXT NOT NULL,
            source_url TEXT NOT NULL,
            unit TEXT NOT NULL,
            frequency TEXT NOT NULL,
            stale_after_hours INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS macro_observations (
            series_id TEXT NOT NULL REFERENCES macro_series(series_id),
            obs_date TEXT NOT NULL,
            value REAL NOT NULL,
            fetched_at TEXT NOT NULL,
            UNIQUE (series_id, obs_date)
        )
        """
    )
    conn.commit()


def _seed_oil(conn):
    conn.execute(
        """
        INSERT INTO macro_series (
            series_id, name, source, source_url, unit, frequency, stale_after_hours
        ) VALUES
            ('BRENT', 'Brent', 'EIA', ?, 'USD per barrel', 'daily', 24),
            ('WTI', 'WTI', 'EIA', ?, 'USD per barrel', 'daily', 24)
        """,
        (BRENT_URL, WTI_URL),
    )
    brent = {}
    wti = {}
    day = date(2025, 10, 13)
    index = 0
    while day < LATEST:
        wti[day] = 80 + 30 * ((index % 17) / 16)
        brent[day] = wti[day] + 4
        day += timedelta(days=7)
        index += 1
    wti[date(2026, 7, 6)] = 90.0
    brent[date(2026, 7, 6)] = 71.78
    wti[date(2026, 9, 6)] = 92.69
    brent[date(2026, 9, 6)] = 102.24
    wti[date(2026, 10, 6)] = 96.24
    brent[date(2026, 10, 6)] = 125.44
    rows = []
    for series_id, series in (("BRENT", brent), ("WTI", wti)):
        for obs_date, value in series.items():
            rows.append((series_id, obs_date.isoformat(), value))
    conn.executemany(
        """
        INSERT INTO macro_observations (series_id, obs_date, value, fetched_at)
        VALUES (?, ?, ?, '2026-10-12T00:00:00Z')
        """,
        rows,
    )
    conn.commit()


def _assert_brent_related(test, queue, run_id):
    test.assertFalse((Path(queue) / run_id / "scout_team").exists())
    centurion = None
    spread = None
    for task_path in (Path(queue) / run_id / "centurion_1").glob("*.task.json"):
        task = json.loads(task_path.read_text(encoding="utf-8"))
        if task["subject"]["ticker"] == "BRENT":
            centurion = task
        if task["subject"]["ticker"] == "BRENT-WTI":
            spread = task
    test.assertIsNotNone(centurion)
    test.assertIsNotNone(spread)
    for task in (centurion, spread):
        prompt = task["prompt"]
        test.assertIn("RELATED_FINDINGS_BEGIN", prompt)
        test.assertIn("Gulf Refining", prompt)
        test.assertIn("Nordic Tankers", prompt)
        test.assertNotIn("Cloud Software", prompt)
        names = {item["company"] for item in task["evidence"]["related_findings"]}
        test.assertIn("Gulf Refining", names)
        test.assertIn("Nordic Tankers", names)
        test.assertNotIn("Cloud Software", names)
    test.assertIn("125.44", centurion["prompt"])


def _assert_brent_report(test, report_path):
    directory = Path(report_path)
    files = sorted(directory.glob("*.md"))
    test.assertGreaterEqual(len(files), 1)
    test.assertLessEqual(len(files), 5)
    test.assertFalse((directory.parent / f"{directory.name}-final.md").exists())
    candidates_path = directory.parent / f"{directory.name}-candidates.md"
    candidates = candidates_path.read_text(encoding="utf-8")
    test.assertGreaterEqual(candidates.count("\n## Rank "), len(files))
    test.assertIn("### Praetorian", candidates)
    test.assertIn("BRENT", candidates)
    brent_path = next(path for path in files if path.name.split("-", 1)[1] == "BRENT.md")
    brent = brent_path.read_text(encoding="utf-8")
    test.assertNotIn("Cap ", brent)
    test.assertIn("### Thesis", brent)
    test.assertIn("### Cause → effect", brent)
    cause = brent.split("### Cause → effect", 1)[1].split("### Thesis", 1)[0]
    test.assertIn("Brent was 125.44 on 2026-10-06", cause)
    test.assertIn("Up 22.7% over one month.", cause)
    test.assertIn("Up 74.8% over three months.", cause)
    test.assertIn("standard deviations above its prior-year average.", cause)
    test.assertNotIn("1m_pct", cause)
    test.assertNotIn("z_score", cause)
    test.assertNotIn("macro_level", cause)
    test.assertIn("Gulf Refining", cause)
    test.assertIn("Nordic Tankers", cause)
    test.assertIn("125.44", brent)
    test.assertIn("Gulf Refining", brent)
    test.assertIn("Nordic Tankers", brent)
    test.assertNotIn("Cloud Software", brent)
    test.assertIn(BRENT_URL, brent)
    test.assertIn("### Evidence", brent)
    test.assertIn("### Praetorian critique", brent)
    test.assertLess(brent.find("### Bottom line"), brent.find("### Praetorian critique"))
    test.assertLess(brent.find("| Ticker |"), brent.find("### Bottom line"))
    test.assertNotIn("Stage skipped.", brent)
    test.assertTrue("No flaws recorded." in brent or "| Evidence |" in brent)
    test.assertIn("### Macro context", brent)
    test.assertIn("| Ticker |", brent)
    test.assertIn("| BRENT | 125.44 USD per barrel (2026-10-06) | +19.9% | n/a | n/a |", brent)
    test.assertIn("| VLO | 10.00 USD (2026-10-12) | n/a | n/a | n/a |", brent)
    test.assertIn("| TNKR | 10.00 USD (2026-10-12) | n/a | n/a | n/a |", brent)
    test.assertLess(brent.find("| BRENT |"), brent.find("| VLO |"))
    test.assertIn("| BRENT | 125.44 USD per barrel (2026-10-06) | +19.9% | n/a | n/a |", candidates)


def _seed_related(conn):
    """A refiner, a tanker, and an unrelated name that must stay off the Brent task."""
    save_run(
        conn,
        "capex",
        "2026-10-12T15:00:00Z",
        [
            _figure(
                "VLO",
                "Gulf Refining",
                "PaymentsToAcquireProductiveAssets",
                "quarter",
                "2026-07-01/2026-09-30",
                5_000_000_000,
                "refiner expansion",
                "https://www.sec.gov/Archives/edgar/data/103500/vlo.htm",
            ),
            _figure(
                "VLO",
                "Gulf Refining",
                "PaymentsToAcquirePropertyPlantAndEquipment",
                "quarter",
                "2025-07-01/2025-09-30",
                1_000_000_000,
                "1000000000",
                "https://www.sec.gov/Archives/edgar/data/103500/vlo-prior.htm",
            ),
            *_caps("VLO", "TNKR", "CLDS"),
        ],
    )
    save_run(
        conn,
        "orders-backlog",
        "2026-10-12T15:01:00Z",
        [
            _figure(
                "TNKR",
                "Nordic Tankers",
                "supplier_order",
                "charter",
                "2026-10-01",
                2_000_000_000,
                "tanker newbuild",
                "https://www.sec.gov/Archives/edgar/data/103501/tnkr.htm",
            ),
            _figure(
                "TNKR",
                "Nordic Tankers",
                "customer_backlog",
                "RevenueRemainingPerformanceObligation",
                "2026-06-30",
                40_000_000,
                "40000000",
                "https://www.sec.gov/Archives/edgar/data/103501/tnkr-prior.htm",
            ),
        ],
    )
    save_run(
        conn,
        "insider-buying",
        "2026-10-12T15:02:00Z",
        [
            _figure(
                "CLDS",
                "Cloud Software",
                "insider_purchase",
                "ceo",
                "2026-10-01|Ada",
                250000,
                "250000 shares",
                "https://www.sec.gov/Archives/edgar/data/103502/clds.xml",
            )
        ],
    )


def _caps(*tickers):
    rows = []
    for ticker in tickers:
        rows.append(
            _figure(
                ticker,
                ticker,
                "EntityCommonStockSharesOutstanding",
                "shares",
                "2026-10-01",
                1_000_000_000,
                "1000000000",
                f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-shares.htm",
            )
        )
        rows.append(
            _figure(
                ticker,
                ticker,
                "share_price",
                "close",
                "2026-10-01",
                10,
                "10",
                f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-price.htm",
            )
        )
    return rows


def _figure(ticker, company, figure_type, instrument, period, value, value_text, source_url):
    return {
        "ticker": ticker,
        "company": company,
        "figure_type": figure_type,
        "instrument": instrument,
        "period": period,
        "value": value,
        "value_text": value_text,
        "currency": "USD",
        "seen_at": "2026-10-12",
        "announced_on": "2026-10-01",
        "source_url": source_url,
        "source_type": "filing",
        "verified": 1,
    }


if __name__ == "__main__":
    unittest.main()
