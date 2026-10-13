"""The pipeline has no broker order-placement calls."""

import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

_CALLS = (
    re.compile(r"\bplaceOrder\s*\("),
    re.compile(r"\bplace_order\s*\("),
    re.compile(r"\bsubmit_order\s*\("),
    re.compile(r"\bsubmitOrder\s*\("),
    re.compile(r"\bcancelOrder\s*\("),
    re.compile(r"\bcancel_order\s*\("),
    re.compile(r"\bcreate_order\s*\("),
    re.compile(r"\breqOpenOrders\s*\("),
    re.compile(r"\breqAllOpenOrders\s*\("),
    re.compile(r"\bexerciseOptions\s*\("),
    re.compile(r"\bib_insync\b"),
)


class NoOrderPlacementTests(unittest.TestCase):
    def test_source_has_no_broker_order_calls(self):
        found = []
        for path in REPO.rglob("*.py"):
            if path.resolve() == Path(__file__).resolve():
                continue
            if any(part in {".git", "__pycache__"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8")
            for pattern in _CALLS:
                if pattern.search(text):
                    found.append(f"{path.relative_to(REPO)}: {pattern.pattern}")
        self.assertEqual(found, [])

    def test_pipeline_does_not_write_a_pending_decision(self):
        root = REPO / "pipeline"
        found = []
        for path in root.rglob("*.py"):
            if "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8")
            if "pending decision" in text or "INSERT INTO decisions" in text:
                found.append(str(path.relative_to(REPO)))
        self.assertEqual(found, [])
