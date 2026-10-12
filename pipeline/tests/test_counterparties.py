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
    FTS_COMPANY_CAP,
    FTS_DOCUMENT_CAP,
    FTS_MIN_INTERVAL,
    TICKERS_URL,
    _RequestBudget,
    _require_sec,
    accept_party,
    archive_document_url,
    efts_url,
    enrich_leads,
    ex10_names,
    parties_from_text,
    registrant_index,
    search_window,
    select_filings,
    submissions_url,
)
import pipeline.counterparties as counterparties
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
ORCL_8K = "0000999001-26-000008"
JUNK = (
    "Network",
    "During",
    "Relationships Subject Us",
    "Agreements We",
    "Services Enterprise",
    "SUMMARY RESULTS OF OPERATIONS",
)


class CounterpartyTests(unittest.TestCase):
    def setUp(self):
        self._email = os.environ.pop("SEC_CONTACT_EMAIL", None)

    def tearDown(self):
        if self._email is None:
            os.environ.pop("SEC_CONTACT_EMAIL", None)
        else:
            os.environ["SEC_CONTACT_EMAIL"] = self._email

    def test_filing_text_names_suppliers_and_customers(self):
        registrants = registrant_index(_tickers())
        eight = (FIXTURES / "orcl_8k.html").read_text(encoding="utf-8")
        tenk = (FIXTURES / "orcl_10k.html").read_text(encoding="utf-8")
        exhibit = (FIXTURES / "orcl_ex10.html").read_text(encoding="utf-8")
        customers = (FIXTURES / "customer_10k.html").read_text(encoding="utf-8")
        contract = (FIXTURES / "customer_8k.html").read_text(encoding="utf-8")
        self.assertEqual(parties_from_text(eight, "supplier", registrants), [])
        self.assertEqual(parties_from_text(tenk, "supplier", registrants), [])
        self.assertEqual(
            parties_from_text("entered into an agreement with Network", "supplier", registrants),
            [],
        )
        self.assertEqual(
            parties_from_text(exhibit, "supplier", registrants),
            ["Oracle Corporation", "Globex Power"],
        )
        self.assertEqual(
            parties_from_text(
                "The Company entered into a supply agreement with Globex Power Inc. The Board approved it.",
                "supplier",
                registrants,
            ),
            ["Globex Power"],
        )
        self.assertEqual(
            parties_from_text("named strategic partner Globex Power in the exhibit.", "supplier", registrants),
            [],
        )
        self.assertEqual(
            parties_from_text("purchase obligations with The Company were $1 billion.", "supplier", registrants),
            [],
        )
        self.assertEqual(parties_from_text(eight, "customer", registrants), [])
        self.assertEqual(
            parties_from_text(customers, "customer", registrants),
            ["Northwind Capital", "Acme Widgets LLC"],
        )
        self.assertEqual(parties_from_text(contract, "customer", registrants), ["Northwind Capital"])
        for junk in JUNK:
            self.assertIsNone(accept_party(junk, registrants), junk)

    def test_junk_strings_never_become_named_parties(self):
        registrants = registrant_index(_tickers())
        empty = registrant_index({})
        for junk in JUNK:
            self.assertIsNone(accept_party(junk, registrants), junk)
            self.assertIsNone(accept_party(junk, empty), junk)
        blob = " ".join(
            [
                f"The Company entered into an agreement with {junk}."
                for junk in JUNK
            ]
            + [
                f"{junk} accounted for 4% of revenue."
                for junk in JUNK
            ]
            + [
                "Our largest customer, Northwind Capital, continued.",
                "The Company entered into a supply agreement with Globex Power Inc.",
            ]
        )
        self.assertEqual(parties_from_text(blob, "supplier", registrants), ["Globex Power"])
        self.assertEqual(
            parties_from_text(blob, "customer", registrants),
            ["Northwind Capital"],
        )
        for junk in JUNK:
            self.assertNotIn(junk, parties_from_text(blob, "supplier", registrants))
            self.assertNotIn(junk, parties_from_text(blob, "customer", registrants))

    def test_suffix_or_registrant_accepts_a_company(self):
        empty = registrant_index({})
        acme = accept_party("Acme Widgets LLC", empty)
        self.assertEqual(acme["name"], "Acme Widgets LLC")
        self.assertIsNone(acme["ticker"])
        self.assertEqual(accept_party("Vodafone plc", empty)["name"], "Vodafone plc")
        self.assertEqual(accept_party("Bank of America Inc.", empty)["name"], "Bank of America Inc.")
        self.assertEqual(accept_party("Smith Co.", empty)["name"], "Smith Co.")
        self.assertIsNone(accept_party("The Company", empty))
        self.assertIsNone(accept_party("Co.", empty))
        self.assertIsNone(accept_party("GE Vernova", empty))
        gev = registrant_index({"0": {"cik_str": 999300, "ticker": "GEV", "title": "GE Vernova"}})
        party = accept_party("GE Vernova", gev)
        self.assertEqual(party["name"], "GE Vernova")
        self.assertEqual(party["ticker"], "GEV")
        self.assertEqual(party["name_normalised"], "ge vernova")

    def test_incorporated_matches_an_edgar_title(self):
        empty = registrant_index({})
        plain = accept_party("Dana Incorporated", empty)
        self.assertEqual(plain["name"], "Dana Incorporated")
        self.assertIsNone(plain["ticker"])
        dana = registrant_index(
            {
                "0": {"cik_str": 26780, "ticker": "DAN", "title": "DANA INC"},
                "1": {"cik_str": 26781, "ticker": "DC", "title": "DANA CO"},
                "2": {"cik_str": 26782, "ticker": "FOO", "title": "FOO CORP"},
                "3": {"cik_str": 26783, "ticker": "BAR", "title": "BAR LTD"},
            }
        )
        party = accept_party("Dana Incorporated", dana)
        self.assertEqual(party["name"], "DANA INC")
        self.assertEqual(party["ticker"], "DAN")
        self.assertEqual(accept_party("The Dana Company", dana)["ticker"], "DC")
        self.assertEqual(accept_party("DANA INC.", dana)["name"], "DANA INC")
        self.assertEqual(accept_party("Foo Corporation", dana)["ticker"], "FOO")
        self.assertEqual(accept_party("Bar Limited", dana)["ticker"], "BAR")
        self.assertIsNone(accept_party("The Company", dana))

    def test_wider_contract_and_concentration_phrases(self):
        registrants = registrant_index({"0": {"cik_str": 26780, "ticker": "DAN", "title": "DANA INC"}})
        samples = [
            "The Company entered into a supply agreement with Dana Incorporated.",
            "supply agreement between the Company and Dana Incorporated.",
            "This supply agreement is by and among the Company and Dana Incorporated.",
            'Dana Incorporated (the "Supplier") accounted for approximately 18% of net sales.',
            "Dana Incorporated represented more than 11% of revenue.",
            "Sales to Dana Incorporated were material.",
            "Our largest customers include Dana Incorporated.",
        ]
        for sample in samples:
            names = parties_from_text(sample, "customer", registrants) + parties_from_text(
                sample, "supplier", registrants
            )
            self.assertIn("DANA INC", names, sample)
            for junk in JUNK:
                self.assertNotIn(junk, names, sample)

    def test_ex10_filenames_match_exhibit_10(self):
        names = ex10_names(
            {
                "directory": {
                    "item": [
                        {"name": "orcl-8k.htm"},
                        {"name": "d1dex101.htm"},
                        {"name": "ex991.htm"},
                        {"name": "ex-10.1.htm"},
                    ]
                }
            }
        )
        self.assertEqual(names, ["d1dex101.htm", "ex-10.1.htm"])
        single = ex10_names({"directory": {"item": {"name": "exhibit_10.htm"}}})
        self.assertEqual(single, ["exhibit_10.htm"])

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
        capex_url = efts_url(ORCL_CIK, CAPEX_QUERY, start, end).replace("%22", "")
        backlog_url = efts_url(ORCL_CIK, BACKLOG_QUERY, start, end).replace("%22", "")
        self.assertIn("agreement+with", capex_url)
        self.assertIn("by+and+between", capex_url)
        self.assertIn("accounted+for", backlog_url)
        self.assertIn("largest+customer", backlog_url)
        self.assertIn("contract+with", backlog_url)
        self.assertTrue((urlsplit(TICKERS_URL).hostname or "").endswith("sec.gov"))
        with self.assertRaises(EndpointError):
            _require_sec("https://example.com/filing.htm")

    def test_a_source_error_drops_the_company_and_does_not_log_the_contact(self):
        os.environ["SEC_CONTACT_EMAIL"] = CONTACT
        lead = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm")
        client = _Client(fail_on="search-index", email=CONTACT)
        client.pages[TICKERS_URL] = _tickers()
        client.pages[submissions_url(ORCL_CIK)] = json.loads(
            (FIXTURES / "orcl_submissions.json").read_text(encoding="utf-8")
        )
        eight = archive_document_url(ORCL_CIK, ORCL_8K, "orcl-8k.htm")
        index = archive_document_url(ORCL_CIK, ORCL_8K, "index.json")
        exhibit = archive_document_url(ORCL_CIK, ORCL_8K, "d1dex101.htm")
        client.pages[index] = {
            "directory": {"item": [{"name": "orcl-8k.htm"}, {"name": "d1dex101.htm"}]}
        }
        client.bodies[eight] = (FIXTURES / "orcl_8k.html").read_text(encoding="utf-8")
        client.bodies[exhibit] = (FIXTURES / "orcl_ex10.html").read_text(encoding="utf-8")
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
        client.pages[TICKERS_URL] = _tickers()
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
        lead.company = "GE Vernova"
        enrich_leads([lead], client=client, names={}, as_of=SWEEP_AT)
        names = [(party["role"], party["name"], party["ticker"]) for party in lead.counterparties]
        self.assertEqual(
            names,
            [("customer", "Northwind Capital", "NWT"), ("customer", "Acme Widgets LLC", None)],
        )
        for junk in JUNK:
            self.assertNotIn(junk, [party["name"] for party in lead.counterparties])
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

    def test_empty_named_lines_say_none_found(self):
        capex = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm")
        capex.company = "Oracle"
        capex.grade_types = []
        capex.figure = _bare_figure()
        capex.counterparties = []
        capex_data = json.loads(StubProvider().complete(centurion_prompt(capex, [], 1), max_tokens=800).text)
        self.assertIn("Suppliers named: none found", capex_data["thesis"])
        self.assertNotIn("Customers named:", capex_data["thesis"])
        backlog = _lead(
            "orders_backlog",
            "https://www.sec.gov/Archives/edgar/data/2088000/gev.htm",
            ticker="GEV",
        )
        backlog.company = "GE Vernova"
        backlog.grade_types = []
        backlog.figure = _bare_figure()
        backlog.figure.figure_type = "backlog"
        backlog.counterparties = []
        backlog_data = json.loads(StubProvider().complete(centurion_prompt(backlog, [], 1), max_tokens=800).text)
        self.assertIn("Customers named: none found", backlog_data["thesis"])
        self.assertNotIn("Suppliers named:", backlog_data["thesis"])

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
        self.assertIn("d1dex101.htm", parties[0]["source_url"])
        stored_names = [row["name"] for row in rows if row["name"]]
        thesis_blob = "\n".join(row["thesis_text"] or "" for row in rows)
        for junk in JUNK:
            self.assertNotIn(junk, stored_names)
            self.assertNotIn(junk, thesis_blob)
        self.assertTrue(all("orcl-10k.htm" not in url for url in client.urls))
        gev = [row for row in rows if row["ticker"] == "GEV" and row["source_scout"] != "legate"]
        self.assertTrue(gev)
        self.assertTrue(all(row["name"] is None for row in gev))
        self.assertTrue(all((urlsplit(url).hostname or "").endswith("sec.gov") for url in client.urls))
        self.assertEqual(client.urls.count(TICKERS_URL), 1)

    def test_registrant_file_is_fetched_once(self):
        client = _Client()
        client.pages[TICKERS_URL] = _tickers()
        leads = [
            _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/a.htm"),
            _lead(
                "orders_backlog",
                "https://www.sec.gov/Archives/edgar/data/2088000/b.htm",
                ticker="GEV",
            ),
        ]
        leads[0].company = "Oracle"
        leads[1].company = "GE Vernova"
        with self.assertLogs("pipeline.counterparties", level="WARNING") as caught:
            enrich_leads(leads, client=client, names={}, as_of=SWEEP_AT)
            enrich_leads(leads, client=client, names={}, as_of=SWEEP_AT)
        self.assertEqual(client.urls.count(TICKERS_URL), 2)
        self.assertEqual(leads[0].counterparties, [])
        self.assertEqual(leads[1].counterparties, [])
        blob = "\n".join(caught.output)
        self.assertIn("failed closed", blob)
        self.assertNotIn(CONTACT, blob)

    def test_cross_reference_names_the_backlog_company_inside_capex_filings(self):
        start, end = search_window(SWEEP_AT)
        client = _Client()
        client.pages[TICKERS_URL] = {
            "0": {"cik_str": 999001, "ticker": "ORCL", "title": "Oracle Corporation"},
            "1": {"cik_str": 999300, "ticker": "GEV", "title": "GE Vernova"},
        }
        client.pages[submissions_url(ORCL_CIK)] = json.loads(
            (FIXTURES / "orcl_submissions.json").read_text(encoding="utf-8")
        )
        client.bodies[archive_document_url(ORCL_CIK, ORCL_8K, "orcl-8k.htm")] = (
            FIXTURES / "orcl_8k.html"
        ).read_text(encoding="utf-8")
        client.pages[archive_document_url(ORCL_CIK, ORCL_8K, "index.json")] = {
            "directory": {"item": [{"name": "orcl-8k.htm"}]}
        }
        client.pages[efts_url(ORCL_CIK, CAPEX_QUERY, start, end)] = {"hits": {"hits": []}}
        cross_doc = archive_document_url(ORCL_CIK, ORCL_8K, "cross-ref.htm")
        client.pages[efts_url(ORCL_CIK, '"GE Vernova"', start, end)] = _fts_hit(
            ORCL_8K, "cross-ref.htm", ["0000999001"]
        )
        gev_8k = "0002088000-26-000001"
        gev_10k = "0002088000-26-000002"
        client.pages[submissions_url(GEV_CIK)] = {
            "filings": {
                "recent": {
                    "form": ["8-K", "10-K"],
                    "accessionNumber": [gev_8k, gev_10k],
                    "filingDate": ["2026-09-01", "2026-06-15"],
                    "primaryDocument": ["gev-8k.htm", "gev-10k.htm"],
                    "items": ["8.01", ""],
                }
            }
        }
        client.bodies[archive_document_url(GEV_CIK, gev_8k, "gev-8k.htm")] = (
            "<html><body><p>The company discussed operations.</p></body></html>"
        )
        client.bodies[archive_document_url(GEV_CIK, gev_10k, "gev-10k.htm")] = (
            "<html><body><p>No single customer was disclosed.</p>"
            "<p>agreement with Network. SUMMARY RESULTS OF OPERATIONS</p></body></html>"
        )
        client.pages[efts_url(GEV_CIK, BACKLOG_QUERY, start, end)] = {"hits": {"hits": []}}
        oracle_doc = archive_document_url(GEV_CIK, gev_10k, "oracle-mention.htm")
        client.pages[efts_url(GEV_CIK, '"Oracle"', start, end)] = _fts_hit(
            gev_10k, "oracle-mention.htm", ["0002088000"]
        )
        client.pages[efts_url(GEV_CIK, '"Oracle Corporation"', start, end)] = {"hits": {"hits": []}}
        capex = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm")
        capex.company = "Oracle"
        backlog = _lead(
            "orders_backlog",
            "https://www.sec.gov/Archives/edgar/data/2088000/gev.htm",
            ticker="GEV",
        )
        backlog.company = "GE Vernova"
        enrich_leads([capex, backlog], client=client, names={}, as_of=SWEEP_AT)
        self.assertEqual(
            [(party["role"], party["name"], party["ticker"]) for party in capex.counterparties],
            [("supplier", "GE Vernova", "GEV")],
        )
        self.assertEqual(capex.counterparties[0]["source_url"], cross_doc)
        self.assertNotIn(cross_doc, client.urls)
        self.assertEqual(
            [(party["role"], party["name"], party["ticker"]) for party in backlog.counterparties],
            [("customer", "Oracle Corporation", "ORCL")],
        )
        self.assertEqual(backlog.counterparties[0]["source_url"], oracle_doc)
        self.assertNotIn(oracle_doc, client.urls)
        named = [party["name"] for party in capex.counterparties + backlog.counterparties]
        for junk in JUNK:
            self.assertNotIn(junk, named)
        self.assertEqual(client.urls.count(TICKERS_URL), 1)
        self.assertTrue(all((urlsplit(url).hostname or "").endswith("sec.gov") for url in client.urls))

    def test_junk_only_capex_says_none_found(self):
        start, end = search_window(SWEEP_AT)
        client = _Client()
        client.pages[TICKERS_URL] = _tickers()
        client.pages[submissions_url(ORCL_CIK)] = json.loads(
            (FIXTURES / "orcl_submissions.json").read_text(encoding="utf-8")
        )
        client.bodies[archive_document_url(ORCL_CIK, ORCL_8K, "orcl-8k.htm")] = (
            FIXTURES / "orcl_8k.html"
        ).read_text(encoding="utf-8")
        client.pages[archive_document_url(ORCL_CIK, ORCL_8K, "index.json")] = {
            "directory": {"item": [{"name": "orcl-8k.htm"}, {"name": "ex991.htm"}]}
        }
        client.pages[efts_url(ORCL_CIK, CAPEX_QUERY, start, end)] = json.loads(
            (FIXTURES / "orcl_efts.json").read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                current = _figure("ORCL", "Oracle", "quarter", "2026-07-01/2026-09-30", 5_000_000_000)
                prior = _figure("ORCL", "Oracle", "quarter", "2025-07-01/2025-09-30", 1_000_000_000)
                for row in (current, prior):
                    row["source_url"] = "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm"
                save_run(conn, "capex", "2026-10-12T14:30:52Z", [current, prior, *_caps("ORCL")])
            finally:
                conn.close()
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
                    SELECT t.thesis_text, c.name
                    FROM leads AS l
                    JOIN theses AS t ON t.lead_id = l.lead_id
                    LEFT JOIN lead_counterparties AS c ON c.lead_id = l.lead_id
                    WHERE l.ticker = 'ORCL'
                    """
                ).fetchall()
            finally:
                conn.close()
        self.assertTrue(rows)
        self.assertTrue(all(row["name"] is None for row in rows))
        thesis = "\n".join(row["thesis_text"] or "" for row in rows)
        self.assertIn("Suppliers named: none found", thesis)
        for junk in JUNK:
            self.assertNotIn(junk, thesis)
        self.assertTrue(all("orcl-10k.htm" not in url for url in client.urls))
        page = json.loads((FIXTURES / "orcl_efts.json").read_text(encoding="utf-8"))
        self.assertNotIn("highlight", page["hits"]["hits"][0])

    def test_fts_without_highlight_reads_the_hit_document(self):
        start, end = search_window(SWEEP_AT)
        page = json.loads((FIXTURES / "efts_no_highlight.json").read_text(encoding="utf-8"))
        hit = page["hits"]["hits"][0]
        self.assertNotIn("highlight", hit)
        self.assertIn("adsh", hit["_source"])
        adsh, filename = hit["_id"].split(":", 1)
        self.assertEqual(hit["_source"]["adsh"], adsh)
        document = archive_document_url(ORCL_CIK, adsh, filename)
        client = _Client()
        client.pages[TICKERS_URL] = {
            "0": {"cik_str": 26780, "ticker": "DAN", "title": "DANA INC"},
            "1": {"cik_str": 999001, "ticker": "ORCL", "title": "Oracle Corporation"},
        }
        client.pages[submissions_url(ORCL_CIK)] = {
            "filings": {
                "recent": {
                    "form": [],
                    "accessionNumber": [],
                    "filingDate": [],
                    "primaryDocument": [],
                    "items": [],
                }
            }
        }
        client.pages[efts_url(ORCL_CIK, CAPEX_QUERY, start, end)] = page
        client.bodies[document] = (
            "<html><body>"
            "<p>The Company entered into a supply agreement with Dana Incorporated.</p>"
            "<p>Dana Incorporated (the \"Supplier\") accounted for approximately 18% of net sales.</p>"
            "<p>agreement with Network. SUMMARY RESULTS OF OPERATIONS</p>"
            "</body></html>"
        )
        lead = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm")
        lead.company = "Oracle"
        enrich_leads([lead], client=client, names={}, as_of=SWEEP_AT)
        self.assertEqual(
            [(party["role"], party["name"], party["ticker"]) for party in lead.counterparties],
            [("supplier", "DANA INC", "DAN")],
        )
        self.assertEqual(lead.counterparties[0]["source_url"], document)
        self.assertIn(document, client.urls)
        for junk in JUNK:
            self.assertNotIn(junk, [party["name"] for party in lead.counterparties])

    def test_fts_document_cap_stops_later_hits(self):
        start, end = search_window(SWEEP_AT)
        first = archive_document_url(ORCL_CIK, "0000999001-26-000041", "dana.htm")
        second = archive_document_url(ORCL_CIK, "0000999001-26-000042", "globex.htm")
        client = _Client()
        client.pages[TICKERS_URL] = {
            "0": {"cik_str": 26780, "ticker": "DAN", "title": "DANA INC"},
            "1": {"cik_str": 999001, "ticker": "ORCL", "title": "Oracle Corporation"},
            "2": {"cik_str": 2088000, "ticker": "GEV", "title": "Globex Power"},
        }
        client.pages[submissions_url(ORCL_CIK)] = {
            "filings": {"recent": {"form": [], "accessionNumber": [], "filingDate": [], "primaryDocument": [], "items": []}}
        }
        client.pages[efts_url(ORCL_CIK, CAPEX_QUERY, start, end)] = {
            "hits": {
                "hits": [
                    {"_id": "0000999001-26-000041:dana.htm", "_source": {"adsh": "0000999001-26-000041", "form": "8-K"}},
                    {"_id": "0000999001-26-000042:globex.htm", "_source": {"adsh": "0000999001-26-000042", "form": "8-K"}},
                ]
            }
        }
        client.bodies[first] = "<p>The Company entered into a supply agreement with Dana Incorporated.</p>"
        client.bodies[second] = "<p>The Company entered into a supply agreement with Globex Power Inc.</p>"
        lead = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm")
        lead.company = "Oracle"
        previous = counterparties.FTS_DOCUMENT_CAP
        counterparties.FTS_DOCUMENT_CAP = 1
        try:
            enrich_leads([lead], client=client, names={}, as_of=SWEEP_AT)
        finally:
            counterparties.FTS_DOCUMENT_CAP = previous
        self.assertEqual([party["name"] for party in lead.counterparties], ["DANA INC"])
        self.assertIn(first, client.urls)
        self.assertNotIn(second, client.urls)

    def test_a_missing_fts_document_drops_the_company(self):
        start, end = search_window(SWEEP_AT)
        client = _Client(email=CONTACT)
        os.environ["SEC_CONTACT_EMAIL"] = CONTACT
        client.pages[TICKERS_URL] = _tickers()
        client.pages[submissions_url(ORCL_CIK)] = {
            "filings": {"recent": {"form": [], "accessionNumber": [], "filingDate": [], "primaryDocument": [], "items": []}}
        }
        client.pages[efts_url(ORCL_CIK, CAPEX_QUERY, start, end)] = json.loads(
            (FIXTURES / "efts_no_highlight.json").read_text(encoding="utf-8")
        )
        lead = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm")
        with self.assertLogs("pipeline.counterparties", level="WARNING") as caught:
            enrich_leads([lead], client=client, names={}, as_of=SWEEP_AT)
        self.assertEqual(lead.counterparties, [])
        blob = "\n".join(caught.output)
        self.assertIn("failed closed", blob)
        self.assertIn("ORCL", blob)
        self.assertNotIn(CONTACT, blob)
        self.assertNotIn("@", blob)

    def test_fts_fetches_stay_at_ten_per_second(self):
        self.assertEqual(FTS_MIN_INTERVAL, 0.1)
        self.assertEqual(FTS_COMPANY_CAP, 5)
        self.assertEqual(FTS_DOCUMENT_CAP, 40)
        now = {"t": 50.0}
        slept = []

        def clock():
            return now["t"]

        def sleep(seconds):
            slept.append(seconds)
            now["t"] += seconds

        budget = _RequestBudget(cap=3, sleep=sleep, clock=clock)
        self.assertTrue(budget.take())
        self.assertEqual(slept, [])
        self.assertTrue(budget.take())
        self.assertTrue(budget.take())
        self.assertEqual(slept, [0.1, 0.1])
        self.assertFalse(budget.take())
        self.assertEqual(slept, [0.1, 0.1])
        company = _RequestBudget(cap=4, company_cap=2, sleep=sleep, clock=clock)
        self.assertTrue(company.take("aaa"))
        self.assertTrue(company.take("aaa"))
        before = list(slept)
        self.assertFalse(company.take("aaa"))
        self.assertEqual(slept, before)
        self.assertTrue(company.take("bbb"))
        self.assertTrue(company.take("bbb"))
        self.assertFalse(company.take("ccc"))
        self.assertEqual(len(slept), 5)

    def test_sentence_role_keeps_amzn_customers_and_deal_parties(self):
        registrants = registrant_index(
            {
                "0": {"cik_str": 1018724, "ticker": "AMZN", "title": "Amazon.com, Inc."},
                "1": {"cik_str": 1326801, "ticker": "META", "title": "Meta Platforms, Inc."},
                "2": {"cik_str": 1543151, "ticker": "UBER", "title": "Uber Technologies, Inc."},
                "3": {"cik_str": 2088000, "ticker": "GEV", "title": "Globex Power"},
                "4": {"cik_str": 26780, "ticker": "DAN", "title": "DANA INC"},
                "5": {"cik_str": 999200, "ticker": "NWT", "title": "Northwind Capital"},
            }
        )
        customers = (
            "Meta Platforms, Inc. and Uber Technologies, Inc. are customers. "
            "The Company has an agreement to deploy Meta Platforms, Inc. technology. "
            "Sales to Uber Technologies, Inc. grew. "
            "Revenue from Meta Platforms, Inc. grew. "
            "Uber Technologies, Inc. uses our storefront."
        )
        self.assertEqual(
            parties_from_text(customers, "customer", registrants),
            ["Meta Platforms, Inc.", "Uber Technologies, Inc."],
        )
        self.assertEqual(parties_from_text(customers, "supplier", registrants), [])
        self.assertEqual(parties_from_text(customers, "deal_party", registrants), [])
        supplier = "The Company will purchase from DANA INC under a supply agreement and procure from Globex Power."
        self.assertEqual(parties_from_text(supplier, "supplier", registrants), ["DANA INC", "Globex Power"])
        self.assertEqual(parties_from_text(supplier, "customer", registrants), [])
        vendor = "Globex Power is the vendor."
        self.assertEqual(parties_from_text(vendor, "supplier", registrants), ["Globex Power"])
        deal = (
            "The Company agreed to acquire Northwind Capital. "
            "The merger with Globex Power remains subject to closing. "
            "The separation from DANA INC was announced."
        )
        self.assertEqual(
            parties_from_text(deal, "deal_party", registrants),
            ["Northwind Capital", "Globex Power", "DANA INC"],
        )
        self.assertEqual(parties_from_text(deal, "supplier", registrants), [])
        self.assertEqual(parties_from_text(deal, "customer", registrants), [])

    def test_affiliates_exhibit_index_and_unity_are_not_counterparties(self):
        unity = registrant_index({"0": {"cik_str": 1810806, "ticker": "U", "title": "Unity Software Inc."}})
        self.assertIsNone(accept_party("community", unity))
        self.assertIsNone(accept_party("Community", unity))
        self.assertIsNone(accept_party("COMMUNITY", unity))
        plain = accept_party("Unity Software Inc.", unity)
        self.assertEqual(plain["ticker"], "U")
        community = "Customers in the community use our products. COMMUNITY remained a heading."
        self.assertEqual(parties_from_text(community, "customer", unity), [])
        self.assertEqual(parties_from_text(community, "supplier", unity), [])
        named = parties_from_text(
            "The Company entered into a supply agreement with Unity Software Inc.",
            "supplier",
            unity,
        )
        self.assertEqual(named, ["Unity Software Inc."])
        bank = accept_party("Community Bank Inc.", unity)
        self.assertEqual(bank["name"], "Community Bank Inc.")
        self.assertIsNone(bank["ticker"])
        registrants = registrant_index(
            {
                "0": {"cik_str": 2088000, "ticker": "GEV", "title": "GE Vernova"},
                "1": {"cik_str": 40545, "ticker": "GE", "title": "GENERAL ELECTRIC CO"},
                "2": {"cik_str": 18230, "ticker": "CAT", "title": "Caterpillar Inc."},
                "3": {"cik_str": 26780, "ticker": "DAN", "title": "DANA INC"},
                "4": {"cik_str": 999401, "ticker": "GEIT", "title": "GE Infrastructure Technology LLC"},
            }
        )
        exhibit = "Exhibit 10.1 Supply Agreement with Dana Incorporated"
        self.assertEqual(parties_from_text(exhibit, "supplier", registrants), [])
        self.assertEqual(parties_from_text("Index of Exhibits. Supply agreement with Dana Incorporated.", "supplier", registrants), [])
        self.assertEqual(
            parties_from_text("The spin-off of GENERAL ELECTRIC, its former parent, was completed.", "supplier", registrants),
            [],
        )
        self.assertEqual(
            parties_from_text("The spin-off of GENERAL ELECTRIC, its former parent, was completed.", "deal_party", registrants),
            [],
        )
        self.assertIn(
            "GE Infrastructure Technology LLC",
            parties_from_text(
                "The Company entered into a supply agreement with GE Infrastructure Technology LLC.",
                "supplier",
                registrants,
            ),
        )

        start, end = search_window(SWEEP_AT)
        gev = "2088000"
        cat = "999400"
        client = _Client()
        client.pages[TICKERS_URL] = {
            "0": {"cik_str": 2088000, "ticker": "GEV", "title": "GE Vernova"},
            "1": {"cik_str": 40545, "ticker": "GE", "title": "GENERAL ELECTRIC CO"},
            "2": {"cik_str": 18230, "ticker": "CAT", "title": "Caterpillar Inc."},
            "3": {"cik_str": 26780, "ticker": "DAN", "title": "DANA INC"},
            "4": {"cik_str": 999401, "ticker": "GEIT", "title": "GE Infrastructure Technology LLC"},
        }
        gev_body = """
        <html><body>
        <p>Exhibit Index</p>
        <p>Exhibit 10.1 Supply Agreement with Dana Incorporated</p>
        <p>The Company entered into a supply agreement with GE Infrastructure Technology LLC.</p>
        <p>GENERAL ELECTRIC, the former parent, completed the spin-off.</p>
        <p>The separation from GENERAL ELECTRIC was a spin-off.</p>
        <p>The Company entered into a supply agreement with Dana Incorporated.</p>
        </body></html>
        """
        cat_body = """
        <html><body>
        <p>The Company entered into a supply agreement with Caterpillar Insurance Company.</p>
        <p>Caterpillar Insurance Company is a wholly owned subsidiary.</p>
        <p>The Company entered into a supply agreement with Dana Incorporated.</p>
        </body></html>
        """
        for cik, accession, filename, body in (
            (gev, "0002088000-26-000008", "gev-8k.htm", gev_body),
            (cat, "0000999400-26-000008", "cat-8k.htm", cat_body),
        ):
            client.pages[submissions_url(cik)] = {
                "filings": {
                    "recent": {
                        "form": ["8-K"],
                        "accessionNumber": [accession],
                        "filingDate": ["2026-08-15"],
                        "primaryDocument": [filename],
                        "items": ["8.01"],
                    }
                }
            }
            client.bodies[archive_document_url(cik, accession, filename)] = body
            client.pages[efts_url(cik, CAPEX_QUERY, start, end)] = {"hits": {"hits": []}}
        gev_lead = _lead("capex", "https://www.sec.gov/Archives/edgar/data/2088000/gev.htm", ticker="GEV")
        gev_lead.company = "GE Vernova"
        cat_lead = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999400/cat.htm", ticker="CAT")
        cat_lead.company = "Caterpillar"
        enrich_leads([gev_lead], client=client, names={}, as_of=SWEEP_AT)
        enrich_leads([cat_lead], client=client, names={}, as_of=SWEEP_AT)
        gev_names = [(party["role"], party["name"]) for party in gev_lead.counterparties]
        cat_names = [(party["role"], party["name"]) for party in cat_lead.counterparties]
        self.assertEqual(gev_names, [("supplier", "DANA INC")])
        self.assertEqual(cat_names, [("supplier", "DANA INC")])
        for party in gev_lead.counterparties + cat_lead.counterparties:
            self.assertNotIn("GE Infrastructure", party["name"])
            self.assertNotIn("GENERAL ELECTRIC", party["name"])
            self.assertNotIn("Caterpillar Insurance", party["name"])

    def test_download_cap_is_five_per_company_and_forty_total(self):
        start, end = search_window(SWEEP_AT)
        previous_total = counterparties.FTS_DOCUMENT_CAP
        previous_company = counterparties.FTS_COMPANY_CAP
        counterparties.FTS_DOCUMENT_CAP = 3
        counterparties.FTS_COMPANY_CAP = 2
        try:
            first_cik = ORCL_CIK
            second_cik = "999101"
            client = _Client()
            client.pages[TICKERS_URL] = {
                "0": {"cik_str": 999001, "ticker": "ORCL", "title": "Oracle Corporation"},
                "1": {"cik_str": 999101, "ticker": "AMZN", "title": "Amazon.com, Inc."},
                "2": {"cik_str": 26780, "ticker": "DAN", "title": "DANA INC"},
            }
            hits = []
            for cik, count in ((first_cik, 3), (second_cik, 2)):
                accession = f"0000{cik}-26-000090"
                rows = []
                for index in range(count):
                    filename = f"doc-{index}.htm"
                    url = archive_document_url(cik, accession, filename)
                    client.bodies[url] = (
                        "<p>The Company entered into a supply agreement with Dana Incorporated.</p>"
                    )
                    rows.append(
                        {"_id": f"{accession}:{filename}", "_source": {"adsh": accession, "form": "8-K"}}
                    )
                    hits.append(url)
                client.pages[submissions_url(cik)] = {
                    "filings": {
                        "recent": {
                            "form": [],
                            "accessionNumber": [],
                            "filingDate": [],
                            "primaryDocument": [],
                            "items": [],
                        }
                    }
                }
                client.pages[efts_url(cik, CAPEX_QUERY, start, end)] = {"hits": {"hits": rows}}
            first = _lead("capex", "https://www.sec.gov/Archives/edgar/data/999001/orcl.htm")
            first.company = "Oracle"
            second = _lead(
                "capex",
                "https://www.sec.gov/Archives/edgar/data/999101/amzn.htm",
                ticker="AMZN",
            )
            second.company = "Amazon.com, Inc."
            enrich_leads([first, second], client=client, names={}, as_of=SWEEP_AT)
        finally:
            counterparties.FTS_DOCUMENT_CAP = previous_total
            counterparties.FTS_COMPANY_CAP = previous_company
        first_docs = [url for url in hits[:3] if url in client.urls]
        second_docs = [url for url in hits[3:] if url in client.urls]
        self.assertEqual(first_docs, hits[:2])
        self.assertEqual(second_docs, hits[3:4])
        self.assertNotIn(hits[2], client.urls)
        self.assertNotIn(hits[4], client.urls)
        self.assertEqual([party["name"] for party in first.counterparties], ["DANA INC"])
        self.assertEqual(first.counterparties[0]["role"], "supplier")

    def test_amzn_customers_do_not_merge_as_suppliers(self):
        os.environ["SEC_CONTACT_EMAIL"] = CONTACT
        start, end = search_window(SWEEP_AT)
        amzn = "999101"
        meta = "999102"
        amzn_8k = "0000999101-26-000008"
        client = _Client(email=CONTACT)
        tickers = _tickers()
        tickers["10"] = {"cik_str": 999101, "ticker": "AMZN", "title": "Amazon.com, Inc."}
        tickers["11"] = {"cik_str": 999102, "ticker": "META", "title": "Meta Platforms, Inc."}
        tickers["12"] = {"cik_str": 999103, "ticker": "UBER", "title": "Uber Technologies, Inc."}
        client.pages[TICKERS_URL] = tickers
        client.pages[submissions_url(amzn)] = {
            "filings": {
                "recent": {
                    "form": ["8-K"],
                    "accessionNumber": [amzn_8k],
                    "filingDate": ["2026-08-15"],
                    "primaryDocument": ["amzn-8k.htm"],
                    "items": ["8.01"],
                }
            }
        }
        client.bodies[archive_document_url(amzn, amzn_8k, "amzn-8k.htm")] = (
            "<html><body>"
            "<p>Meta Platforms, Inc. and Uber Technologies, Inc. are customers.</p>"
            "<p>The Company has an agreement to deploy Meta Platforms, Inc. technology.</p>"
            "<p>Sales to Uber Technologies, Inc. grew, and revenue from Meta Platforms, Inc. grew.</p>"
            "<p>Uber Technologies, Inc. uses our storefront.</p>"
            "<p>The Company agreed to acquire Northwind Capital.</p>"
            "</body></html>"
        )
        client.pages[efts_url(amzn, CAPEX_QUERY, start, end)] = {"hits": {"hits": []}}
        client.pages[efts_url(amzn, '"Meta Platforms, Inc."', start, end)] = {"hits": {"hits": []}}
        client.pages[submissions_url(meta)] = {
            "filings": {
                "recent": {
                    "form": [],
                    "accessionNumber": [],
                    "filingDate": [],
                    "primaryDocument": [],
                    "items": [],
                }
            }
        }
        client.pages[efts_url(meta, BACKLOG_QUERY, start, end)] = {"hits": {"hits": []}}
        client.pages[efts_url(meta, '"Amazon.com, Inc."', start, end)] = {"hits": {"hits": []}}
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            conn = _history(path)
            try:
                current = _figure("AMZN", "Amazon.com, Inc.", "quarter", "2026-07-01/2026-09-30", 5_000_000_000)
                prior = _figure("AMZN", "Amazon.com, Inc.", "quarter", "2025-07-01/2025-09-30", 1_000_000_000)
                for row in (current, prior):
                    row["source_url"] = "https://www.sec.gov/Archives/edgar/data/999101/amzn.htm"
                backlog = _orders("META", "filing", "2026-06-30", 2_000_000_000, figure_type="backlog")
                backlog["company"] = "Meta Platforms, Inc."
                backlog["source_url"] = "https://www.sec.gov/Archives/edgar/data/999102/meta.htm"
                save_run(conn, "capex", "2026-10-12T14:30:52Z", [current, prior, *_caps("AMZN", "META")])
                save_run(conn, "orders-backlog", "2026-10-12T14:31:00Z", [backlog])
            finally:
                conn.close()
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
                           c.role, c.name, c.ticker AS party_ticker
                    FROM leads AS l
                    JOIN theses AS t ON t.lead_id = l.lead_id
                    LEFT JOIN lead_counterparties AS c ON c.lead_id = l.lead_id
                    """
                ).fetchall()
            finally:
                conn.close()
        self.assertFalse(any(row["source_scout"] == "legate" for row in rows))
        self.assertFalse(any("cross-company" in (row["summary"] or "") for row in rows))
        amzn_rows = [row for row in rows if row["ticker"] == "AMZN"]
        self.assertTrue(amzn_rows)
        parties = {(row["role"], row["name"], row["party_ticker"]) for row in amzn_rows if row["name"]}
        self.assertIn(("customer", "Meta Platforms, Inc.", "META"), parties)
        self.assertIn(("customer", "Uber Technologies, Inc.", "UBER"), parties)
        self.assertIn(("deal_party", "Northwind Capital", "NWT"), parties)
        self.assertFalse(any(role == "supplier" for role, _name, _ticker in parties))
        thesis = "\n".join(row["thesis_text"] or "" for row in amzn_rows)
        self.assertIn("Customers named: Meta Platforms, Inc.", thesis)
        self.assertIn("Uber Technologies, Inc.", thesis)
        self.assertIn("Suppliers named: none found", thesis)
        self.assertNotIn("Suppliers named: Meta", thesis)
        self.assertNotIn("Suppliers named: Uber", thesis)
        self.assertNotIn("Northwind", thesis)


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


