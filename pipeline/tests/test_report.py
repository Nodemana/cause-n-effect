"""Selected and candidate reports keep tickers, sections, and relevant macro series."""

import unittest
from types import SimpleNamespace

from pipeline.report import render_candidates, render_selected


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


def _lead(summary="aircraft backlog", macro_context=_DUMP, markdown=_MARKDOWN):
    thesis = SimpleNamespace(
        markdown=markdown,
        recommendation="Overweight the supplier on the disclosed backlog.",
        roi="18%",
        risks_text="Budget delay\nProgram cut",
        assumptions_text="- Filings stay the source.\n- No new award is assumed.",
        timeframe_days=180,
        price_run="new",
        macro_context=macro_context,
        suppliers=[],
        customers=[],
        buyers=[],
        review={"flaws": []},
        source_url="https://www.sec.gov/Archives/edgar/data/example",
    )
    return SimpleNamespace(
        ticker="LMT, LMT",
        company="Lockheed Martin",
        lead_type="orders_backlog",
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
    def test_tickers_sections_and_relevant_macro(self):
        text = render_selected(_lead(), macro=_DUMP)
        ticker_line = next(line for line in text.splitlines() if line.startswith("**Tickers:**"))
        self.assertEqual(ticker_line, "**Tickers:** LMT")
        self.assertNotIn("OVERWEIGHT", ticker_line)
        self.assertNotIn("LMT, LMT", text)
        thesis = text.split("### Recommendation", 1)[0]
        self.assertIn("Lockheed's order book is growing.", thesis)
        self.assertIn("A second paragraph stays with the thesis.", thesis)
        self.assertNotIn("## Recommendation", thesis)
        self.assertNotIn("Expected return:", thesis)
        self.assertNotIn("Risks:", thesis)
        self.assertNotIn("Timeframe:", thesis)
        self.assertIn("### Recommendation\n\nOverweight the supplier on the disclosed backlog.", text)
        self.assertIn("### Expected return\n\n18%", text)
        self.assertIn("### Risks\n\n- Budget delay\n- Program cut", text)
        self.assertIn("### Timeframe\n\n180 days", text)
        self.assertIn("### Assumptions\n\n- Filings stay the source.\n- No new award is assumed.", text)
        macro = text.split("### Macro context", 1)[1].split("### Price run", 1)[0]
        self.assertNotIn("unavailable:", macro)
        self.assertNotIn("DFF", macro)
        self.assertNotIn("DGS2", macro)
        self.assertNotIn("DGS10", macro)
        self.assertNotIn("RNGWHHD", macro)
        self.assertIn("\nnone\n", macro)

        oil = render_selected(
            _lead(summary="A refiner and a tanker move with the crude price."),
            macro=_DUMP,
        )
        oil_macro = oil.split("### Macro context", 1)[1].split("### Price run", 1)[0]
        self.assertIn("BRENT Brent spot:", oil_macro)
        self.assertIn("WTI WTI spot:", oil_macro)
        self.assertNotIn("DGS10", oil_macro)
        self.assertNotIn("RNGWHHD", oil_macro)
        self.assertNotIn("unavailable:", oil_macro)
        self.assertNotIn("DFF", oil_macro)

        summary = render_selected(_lead(macro_context="Policy rates unchanged."), macro=None)
        self.assertIn("### Macro context\n\nPolicy rates unchanged.\n", summary)

        candidates = render_candidates("run", [_lead()], macro=_DUMP)
        candidate_tickers = next(line for line in candidates.splitlines() if line.startswith("**Tickers:**"))
        self.assertEqual(candidate_tickers, "**Tickers:** LMT")
        self.assertIn("### Risks\n\n- Budget delay\n- Program cut", candidates)
        self.assertNotIn("## Recommendation", candidates.split("### Recommendation", 1)[0])

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
        text = render_selected(lead, macro=None)
        thesis = text.split("### Recommendation", 1)[0]
        self.assertIn("Lockheed's order book is growing.", thesis)
        self.assertNotIn("Expected return:", thesis)
        self.assertNotIn("Risks:", thesis)
        self.assertNotIn("Timeframe:", thesis)
        self.assertIn("### Risks\n\n- Budget delay\n- Program cut", text)
        self.assertIn("### Timeframe\n\n180 days", text)
        self.assertIn("- Filings stay the source.", text)


if __name__ == "__main__":
    unittest.main()
