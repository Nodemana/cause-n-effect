"""Gates exposed by the live stub sweeps. No network."""

import json
import os
import socket
import sqlite3
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import pipeline.flow as flow  # noqa: E402
from pipeline.flow import run_pipeline  # noqa: E402
from pipeline.llm import Completion, StubProvider  # noqa: E402
from pipeline.load import load_latest_figures  # noqa: E402
from pipeline.marketcap import resolve_market_cap  # noqa: E402
from pipeline.prefilter import extract_parties  # noqa: E402
from pipeline.prompts import praetorian_prompt  # noqa: E402
from pipeline.store import search_rejects  # noqa: E402
from shared.pipeline_store import connect  # noqa: E402
from shared.shares import cache_shares_outstanding, shares_from_companyfacts  # noqa: E402
from shared.store import save_run  # noqa: E402

SWEEP_AT = "2026-10-12T18:00:00Z"


class GateTests(unittest.TestCase):
    def setUp(self):
        self._utc_now = flow.utc_now
        flow.utc_now = lambda: SWEEP_AT
        self._email = os.environ.pop("SEC_CONTACT_EMAIL", None)

    def tearDown(self):
        flow.utc_now = self._utc_now
        if self._email is None:
            os.environ.pop("SEC_CONTACT_EMAIL", None)
        else:
            os.environ["SEC_CONTACT_EMAIL"] = self._email
    def test_scout_modules_do_not_shadow_the_pipeline_package(self):
        import pipeline
        from Scouts.accumulation import scout_pipeline as accumulation
        from Scouts.capex import scout_pipeline as capex
        from Scouts.debt_raise import scout_pipeline as debt
        from Scouts.insider_buying import scout_pipeline as insider
        from Scouts.macro import scout_pipeline as macro
        from Scouts.orders_backlog import scout_pipeline as orders

        package = sys.modules["pipeline"]
        self.assertTrue(hasattr(package, "__path__"))
        self.assertIs(package, pipeline)
        from pipeline.flow import run_pipeline as sweep

        self.assertTrue(callable(sweep))
        self.assertTrue(callable(accumulation.analyse))
        self.assertTrue(callable(capex.run))
        self.assertTrue(callable(debt.run))
        self.assertTrue(callable(insider.build_table))
        self.assertTrue(callable(orders.build_table))
        self.assertTrue(callable(macro.refresh))

    def test_limit_keeps_the_newest_announced_figure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T14:30:52Z",
                    [
                        _figure("AAA", "Alpha", "quarter", "2026-07-01/2026-09-30", 5_000_000_000, announced_on="2026-01-01"),
                        _figure("ZZZ", "Zulu", "quarter", "2026-07-01/2026-09-30", 5_000_000_000, announced_on="2026-10-01"),
                        _shares("AAA"),
                    ],
                )
            finally:
                conn.close()
            result = run_pipeline(path, provider=StubProvider(), limit=1, token_budget=80000)
            self.assertEqual(result["figures"], 1)
            conn = connect(path)
            try:
                tickers = {row["ticker"] for row in conn.execute("SELECT ticker FROM lead_rejects")}
                lead_tickers = {row["ticker"] for row in conn.execute("SELECT ticker FROM leads")}
            finally:
                conn.close()
        self.assertIn("ZZZ", tickers | lead_tickers)
        self.assertNotIn("AAA", tickers | lead_tickers)

    def test_only_the_latest_period_and_one_capex_span_are_rated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T14:30:52Z",
                    [
                        _figure("CAT", "Caterpillar", "quarter", "2026-07-01/2026-09-30", 5_000_000_000),
                        _figure("CAT", "Caterpillar", "quarter", "2026-04-01/2026-06-30", 2_000_000_000),
                        _figure("CAT", "Caterpillar", "quarter", "2025-07-01/2025-09-30", 1_000_000_000),
                        _figure("CAT", "Caterpillar", "ttm", "2025-10-01/2026-09-30", 9_000_000_000),
                        *_caps("CAT"),
                    ],
                )
            finally:
                conn.close()
            run_pipeline(path, provider=StubProvider(), token_budget=80000)
            conn = connect(path)
            try:
                reasons = [row["reason"] for row in conn.execute("SELECT reason FROM lead_rejects WHERE ticker = 'CAT'")]
                periods = [
                    row["period"]
                    for row in conn.execute(
                        """
                        SELECT f.period
                        FROM leads AS l
                        JOIN lead_figures AS link ON link.lead_id = l.lead_id
                        JOIN figures AS f ON f.figure_id = link.figure_id
                        WHERE l.ticker = 'CAT'
                        """
                    )
                ]
            finally:
                conn.close()
        self.assertEqual(periods, ["2026-07-01/2026-09-30"])
        self.assertTrue(any("older period kept for growth comparison" in reason for reason in reasons))
        self.assertTrue(any("extra span" in reason for reason in reasons))
        self.assertEqual(sum(1 for reason in reasons if "older period" in reason), 2)

    def test_failed_growth_and_missing_market_cap_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T14:30:52Z",
                    [
                        _figure("FLAT", "Flat Co", "quarter", "2026-07-01/2026-09-30", 1_050_000_000),
                        _figure("FLAT", "Flat Co", "quarter", "2025-07-01/2025-09-30", 1_000_000_000),
                        _figure("NONE", "No Cap", "quarter", "2026-07-01/2026-09-30", 5_000_000_000),
                        _figure("NONE", "No Cap", "quarter", "2025-07-01/2025-09-30", 1_000_000_000),
                        _figure("SHR", "Shares Only", "quarter", "2026-07-01/2026-09-30", 5_000_000_000),
                        _figure("SHR", "Shares Only", "quarter", "2025-07-01/2025-09-30", 1_000_000_000),
                        _shares("SHR"),
                        _price("SHR", source_type="media", source_url="https://example.com/quote"),
                        _figure("SMALL", "Small Cap", "quarter", "2026-07-01/2026-09-30", 5_000_000_000),
                        _figure("SMALL", "Small Cap", "quarter", "2025-07-01/2025-09-30", 1_000_000_000),
                        _shares("SMALL", value=1_000),
                        _price("SMALL", value=10),
                        *_caps("FLAT"),
                    ],
                )
            finally:
                conn.close()
            run_pipeline(path, provider=StubProvider(), token_budget=80000)
            conn = connect(path)
            try:
                reasons = {}
                for row in conn.execute(
                    "SELECT ticker, reason FROM lead_rejects WHERE rejection_stage = 'deterministic'"
                ):
                    reasons.setdefault(row["ticker"], []).append(row["reason"])
                leads = {row["ticker"] for row in conn.execute("SELECT ticker FROM leads")}
                carried = {
                    row["ticker"]: row
                    for row in conn.execute(
                        """
                        SELECT l.ticker, t.thesis_text, t.assumptions,
                               r.assumptions_that_fail, r.surviving_issues,
                               k.surviving_issues AS ranked_issues
                        FROM leads AS l
                        JOIN theses AS t ON t.lead_id = l.lead_id
                        JOIN praetorian_reviews AS r ON r.thesis_id = t.thesis_id
                        JOIN rankings AS k ON k.lead_id = l.lead_id
                        WHERE l.ticker IN ('NONE', 'SHR')
                        """
                    )
                }
            finally:
                conn.close()
        self.assertTrue(any("capex_growth_yoy" in reason and "below" in reason for reason in reasons["FLAT"]))
        self.assertTrue(any("is below" in reason for reason in reasons["SMALL"]))
        self.assertNotIn("FLAT", leads)
        self.assertNotIn("SMALL", leads)
        self.assertTrue(any("older period" in reason for reason in reasons["NONE"]))
        self.assertTrue(any("older period" in reason for reason in reasons["SHR"]))
        self.assertFalse(any("unverified" in reason or "official price" in reason for reason in reasons["NONE"]))
        self.assertFalse(any("official price" in reason or "unverified" in reason for reason in reasons["SHR"]))
        self.assertFalse(any("is below" in reason for reason in reasons["NONE"] + reasons["SHR"]))
        self.assertIn("NONE", leads)
        self.assertIn("SHR", leads)
        for ticker in ("NONE", "SHR"):
            row = carried[ticker]
            self.assertIn("market cap unverified", row["thesis_text"])
            self.assertIn("market cap unverified", row["assumptions"])
            self.assertIn("market cap unverified", row["assumptions_that_fail"])
            self.assertGreaterEqual(row["surviving_issues"], 1)
            self.assertGreaterEqual(row["ranked_issues"], 1)

    def test_market_cap_is_shares_times_an_official_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T14:30:52Z",
                    [
                        _shares("AMZN", value=1_000_000_000),
                        _price("AMZN", value=12),
                        _price(
                            "AMZN",
                            value=10,
                            currency="EUR",
                            instrument="eur",
                            announced_on="2026-10-02",
                        ),
                    ],
                )
                cap, reason = resolve_market_cap(conn, "AMZN")
            finally:
                conn.close()
        self.assertEqual(cap, 12_000_000_000)
        self.assertIn("official price", reason)

    def test_instrument_labels_are_not_counterparties(self):
        figure = _loaded("orders", "40600000000")
        self.assertEqual(extract_parties(figure, "orders_backlog", {}, {}), [])
        labeled = _loaded("backlog|40600000000", "40600000000")
        self.assertEqual(extract_parties(labeled, "orders_backlog", {}, {}), [])
        named = _loaded("Amazon", "2000000000 buyer: Amazon")
        parties = extract_parties(named, "orders_backlog", {"amazon": "AMZN"}, {"AMZN": "1018724"})
        self.assertEqual([party["name"] for party in parties], ["Amazon"])
        self.assertEqual(parties[0]["role"], "buyer")

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "orders-backlog",
                    "2026-10-12T14:31:00Z",
                    [
                        _order("GEV", "orders", "40600000000"),
                        _order("GEV", "backlog|40600000000", "40600000000", figure_type="backlog"),
                        _order("GEV", "Amazon", "2000000000 buyer: Amazon"),
                    ],
                )
            finally:
                conn.close()
            run_pipeline(path, provider=StubProvider(), token_budget=80000)
            conn = connect(path)
            try:
                names = [
                    row["name"]
                    for row in conn.execute("SELECT name FROM lead_reject_counterparties")
                ]
            finally:
                conn.close()
        self.assertIn("Amazon", names)
        self.assertNotIn("orders", names)
        self.assertFalse(any(name.startswith("backlog|") for name in names))

    def test_similar_history_is_routine_and_a_stale_period_is_late(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T14:30:52Z",
                    [
                        _figure("SIM", "Similar Co", "quarter", "2026-07-01/2026-09-30", 5_000_000_000),
                        _figure("SIM", "Similar Co", "quarter", "2025-07-01/2025-09-30", 4_500_000_000),
                        _figure("OLD", "Stale Co", "quarter", "2026-02-01/2026-05-01", 5_000_000_000),
                        _figure("OLD", "Stale Co", "quarter", "2025-02-01/2025-05-01", 1_000_000_000),
                        *_caps("SIM", "OLD"),
                    ],
                )
            finally:
                conn.close()
            run_pipeline(path, provider=StubProvider(), token_budget=80000)
            conn = connect(path)
            try:
                rows = {
                    row["ticker"]: row
                    for row in conn.execute(
                        "SELECT ticker, rejection_stage, reason, significance_novelty, significance_timing FROM lead_rejects"
                    )
                }
            finally:
                conn.close()
        self.assertEqual(rows["SIM"]["rejection_stage"], "llm")
        self.assertEqual(rows["SIM"]["significance_novelty"], "routine")
        self.assertIn("novelty routine", rows["SIM"]["reason"])
        self.assertEqual(rows["OLD"]["rejection_stage"], "llm")
        self.assertEqual(rows["OLD"]["significance_timing"], "late")
        self.assertIn("timing late", rows["OLD"]["reason"])

    def test_stub_praetorian_counts_failed_grades(self):
        failed = praetorian_prompt(
            "\n".join(
                [
                    "## Grade",
                    "",
                    "- min_market_cap: fail. market cap is not in the stored figure",
                    "- capex_growth_yoy: pass. missing evidence",
                    "",
                    "Source: https://www.sec.gov/Archives/edgar/data/1/x.htm",
                ]
            ),
            "new",
        )
        clean = praetorian_prompt(
            "\n".join(
                [
                    "## Grade",
                    "",
                    "- min_market_cap: pass. shares outstanding times an official price",
                    "- capex_growth_yoy: pass. prior comparison in the figures table",
                    "",
                    "Source: https://www.sec.gov/Archives/edgar/data/1/x.htm",
                ]
            ),
            "new",
        )
        provider = StubProvider()
        bad = json.loads(provider.complete(failed, max_tokens=400).text)
        good = json.loads(provider.complete(clean, max_tokens=400).text)
        self.assertGreaterEqual(bad["surviving_issues"], 2)
        self.assertIn("fail", bad["assumptions_that_fail"])
        self.assertIn("missing evidence", bad["assumptions_that_fail"])
        self.assertEqual(good["surviving_issues"], 0)

    def test_a_failed_hard_grade_is_rejected_before_ranking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T14:30:52Z",
                    [
                        _figure("AMZN", "Amazon", "quarter", "2026-07-01/2026-09-30", 5_000_000_000),
                        _figure("AMZN", "Amazon", "quarter", "2025-07-01/2025-09-30", 1_000_000_000),
                        *_caps("AMZN"),
                    ],
                )
            finally:
                conn.close()
            run_pipeline(path, provider=_FailMarketCap(), token_budget=80000)
            conn = connect(path)
            try:
                rows = conn.execute(
                    "SELECT rejection_stage, reason FROM lead_rejects WHERE ticker = 'AMZN'"
                ).fetchall()
                ranked = conn.execute("SELECT COUNT(*) FROM rankings").fetchone()[0]
                leads = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
            finally:
                conn.close()
        failed = [row for row in rows if "failed min_market_cap" in row["reason"]]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["rejection_stage"], "llm")
        self.assertEqual(ranked, 0)
        self.assertEqual(leads, 0)

    def test_recent_window_keeps_an_earlier_run_for_the_same_scout(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "debt-raise",
                    "2026-08-01T12:00:00Z",
                    [_debt("OLD", "facility", 80_000_000)],
                )
                save_run(
                    conn,
                    "debt-raise",
                    "2026-09-20T12:00:00Z",
                    [
                        _debt("SCG.AX", "facility", 1_000_000),
                        _debt("SCG.AX", "bond", 90_000_000),
                    ],
                )
                save_run(
                    conn,
                    "debt-raise",
                    "2026-10-05T12:00:00Z",
                    [_debt("SCG.AX", "facility", 2_000_000), _debt("AAPL", "facility", 1_000_000)],
                )
                loaded = load_latest_figures(conn, as_of=SWEEP_AT)
            finally:
                conn.close()
            by_key = {(figure.ticker, figure.instrument): figure.value for figure in loaded}
            self.assertEqual(by_key[("SCG.AX", "facility")], 2_000_000)
            self.assertEqual(by_key[("SCG.AX", "bond")], 90_000_000)
            self.assertIn(("AAPL", "facility"), by_key)
            self.assertNotIn("OLD", {figure.ticker for figure in loaded})
            run_pipeline(path, provider=StubProvider(), token_budget=80000, now=SWEEP_AT)
            conn = connect(path)
            try:
                tickers = {row["ticker"] for row in conn.execute("SELECT ticker FROM lead_rejects")}
            finally:
                conn.close()
        self.assertIn("SCG.AX", tickers)
        self.assertNotIn("OLD", tickers)

    def test_counterparty_search_is_a_token_prefix_and_holders_are_not_buyers(self):
        holder = _figure(
            "NWT",
            "North West",
            "SC 13D",
            "2026-10-01|0001234567-26-000009|Northwind Capital",
            2,
            value_text="2%",
        )
        holder["figure_type"] = "substantial_holding"
        missing = _figure("ZZZ", "Small Supplier", "Amazon", "2026-10-01", None, value_text="buyer: Amazon")
        missing["figure_type"] = "supplier_order"
        parties = extract_parties(_view(holder), "accumulation", {}, {})
        self.assertEqual(parties[0]["role"], "holder")
        self.assertEqual(parties[0]["name"], "Northwind Capital")

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(conn, "accumulation", "2026-10-12T14:34:00Z", [holder])
                save_run(
                    conn,
                    "orders-backlog",
                    "2026-10-12T14:31:00Z",
                    [missing, _figure("AMZN", "Amazon", "quarter", "2026-07-01/2026-09-30", 5_000_000_000)],
                )
            finally:
                conn.close()
            run_pipeline(path, provider=StubProvider(), token_budget=80000)
            conn = connect(path)
            try:
                roles = {
                    row["name"]: row["role"]
                    for row in conn.execute("SELECT name, role FROM lead_reject_counterparties")
                }
                self.assertEqual(roles["Northwind Capital"], "holder")
                self.assertEqual(
                    [row["ticker"] for row, _parties in search_rejects(conn, counterparty="north")],
                    ["NWT"],
                )
                self.assertEqual(
                    [row["ticker"] for row, _parties in search_rejects(conn, counterparty="NORTH")],
                    ["NWT"],
                )
                self.assertEqual(search_rejects(conn, counterparty="wind"), [])
                self.assertEqual(
                    [row["ticker"] for row, _parties in search_rejects(conn, counterparty="Northwind Capital")],
                    ["NWT"],
                )
                amz = search_rejects(conn, counterparty="amz")
                self.assertEqual([row["ticker"] for row, _parties in amz], ["ZZZ"])
                self.assertEqual(amz[0][1][0]["ticker"], "AMZN")
            finally:
                conn.close()

    def test_implausible_company_total_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                save_run(
                    conn,
                    "orders-backlog",
                    "2026-10-12T14:31:00Z",
                    [
                        _orders("HUBB", "RevenueRemainingPerformanceObligation", "2026-04-01/2026-06-30", 1_000_000_000),
                        _orders("HUBB", "RevenueRemainingPerformanceObligation", "2026-07-01/2026-09-30", 11_000_000),
                        _orders("LOW", "backlog", "2026-04-01/2026-06-30", 1_000_000_000),
                        _orders("LOW", "backlog", "2026-07-01/2026-09-30", 90_000_000),
                        _orders("EDGE", "backlog", "2026-04-01/2026-06-30", 1_000_000_000),
                        _orders("EDGE", "backlog", "2026-07-01/2026-09-30", 100_000_000),
                        _orders("SEG", "backlog:Power", "2026-04-01/2026-06-30", 1_000_000_000),
                        _orders("SEG", "backlog:Power", "2026-07-01/2026-09-30", 11_000_000),
                        _orders("NAM", "Amazon", "2026-04-01/2026-06-30", 1_000_000_000, figure_type="supplier_order"),
                        _orders("NAM", "Amazon", "2026-07-01/2026-09-30", 11_000_000, figure_type="supplier_order"),
                    ],
                )
            finally:
                conn.close()
            run_pipeline(path, provider=StubProvider(), token_budget=80000)
            conn = connect(path)
            try:
                reasons = {}
                for row in conn.execute(
                    "SELECT ticker, reason FROM lead_rejects WHERE rejection_stage = 'deterministic'"
                ):
                    reasons.setdefault(row["ticker"], []).append(row["reason"])
            finally:
                conn.close()
        self.assertTrue(any("implausible backlog" in reason and "10%" in reason for reason in reasons["HUBB"]))
        self.assertTrue(any("implausible backlog" in reason for reason in reasons["LOW"]))
        self.assertFalse(any("implausible" in reason for reason in reasons["EDGE"]))
        self.assertFalse(any("implausible" in reason for reason in reasons["SEG"]))
        self.assertFalse(any("implausible" in reason for reason in reasons["NAM"]))

    def test_shares_helper_caches_companyfacts_without_a_price_or_a_socket(self):
        payload = {
            "entityName": "NO CAP CO",
            "facts": {
                "dei": {
                    "EntityCommonStockSharesOutstanding": {
                        "units": {
                            "shares": [
                                {"end": "2025-12-31", "val": 500, "filed": "2026-02-01"},
                                {"end": "2026-09-30", "val": 54_000_000, "filed": "2026-10-01"},
                                {"end": "2026-09-30", "val": -1, "filed": "2026-10-02"},
                            ]
                        }
                    }
                },
                "us-gaap": {
                    "CommonStockSharesOutstanding": {
                        "units": {"shares": [{"end": "2026-10-01", "val": 1, "filed": "2026-10-02"}]}
                    }
                },
            },
        }
        parsed = shares_from_companyfacts(payload)
        self.assertEqual(parsed["value"], 54_000_000)
        self.assertEqual(parsed["figure_type"], "dei:EntityCommonStockSharesOutstanding")
        self.assertNotIn("price", parsed)

        class Fake:
            def __init__(self):
                self.urls = []

            def json(self, url):
                self.urls.append(url)
                return payload

        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            fake = Fake()
            try:
                os.environ["SEC_CONTACT_EMAIL"] = "research@example.com"
                real_socket = socket.socket
                real_open = urllib.request.urlopen
                socket.socket = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("socket opened"))
                urllib.request.urlopen = lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("socket opened"))
                try:
                    cached = cache_shares_outstanding(
                        conn,
                        [{"ticker": "NCP", "cik": "1018724", "company": "No Cap Co"}],
                        client=None,
                        fetched_at=SWEEP_AT,
                    )
                finally:
                    socket.socket = real_socket
                    urllib.request.urlopen = real_open
                self.assertEqual(cached, [])
                rows = cache_shares_outstanding(
                    conn,
                    [{"ticker": "NCP", "cik": "1018724", "company": "No Cap Co"}],
                    client=fake,
                    fetched_at=SWEEP_AT,
                )
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["figure_type"], "dei:EntityCommonStockSharesOutstanding")
                self.assertEqual(rows[0]["value"], 54_000_000)
                self.assertNotIn("share_price", {row["figure_type"] for row in rows})
                self.assertIn("CIK0001018724", fake.urls[0])
                again = Fake()
                again.json = lambda url: (_ for _ in ()).throw(AssertionError("refetched"))
                self.assertEqual(
                    cache_shares_outstanding(
                        conn,
                        [{"ticker": "NCP", "cik": "1018724"}],
                        client=again,
                        fetched_at=SWEEP_AT,
                    ),
                    [],
                )
                cap, reason = resolve_market_cap(conn, "NCP")
                self.assertIsNone(cap)
                self.assertEqual(reason, "market cap unverified")
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T14:30:52Z",
                    [
                        _figure(
                            "NCP",
                            "No Cap Co",
                            "quarter",
                            "2026-07-01/2026-09-30",
                            5_000_000_000,
                            value_text="5000000000",
                        ),
                        _figure(
                            "NCP",
                            "No Cap Co",
                            "quarter",
                            "2025-07-01/2025-09-30",
                            1_000_000_000,
                        ),
                    ],
                )
            finally:
                conn.close()
            dry = Fake()
            run_pipeline(path, provider=StubProvider(), dry_run=True, token_budget=80000, shares_client=dry)
            self.assertEqual(dry.urls, [])
            live = Fake()
            run_pipeline(path, provider=StubProvider(), token_budget=80000, shares_client=live)
            self.assertEqual(live.urls, [])
            conn = connect(path)
            try:
                stored = conn.execute(
                    """
                    SELECT figure_type, value FROM figures
                    WHERE ticker = 'NCP' AND figure_type = 'dei:EntityCommonStockSharesOutstanding'
                    """
                ).fetchall()
                prices = conn.execute(
                    "SELECT COUNT(*) FROM figures WHERE ticker = 'NCP' AND figure_type = 'share_price'"
                ).fetchone()[0]
                thesis = conn.execute(
                    """
                    SELECT t.thesis_text, r.assumptions_that_fail, k.surviving_issues
                    FROM leads AS l
                    JOIN theses AS t ON t.lead_id = l.lead_id
                    JOIN praetorian_reviews AS r ON r.thesis_id = t.thesis_id
                    JOIN rankings AS k ON k.lead_id = l.lead_id
                    WHERE l.ticker = 'NCP'
                    """
                ).fetchone()
            finally:
                conn.close()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["value"], 54_000_000)
        self.assertEqual(prices, 0)
        self.assertIn("market cap unverified", thesis["thesis_text"])
        self.assertIn("market cap unverified", thesis["assumptions_that_fail"])
        self.assertGreaterEqual(thesis["surviving_issues"], 1)