def _fts_hit(adsh, filename, ciks):
    """One EDGAR full-text hit: _source metadata only, _id is adsh:filename."""
    return {
        "took": 9,
        "timed_out": False,
        "hits": {
            "total": {"value": 1, "relation": "eq"},
            "max_score": 5.0,
            "hits": [
                {
                    "_index": "edgar_file",
                    "_id": f"{adsh}:{filename}",
                    "_score": 5.0,
                    "_source": {
                        "ciks": ciks,
                        "form": "8-K",
                        "file_date": "2026-08-20",
                        "adsh": adsh,
                        "file_type": "8-K",
                        "display_names": ["Example Corp  (EX)  (CIK 0000000001)"],
                        "items": ["1.01"],
                    },
                }
            ],
        },
    }


def _tickers():
    return json.loads((FIXTURES / "company_tickers.json").read_text(encoding="utf-8"))


def _orcl_client(start, end):
    client = _Client(fail_on=GEV_CIK, email=CONTACT)
    client.pages[TICKERS_URL] = _tickers()
    client.pages[submissions_url(ORCL_CIK)] = json.loads(
        (FIXTURES / "orcl_submissions.json").read_text(encoding="utf-8")
    )
    client.pages[efts_url(ORCL_CIK, CAPEX_QUERY, start, end)] = json.loads(
        (FIXTURES / "orcl_efts.json").read_text(encoding="utf-8")
    )
    client.pages[efts_url(ORCL_CIK, '"Globex Power"', start, end)] = {"hits": {"hits": []}}
    client.bodies[archive_document_url(ORCL_CIK, ORCL_8K, "orcl-8k.htm")] = (
        FIXTURES / "orcl_8k.html"
    ).read_text(encoding="utf-8")
    client.pages[archive_document_url(ORCL_CIK, ORCL_8K, "index.json")] = {
        "directory": {
            "item": [
                {"name": "orcl-8k.htm"},
                {"name": "d1dex101.htm"},
                {"name": "ex991.htm"},
            ]
        }
    }
    client.bodies[archive_document_url(ORCL_CIK, ORCL_8K, "d1dex101.htm")] = (
        FIXTURES / "orcl_ex10.html"
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
