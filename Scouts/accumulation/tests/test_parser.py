"""Parser checks against small fictional fixtures. No network."""

import json
import pathlib
import unittest

from Scouts.accumulation.model import Document
from Scouts.accumulation.parser import parse_document

FIX = pathlib.Path(__file__).resolve().parents[1] / "fixtures"


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


def tickers():
    raw = json.loads(load("company_tickers.json"))
    return {str(int(row["cik_str"])).zfill(10): row["ticker"] for row in raw.values()}


class ParserTests(unittest.TestCase):
    def test_schedule_13d_xml(self):
        doc = Document(
            source="sec",
            body=load("sec_13d.xml"),
            url="https://www.sec.gov/Archives/edgar/data/999001/000099900126000001/primary_doc.xml",
            form_type="SCHEDULE 13D",
            filing_date="2026-09-16",
            accession="0000999001-26-000001",
        )
        position = parse_document(doc, tickers())[0]
        self.assertEqual(position.form_family, "13D")
        self.assertFalse(position.amendment)
        self.assertEqual(position.filer, "Northbridge Partners LP")
        self.assertEqual(position.filer_id, "0000999001")
        self.assertEqual(position.target_name, "Northwind Resources Inc")
        self.assertEqual(position.target_id, "0000999101")
        self.assertEqual(position.ticker, "NWND")
        self.assertEqual(position.stake_pct, 7.5)
        self.assertEqual(position.share_count, 1250000)
        self.assertEqual(position.event_date, "2026-09-15")
        self.assertEqual(position.filing_date, "2026-09-16")
        self.assertIn("investment purposes", position.purpose_text)
        self.assertEqual(position.flags, [])

    def test_schedule_13g_xml(self):
        doc = Document(
            source="sec",
            body=load("sec_13g.xml"),
            url="https://www.sec.gov/Archives/edgar/data/999002/000099900226000002/primary_doc.xml",
            form_type="SCHEDULE 13G",
            filing_date="2026-09-03",
            accession="0000999002-26-000002",
        )
        position = parse_document(doc, tickers())[0]
        self.assertEqual(position.form_family, "13G")
        self.assertFalse(position.amendment)
        self.assertEqual(position.filer, "Harbour Lane Capital")
        self.assertEqual(position.target_name, "Bramble Medical Ltd")
        self.assertEqual(position.target_id, "0000999102")
        self.assertEqual(position.ticker, "BRMB")
        self.assertEqual(position.stake_pct, 6.2)
        self.assertEqual(position.share_count, 900000)
        self.assertEqual(position.event_date, "2026-09-02")
        self.assertIsNone(position.purpose_text)
        self.assertFalse(position.ceased)
        self.assertEqual(position.flags, [])

    def test_legacy_sc_13d_amendment_text(self):
        doc = Document(
            source="sec",
            body=load("sec_13d_legacy.txt"),
            url="https://www.sec.gov/Archives/edgar/data/999003/legacy.txt",
            form_type="SC 13D/A",
            filing_date="2026-09-21",
        )
        position = parse_document(doc, {})[0]
        self.assertEqual(position.form_type, "SC 13D/A")
        self.assertEqual(position.form_family, "13D")
        self.assertTrue(position.amendment)
        self.assertEqual(position.filer, "Cinder & Co")
        self.assertEqual(position.target_name, "Copperline Holdings Inc")
        self.assertEqual(position.stake_pct, 6.25)
        self.assertEqual(position.share_count, 800000)
        self.assertEqual(position.event_date, "2026-09-20")
        self.assertIn("board representation", position.purpose_text)
        self.assertIn("merger", position.purpose_text)

    def test_item5_disagreement_is_conflicting(self):
        body = load("sec_13d.xml").replace("7.50% of the class", "12% of the class")
        doc = Document(source="sec", body=body, url="https://www.sec.gov/x", form_type="SCHEDULE 13D", filing_date="2026-09-16")
        position = parse_document(doc, tickers())[0]
        self.assertIn("conflicting_data", position.flags)
        self.assertEqual(position.stake_pct, 7.5)

    def test_reporting_person_spread_is_conflicting(self):
        extra = """
        <reportingPersonInfo>
          <reportingPersonName>Other Vehicle LP</reportingPersonName>
          <percentOfClass>12.0</percentOfClass>
          <aggregateAmountOwned>2000000</aggregateAmountOwned>
        </reportingPersonInfo>
        """
        body = load("sec_13d.xml").replace("</reportingPersons>", extra + "</reportingPersons>")
        doc = Document(source="sec", body=body, url="https://www.sec.gov/x", form_type="SCHEDULE 13D", filing_date="2026-09-16")
        position = parse_document(doc, tickers())[0]
        self.assertIn("conflicting_data", position.flags)
        self.assertEqual(position.filer, "Northbridge Partners LP")
        self.assertIn("Other Vehicle LP", position.co_filers)

    def test_matching_joint_filers_stay_one_position(self):
        extra = """
        <reportingPersonInfo>
          <reportingPersonName>Northbridge GP LLC</reportingPersonName>
          <percentOfClass>7.50</percentOfClass>
          <aggregateAmountOwned>1250000</aggregateAmountOwned>
        </reportingPersonInfo>
        """
        body = load("sec_13d.xml").replace("</reportingPersons>", extra + "</reportingPersons>")
        doc = Document(source="sec", body=body, url="https://www.sec.gov/x", form_type="SCHEDULE 13D", filing_date="2026-09-16")
        positions = parse_document(doc, tickers())
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].flags, [])
        self.assertEqual(positions[0].co_filers, ["Northbridge GP LLC"])

    def test_form_family_mismatch(self):
        doc = Document(source="sec", body=load("sec_13d.xml"), url="https://www.sec.gov/x", form_type="SC 13G", filing_date="2026-09-16")
        position = parse_document(doc, tickers())[0]
        self.assertEqual(position.form_family, "13D")
        self.assertIn("conflicting_data", position.flags)

    def test_13g_exit_flag(self):
        body = load("sec_13g.xml").replace("<classPercent>6.20</classPercent>", "<classPercent>4.00</classPercent>")
        body = body.replace("<classOwnership5PercentOrLess>N</classOwnership5PercentOrLess>", "<classOwnership5PercentOrLess>Y</classOwnership5PercentOrLess>")
        # item4 still says 6.20; the cover person block was the first classPercent.
        doc = Document(source="sec", body=body, url="https://www.sec.gov/x", form_type="SCHEDULE 13G", filing_date="2026-09-03")
        position = parse_document(doc, tickers())[0]
        self.assertTrue(position.ceased)
        self.assertNotIn("unparseable_stake", position.flags)

    def test_pdf_is_unparseable(self):
        doc = Document(source="sec", body="%PDF-1.7 not text", url="https://www.sec.gov/x", form_type="SC 13D", filing_date="2026-09-16")
        position = parse_document(doc, {})[0]
        self.assertIn("unparseable_stake", position.flags)
        self.assertIsNone(position.stake_pct)

    def test_asx_603_604_605(self):
        initial = parse_document(Document(
            source="asx", body=load("asx_603.txt"), url="https://www.asx.com.au/a",
            form_type="603", filing_date="2026-09-04", ticker="nwnd",
        ))[0]
        self.assertEqual(initial.form_family, "603")
        self.assertEqual(initial.filer, "Harbour Lane Capital")
        self.assertEqual(initial.target_name, "Northwind Resources Limited")
        self.assertEqual(initial.target_id, "123456789")
        self.assertEqual(initial.ticker, "NWND")
        self.assertEqual(initial.stake_pct, 8.4)
        self.assertEqual(initial.share_count, 12500000)
        self.assertEqual(initial.event_date, "2026-09-02")
        self.assertFalse(initial.ceased)
        self.assertEqual(initial.flags, [])

        change = parse_document(Document(
            source="asx", body=load("asx_604.txt"), url="https://www.asx.com.au/b",
            form_type="604", filing_date="2026-09-21", ticker="BRMB",
        ))[0]
        self.assertEqual(change.stake_pct, 11.25)
        self.assertEqual(change.share_count, 9000000)
        self.assertEqual(change.event_date, "2026-09-20")
        self.assertTrue(change.amendment)
        self.assertTrue(any("5.1" in note for note in change.notes))

        ceased = parse_document(Document(
            source="asx", body=load("asx_605.txt"), url="https://www.asx.com.au/c",
            form_type="605", filing_date="2026-09-29", ticker="CPRL",
        ))[0]
        self.assertTrue(ceased.ceased)
        self.assertEqual(ceased.filer, "Cinder & Co")
        self.assertEqual(ceased.event_date, "2026-09-28")
        self.assertIsNone(ceased.stake_pct)
        self.assertNotIn("unparseable_stake", ceased.flags)

    def test_asx_table_layout_603_and_604(self):
        initial = parse_document(Document(
            source="asx", body=load("asx_603_layout.txt"), url="https://www.asx.com.au/layout603",
            form_type="603", filing_date="2026-09-04", ticker="NWND",
        ))[0]
        self.assertEqual(initial.flags, [])
        self.assertEqual(initial.filer, "Harbour Lane Capital")
        self.assertEqual(initial.target_name, "Northwind Resources Limited")
        self.assertEqual(initial.stake_pct, 8.4)
        self.assertEqual(initial.share_count, 12500000)
        self.assertEqual(initial.event_date, "2026-09-02")

        change = parse_document(Document(
            source="asx", body=load("asx_604_layout.txt"), url="https://www.asx.com.au/layout604",
            form_type="604", filing_date="2026-09-21", ticker="BRMB",
        ))[0]
        self.assertEqual(change.flags, [])
        self.assertEqual(change.filer, "Northbridge Partners LP")
        self.assertEqual(change.target_name, "Bramble Medical Limited")
        self.assertEqual(change.stake_pct, 11.25)
        self.assertEqual(change.share_count, 9000000)
        self.assertEqual(change.event_date, "2026-09-20")
        self.assertTrue(any("5.1" in note for note in change.notes))

        from Scouts.accumulation.scout_pipeline import figure_from_position

        figure = figure_from_position(
            {
                "source": "asx",
                "form_type": "603",
                "filer": initial.filer,
                "target": {"name": initial.target_name, "ticker": initial.ticker},
                "stake_pct": initial.stake_pct,
                "share_count": initial.share_count,
                "filing_date": initial.filing_date,
                "document_url": "https://asx.api.markitdigital.com/asx-research/1.0/file/notice603",
                "accession": "notice603",
            },
            "2026-09-04",
            [],
        )
        self.assertEqual(figure["verified"], 1)
        self.assertEqual(figure["currency"], "pct")
        self.assertEqual(figure["ticker"], "NWND.AX")
        self.assertEqual(figure["value"], 8.4)
        self.assertIn("12500000", figure["value_text"])
        self.assertIn("notice603", figure["source_url"])
        self.assertIn("notice603", figure["period"])

    def test_asx_604_stacked_labels_keep_the_holder_and_a_percent(self):
        change = parse_document(Document(
            source="asx", body=load("asx_604_bhp.txt"), url="https://www.asx.com.au/bhp604",
            form_type="604", filing_date="2026-10-09", ticker="BHP",
        ))[0]
        self.assertEqual(change.filer, "BlackRock, Inc.")
        self.assertNotEqual(change.filer, "Address")
        self.assertEqual(change.target_name, "BHP Group Limited")
        self.assertEqual(change.stake_pct, 6.1)
        self.assertEqual(change.share_count, 110000000)
        self.assertEqual(change.event_date, "2026-10-08")
        self.assertEqual(change.flags, [])

        from Scouts.accumulation.scout_pipeline import figure_from_position

        figure = figure_from_position(
            {
                "source": "asx",
                "form_type": "604",
                "filer": change.filer,
                "target": {"name": change.target_name, "ticker": "BHP"},
                "stake_pct": change.stake_pct,
                "share_count": change.share_count,
                "filing_date": change.filing_date,
                "document_url": "https://www.asx.com.au/bhp604",
                "accession": "bhp604",
            },
            "2026-10-09",
            [],
        )
        self.assertEqual(figure["ticker"], "BHP.AX")
        self.assertEqual(figure["value"], 6.1)
        self.assertEqual(figure["currency"], "pct")
        self.assertIn("BlackRock, Inc.", figure["period"])

    def test_asx_604_rejects_a_street_and_keeps_the_holder_name(self):
        from Scouts.accumulation.parser import _address_like

        self.assertTrue(_address_like("5800 S Corporate Place"))
        self.assertTrue(_address_like("Sioux Falls, SD 57108"))
        self.assertFalse(_address_like("Harbour Lane Capital"))
        self.assertFalse(_address_like("Citigroup Global Markets Australia Pty Limited"))

        change = parse_document(Document(
            source="asx",
            body=load("asx_604_bhp_citigroup.txt"),
            url="https://www.asx.com.au/asx/statistics/announcements/2924-03145697-3A703571",
            form_type="604",
            filing_date="2026-03-04",
            ticker="BHP",
            accession="2924-03145697-3A703571",
        ))[0]
        self.assertEqual(change.filer, "Citigroup Global Markets Australia Pty Limited")
        self.assertNotEqual(change.filer, "5800 S Corporate Place")
        self.assertNotIn("5800", change.filer)
        self.assertNotIn("Corporate Place", change.filer)
        self.assertEqual(change.target_name, "BHP Group Limited")
        self.assertEqual(change.ticker, "BHP")

    def test_fixtures_contain_no_email(self):
        import re
        for path in FIX.iterdir():
            text = path.read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text), path.name)


if __name__ == "__main__":
    unittest.main()
