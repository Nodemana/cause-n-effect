"""Screen checks. Dates are fixed so the window does not depend on today."""

import unittest
from datetime import date

from scouts.accumulation.filters import ScreenConfig, has_activist_purpose, screen
from scouts.accumulation.model import Position

AS_OF = date(2026, 10, 1)
SECTORS = {"NWND": "materials", "BRMB": "health", "CPRL": "materials"}


def config(**kwargs):
    base = dict(as_of=AS_OF, recent_days=30, min_stake_pct=5.0, repeat_filer_min_targets=2, sector_min_positions=2)
    base.update(kwargs)
    return ScreenConfig(**base)


def pos(**kwargs):
    base = dict(
        source="sec",
        form_type="SCHEDULE 13G",
        form_family="13G",
        amendment=False,
        filer="Northbridge Partners LP",
        filer_id="0000999001",
        target_name="Northwind Resources Inc",
        target_id="0000999101",
        ticker="NWND",
        stake_pct=7.5,
        share_count=1250000,
        event_date="2026-09-15",
        filing_date="2026-09-16",
        purpose_text=None,
        document_url="https://www.sec.gov/Archives/northwind",
    )
    base.update(kwargs)
    return Position(**base)


class FilterTests(unittest.TestCase):
    def test_stake_in_window_is_a_candidate(self):
        report = screen([pos()], config(), SECTORS)
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["review"], [])
        matched = report["candidates"][0]["matched_filters"]
        self.assertIn("stake_above_threshold", matched)
        self.assertIn("recent_window", matched)
        self.assertEqual(report["candidates"][0]["sector"], "materials")
        self.assertEqual(report["candidates"][0]["target"]["ticker"], "NWND")

    def test_below_threshold_one_off_is_omitted(self):
        report = screen([pos(stake_pct=3.0)], config(), SECTORS)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["review"], [])

    def test_outside_window_is_omitted(self):
        report = screen([pos(filing_date="2026-01-02", event_date="2026-01-01")], config(), SECTORS)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["review"], [])

    def test_repeat_filer_across_targets(self):
        filings = [
            pos(stake_pct=3.0),
            pos(
                target_name="Bramble Medical Ltd",
                target_id="0000999102",
                ticker="BRMB",
                stake_pct=3.0,
                document_url="https://www.sec.gov/Archives/bramble",
            ),
        ]
        report = screen(filings, config(), SECTORS)
        self.assertEqual(len(report["candidates"]), 2)
        for row in report["candidates"]:
            self.assertIn("repeat_filer", row["matched_filters"])
            self.assertNotIn("stake_above_threshold", row["matched_filters"])
            self.assertNotIn("sector_concentration", row["matched_filters"])

    def test_sector_concentration(self):
        filings = [
            pos(stake_pct=3.0),
            pos(
                target_name="Copperline Holdings Inc",
                target_id="0000999103",
                ticker="CPRL",
                stake_pct=3.0,
                document_url="https://www.sec.gov/Archives/copper",
            ),
        ]
        report = screen(filings, config(), SECTORS)
        self.assertEqual(len(report["candidates"]), 2)
        for row in report["candidates"]:
            self.assertIn("sector_concentration", row["matched_filters"])
            self.assertEqual(row["sector"], "materials")

    def test_same_target_amendment_is_not_a_repeat(self):
        filings = [
            pos(stake_pct=3.0, document_url="https://www.sec.gov/Archives/a"),
            pos(stake_pct=3.2, amendment=True, form_type="SCHEDULE 13G/A", document_url="https://www.sec.gov/Archives/b"),
        ]
        report = screen(filings, config(), SECTORS)
        self.assertEqual(report["candidates"], [])

    def test_unknown_sector_withholds_candidate(self):
        report = screen([pos()], config(), {})
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["review"][0]["flags"], ["unknown_sector"])
        self.assertIn("stake_above_threshold", report["review"][0]["position"]["matched_filters"])

    def test_unknown_sector_can_be_turned_off(self):
        report = screen([pos()], config(require_known_sector=False), {})
        self.assertEqual(len(report["candidates"]), 1)
        self.assertIsNone(report["candidates"][0]["sector"])

    def test_activist_language_goes_to_review(self):
        filing = pos(
            form_type="SCHEDULE 13D",
            form_family="13D",
            purpose_text="The reporting person intends to seek board representation and may propose a merger.",
        )
        report = screen([filing], config(), SECTORS)
        self.assertEqual(report["candidates"], [])
        self.assertIn("activist_purpose", report["review"][0]["flags"])

    def test_denial_boilerplate_is_not_activist(self):
        text = (
            "The shares were acquired for investment purposes. "
            "The reporting person does not have any plans or proposals to seek control of the issuer."
        )
        self.assertFalse(has_activist_purpose(text))
        filing = pos(form_type="SCHEDULE 13D", form_family="13D", purpose_text=text, stake_pct=8.0)
        report = screen([filing], config(), SECTORS)
        self.assertEqual(len(report["candidates"]), 1)
        self.assertEqual(report["review"], [])

    def test_13g_to_13d_switch_is_review(self):
        earlier = pos(
            form_family="13G",
            form_type="SCHEDULE 13G",
            filing_date="2026-06-01",
            event_date="2026-05-28",
            document_url="https://www.sec.gov/Archives/g",
        )
        later = pos(
            form_family="13D",
            form_type="SCHEDULE 13D",
            filing_date="2026-09-20",
            event_date="2026-09-18",
            purpose_text="The shares were acquired for investment purposes.",
            document_url="https://www.sec.gov/Archives/d",
        )
        report = screen([earlier, later], config(), SECTORS)
        self.assertEqual(report["candidates"], [])
        urls = {row["position"]["document_url"] for row in report["review"]}
        self.assertIn("https://www.sec.gov/Archives/d", urls)
        switch = [row for row in report["review"] if row["position"]["document_url"].endswith("/d")][0]
        self.assertIn("switch_13g_to_13d", switch["flags"])

    def test_unparseable_stake_is_review(self):
        filing = pos(stake_pct=None, flags=["unparseable_stake"], notes=["cover page percent missing"])
        report = screen([filing], config(), SECTORS)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["review"][0]["flags"], ["unparseable_stake"])

    def test_conflicting_data_is_review(self):
        filing = pos(flags=["conflicting_data"], notes=["Item 5 disagrees"])
        report = screen([filing], config(), SECTORS)
        self.assertEqual(report["candidates"], [])
        self.assertIn("conflicting_data", report["review"][0]["flags"])

    def test_ceasing_notice_is_not_a_candidate(self):
        filing = pos(
            source="asx",
            form_type="605",
            form_family="605",
            ceased=True,
            stake_pct=None,
            target_id="444555666",
            ticker="CPRL",
            document_url="https://www.asx.com.au/cease",
        )
        report = screen([filing], config(), SECTORS)
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["review"], [])

    def test_json_shape(self):
        report = screen([pos()], config(), SECTORS)
        self.assertEqual(report["scout"], "accumulation")
        self.assertEqual(report["schema"], 1)
        self.assertEqual(report["as_of"], "2026-10-01")
        self.assertEqual(set(report), {"scout", "schema", "as_of", "config", "candidates", "review"})
        row = report["candidates"][0]
        self.assertEqual(
            set(row),
            {
                "source", "form_type", "form_family", "amendment", "filer", "filer_id",
                "co_filers", "target", "sector", "stake_pct", "share_count", "event_date",
                "filing_date", "purpose_text", "ceased", "document_url", "accession",
                "matched_filters",
            },
        )


if __name__ == "__main__":
    unittest.main()
