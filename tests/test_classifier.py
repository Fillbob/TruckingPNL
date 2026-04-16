from __future__ import annotations

import unittest
from datetime import date

from financial_validator_mvp.models.schemas import Transaction
from financial_validator_mvp.services.classifier import classify_transaction


class ClassifierTests(unittest.TestCase):
    def test_classifier_fallback_maps_common_andi_descriptions(self) -> None:
        txn = Transaction(
            date=date(2025, 1, 3),
            raw_description="Mobile Deposit PNC- 9333",
            normalized_description="mobile deposit pnc- 9333",
            amount=4500.0,
            direction="credit",
            source_account="PNC- 9333",
            source_file="gl.pdf",
            original_category=None,
            resolved_category=None,
            confidence=0.0,
            flags=[],
            source_type="ledger",
        )
        classify_transaction(txn, rules=[])
        self.assertEqual(txn.resolved_category, "Services")
        self.assertIn("fallback_category", txn.flags)


if __name__ == "__main__":
    unittest.main()

