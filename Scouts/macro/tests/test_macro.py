"""Fixture tests for the macro scout. No network."""

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from Scouts.macro import cache, catalog, fetcher, geopolitics, live, parsers, pipeline, store  # noqa: E402
from shared.nethttp import Client, EndpointError  # noqa: E402

FIX = Path(__file__).resolve().parents[1] / "fixtures"
NOW = "2026-10-12T12:00:00Z"
KEY = "abcdef0123456789abcdef0123456789"


class MacroTests(unittest.TestCase):
    def setUp(self):
        self._urlopen = urllib.request.urlopen
        urllib.request.urlopen = self._refuse
        self._env = {
            "FRED_API_KEY": os.environ.get("FRED_API_KEY"),
            "EIA_API_KEY": os.environ.get("EIA_API_KEY"),
            "MACRO_DB": os.environ.get("MACRO_DB"),
        }
        os.environ.pop("FRED_API_KEY", None)
        os.environ.pop("EIA_API_KEY", None)
        os.environ.pop("MACRO_DB", None)
        cache.use(None)

    def tearDown(self):
        urllib.request.urlopen = self._urlopen
        for name, value in self._env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        cache.use(None)

    def _refuse(self, *args, **kwargs):
        raise AssertionError("network call")

    def test_help_makes_no_network_call(self):
        os.environ["FRED_API_KEY"] = KEY
        os.environ["EIA_API_KEY"] = KEY
        buf = io.StringIO()
        with redirect_stdout(buf):
            with self.assertRaises(SystemExit) as caught:
                live.main(["--help"])
        self.assertEqual(caught.exception.code, 0)
        text = buf.getvalue()
        self.assertIn("--force", text)
        self.assertIn("Does not lodge leads", text)
        self.assertNotIn(KEY, text)

    def test_catalog_covers_the_macro_set(self):
        ids = {row["series_id"] for row in catalog.SERIES}
        self.assertTrue(
            {
                "DFF",
                "DGS2",
                "DGS10",
                "RBA_CASH",
                "AU10Y",
                "US_CPI",
                "AU_CPI",
                "WALCL",
                "RBA_ASSETS",
                "IG_OAS",
                "HY_OAS",
                "USD_BROAD",
                "AUDUSD",
                "WTI",
                "BRENT",
                "HENRY_HUB",
                "US_FED_OUTLAYS",
                "US_FED_SURPLUS",
                "AU_GOV_SPENDING",
                "AU_GFS_BALANCE",
                "GSCPI",
            }
            <= ids
        )
        sources = {row["source"] for row in catalog.SERIES}
        self.assertTrue(sources <= {"FRED", "BLS", "ABS", "RBA", "NY Fed", "EIA"})
        for row in catalog.SERIES:
            self.assertNotIn("api_key", row["source_url"])
            self.assertNotIn("@", row["source_url"])
        client = fetcher.macro_client()
        self.assertIsInstance(client, Client)
        self.assertEqual(client.min_interval, 1.0)

    def test_migration_is_idempotent_and_does_not_store_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "macro.sqlite"
            conn = store.open_db(path)
            try:
                self.assertEqual(
                    [row["name"] for row in conn.execute("PRAGMA table_info(macro_series)")],
                    ["series_id", "name", "source", "source_url", "unit", "frequency", "stale_after_hours"],
                )
                self.assertEqual(
                    [row["name"] for row in conn.execute("PRAGMA table_info(macro_observations)")],
                    ["series_id", "obs_date", "value", "fetched_at"],
                )
                results = pipeline.refresh(conn, fixture_dir=FIX, now=NOW, keys={})
            finally:
                conn.close()
            again = store.open_db(path)
            try:
                names = [row["name"] for row in again.execute("SELECT name FROM schema_migrations")]
                self.assertEqual(names.count("001_macro.sql"), 1)
                self.assertEqual(again.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"], 0)
                self.assertEqual(again.execute("SELECT COUNT(*) AS n FROM figures").fetchone()["n"], 0)
                self.assertEqual(again.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"], 0)
                stored = {
                    row["series_id"]
                    for row in again.execute("SELECT DISTINCT series_id FROM macro_observations")
                }
            finally:
                again.close()
        self.assertTrue(all(row["status"] == "refreshed" for row in results))
        self.assertEqual(stored, {row["series_id"] for row in catalog.SERIES})

    def test_unique_observation_updates_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.open_db(Path(tmp) / "macro.sqlite")
            try:
                spec = catalog.by_id()["DFF"]
                store.upsert_series(conn, spec)
                store.save_observations(
                    conn,
                    "DFF",
                    [{"obs_date": "2024-01-02", "value": 5.0}],
                    "2026-10-01T00:00:00Z",
                )
                store.save_observations(
                    conn,
                    "DFF",
                    [{"obs_date": "2024-01-02", "value": 5.33}],
                    "2026-10-12T00:00:00Z",
                )
                conn.commit()
                row = conn.execute(
                    "SELECT value, fetched_at, COUNT(*) AS n FROM macro_observations WHERE series_id = 'DFF'"
                ).fetchone()
                self.assertEqual(row["n"], 1)
                self.assertEqual(row["value"], 5.33)
                self.assertEqual(row["fetched_at"], "2026-10-12T00:00:00Z")
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO macro_observations (series_id, obs_date, value, fetched_at)
                        VALUES ('DFF', '2024-01-02', 1, '2026-10-12T00:00:00Z')
                        """
                    )
            finally:
                conn.close()

    def test_refresh_respects_stale_window_unless_forced(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.open_db(Path(tmp) / "macro.sqlite")
            try:
                first = pipeline.refresh(
                    conn, series_ids=["DFF"], fixture_dir=FIX, now=NOW, keys={}
                )
                self.assertEqual(first[0]["status"], "refreshed")
                self.assertEqual(first[0]["count"], 2)
                fresh = pipeline.refresh(
                    conn,
                    series_ids=["DFF"],
                    fixture_dir="/tmp/macro-fixtures-absent",
                    now="2026-10-12T12:00:01Z",
                    keys={},
                )
                self.assertEqual(fresh[0]["status"], "fresh")
                conn.execute(
                    "UPDATE macro_observations SET fetched_at = ? WHERE series_id = 'DFF'",
                    ("2026-10-11T12:00:01Z",),
                )
                conn.commit()
                self.assertFalse(store.is_stale(conn, "DFF", 24, NOW))
                held = pipeline.refresh(conn, series_ids=["DFF"], fixture_dir=FIX, now=NOW, keys={})
                self.assertEqual(held[0]["status"], "fresh")
                conn.execute(
                    "UPDATE macro_observations SET fetched_at = ? WHERE series_id = 'DFF'",
                    ("2026-10-11T12:00:00Z",),
                )
                conn.commit()
                self.assertTrue(store.is_stale(conn, "DFF", 24, NOW))
                due = pipeline.refresh(conn, series_ids=["DFF"], fixture_dir=FIX, now=NOW, keys={})
                self.assertEqual(due[0]["status"], "refreshed")
                forced = pipeline.refresh(
                    conn,
                    series_ids=["DFF"],
                    fixture_dir=FIX,
                    force=True,
                    now="2026-10-13T00:00:00Z",
                    keys={},
                )
                self.assertEqual(forced[0]["status"], "refreshed")
                latest = cache.get_latest("DFF", db=conn)
                self.assertEqual(latest["obs_date"], "2024-01-04")
                self.assertEqual(latest["value"], 5.33)
                self.assertEqual(latest["fetched_at"], "2026-10-13T00:00:00Z")
                window = cache.get_series("DFF", since="2024-01-04", db=conn)
                self.assertEqual([row["obs_date"] for row in window], ["2024-01-04"])
            finally:
                conn.close()

    def test_missing_api_key_skips_without_a_request(self):
        calls = []

        class Boom:
            def get(self, url, accept="application/json"):
                calls.append(url)
                raise AssertionError(url)

        with tempfile.TemporaryDirectory() as tmp:
            conn = store.open_db(Path(tmp) / "macro.sqlite")
            try:
                skipped = pipeline.refresh(
                    conn,
                    series_ids=["DFF", "WTI"],
                    client=Boom(),
                    now=NOW,
                    keys={"FRED_API_KEY": "", "EIA_API_KEY": ""},
                )
            finally:
                conn.close()
        self.assertEqual(calls, [])
        self.assertEqual(skipped[0]["status"], "skipped")
        self.assertIn("FRED_API_KEY is unset", skipped[0]["reason"])
        self.assertEqual(skipped[1]["status"], "skipped")
        self.assertIn("EIA_API_KEY is unset", skipped[1]["reason"])

    def test_api_key_is_redacted_from_errors_and_the_database(self):
        class Boom:
            def get(self, url, accept="application/json"):
                raise EndpointError(f"HTTP 400 for {url}")

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "macro.sqlite"
            conn = store.open_db(path)
            try:
                failed = pipeline.refresh(
                    conn,
                    series_ids=["DFF"],
                    client=Boom(),
                    now=NOW,
                    keys={"FRED_API_KEY": KEY, "EIA_API_KEY": ""},
                )
            finally:
                conn.close()
            blob = path.read_bytes()
        self.assertEqual(failed[0]["status"], "error")
        self.assertNotIn(KEY, failed[0]["reason"])
        self.assertIn("[redacted]", failed[0]["reason"])
        self.assertNotIn(KEY.encode(), blob)

    def test_parsers_keep_published_observations(self):
        fred = parsers.parse(catalog.by_id()["DFF"], (FIX / "fred_observations.json").read_bytes())
        self.assertEqual(
            fred,
            [
                {"obs_date": "2024-01-02", "value": 5.33},
                {"obs_date": "2024-01-04", "value": 5.33},
            ],
        )
        with self.assertRaises(fetcher.MissingKey):
            fetcher.load_observations(
                catalog.by_id()["DFF"],
                None,
                {},
                None,
                date(2026, 10, 12),
            )
        truncated = dict(json.loads((FIX / "fred_observations.json").read_text(encoding="utf-8")))
        truncated["count"] = 9
        with self.assertRaises(ValueError):
            parsers.parse_fred(truncated)

        cpi = parsers.parse(catalog.by_id()["US_CPI"], (FIX / "bls_cpi.json").read_bytes())
        self.assertEqual(
            cpi,
            [
                {"obs_date": "2024-12-31", "value": 315.605},
                {"obs_date": "2024-01-31", "value": 308.417},
            ],
        )
        self.assertEqual(fetcher.bls_years(date(2026, 10, 12)), (2017, 2026))
        failed = {"status": "REQUEST_NOT_PROCESSED", "message": ["year span exceeds 10 years"]}
        with self.assertRaises(ValueError) as caught:
            parsers.parse_bls(failed)
        self.assertIn("year span", str(caught.exception))

        au_cpi = parsers.parse(catalog.by_id()["AU_CPI"], (FIX / "abs_cpi.csv").read_bytes())
        self.assertEqual([row["obs_date"] for row in au_cpi], ["2024-03-31", "2024-09-30"])
        spending = parsers.parse(catalog.by_id()["AU_GOV_SPENDING"], (FIX / "abs_gov.csv").read_bytes())
        self.assertEqual(spending[0]["value"], 170659)
        self.assertEqual(spending[0]["obs_date"], "2025-12-31")

        cash = parsers.parse(catalog.by_id()["RBA_CASH"], (FIX / "rba_f1.csv").read_bytes())
        self.assertEqual([row["value"] for row in cash], [4.75, 4.75, 4.50])
        bond = parsers.parse(catalog.by_id()["AU10Y"], (FIX / "rba_f1.csv").read_bytes())
        self.assertEqual([row["obs_date"] for row in bond], ["2011-01-04", "2011-01-06"])
        self.assertEqual(bond[0]["value"], 5.55)
        aud = parsers.parse(catalog.by_id()["AUDUSD"], (FIX / "rba_f1.csv").read_bytes())
        self.assertEqual([row["value"] for row in aud], [1.0, 0.99])
        with self.assertRaises(ValueError):
            parsers.parse_rba((FIX / "rba_f1.csv").read_text(encoding="utf-8"), "MISSING")

        gscpi = parsers.parse(catalog.by_id()["GSCPI"], (FIX / "gscpi.csv").read_bytes())
        self.assertEqual(
            gscpi,
            [
                {"obs_date": "1997-09-30", "value": -0.05},
                {"obs_date": "1997-10-31", "value": -0.18},
            ],
        )

        oil = parsers.parse(catalog.by_id()["WTI"], (FIX / "eia_spot.json").read_bytes())
        self.assertEqual(oil, [{"obs_date": "2024-01-02", "value": 72.10}])

        balance = parsers.parse(catalog.by_id()["AU_GFS_BALANCE"], (FIX / "gfs_table1.xlsx").read_bytes())
        self.assertEqual(
            balance,
            [
                {"obs_date": "2025-09-30", "value": -34338},
                {"obs_date": "2025-12-31", "value": -1474},
            ],
        )
        link = parsers.gfs_xlsx_url((FIX / "gfs_latest.html").read_text(encoding="utf-8"))
        self.assertEqual(
            link,
            "https://www.abs.gov.au/statistics/economy/government/"
            "government-finance-statistics-australia/jun-2026/Jun%20Quarter%202026.xlsx",
        )

    def test_eia_pages_follow_the_total(self):
        spec = catalog.by_id()["WTI"]
        key = "eia-test-key"
        first = fetcher.eia_url(spec["route"], spec["remote_id"], key, fetcher.HISTORY_START, 0, 2)
        second = fetcher.eia_url(spec["route"], spec["remote_id"], key, fetcher.HISTORY_START, 2, 2)

        class Pages:
            def __init__(self):
                self.urls = []

            def get(self, url, accept="application/json"):
                self.urls.append(url)
                if url == first:
                    payload = {
                        "response": {
                            "total": "3",
                            "data": [
                                {"period": "2024-01-02", "value": "70"},
                                {"period": "2024-01-03", "value": "71"},
                            ],
                        }
                    }
                elif url == second:
                    payload = {
                        "response": {
                            "total": "3",
                            "data": [{"period": "2024-01-04", "value": "72"}],
                        }
                    }
                else:
                    raise AssertionError(url)
                return json.dumps(payload).encode("utf-8"), accept

        client = Pages()
        rows = parsers.parse_eia(fetcher._eia_pages(spec, client, key, length=2))
        self.assertEqual([row["value"] for row in rows], [70, 71, 72])
        self.assertEqual(client.urls, [first, second])
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(first).query)
        self.assertEqual(query["facets[series][]"], ["RWTC"])
        self.assertEqual(query["api_key"], [key])

    def test_cache_reads_by_series_and_since(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "macro.sqlite"
            conn = store.open_db(path)
            try:
                pipeline.refresh(conn, series_ids=["DGS10"], fixture_dir=FIX, now=NOW, keys={})
            finally:
                conn.close()
            cache.use(path)
            latest = cache.get_latest("DGS10")
            self.assertEqual(latest["obs_date"], "2024-01-04")
            self.assertEqual(latest["unit"], "percent")
            self.assertEqual(latest["source"], "FRED")
            self.assertIsNone(cache.get_latest("MISSING"))
            self.assertEqual(
                [row["obs_date"] for row in cache.get_series("DGS10", "2024-01-03")],
                ["2024-01-04"],
            )
            self.assertEqual(cache.get_series("MISSING"), [])
            with self.assertRaises(ValueError):
                cache.get_series("DGS10", "2024/01/03")
            os.environ["MACRO_DB"] = str(path)
            cache.use(None)
            self.assertEqual(cache.get_latest("DGS10")["series_id"], "DGS10")

    def test_geopolitics_placeholder_has_no_notes(self):
        notes = geopolitics.get_notes()
        self.assertEqual([item["topic"] for item in notes], ["geopolitics", "election", "regulatory"])
        for item in notes:
            self.assertEqual(item["status"], "placeholder")
            self.assertEqual(item["notes"], [])
        self.assertEqual(geopolitics.get_notes("election")["notes"], [])
        with self.assertRaises(ValueError):
            geopolitics.get_notes("headlines")
        self.assertIn("placeholder", pipeline.risk_note_line())

    def test_live_fixture_run_and_unknown_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "macro.sqlite")
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = live.main(["--db", path, "--fixture-dir", str(FIX), "--series", "RBA_CASH,GSCPI"])
            self.assertEqual(code, 0)
            text = buf.getvalue()
            self.assertIn("RBA_CASH refreshed 3", text)
            self.assertIn("GSCPI refreshed 2", text)
            self.assertIn("risk notes: placeholder", text)
            self.assertNotIn(KEY, text)
            buf = io.StringIO()
            with redirect_stdout(buf):
                skipped = live.main(["--db", path, "--series", "DFF,WTI"])
            self.assertEqual(skipped, 0)
            self.assertIn("DFF skipped FRED_API_KEY is unset", buf.getvalue())
            self.assertIn("WTI skipped EIA_API_KEY is unset", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                unknown = live.main(["--db", path, "--series", "NOT_A_SERIES"])
            self.assertEqual(unknown, 2)
            self.assertIn("unknown series: NOT_A_SERIES", buf.getvalue())
            conn = sqlite3.connect(path)
            try:
                leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
                cash = conn.execute(
                    "SELECT value FROM macro_observations WHERE series_id = 'RBA_CASH' ORDER BY obs_date"
                ).fetchall()
            finally:
                conn.close()
        self.assertEqual(leads, 0)
        self.assertEqual([row[0] for row in cash], [4.75, 4.75, 4.5])

    def test_pipeline_and_macro_migrations_apply_in_either_order(self):
        from pipeline.prompts import load_macro
        from shared.pipeline_store import connect as pipeline_connect
        from shared.pipeline_store import ensure_pipeline

        with tempfile.TemporaryDirectory() as tmp:
            macro_first = Path(tmp) / "macro-first.sqlite"
            conn = store.open_db(macro_first)
            try:
                ensure_pipeline(conn)
                pipeline.refresh(conn, series_ids=["DGS10"], fixture_dir=FIX, now=NOW, keys={})
                text = load_macro(conn, path=Path(tmp) / "absent.json")
                names = [row["name"] for row in conn.execute("SELECT name FROM schema_migrations")]
            finally:
                conn.close()
            again = store.open_db(macro_first)
            try:
                ensure_pipeline(again)
                names_again = [row["name"] for row in again.execute("SELECT name FROM schema_migrations")]
            finally:
                again.close()

            pipeline_first = Path(tmp) / "pipeline-first.sqlite"
            conn = pipeline_connect(str(pipeline_first))
            try:
                store.migrate(conn)
                pipeline.refresh(conn, series_ids=["DGS10"], fixture_dir=FIX, now=NOW, keys={})
                second = load_macro(conn, path=Path(tmp) / "absent.json")
                pipeline_names = [row["name"] for row in conn.execute("SELECT name FROM schema_migrations")]
            finally:
                conn.close()

        for recorded in (names, names_again, pipeline_names):
            self.assertEqual(recorded.count("001_macro.sql"), 1)
            self.assertEqual(recorded.count("001_pipeline.sql"), 1)
            self.assertEqual(recorded.count("002_lead_pipeline.sql"), 1)
        for reading in (text, second):
            self.assertIn("DGS10", reading)
            self.assertIn("US Treasury constant maturity, 10-year", reading)
            self.assertIn("5.33 percent", reading)
            self.assertIn("2024-01-04", reading)
            self.assertIn("(FRED)", reading)
            self.assertNotIn("2024-01-02", reading)


if __name__ == "__main__":
    unittest.main()
