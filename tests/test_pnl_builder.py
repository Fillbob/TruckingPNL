from __future__ import annotations

import unittest
from datetime import date

from financial_validator_mvp.models.schemas import Transaction
from financial_validator_mvp.services.pnl_builder import build_monthly_yearly_pnl


class PnlBuilderTests(unittest.TestCase):
    def test_builds_monthly_and_yearly_summary(self) -> None:
        txns = [
            Transaction(
                date=date(2025, 1, 5),
                raw_description="mobile deposit",
                normalized_description="mobile deposit",
                amount=10000.0,
                direction="credit",
                source_account="acct",
                source_file="bank.pdf",
                original_category="Deposits",
                resolved_category="Gross Trucking Income",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
            Transaction(
                date=date(2025, 1, 10),
                raw_description="pilot fuel",
                normalized_description="pilot fuel",
                amount=-3500.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category="Ach Deductions",
                resolved_category="Fuel for Hired Vehicles",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
            Transaction(
                date=date(2025, 2, 2),
                raw_description="insurance payment",
                normalized_description="insurance payment",
                amount=-1200.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category="Ach Deductions",
                resolved_category="Insurance Expense",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
        ]

        result = build_monthly_yearly_pnl(txns)
        self.assertFalse(result.monthly_detail.empty)
        self.assertFalse(result.yearly_summary.empty)

        lines = {row["line_item"]: row["amount"] for _, row in result.yearly_summary.iterrows()}
        self.assertEqual(lines["Total Income"], 10000.0)
        self.assertEqual(lines["Total COGS"], -3500.0)
        self.assertEqual(lines["Total Expenses"], -1200.0)
        self.assertEqual(lines["Net Income"], 14700.0)


if __name__ == "__main__":
    unittest.main()

