"""Capex-chain and smart-money leads. The live commands never call a model."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from pipeline.fixture import run_with_fixture  # noqa: E402
from shared.pipeline_store import connect  # noqa: E402
from shared.store import save_run  # noqa: E402

SWEEP_AT = "2026-10-12T18:00:00Z"
SPENDER_URL = "https://www.sec.gov/Archives/edgar/data/1018724/hspd.htm"
SUPPLIER_URL = "https://www.sec.gov/Archives/edgar/data/2088000/nsup.htm"
FUNDING_URL = "https://www.sec.gov/Archives/edgar/data/1018724/hspd-debt.htm"
QUIET_URL = "https://www.sec.gov/Archives/edgar/data/1018725/qmll.htm"
FORM4_URL = "https://www.sec.gov/Archives/edgar/data/789019/smon.xml"
STAKE_URL = "https://www.sec.gov/Archives/edgar/data/789019/smon-13d.htm"
OTHER_URL = "https://www.sec.gov/Archives/edgar/data/789020/othr.xml"
LATE_FORM_URL = "https://www.sec.gov/Archives/edgar/data/789021/late.xml"
LATE_STAKE_URL = "https://www.sec.gov/Archives/edgar/data/789021/late-13d.htm"


class ScoutGroupTests(unittest.TestCase):
    def test_capex_backlog_and_debt_become_one_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "history.sqlite")
            queue = str(Path(tmp) / "agent_queue")
            _seed(path)
            result = run_with_fixture(path, queue_dir=queue, now=SWEEP_AT)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["merges"], 0)
            self.assertFalse((Path(queue) / result["run_id"] / "centurion_2").exists())
            self.assertFalse((Path(queue) / result["run_id"] / "scout_team").exists())
            chain_task = None
            for task_path in (Path(queue) / result["run_id"] / "centurion_1").glob("*.task.json"):
                task = json.loads(task_path.read_text(encoding="utf-8"))
                kinds = {item["lead_type"] for item in task["evidence"]["constituents"]}
                if kinds == {"capex", "orders_backlog", "debt_raise"}:
                    chain_task = task
            self.assertIsNotNone(chain_task)
            self.assertEqual(chain_task["evidence"]["pass_number"], 1)
            self.assertEqual(len(chain_task["evidence"]["constituents"]), 3)
            self.assertIn("CONSTITUENTS_BEGIN", chain_task["prompt"])
            conn = connect(path)
            try:
                chain = conn.execute(
                    """
                    SELECT lead_id, ticker, lead_type, source_scout, summary
                    FROM leads WHERE source_scout = 'capex_chain'
                    """
                ).fetchone()
                figures = conn.execute(
                    """
                    SELECT f.ticker, f.source_url
                    FROM lead_figures AS link
                    JOIN figures AS f ON f.figure_id = link.figure_id
                    WHERE link.lead_id = ?
                    """,
                    (chain["lead_id"],),
                ).fetchall()
                quiet = conn.execute(
                    "SELECT source_scout, lead_type FROM leads WHERE ticker = 'QMLL'"
                ).fetchone()
                smart = conn.execute(
                    """
                    SELECT ticker, lead_type, source_scout, summary
                    FROM leads WHERE source_scout = 'smart_money'
                    """
                ).fetchall()
                other = conn.execute(
                    "SELECT source_scout, lead_type FROM leads WHERE ticker = 'OTHR'"
                ).fetchone()
                late = conn.execute(
                    """
                    SELECT ticker, source_scout, lead_type
                    FROM leads WHERE ticker = 'LATE'
                    ORDER BY lead_type
                    """
                ).fetchall()
                chains = conn.execute("SELECT COUNT(*) FROM leads WHERE source_scout = 'capex_chain'").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(chains, 1)
        self.assertEqual(chain["ticker"], "HSPD")
        self.assertEqual(chain["lead_type"], "capex")
        self.assertIn("Spender HSPD", chain["summary"])
        self.assertIn("Supplier (backlog) NSUP", chain["summary"])
        self.assertIn("Funding HSPD", chain["summary"])
        self.assertIn(SPENDER_URL, chain["summary"])
        self.assertIn(SUPPLIER_URL, chain["summary"])
        self.assertIn(FUNDING_URL, chain["summary"])
        self.assertEqual({row["source_url"] for row in figures}, {SPENDER_URL, SUPPLIER_URL, FUNDING_URL})
        self.assertEqual({row["ticker"] for row in figures}, {"HSPD", "NSUP"})
        self.assertEqual(quiet["source_scout"], "capex")
        self.assertEqual(quiet["lead_type"], "capex")
        self.assertEqual(len(smart), 1)
        self.assertEqual(smart[0]["ticker"], "SMON")
        self.assertEqual(smart[0]["lead_type"], "insider_buying")
        self.assertIn("Form 4", smart[0]["summary"])
        self.assertIn("13D/13G", smart[0]["summary"])
        self.assertIn("SC 13D", smart[0]["summary"])
        self.assertIn(FORM4_URL, smart[0]["summary"])
        self.assertIn(STAKE_URL, smart[0]["summary"])
        self.assertEqual(other["source_scout"], "insider-buying")
        self.assertEqual(other["lead_type"], "insider_buying")
        self.assertEqual([row["source_scout"] for row in late], ["accumulation", "insider-buying"])


def _seed(path):
    conn = connect(path)
    try:
        save_run(
            conn,
            "capex",
            "2026-10-12T14:30:00Z",
            [
                _figure(
                    "HSPD",
                    "Harbor Spender",
                    "PaymentsToAcquireProductiveAssets",
                    "quarter",
                    "2026-07-01/2026-09-30",
                    5_000_000_000,
                    "5000000000 supplier: Nordic Supply",
                    SPENDER_URL,
                ),
                _figure(
                    "HSPD",
                    "Harbor Spender",
                    "PaymentsToAcquirePropertyPlantAndEquipment",
                    "quarter",
                    "2025-07-01/2025-09-30",
                    1_000_000_000,
                    "1000000000",
                    SPENDER_URL + "#prior",
                ),
                _figure(
                    "QMLL",
                    "Quiet Mills",
                    "PaymentsToAcquireProductiveAssets",
                    "quarter",
                    "2026-07-01/2026-09-30",
                    4_000_000_000,
                    "4000000000",
                    QUIET_URL,
                ),
                _figure(
                    "QMLL",
                    "Quiet Mills",
                    "PaymentsToAcquirePropertyPlantAndEquipment",
                    "quarter",
                    "2025-07-01/2025-09-30",
                    1_000_000_000,
                    "1000000000",
                    QUIET_URL + "#prior",
                ),
                *_caps("HSPD", "NSUP", "QMLL", "SMON", "OTHR", "LATE"),
            ],
        )
        save_run(
            conn,
            "orders-backlog",
            "2026-10-12T14:31:00Z",
            [
                _figure(
                    "NSUP",
                    "Nordic Supply",
                    "supplier_order",
                    "Harbor Spender",
                    "2026-10-01",
                    2_000_000_000,
                    "2000000000 buyer: Harbor Spender",
                    SUPPLIER_URL,
                )
            ],
        )
        save_run(
            conn,
            "debt-raise",
            "2026-10-12T14:32:00Z",
            [
                _figure(
                    "HSPD",
                    "Harbor Spender",
                    "facility_amount",
                    "facility",
                    "2026-10-01",
                    600_000_000,
                    "600000000 acquisition",
                    FUNDING_URL,
                )
            ],
        )
        save_run(
            conn,
            "insider-buying",
            "2026-10-12T14:33:00Z",
            [
                _figure("SMON", "Smart Money Co", "insider_purchase", "ceo", "2026-10-01|Ada", 250000, "250000 shares", FORM4_URL),
                _figure("OTHR", "Other Co", "insider_purchase", "ceo", "2026-10-01|Bea", 250000, "250000 shares", OTHER_URL),
                _figure(
                    "LATE",
                    "Late Co",
                    "insider_purchase",
                    "ceo",
                    "2026-06-24|Cara",
                    250000,
                    "250000 shares",
                    LATE_FORM_URL,
                    announced_on="2026-07-20",
                ),
            ],
        )
        save_run(
            conn,
            "accumulation",
            "2026-10-12T14:34:00Z",
            [
                _figure(
                    "SMON",
                    "Smart Money Co",
                    "substantial_holding",
                    "SC 13D",
                    "2026-09-15|0001234567-26-000010|Northwind Capital",
                    8,
                    "8%",
                    STAKE_URL,
                ),
                _figure(
                    "LATE",
                    "Late Co",
                    "substantial_holding",
                    "SC 13D",
                    "2026-10-01|0001234567-26-000011|Northwind Capital",
                    8,
                    "8%",
                    LATE_STAKE_URL,
                ),
            ],
        )
    finally:
        conn.close()


def _caps(*tickers):
    rows = []
    for ticker in tickers:
        rows.append(
            _figure(
                ticker,
                ticker,
                "EntityCommonStockSharesOutstanding",
                "shares",
                "2026-10-01",
                1_000_000_000,
                "1000000000",
                f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-shares.htm",
            )
        )
        rows.append(
            _figure(
                ticker,
                ticker,
                "share_price",
                "close",
                "2026-10-01",
                10,
                "10",
                f"https://www.sec.gov/Archives/edgar/data/1/{ticker.lower()}-price.htm",
            )
        )
    return rows


def _figure(ticker, company, figure_type, instrument, period, value, value_text, source_url, announced_on="2026-10-01"):
    return {
        "ticker": ticker,
        "company": company,
        "figure_type": figure_type,
        "instrument": instrument,
        "period": period,
        "value": value,
        "value_text": value_text,
        "currency": "USD",
        "seen_at": "2026-10-12",
        "announced_on": announced_on,
        "source_url": source_url,
        "source_type": "filing",
        "verified": 1,
    }


if __name__ == "__main__":
    unittest.main()
