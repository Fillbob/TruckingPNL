from __future__ import annotations

import unittest
from pathlib import Path

from financial_validator_mvp.parsers.balance_sheet_parser import parse_balance_sheet_text
from financial_validator_mvp.parsers.bank_statement_parser_pnc import (
    parse_pnc_daily_balances_text,
    parse_pnc_statement_balance_summary_text,
    parse_pnc_statement_text,
)
from financial_validator_mvp.parsers.general_ledger_parser import parse_general_ledger_text
from financial_validator_mvp.parsers.pnl_parser import parse_pnl_text

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class ParserTests(unittest.TestCase):
    def test_general_ledger_parser_extracts_transactions(self) -> None:
        text = (FIXTURES_DIR / "general_ledger_sample.txt").read_text(encoding="utf-8")
        transactions = parse_general_ledger_text(text=text, source_file="general_ledger_sample.txt")

        self.assertEqual(len(transactions), 5)
        self.assertEqual(transactions[0].original_category, "Services")
        self.assertEqual(transactions[1].amount, -250.0)
        self.assertEqual(transactions[2].transaction_type, "Transfer")
        self.assertEqual(transactions[0].source_account, "Business Checking")
        self.assertEqual(transactions[0].source_type, "ledger")

    def test_gl_parser_handles_account_prefix_before_date(self) -> None:
        text = (FIXTURES_DIR / "general_ledger_qb_sample.txt").read_text(encoding="utf-8")
        transactions = parse_general_ledger_text(text=text, source_file="general_ledger_qb_sample.txt")

        self.assertGreaterEqual(len(transactions), 3)
        self.assertEqual(transactions[0].source_account, "PNC- 9333")
        self.assertEqual(transactions[0].date.isoformat(), "2026-01-02")
        self.assertEqual(transactions[0].amount, -286.0)

    def test_gl_parser_extracts_category_from_single_column_andi_lines(self) -> None:
        text = """
        General Ledger
        PNC- 9333 01/03/2025 Expense ACH Settlement Payments PNC- 9333 -817.00 7929875.60
        PNC- 9333 01/03/2025 Deposit Mobile Deposit PNC- 9333 4500.00 7934375.60
        """
        transactions = parse_general_ledger_text(text=text, source_file="andi_single_column.txt")
        self.assertEqual(len(transactions), 2)
        self.assertEqual(transactions[0].original_category, "Other Costs of Services")
        self.assertEqual(transactions[1].original_category, "Services")

    def test_pnl_parser_extracts_leaf_category_totals(self) -> None:
        text = (FIXTURES_DIR / "pnl_sample.txt").read_text(encoding="utf-8")
        totals = parse_pnl_text(text=text, source_file="pnl_sample.txt")
        by_category = {item.category_name: item.amount for item in totals}

        self.assertEqual(by_category["Services"], 5000.0)
        self.assertEqual(by_category["Supplies & Materials"], 250.0)
        self.assertEqual(by_category["Taxes Paid"], 300.0)
        self.assertNotIn("Total Income", by_category)
        self.assertNotIn("Net Income", by_category)

    def test_balance_sheet_parser_extracts_cash_and_net_income(self) -> None:
        text = (FIXTURES_DIR / "balance_sheet_sample.txt").read_text(encoding="utf-8")
        snapshot = parse_balance_sheet_text(text=text, source_file="balance_sheet_sample.txt")

        self.assertEqual(snapshot.ending_cash, 3150.0)
        self.assertEqual(snapshot.net_income, 4425.0)
        self.assertEqual(snapshot.source_file, "balance_sheet_sample.txt")

    def test_pnc_parser_extracts_activity_transactions(self) -> None:
        text = (FIXTURES_DIR / "bank_statement_pnc_sample.txt").read_text(encoding="utf-8")
        transactions = parse_pnc_statement_text(text=text, source_file="bank_statement_pnc_sample.txt")

        self.assertEqual(len(transactions), 5)
        self.assertEqual(transactions[0].amount, 4500.0)
        self.assertEqual(transactions[1].amount, 8765.92)
        self.assertEqual(transactions[2].amount, -350.0)
        self.assertEqual(transactions[3].amount, -4.75)
        self.assertEqual(transactions[4].amount, -15.0)
        self.assertEqual(transactions[2].reference_number, "003320205")
        self.assertEqual(transactions[0].source_type, "bank")

    def test_pnc_parser_extracts_balance_summary(self) -> None:
        text = (FIXTURES_DIR / "bank_statement_pnc_sample.txt").read_text(encoding="utf-8")
        summary = parse_pnc_statement_balance_summary_text(text=text, source_file="bank_statement_pnc_sample.txt")

        self.assertIsNotNone(summary)
        assert summary is not None
        self.assertEqual(summary.beginning_balance, 10000.0)
        self.assertEqual(summary.additions_total, 13265.92)
        self.assertEqual(summary.deductions_total, 369.75)
        self.assertEqual(summary.ending_balance, 22896.17)

    def test_pnc_parser_extracts_daily_balances(self) -> None:
        text = (FIXTURES_DIR / "bank_statement_pnc_sample.txt").read_text(encoding="utf-8")
        points = parse_pnc_daily_balances_text(text=text, source_file="bank_statement_pnc_sample.txt")

        self.assertEqual(len(points), 22)
        self.assertEqual(points[0].date.isoformat(), "2025-01-01")
        self.assertEqual(points[0].ledger_balance, 295155.56)
        self.assertEqual(points[-1].date.isoformat(), "2025-01-31")
        self.assertEqual(points[-1].ledger_balance, 349605.71)

    def test_pnc_parser_extracts_daily_balances_across_continued_pages(self) -> None:
        text = """
        Business Checking With Interest
        For the Period 05/01/2025 to 05/30/2025
        Daily Balance
        Date Ledger balance
        05/26 301,000.00
        05/27 302,000.00
        Page 3 of 21
        Business Checking With Interest Account Number: XX-XXXX-9333
        Daily Balance - continued
        Date Ledger balance
        05/28 303,000.00
        05/29 304,000.00
        05/30 305,000.00
        Page 4 of 21
        Activity Detail
        Deposits
        05/30 10.00 test 12345
        """
        points = parse_pnc_daily_balances_text(text=text, source_file="continued_daily_balance_sample.txt")
        by_date = {point.date.isoformat(): point.ledger_balance for point in points if point.date is not None}
        self.assertEqual(len(points), 5)
        self.assertEqual(by_date["2025-05-28"], 303000.0)
        self.assertEqual(by_date["2025-05-29"], 304000.0)
        self.assertEqual(by_date["2025-05-30"], 305000.0)


if __name__ == "__main__":
    unittest.main()
