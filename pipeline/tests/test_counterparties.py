"""SEC counterparty enrichment. Fixtures stand in for EDGAR. No network."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

from pipeline.counterparties import (
    BACKLOG_QUERY,
    CAPEX_QUERY,
    _require_sec,
    archive_document_url,
    efts_url,
    enrich_leads,
    parties_from_text,
    search_window,
    select_filings,
    submissions_url,
)
from pipeline.flow import PlannedLead, run_pipeline
from pipeline.llm import StubProvider
from pipeline.prompts import centurion_prompt
from shared.nethttp import EndpointError
from shared.store import save_run

from pipeline.tests.test_gates import (  # noqa: E402
    SWEEP_AT,
    _caps,
    _figure,
    _history,
    _orders,
    connect,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ORCL_CIK = "999001"
GEV_CIK = "2088000"
CONTACT = "research@example.com"


class CounterpartyTests(unittest.TestCase):
    def setUp(self):
        self._email = os.environ.pop("SEC_CONTACT_EMAIL", None)

    def tearDown(self):
        if self._email is None:
            os.environ.pop("SEC_CONTACT_EMAIL", None)
        else:
            os.environ["SEC_CONTACT_EMAIL"] = self._email

    def test_filing_text_names_suppliers_and_customers(self):
        eight = (FIXTURES / "orcl_8k.html").read_text(encoding="utf-8")
        tenk = (FIXTURES / "orcl_10k.html").read_text(encoding="utf-8")
        highlight = json.loads((FIXTURES / "orcl_efts.json").read_text(encoding="utf-8"))
        snippet = highlight["hits"]["hits"][0]["highlight"]["doc_text"][0]
        customers = (FIXTURES / "customer_10k.html").read_text(encoding="utf-8")
        contract = (FIXTURES / "customer_8k.html").read_text(encoding="utf-8")
        self.assertEqual(parties_from_text(eight, "supplier"), ["Globex Power"])
        self.assertEqual(parties_from_text(tenk, "supplier"), ["Globex Power"])
        self.assertEqual(parties_from_text(snippet, "supplier"), ["Globex Power"])
        self.assertEqual(parties_from_text(eight, "customer"), [])
        self.assertEqual(
            parties_from_text(customers, "customer"),
            ["Northwind Capital", "Globex Power"],
        )
        self.assertEqual(parties_from_text(contract, "customer"), ["Northwind Capital"])
        self.assertEqual(
            parties_from_text("purchase obligations with The Company were $1 billion.", "supplier"),
            [],
        )
        self.assertEqual(
            parties_from_text(
                "The Company entered into a supply agreement with Globex Power. The Board approved it.",
                "supplier",
            ),
            ["Globex Power"],
        )
        self.assertEqual(
            parties_from_text("named strategic partner Globex Power in the exhibit.", "supplier"),
            ["Globex Power"],
        )

    def test_filings_keep_item_1_01_and_8_01_and_the_latest_10k(self):
        payload = {
            "filings": {
                "recent": {
                    "form": ["8-K", "8-K", "8-K", "8-K", "8-K", "10-Q", "10-K", "10-K", "8-K"],
                    "accessionNumber": [
                        "0000999001-26-000010",
                        "0000999001-26-000009",
                        "0000999001-26-000008",
                        "0000999001-26-000007",
                        "0000999001-24-000001",
                        "0000999001-26-000004",
                        "0000999001-26-000001",
                        "0000999001-25-000001",
                        "0000999001-26-000002",
                    ],
                    "filingDate": [
                        "2026-09-01",
                        "2026-08-01",
                        "2026-07-01",
                        "2026-06-01",
                        "2024-01-01",
                        "2026-08-02",
                        "2026-06-15",
                        "2025-06-15",
                        "2026-03-01",
                    ],
                    "primaryDocument": [
                        "a.htm",
                        "b.htm",
                        "c.htm",
                        "d.htm",
                        "old.htm",
                        "q.htm",
                        "k.htm",
                        "k-old.htm",
                        "f.xml",
                    ],
                    "items": ["1.01", "8.01", "1.01,8.01", "1.01", "1.01", "", "", "", "2.02"],
                }
            }
        }
        chosen = select_filings(
            payload,
            suppliers=True,
            customers=False,
            start="2025-04-20",
            end="2026-10-12",
        )
        self.assertEqual(
            [row["accession"] for row in chosen],
            [
                "0000999001-26-000010",
                "0000999001-26-000009",
                "0000999001-26-000008",
                "0000999001-26-000001",
            ],
        )
        self.assertTrue(all(row["form"] in {"8-K", "10-K"} for row in chosen))
        loaded = json.loads((FIXTURES / "orcl_submissions.json").read_text(encoding="utf-8"))
        start, end = search_window(SWEEP_AT)
        forms = [
            row["form"]
            for row in select_filings(loaded, suppliers=True, customers=False, start=start, end=end)
        ]
        self.assertEqual(forms, ["8-K", "10-K"])

    def test_urls_are_sec_hosts(self):
        start, end = search_window(SWEEP_AT)
        urls = [
            submissions_url(ORCL_CIK),
            efts_url(ORCL_CIK, CAPEX_QUERY, start, end),
            efts_url(ORCL_CIK, BACKLOG_QUERY, start, end),
            archive_document_url(ORCL_CIK, "0000999001-26-000008", "orcl-8k.htm"),
        ]
        for url in urls:
            host = (urlsplit(url).hostname or "").lower()
            self.assertTrue(host == "sec.gov" or host.endswith(".sec.gov"), url)
        self.assertIn("CIK0000999001", submissions_url(ORCL_CIK))
        self.assertIn("purchase+obligation", efts_url(ORCL_CIK, CAPEX_QUERY, start, end).replace("%22", ""))
        self.assertIn("accounted+for", efts_url(ORCL_CIK, BACKLOG_QUERY, start, end).replace("%22", ""))
        with self.assertRaises(EndpointError):
            _require_sec("https://example.com/filing.htm")

    def test_a_source_error_drops_the_company_and_does_not_log_the_contact(self):
        os.environ["SEC_CONTACT_EMAIL"] = CONTACT
        lead = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm")
        client = _Client(fail_on="search-index", email=CONTACT)
        client.pages[submissions_url(ORCL_CIK)] = json.loads(
            (FIXTURES / "orcl_submissions.json").read_text(encoding="utf-8")
        )
        start, end = search_window(SWEEP_AT)
        eight = archive_document_url(ORCL_CIK, "0000999001-26-000008", "orcl-8k.htm")
        tenk = archive_document_url(ORCL_CIK, "0000999001-26-000001", "orcl-10k.htm")
        client.bodies[eight] = (FIXTURES / "orcl_8k.html").read_text(encoding="utf-8")
        client.bodies[tenk] = (FIXTURES / "orcl_10k.html").read_text(encoding="utf-8")
        with self.assertLogs("pipeline.counterparties", level="WARNING") as caught:
            enrich_leads([lead], client=client, names={}, as_of=SWEEP_AT)
        self.assertEqual(lead.counterparties, [])
        blob = "\n".join(caught.output)
        self.assertIn("failed closed", blob)
        self.assertIn("ORCL", blob)
        self.assertNotIn(CONTACT, blob)
        self.assertNotIn("@", blob)

    def test_backlog_customers_come_from_the_filing_and_the_concentration_note(self):
        start, end = search_window(SWEEP_AT)
        sub = submissions_url(ORCL_CIK)
        eight = archive_document_url(ORCL_CIK, "0000999001-26-000020", "customer-8k.htm")
        tenk = archive_document_url(ORCL_CIK, "0000999001-26-000021", "customer-10k.htm")
        client = _Client()
        client.pages[sub] = {
            "filings": {
                "recent": {
                    "form": ["8-K", "10-K"],
                    "accessionNumber": ["0000999001-26-000020", "0000999001-26-000021"],
                    "filingDate": ["2026-09-01", "2026-06-15"],
                    "primaryDocument": ["customer-8k.htm", "customer-10k.htm"],
                    "items": ["8.01", ""],
                }
            }
        }
        client.pages[efts_url(ORCL_CIK, BACKLOG_QUERY, start, end)] = {"hits": {"hits": []}}
        client.bodies[eight] = (FIXTURES / "customer_8k.html").read_text(encoding="utf-8")
        client.bodies[tenk] = (FIXTURES / "customer_10k.html").read_text(encoding="utf-8")
        lead = _lead(
            "orders_backlog",
            "https://www.sec.gov/Archives/edgar/data/999001/backlog.htm",
            ticker="GEV",
        )
        enrich_leads([lead], client=client, names={"northwind capital": "NWT"}, as_of=SWEEP_AT)
        names = [(party["role"], party["name"], party["ticker"]) for party in lead.counterparties]
        self.assertEqual(
            names,
            [("customer", "Northwind Capital", "NWT"), ("customer", "Globex Power", None)],
        )
        self.assertTrue(all("sec.gov" in party["source_url"] for party in lead.counterparties))
        self.assertTrue(all(urlsplit(url).hostname.endswith("sec.gov") for url in client.urls))

    def test_prompt_puts_sec_parties_on_the_suppliers_named_line(self):
        lead = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm")
        lead.company = "Oracle"
        lead.grade_types = []
        lead.figure = _bare_figure()
        lead.counterparties = [
            {
                "role": "supplier",
                "name": "Globex Power",
                "ticker": "GEV",
                "source_url": "https://www.sec.gov/Archives/edgar/data/999001/orcl-8k.htm",
            }
        ]
        prompt = centurion_prompt(lead, [], 1)
        self.assertIn("COUNTERPARTIES_BEGIN", prompt)
        self.assertIn("Globex Power", prompt)
        data = json.loads(StubProvider().complete(prompt, max_tokens=800).text)
        self.assertEqual(data["suppliers"][0]["name"], "Globex Power")
        self.assertIn("Suppliers named: Globex Power", data["thesis"])

    def test_orcl_capex_links_to_a_supplier_lead_through_a_named_party(self):
        os.environ["SEC_CONTACT_EMAIL"] = CONTACT
        start, end = search_window(SWEEP_AT)
        client = _orcl_client(start, end)
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                current = _figure("ORCL", "Oracle", "quarter", "2026-07-01/2026-09-30", 5_000_000_000)
                prior = _figure("ORCL", "Oracle", "quarter", "2025-07-01/2025-09-30", 1_000_000_000)
                for row in (current, prior):
                    row["source_url"] = "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm"
                supplier = _orders("GEV", "filing", "2026-06-30", 2_000_000_000, figure_type="supplier_order")
                supplier["company"] = "Globex Power"
                supplier["source_url"] = "https://www.sec.gov/Archives/edgar/data/2088000/gev.htm"
                save_run(
                    conn,
                    "capex",
                    "2026-10-12T14:30:52Z",
                    [current, prior, *_caps("ORCL", "GEV")],
                )
                save_run(conn, "orders-backlog", "2026-10-12T14:31:00Z", [supplier])
            finally:
                conn.close()
            with self.assertLogs("pipeline.counterparties", level="WARNING") as caught:
                run_pipeline(
                    path,
                    provider=StubProvider(),
                    token_budget=80000,
                    now=SWEEP_AT,
                    parties_client=client,
                )
            conn = connect(path)
            try:
                rows = conn.execute(
                    """
                    SELECT l.source_scout, l.ticker, l.summary, t.thesis_text,
                           c.role, c.name, c.ticker AS party_ticker, c.source_url
                    FROM leads AS l
                    JOIN theses AS t ON t.lead_id = l.lead_id
                    LEFT JOIN lead_counterparties AS c ON c.lead_id = l.lead_id
                    """
                ).fetchall()
            finally:
                conn.close()
        blob = "\n".join(caught.output)
        self.assertIn("failed closed", blob)
        self.assertIn("GEV", blob)
        self.assertNotIn(CONTACT, blob)
        legate = [row for row in rows if row["source_scout"] == "legate"]
        self.assertTrue(legate)
        self.assertIn("cross-company capex-to-backlog", legate[0]["summary"])
        self.assertTrue(any("Suppliers named: Globex Power" in (row["thesis_text"] or "") for row in legate))
        parties = [row for row in legate if row["name"] == "Globex Power"]
        self.assertTrue(parties)
        self.assertEqual(parties[0]["role"], "supplier")
        self.assertEqual(parties[0]["party_ticker"], "GEV")
        self.assertIn("https://www.sec.gov/", parties[0]["source_url"])
        gev = [row for row in rows if row["ticker"] == "GEV" and row["source_scout"] != "legate"]
        self.assertTrue(gev)
        self.assertTrue(all(row["name"] is None for row in gev))
        self.assertTrue(all((urlsplit(url).hostname or "").endswith("sec.gov") for url in client.urls))


class _Client:
    def __init__(self, fail_on=None, email=""):
        self.pages = {}
        self.bodies = {}
        self.urls = []
        self.fail_on = fail_on
        self.email = email

    def json(self, url):
        self.urls.append(url)
        self._maybe_fail(url)
        if url not in self.pages:
            raise EndpointError(f"HTTP 404 for {url} {self.email}".rstrip())
        return self.pages[url]

    def get(self, url, accept="text/html,text/plain"):
        self.urls.append(url)
        self._maybe_fail(url)
        if url not in self.bodies:
            raise EndpointError(f"HTTP 404 for {url} {self.email}".rstrip())
        body = self.bodies[url]
        if isinstance(body, str):
            body = body.encode("utf-8")
        return body, "text/html"

    def _maybe_fail(self, url):
        if self.fail_on and self.fail_on in url:
            raise EndpointError(f"HTTP 503 for {url} User-Agent cause-n-effect ({self.email})")


def _orcl_client(start, end):
    client = _Client(fail_on=GEV_CIK, email=CONTACT)
    client.pages[submissions_url(ORCL_CIK)] = json.loads(
        (FIXTURES / "orcl_submissions.json").read_text(encoding="utf-8")
    )
    client.pages[efts_url(ORCL_CIK, CAPEX_QUERY, start, end)] = json.loads(
        (FIXTURES / "orcl_efts.json").read_text(encoding="utf-8")
    )
    client.bodies[archive_document_url(ORCL_CIK, "0000999001-26-000008", "orcl-8k.htm")] = (
        FIXTURES / "orcl_8k.html"
    ).read_text(encoding="utf-8")
    client.bodies[archive_document_url(ORCL_CIK, "0000999001-26-000001", "orcl-10k.htm")] = (
        FIXTURES / "orcl_10k.html"
    ).read_text(encoding="utf-8")
    return client


def _lead(lead_type, source_url, ticker="ORCL"):
    lead = PlannedLead()
    lead.lead_type = lead_type
    lead.ticker = ticker
    lead.source_url = source_url
    return lead


def _bare_figure():
    figure = type("Figure", (), {})()
    figure.figure_type = "PaymentsToAcquireProductiveAssets"
    figure.value = 5_000_000_000
    figure.value_text = "5000000000"
    figure.instrument = "quarter"
    figure.period = "2026-07-01/2026-09-30"
    figure.source_url = "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm"
    figure.prior_comparison = True
    figure.growth_pct = 400
    figure.growth_text = ""
    figure.market_cap = 10_000_000_000
    return figure


if __name__ == "__main__":
    unittest.main()
