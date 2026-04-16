from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
import shutil
from uuid import uuid4

from financial_validator_mvp.models.schemas import BankDailyBalancePoint, BankStatementBalanceSummary, Transaction
from financial_validator_mvp.services.output_checks import run_export_reconciliation_checks
from financial_validator_mvp.services.pnl_builder import build_monthly_yearly_pnl
from financial_validator_mvp.services.reporting import transactions_dataframe


class OutputChecksTests(unittest.TestCase):
    @staticmethod
    def _make_output_dir() -> Path:
        root = Path("financial_validator_mvp/tests/.tmp_output_checks")
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"run_{uuid4().hex}"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def test_reconciliation_checks_pass_for_valid_exports(self) -> None:
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
                amount=-1200.0,
                direction="debit",
                source_account="x1",
                source_file="bank.pdf",
                original_category="ACH Deductions",
                resolved_category="Fuel for Hired Vehicles",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
            Transaction(
                date=date(2025, 2, 3),
                raw_description="owner transfer",
                normalized_description="owner transfer",
                amount=-500.0,
                direction="debit",
                source_account="x1",
                source_file="bank.pdf",
                original_category="ACH Deductions",
                resolved_category="NON_PNL",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
        ]
        pnl = build_monthly_yearly_pnl(transactions)

        output_dir = self._make_output_dir()
        try:
            raw_df = transactions_dataframe(transactions)

            raw_csv = output_dir / "raw.csv"
            raw_xlsx = output_dir / "raw.xlsx"
            raw_df.to_csv(raw_csv, index=False)
            raw_df.to_excel(raw_xlsx, index=False)

            monthly_csv = output_dir / "monthly.csv"
            monthly_xlsx = output_dir / "monthly.xlsx"
            pnl.monthly_detail.to_csv(monthly_csv, index=False)
            pnl.monthly_detail.to_excel(monthly_xlsx, index=False)

            yearly_csv = output_dir / "yearly.csv"
            yearly_xlsx = output_dir / "yearly.xlsx"
            pnl.yearly_detail.to_csv(yearly_csv, index=False)
            pnl.yearly_detail.to_excel(yearly_xlsx, index=False)

            summary_csv = output_dir / "summary.csv"
            summary_xlsx = output_dir / "summary.xlsx"
            pnl.yearly_summary.to_csv(summary_csv, index=False)
            pnl.yearly_summary.to_excel(summary_xlsx, index=False)

            checks_df = run_export_reconciliation_checks(
                transactions=transactions,
                pnl=pnl,
                export_pairs={
                    "raw": (raw_csv, raw_xlsx),
                    "monthly": (monthly_csv, monthly_xlsx),
                    "yearly": (yearly_csv, yearly_xlsx),
                    "summary": (summary_csv, summary_xlsx),
                },
            )
            self.assertFalse(checks_df.empty)
            self.assertEqual(0, int((checks_df["status"] == "FAIL").sum()))
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_reconciliation_detects_csv_xlsx_mismatch(self) -> None:
        transactions = [
            Transaction(
                date=date(2025, 1, 5),
                raw_description="deposit",
                normalized_description="deposit",
                amount=1000.0,
                direction="credit",
                source_account="x1",
                source_file="bank.pdf",
                original_category="Deposits",
                resolved_category="Gross Trucking Income",
                confidence=0.9,
                flags=[],
                source_type="bank",
            )
        ]
        pnl = build_monthly_yearly_pnl(transactions)

        output_dir = self._make_output_dir()
        try:
            monthly_csv = output_dir / "monthly.csv"
            monthly_xlsx = output_dir / "monthly.xlsx"
            pnl.monthly_detail.to_csv(monthly_csv, index=False)

            # Deliberately write a different frame to Excel to force a mismatch.
            altered = pnl.monthly_detail.copy()
            altered.loc[:, "amount"] = altered["amount"] + 1.0
            altered.to_excel(monthly_xlsx, index=False)

            checks_df = run_export_reconciliation_checks(
                transactions=transactions,
                pnl=pnl,
                export_pairs={"monthly": (monthly_csv, monthly_xlsx)},
            )
            self.assertGreater(int((checks_df["status"] == "FAIL").sum()), 0)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_balance_summary_checks_pass_when_statement_ties_out(self) -> None:
        transactions = [
            Transaction(
                date=date(2025, 1, 5),
                raw_description="deposit",
                normalized_description="deposit",
                amount=1000.0,
                direction="credit",
                source_account="x1",
                source_file="jan_statement.pdf",
                original_category="Deposits",
                resolved_category="Gross Trucking Income",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
            Transaction(
                date=date(2025, 1, 7),
                raw_description="fuel",
                normalized_description="fuel",
                amount=-300.0,
                direction="debit",
                source_account="x1",
                source_file="jan_statement.pdf",
                original_category="ACH Deductions",
                resolved_category="Fuel for Hired Vehicles",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
        ]
        summary = BankStatementBalanceSummary(
            source_file="jan_statement.pdf",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            beginning_balance=5000.0,
            additions_total=1000.0,
            deductions_total=300.0,
            ending_balance=5700.0,
        )
        pnl = build_monthly_yearly_pnl(transactions)

        output_dir = self._make_output_dir()
        try:
            monthly_csv = output_dir / "monthly.csv"
            monthly_xlsx = output_dir / "monthly.xlsx"
            pnl.monthly_detail.to_csv(monthly_csv, index=False)
            pnl.monthly_detail.to_excel(monthly_xlsx, index=False)

            checks_df = run_export_reconciliation_checks(
                transactions=transactions,
                pnl=pnl,
                export_pairs={"monthly": (monthly_csv, monthly_xlsx)},
                balance_summaries=[summary],
            )
            balance_checks = checks_df[checks_df["check_name"].str.startswith("balance_summary_")]
            self.assertFalse(balance_checks.empty)
            self.assertEqual(0, int((balance_checks["status"] == "FAIL").sum()))
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_balance_summary_checks_fail_when_statement_does_not_tie(self) -> None:
        transactions = [
            Transaction(
                date=date(2025, 1, 5),
                raw_description="deposit",
                normalized_description="deposit",
                amount=1000.0,
                direction="credit",
                source_account="x1",
                source_file="jan_statement.pdf",
                original_category="Deposits",
                resolved_category="Gross Trucking Income",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
            Transaction(
                date=date(2025, 1, 7),
                raw_description="fuel",
                normalized_description="fuel",
                amount=-300.0,
                direction="debit",
                source_account="x1",
                source_file="jan_statement.pdf",
                original_category="ACH Deductions",
                resolved_category="Fuel for Hired Vehicles",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
        ]
        summary = BankStatementBalanceSummary(
            source_file="jan_statement.pdf",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            beginning_balance=5000.0,
            additions_total=900.0,
            deductions_total=250.0,
            ending_balance=5700.0,
        )
        pnl = build_monthly_yearly_pnl(transactions)

        output_dir = self._make_output_dir()
        try:
            monthly_csv = output_dir / "monthly.csv"
            monthly_xlsx = output_dir / "monthly.xlsx"
            pnl.monthly_detail.to_csv(monthly_csv, index=False)
            pnl.monthly_detail.to_excel(monthly_xlsx, index=False)

            checks_df = run_export_reconciliation_checks(
                transactions=transactions,
                pnl=pnl,
                export_pairs={"monthly": (monthly_csv, monthly_xlsx)},
                balance_summaries=[summary],
            )
            balance_checks = checks_df[checks_df["check_name"].str.startswith("balance_summary_")]
            self.assertGreater(int((balance_checks["status"] == "FAIL").sum()), 0)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def test_daily_balance_checks_pass_when_daily_rollforward_matches(self) -> None:
        transactions = [
            Transaction(
                date=date(2025, 1, 2),
                raw_description="deposit",
                normalized_description="deposit",
                amount=1000.0,
                direction="credit",
                source_account="x1",
                source_file="jan_statement.pdf",
                original_category="Deposits",
                resolved_category="Gross Trucking Income",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
            Transaction(
                date=date(2025, 1, 3),
                raw_description="fuel",
                normalized_description="fuel",
                amount=-250.0,
                direction="debit",
                source_account="x1",
                source_file="jan_statement.pdf",
                original_category="ACH Deductions",
                resolved_category="Fuel for Hired Vehicles",
                confidence=0.9,
                flags=[],
                source_type="bank",
            ),
        ]
        summary = BankStatementBalanceSummary(
            source_file="jan_statement.pdf",
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            beginning_balance=5000.0,
            additions_total=1000.0,
            deductions_total=250.0,
            ending_balance=5750.0,
        )
        daily_points = [
            BankDailyBalancePoint(source_file="jan_statement.pdf", date=date(2025, 1, 2), ledger_balance=6000.0),
            BankDailyBalancePoint(source_file="jan_statement.pdf", date=date(2025, 1, 3), ledger_balance=5750.0),
        ]
        pnl = build_monthly_yearly_pnl(transactions)

        output_dir = self._make_output_dir()
        try:
            monthly_csv = output_dir / "monthly.csv"
            monthly_xlsx = output_dir / "monthly.xlsx"
            pnl.monthly_detail.to_csv(monthly_csv, index=False)
            pnl.monthly_detail.to_excel(monthly_xlsx, index=False)

            checks_df = run_export_reconciliation_checks(
                transactions=transactions,
                pnl=pnl,
                export_pairs={"monthly": (monthly_csv, monthly_xlsx)},
                balance_summaries=[summary],
                daily_balances_by_file={"jan_statement.pdf": daily_points},
            )
            daily_checks = checks_df[checks_df["check_name"].str.startswith("daily_balance_")]
            self.assertFalse(daily_checks.empty)
            self.assertEqual(0, int((daily_checks["status"] == "FAIL").sum()))
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
