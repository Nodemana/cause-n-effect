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
        self.assertLess(text.find("**Signal date:**"), text.find("| Ticker |"))
        self.assertLess(text.find("| Ticker |"), text.find("### Bottom line"))
        self.assertIn("**After the signal:** n/a", text.split("| Ticker |", 1)[0])
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
        self.assertIn("> - **Recommendation:** Overweight the supplier on the disclosed backlog.", bottom)
        self.assertIn("> - **Expected return:** 18%", bottom)
        self.assertIn("> - **Timeframe:** 180 days", bottom)
        self.assertIn("> - **Conviction:** The backlog is already in the filing.", bottom)
        self.assertIn("> - **Biggest risk:** Budget delay", bottom)
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

    def test_the_signal_block_states_the_filing_date_and_the_move(self):
        fresh = _lead()
        fresh.signal_date = "2026-10-01"
        fresh.days_since_signal = 11
        text = render_selected(fresh, macro=None, prices=_PRICES, levels={})
        head = text.split("| Ticker |", 1)[0]
        self.assertIn("**Signal date:** 2026-10-01", head)
        self.assertIn("**Age:** 11 days", head)
        self.assertIn("**After the signal:** n/a", head)
        self.assertNotIn("%", head)

        faded = _lead()
        faded.signal_date = "2026-07-23"
        faded.days_since_signal = 81
        faded.close_before_signal = 100
        faded.max_runup_pct = 20
        faded.current_vs_pre_pct = 0
        faded.popped_and_faded = True
        faded.move_passed = True
        moved = render_selected(faded, macro=None, prices=_PRICES, levels={})
        block = moved.split("| Ticker |", 1)[0]
        self.assertIn("**Signal date:** 2026-07-23", block)
        self.assertIn("**Age:** 81 days", block)
        self.assertIn("close before the filing was 100.00", block)
        self.assertIn("the peak was +20.0%", block)
        self.assertIn("the price is now +0.0% versus that close", block)
        self.assertIn("The pop has faded", block)

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
        self.assertIn("> - **Recommendation:** Overweight the supplier on the disclosed backlog.", text)
        self.assertIn("> - **Expected return:** 18%", text)
        self.assertIn("> - **Timeframe:** 180 days", text)
        self.assertIn("> - **Conviction:** n/a", text)
        self.assertIn("### Risks\n\n- Budget delay\n- Program cut", text)
        self.assertIn("- Filings stay the source.", text)


class PhoneLayoutTests(unittest.TestCase):
    def test_multiline_risks_keep_semicolons_inside_one_item(self):
        from pipeline.report import _bullet_items

        items = _bullet_items("Spread mean-reverts; arbitrage closes it.\nShares near highs.")
        self.assertEqual(items, ["Spread mean-reverts; arbitrage closes it.", "Shares near highs."])
        self.assertEqual(_bullet_items("a; b"), ["a", "b"])

    def test_long_thesis_breaks_at_section_labels(self):
        from types import SimpleNamespace
        from pipeline.report import _thesis_prose

        thesis = SimpleNamespace(markdown="## Thesis\n\nCause: x. Effect: y. Priced-in check: z. Conclusion: w.")
        self.assertEqual(_thesis_prose(thesis).split("\n\n"), ["Cause: x.", "Effect: y.", "Priced-in check: z.", "Conclusion: w."])

    def test_macro_named_tickers_need_a_stored_quote(self):
        from types import SimpleNamespace
        from pipeline.report import _price_table

        lead = SimpleNamespace(
            lead_type="macro", ticker="BRENT", members=[], related_findings=[],
            thesis=SimpleNamespace(recommendation="Winners: VLO (US refiners). Losers: UAL.", suppliers=[], customers=[], buyers=[]),
        )
        prices = {"VLO": {"price": 436.0, "currency": "USD", "as_of": "2026-10-12", "prior_year_close": 162.79, "close_1y": 161.41, "low_52w": 155.29, "high_52w": 449.77}}
        table = _price_table(lead, prices, {})
        self.assertIn("| VLO | 436.00 USD (2026-10-12) | +167.8% | +170.1% | 155.29–449.77 |", table)
        self.assertIn("| WTI |", table)
        self.assertNotIn("| UAL |", table)
        self.assertNotIn("| US |", table)

    def test_macro_cause_is_plain_english(self):
        from pipeline.report import _cause_bullets

        lead = _lead(summary="", lead_type="macro")
        lead.ticker = "BRENT"
        lead.company = "Brent"
        lead.summary = (
            "macro_level BRENT 125.44 on 2026-10-06 "
            "source https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm; "
            "1m_pct 22.6917; 3m_pct 74.7562; z_score 2.7281; "
            "reasons 1m_pct 3m_pct z_score 2026-10-06"
        )
        lead.related_findings = [
            {
                "ticker": "VLO",
                "company": "Gulf Refining",
                "lead_type": "capex",
                "source_url": "https://www.sec.gov/Archives/edgar/data/103500/vlo.htm",
            }
        ]
        cause = _cause_bullets(lead)
        self.assertIn("- Brent was 125.44 on 2026-10-06 ([source](https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm)).", cause)
        self.assertIn("- Up 22.7% over one month.", cause)
        self.assertIn("- Up 74.8% over three months.", cause)
        self.assertIn("- 2.7 standard deviations above its prior-year average.", cause)
        self.assertIn("the one-month move, the three-month move, and an unusual move versus the prior year", cause)
        self.assertNotIn("1m_pct", cause)
        self.assertNotIn("z_score", cause)
        self.assertNotIn("macro_level", cause)
        self.assertIn("- Gulf Refining VLO capex", cause)

        spread = _lead(summary="", lead_type="macro")
        spread.ticker = "BRENT-WTI"
        spread.company = "Brent minus WTI"
        spread.summary = (
            "macro_spread BRENT 125.44 on 2026-10-06 source https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm; "
            "WTI 96.24 on 2026-10-06 source https://www.eia.gov/dnav/pet/pet_pri_spt_s1_d.htm; "
            "spread 29.2000; range_low 1.5000; range_high 8.2500; "
            "reasons spread_outside_1y_range 2026-10-06"
        )
        spread.related_findings = []
        text = _cause_bullets(spread)
        self.assertIn("- Brent was 125.44 on 2026-10-06", text)
        self.assertIn("- WTI was 96.24 on 2026-10-06", text)
        self.assertIn("- Brent minus WTI was 29.20, outside its prior-year range of 1.50 to 8.25.", text)
        self.assertNotIn("range_low", text)
        self.assertNotIn("macro_spread", text)


if __name__ == "__main__":
    unittest.main()