class _FailMarketCap(StubProvider):
    def complete(self, prompt, *, max_tokens):
        result = super().complete(prompt, max_tokens=max_tokens)
        if "TASK: centurion" not in prompt:
            return result
        data = json.loads(result.text)
        for grade in data.get("grades") or []:
            if grade.get("name") == "min_market_cap":
                grade["passed"] = False
                grade["evidence"] = "market cap is not in the stored figure"
        text = json.dumps(data, sort_keys=True)
        return Completion(text, result.prompt_tokens, max(1, (len(text) + 3) // 4))


def _history(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    from shared.store import ensure_history

    ensure_history(conn)
    return conn


def _figure(ticker, company, instrument, period, value, announced_on="2026-10-01", value_text=None):
    return {
        "ticker": ticker,
        "company": company,
        "figure_type": "PaymentsToAcquireProductiveAssets",
        "instrument": instrument,
        "period": period,
        "value": value,
        "value_text": value_text if value_text is not None else str(value),
        "currency": "USD",
        "seen_at": "2026-10-12",
        "announced_on": announced_on,
        "source_url": f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-{period.replace('/', '-')}.htm",
        "source_type": "filing",
        "verified": 1,
    }


def _caps(*tickers):
    rows = []
    for ticker in tickers:
        rows.append(_shares(ticker))
        rows.append(_price(ticker))
    return rows


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


def _price(
    ticker,
    value=10,
    currency="USD",
    source_type="filing",
    source_url=None,
    instrument="close",
    announced_on="2026-10-01",
):
    return {
        "ticker": ticker,
        "company": ticker,
        "figure_type": "share_price",
        "instrument": instrument,
        "period": "2026-10-01",
        "value": value,
        "value_text": str(value),
        "currency": currency,
        "seen_at": "2026-10-12",
        "announced_on": announced_on,
        "source_url": source_url or f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-price.htm",
        "source_type": source_type,
        "verified": 1,
    }


def _debt(ticker, instrument, value):
    row = _figure(ticker, ticker, instrument, "2026-10-01", value)
    row["figure_type"] = "bond_amount" if instrument == "bond" else "facility_amount"
    row["currency"] = "USD"
    return row


def _orders(ticker, instrument, period, value, figure_type="backlog"):
    row = _figure(ticker, ticker, instrument, period, value)
    row["figure_type"] = figure_type
    return row


def _view(row):
    return type("Row", (), dict(row))()


def _order(ticker, instrument, value_text, figure_type="supplier_order"):
    return {
        "ticker": ticker,
        "company": "Globex Power",
        "figure_type": figure_type,
        "instrument": instrument,
        "period": "2026-10-01",
        "value": 40_600_000_000,
        "value_text": value_text,
        "currency": "USD",
        "seen_at": "2026-10-12",
        "announced_on": "2026-10-01",
        "source_url": f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-{instrument.split('|')[0]}.htm",
        "source_type": "filing",
        "verified": 1,
    }


class _Row:
    def __init__(self, instrument, value_text):
        self.instrument = instrument
        self.value_text = value_text
        self.period = "2026-10-01"


def _loaded(instrument, value_text):
    return _Row(instrument, value_text)


if __name__ == "__main__":
    unittest.main()
