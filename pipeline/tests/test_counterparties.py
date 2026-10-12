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
    TICKERS_URL,
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
        highlight = json.loads((FIXTURES / "orcl_efts.json").read_text(encoding="utf-8"))
        snippet = highlight["hits"]["hits"][0]["highlight"]["doc_text"][0]
        customers = (FIXTURES / "customer_10k.html").read_text(encoding="utf-8")
        contract = (FIXTURES / "customer_8k.html").read_text(encoding="utf-8")
        self.assertEqual(parties_from_text(eight, "supplier", registrants), [])
        self.assertEqual(parties_from_text(tenk, "supplier", registrants), [])
        self.assertEqual(parties_from_text(snippet, "supplier", registrants), [])
        self.assertEqual(
            parties_from_text(exhibit, "supplier", registrants),
            ["Oracle Corporation", "Globex Power"],
        )
        self.assertEqual(
            parties_from_text(
                "The Company entered into an agreement with Globex Power Inc. The Board approved it.",
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
                "The Company entered into an agreement with Globex Power Inc.",
            ]
        )
        self.assertEqual(parties_from_text(blob, "supplier", registrants), ["Globex Power"])
        self.assertEqual(
            parties_from_text(blob, "customer", registrants),
            ["Globex Power", "Northwind Capital"],
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
        client.pages[efts_url(ORCL_CIK, '"GE Vernova"', start, end)] = {
            "hits": {
                "hits": [
                    {
                        "_id": f"{ORCL_8K}:orcl-8k.htm",
                        "_source": {"adsh": ORCL_8K},
                        "highlight": {
                            "doc_text": [
                                "agreement with Network. GE Vernova was named in the exhibit."
                            ]
                        },
                    }
                ]
            }
        }
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
        client.pages[efts_url(GEV_CIK, '"Oracle"', start, end)] = {
            "hits": {
                "hits": [
                    {
                        "_id": f"{gev_10k}:gev-10k.htm",
                        "_source": {"adsh": gev_10k},
                        "highlight": {"doc_text": ["agreement with During. Oracle equipment."]},
                    }
                ]
            }
        }
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
        self.assertEqual(
            [(party["role"], party["name"], party["ticker"]) for party in backlog.counterparties],
            [("customer", "Oracle Corporation", "ORCL")],
        )
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
