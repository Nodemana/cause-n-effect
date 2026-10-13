"""A sample thesis file: price table, bottom line, then the sections."""

import sqlite3
import unittest
from types import SimpleNamespace

from pipeline.report import render_candidates, render_selected, series_levels


_MARKDOWN = """## Thesis

Lockheed's order book is growing.

A second paragraph stays with the thesis.

Source: https://www.sec.gov/Archives/edgar/data/example

## Recommendation

Recommendation: Overweight the supplier on the disclosed backlog.
Expected return: 18%
Risks: Budget delay; Program cut
Timeframe: 180 days

## Assumptions

- Filings stay the source.
- No new award is assumed.
"""

_DUMP = (
    "Macro scout cache: DGS10 10-year: 4.2 percent on 2026-10-01 (FRED); "
    "BRENT Brent spot: 80 USD on 2026-10-01 (EIA); "
    "WTI WTI spot: 75 USD on 2026-10-01 (EIA); "
    "RNGWHHD Henry Hub: 3 USD on 2026-10-01 (EIA). "
    "unavailable: FRED (missing key): DFF, DGS2, WALCL"
)

_PRICES = {
    "LMT": {
        "price": 450,
        "currency": "USD",
        "as_of": "2026-10-12",
        "prior_year_close": 400,
        "close_1y": 375,
        "low_52w": 360,
        "high_52w": 480,
    }
}

_HEADINGS = (
    "### Bottom line",
    "### Cause → effect",
    "### Thesis",
    "### Evidence",
    "### Risks",
    "### Assumptions",
    "### Praetorian critique",
    "### Legate rationale",
    "### Macro context",
)


def _lead(summary="aircraft backlog", macro_context=_DUMP, markdown=_MARKDOWN, lead_type="orders_backlog"):
    thesis = SimpleNamespace(
        markdown=markdown,
        recommendation="Overweight the supplier on the disclosed backlog.",
        conviction="The backlog is already in the filing.",
        roi="18%",
        risks_text="Budget delay\nProgram cut",
        assumptions_text="- Filings stay the source.\n- No new award is assumed.",
        timeframe_days=180,
        price_run="new",
        macro_context=macro_context,
        suppliers=[{"name": "Northrop", "ticker": "NOC"}],
        customers=[],
        buyers=[],
        review={
            "flaws": [
                {"severity": "moderate", "flaw": "The award can slip.", "evidence": "The filing gives no date."},
                {"severity": "high", "flaw": "One customer is most of the book.", "evidence": "The 10-K names one buyer."},
            ]
        },
        source_url="https://www.sec.gov/Archives/edgar/data/example",
    )
    return SimpleNamespace(
        ticker="LMT, LMT",
        company="Lockheed Martin",
        lead_type=lead_type,
        source_scout="orders_backlog",
        rank=1,
        real_id=7,
        pick_order=1,
        selection_rationale="Backlog is disclosed.",
        thesis=thesis,
        members=[],
        summary=summary,
        source_url="https://www.sec.gov/Archives/edgar/data/example",
        related_findings=[],
        figure=None,
        counterparties=[],
    )


