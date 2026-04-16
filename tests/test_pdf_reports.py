from __future__ import annotations

import importlib.util
import shutil
import unittest
from datetime import date
from pathlib import Path
from uuid import uuid4

from financial_validator_mvp.models.schemas import BankStatementBalanceSummary, Transaction
from financial_validator_mvp.services.pdf_reports import generate_balance_sheet_pdf, generate_profit_and_loss_pdf
from financial_validator_mvp.services.pnl_builder import build_monthly_yearly_pnl


@unittest.skipIf(importlib.util.find_spec("reportlab") is None, "reportlab not installed")
class PdfReportsTests(unittest.TestCase):
    @staticmethod
    def _make_output_dir() -> Path:
        root = Path("financial_validator_mvp/tests/.tmp_pdf_reports")
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"run_{uuid4().hex}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def test_pdf_generation_creates_files(self) -> None:
        transactions = [
            Transaction(
                date=date(2025, 1, 5),
                raw_description="deposit",
                normalized_description="deposit",
                amount=5000.0,
                direction="credit",
                source_account="x1",
                source_file="bank.pdf",
                original_category="Deposits",
                resolved_category="Gross Trucking Income",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
            Transaction(
                date=date(2025, 1, 8),
                raw_description="fuel",
                normalized_description="fuel",
                amount=-1000.0,
                direction="debit",
                source_account="x1",
                source_file="bank.pdf",
                original_category="ACH Deductions",
                resolved_category="Fuel for Hired Vehicles",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
        ]
        summaries = [
            BankStatementBalanceSummary(
                source_file="bank.pdf",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                beginning_balance=10000.0,
                additions_total=5000.0,
                deductions_total=1000.0,
                ending_balance=14000.0,
            )
        ]
        pnl = build_monthly_yearly_pnl(transactions)

        output_dir = self._make_output_dir()
        try:
            pnl_pdf = output_dir / "pnl.pdf"
            bs_pdf = output_dir / "balance.pdf"

            generate_profit_and_loss_pdf(
                pnl=pnl,
                output_path=pnl_pdf,
                company_name="ANDI GROUP TRUCKING INC",
                period_label="For the period 01/01/2025 to 01/31/2025",
            )
            generate_balance_sheet_pdf(
                pnl=pnl,
                balance_summaries=summaries,
                output_path=bs_pdf,
                company_name="ANDI GROUP TRUCKING INC",
                as_of_label="01/31/2025",
            )

            self.assertTrue(pnl_pdf.exists())
            self.assertTrue(bs_pdf.exists())
            self.assertGreater(pnl_pdf.stat().st_size, 0)
            self.assertGreater(bs_pdf.stat().st_size, 0)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
