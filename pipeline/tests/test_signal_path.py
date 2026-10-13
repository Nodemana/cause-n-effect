"""Signal age and the post-signal price path. No network."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pipeline.fixture import fixture_response
from pipeline.prompts import centurion_prompt, legate_selection_prompt, praetorian_prompt
from pipeline.signal_path import assess, window_start
from pipeline.store import record_price_path
from shared.pipeline_store import connect, lodge_lead

SWEEP_AT = "2026-10-12T18:00:00Z"
SEC = "https://www.sec.gov/Archives/edgar/data/1/lmt.htm"

_LMT_CLOSES = [
    {"date": "2026-07-16", "close": 100},
    {"date": "2026-07-22", "close": 100},
    {"date": "2026-07-23", "close": 110},
    {"date": "2026-07-30", "close": 114},
    {"date": "2026-08-14", "close": 120},
    {"date": "2026-10-12", "close": 100},
]


class SignalPathTests(unittest.TestCase):
    def test_a_july_filing_that_popped_and_reverted_has_passed(self):
        path = assess("2026-07-23", SWEEP_AT, _LMT_CLOSES, current=100)
        self.assertEqual(window_start("2026-07-23"), "2026-07-16")
        self.assertEqual(path["signal_date"], "2026-07-23")
        self.assertEqual(path["days_since_signal"], 81)
        self.assertEqual(path["close_before_signal"], 100)
        self.assertAlmostEqual(path["max_runup_pct"], 20)
        self.assertAlmostEqual(path["current_vs_pre_pct"], 0)
        self.assertTrue(path["popped_and_faded"])
        self.assertTrue(path["move_passed"])
        self.assertTrue(path["stale"])

    def test_a_close_on_the_filing_date_is_not_the_price_before_it(self):
        path = assess("2026-07-23", "2026-10-12", [{"date": "2026-07-23", "close": 110}])
        self.assertIsNone(path["close_before_signal"])
        self.assertIsNone(path["move_passed"])
        outside = assess(
            "2026-07-23",
            "2026-10-12",
            [
                {"date": "2026-07-01", "close": 80},
                {"date": "2026-07-22", "close": 100},
                {"date": "2026-08-14", "close": 120},
            ],
            current=100,
        )
        self.assertEqual(outside["close_before_signal"], 100)

    def test_a_fresh_signal_with_no_bars_stays_blank(self):
        path = assess("2026-10-01", "2026-10-12", [], current=450)
        self.assertEqual(path["days_since_signal"], 11)
        self.assertFalse(path["stale"])
        self.assertIsNone(path["close_before_signal"])
        self.assertIsNone(path["max_runup_pct"])
        self.assertIsNone(path["current_vs_pre_pct"])
        self.assertIsNone(path["popped_and_faded"])
        self.assertIsNone(path["move_passed"])

    def test_a_decline_is_not_a_move_that_already_happened(self):
        path = assess(
            "2026-10-01",
            "2026-10-12",
            [{"date": "2026-09-24", "close": 100}, {"date": "2026-10-12", "close": 90}],
        )
        self.assertEqual(window_start("2026-10-01"), "2026-09-24")
        self.assertAlmostEqual(path["max_runup_pct"], -10)
        self.assertFalse(path["popped_and_faded"])
        self.assertFalse(path["move_passed"])

    def test_age_alone_does_not_mark_the_move_passed(self):
        path = assess("2026-07-23", "2026-10-12", [])
        self.assertEqual(path["days_since_signal"], 81)
        self.assertTrue(path["stale"])
        self.assertIsNone(path["move_passed"])

    def test_the_path_is_stored_on_the_lead(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect(str(Path(tmp) / "history.sqlite"))
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO runs (run_id, scout, started_at) VALUES ('run', 'capex', ?)",
                        (SWEEP_AT,),
                    )
                lead_id = lodge_lead(conn, "capex", "LMT", "US", "capex", "run")
                lead = SimpleNamespace(
                    signal_date="2026-07-23",
                    days_since_signal=81,
                    close_before_signal=100.0,
                    max_runup_pct=20.0,
                    current_vs_pre_pct=0.0,
                    popped_and_faded=True,
                    move_passed=True,
                )
                record_price_path(conn, lead_id, lead, SWEEP_AT)
                with conn:
                    conn.execute(
                        "UPDATE leads SET signal_date = ? WHERE lead_id = ?",
                        ("2026-07-23", lead_id),
                    )
                row = conn.execute("SELECT * FROM lead_price_paths").fetchone()
                stored = conn.execute("SELECT signal_date FROM leads").fetchone()
            finally:
                conn.close()
        self.assertEqual(stored["signal_date"], "2026-07-23")
        self.assertEqual(row["days_since_signal"], 81)
        self.assertEqual(row["close_before_signal"], 100)
        self.assertEqual(row["popped_and_faded"], 1)
        self.assertEqual(row["move_passed"], 1)
        self.assertIsNone(row["as_of"])

    def test_praetorian_flags_a_stale_faded_path_and_a_fresh_path_stays_clean(self):
        path = assess("2026-07-23", SWEEP_AT, _LMT_CLOSES, current=100)
        stale = praetorian_prompt(f"Source: {SEC}", "new", path)
        self.assertIn("STALENESS AND PRICE REACTION", stale)
        self.assertIn("Do not use the 52-week high", stale)
        flaws = fixture_response(stale)["flaws"]
        self.assertEqual(len(flaws), 1)
        self.assertEqual(flaws[0]["severity"], "high")
        self.assertIn("move has likely passed", flaws[0]["flaw"])
        self.assertIn("2026-07-23", flaws[0]["evidence"])
        self.assertIn("81", flaws[0]["evidence"])
        self.assertIn("+20.0", flaws[0]["evidence"])
        fresh = praetorian_prompt(
            f"Source: {SEC}",
            "new",
            {
                "signal_date": "2026-10-01",
                "days_since_signal": 11,
                "close_before_signal": None,
                "max_runup_pct": None,
                "current_vs_pre_pct": None,
                "popped_and_faded": None,
                "move_passed": None,
            },
        )
        self.assertEqual(fixture_response(fresh)["flaws"], [])
        blank = praetorian_prompt(f"Source: {SEC}", "new")
        self.assertEqual(fixture_response(blank)["flaws"], [])

    def test_centurion_priced_in_and_legate_earliness_follow_the_path(self):
        figure = SimpleNamespace(
            figure_type="backlog",
            value=2_000_000_000,
            value_text="orders",
            instrument="quarter",
            period="2026-04-01/2026-06-30",
            source_url=SEC,
            prior_comparison=True,
            growth_pct=20,
            growth_text="up 20 percent",
            market_cap=100_000_000_000,
        )
        lead = SimpleNamespace(
            lead_type="orders_backlog",
            ticker="LMT",
            company="Lockheed Martin",
            figure=figure,
            grade_types=["orders_backlog"],
            counterparties=[],
            gate_results=[],
            signal_date="2026-07-23",
            days_since_signal=81,
            close_before_signal=100.0,
            max_runup_pct=20.0,
            current_vs_pre_pct=0.0,
            popped_and_faded=True,
            move_passed=True,
        )
        prompt = centurion_prompt(lead, [], 1)
        self.assertIn("MOVE_PASSED: yes", prompt)
        self.assertIn("52-week high", prompt)
        self.assertIn("post-signal path", prompt)
        self.assertEqual(fixture_response(prompt)["price_run"], "priced-in")
        legate = legate_selection_prompt(
            [
                {
                    "lead_id": 1,
                    "ticker": "LMT",
                    "rank": 1,
                    "source_scout": "orders_backlog",
                    "summary": "July filing",
                    "flaws": 1,
                }
            ]
        )
        self.assertIn("six weeks", legate)
        self.assertIn("confirming signal", legate)
        self.assertIn("52-week high", legate)