class ReportFormatTests(unittest.TestCase):
    def test_sample_report_puts_the_price_table_and_bottom_line_first(self):
        text = render_selected(_lead(), macro=_DUMP, prices=_PRICES, levels={})
        self.assertLess(text.find("| Ticker |"), text.find("### Bottom line"))
        self.assertNotIn("**Tickers:**", text)
        self.assertNotIn("LMT, LMT", text)
        self.assertNotIn("OVERWEIGHT", text.split("### Bottom line", 1)[0])
        self.assertIn("| LMT | 450.00 USD (2026-10-12) | +12.5% | +20.0% | 360.00–480.00 |", text)
        self.assertIn("| NOC | n/a | n/a | n/a | n/a |", text)
        self.assertEqual(text.count("| LMT |"), 1)
        positions = [text.find(heading) for heading in _HEADINGS]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(all(pos > 0 for pos in positions))
        bottom = text.split("### Bottom line", 1)[1].split("### Cause", 1)[0]
        self.assertIn("> Recommendation: Overweight the supplier on the disclosed backlog.", bottom)
        self.assertIn("> Expected return: 18%", bottom)
        self.assertIn("> Timeframe: 180 days", bottom)
        self.assertIn("> Conviction: The backlog is already in the filing.", bottom)
        self.assertIn("> Biggest risk: Budget delay", bottom)
        cause = text.split("### Cause → effect", 1)[1].split("### Thesis", 1)[0]
        self.assertIn("- Supplier (backlog) LMT", cause)
        self.assertNotIn("→", cause)
        thesis = text.split("### Thesis", 1)[1].split("### Evidence", 1)[0]
        self.assertIn("Lockheed's order book is growing.", thesis)
        self.assertIn("A second paragraph stays with the thesis.", thesis)
        self.assertNotIn("## Recommendation", thesis)
        self.assertNotIn("Expected return:", thesis)
        self.assertNotIn("Risks:", thesis)
        self.assertNotIn("Timeframe:", thesis)
        self.assertIn("### Risks\n\n- Budget delay\n- Program cut", text)
        self.assertIn("- Filings stay the source.", text)
        critique = text.split("### Praetorian critique", 1)[1].split("### Legate rationale", 1)[0]
        self.assertLess(critique.find("| high |"), critique.find("| moderate |"))
        self.assertIn("One customer is most of the book.", critique)
        self.assertIn("### Legate rationale\n\nBacklog is disclosed.", text)
        macro = text.split("### Macro context", 1)[1]
        self.assertNotIn("unavailable:", macro)
        self.assertNotIn("DFF", macro)
        self.assertNotIn("DGS10", macro)
        self.assertNotIn("RNGWHHD", macro)
        self.assertIn("\nnone\n", macro)

        oil = render_selected(
            _lead(summary="A refiner and a tanker move with the crude price."),
            macro=_DUMP,
            prices=_PRICES,
            levels={},
        )
        oil_macro = oil.split("### Macro context", 1)[1]
        self.assertIn("BRENT Brent spot:", oil_macro)
        self.assertIn("WTI WTI spot:", oil_macro)
        self.assertNotIn("DGS10", oil_macro)
        self.assertNotIn("unavailable:", oil_macro)

        bare = render_selected(
            _lead(macro_context="Policy rates unchanged."),
            macro=None,
            prices={"LMT": {"price": 450, "currency": "USD", "as_of": "2026-10-12"}},
            levels={},
        )
        self.assertIn("| LMT | 450.00 USD (2026-10-12) | n/a | n/a | n/a |", bare)
        self.assertIn("### Macro context\n\nPolicy rates unchanged.\n", bare)

        candidates = render_candidates("run", [_lead()], macro=_DUMP, prices=_PRICES, levels={})
        self.assertIn("| LMT | 450.00 USD (2026-10-12) | +12.5% | +20.0% | 360.00–480.00 |", candidates)
        self.assertIn("### Bottom line", candidates)
        self.assertNotIn("**Tickers:**", candidates)
        self.assertNotIn("unavailable:", candidates)

    def test_a_macro_lead_shows_the_cause_series_from_the_cache(self):
        lead = _lead(summary="Refiners and tankers", lead_type="macro")
        lead.ticker = "BRENT"
        lead.company = "Brent"
        lead.related_findings = [
            {"ticker": "VLO", "company": "Gulf Refining", "lead_type": "capex", "source_url": "https://www.sec.gov/Archives/edgar/data/example"},
        ]
        lead.thesis.suppliers = []
        levels = {
            "BRENT": {
                "name": "Brent",
                "unit": "USD",
                "level": 80,
                "as_of": "2026-10-01",
                "prior_year_close": 100,
                "close_1y": 50,
            }
        }
        text = render_selected(lead, macro=None, prices={}, levels=levels)
        self.assertIn("| BRENT | 80.00 USD (2026-10-01) | -20.0% | +60.0% | n/a |", text)
        self.assertIn("| VLO | n/a | n/a | n/a | n/a |", text)
        self.assertLess(text.find("| BRENT |"), text.find("| VLO |"))

    def test_series_levels_use_stored_observations_only(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            CREATE TABLE macro_series (
                series_id TEXT PRIMARY KEY,
                name TEXT,
                unit TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE macro_observations (
                series_id TEXT,
                obs_date TEXT,
                value REAL
            )
            """
        )
        conn.executemany(
            "INSERT INTO macro_series (series_id, name, unit) VALUES (?, ?, ?)",
            [("BRENT", "Brent", "USD"), ("DGS10", "10-year", "percent")],
        )
        conn.executemany(
            "INSERT INTO macro_observations (series_id, obs_date, value) VALUES (?, ?, ?)",
            [
                ("BRENT", "2025-10-02", 50),
                ("BRENT", "2025-12-31", 100),
                ("BRENT", "2026-06-01", 90),
                ("BRENT", "2026-10-01", 80),
                ("DGS10", "2026-10-01", 4.2),
            ],
        )
        found = series_levels(conn, ["BRENT", "DGS10", "WTI"], "2026-10-13")
        self.assertEqual(found["BRENT"]["level"], 80)
        self.assertEqual(found["BRENT"]["prior_year_close"], 100)
        self.assertEqual(found["BRENT"]["close_1y"], 50)
        self.assertEqual(found["DGS10"]["level"], 4.2)
        self.assertIsNone(found["DGS10"]["prior_year_close"])
        self.assertIsNone(found["DGS10"]["close_1y"])
        self.assertNotIn("WTI", found)

    def test_flattened_thesis_markdown_still_splits_sections(self):
        flat = (
            "## Thesis Lockheed's order book is growing. "
            "## Recommendation Recommendation: Overweight the supplier on the disclosed backlog. "
            "Expected return: 18% Risks: Budget delay; Program cut Timeframe: 180 days "
            "## Assumptions - Filings stay the source."
        )
        lead = _lead(markdown=flat)
        lead.thesis.risks_text = ""
        lead.thesis.assumptions_text = ""
        lead.thesis.recommendation = ""
        lead.thesis.roi = ""
        lead.thesis.timeframe_days = None
        lead.thesis.conviction = ""
        text = render_selected(lead, macro=None, prices={}, levels={})
        thesis = text.split("### Thesis", 1)[1].split("### Evidence", 1)[0]
        self.assertIn("Lockheed's order book is growing.", thesis)
        self.assertNotIn("Expected return:", thesis)
        self.assertNotIn("Risks:", thesis)
        self.assertNotIn("Timeframe:", thesis)
        self.assertIn("> Recommendation: Overweight the supplier on the disclosed backlog.", text)
        self.assertIn("> Expected return: 18%", text)
        self.assertIn("> Timeframe: 180 days", text)
        self.assertIn("> Conviction: n/a", text)
        self.assertIn("### Risks\n\n- Budget delay\n- Program cut", text)
        self.assertIn("- Filings stay the source.", text)


if __name__ == "__main__":
    unittest.main()
