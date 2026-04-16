from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from financial_validator_mvp.models.schemas import PnLCategoryTotal, Transaction
from financial_validator_mvp.parsers.general_ledger_parser import parse_general_ledger_text
from financial_validator_mvp.parsers.pnl_parser import parse_pnl_text
from financial_validator_mvp.services.classifier import classify_transactions, parse_rule_dicts
from financial_validator_mvp.services.normalization import normalize_transactions
from financial_validator_mvp.services.validator import validate_against_pnl

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class ValidatorTests(unittest.TestCase):
    def test_validation_flags_variance_non_pnl_and_duplicates(self) -> None:
        ledger_text = (FIXTURES_DIR / "general_ledger_sample.txt").read_text(encoding="utf-8")
        pnl_text = (FIXTURES_DIR / "pnl_sample.txt").read_text(encoding="utf-8")

        transactions = parse_general_ledger_text(ledger_text, source_file="general_ledger_sample.txt")
        pnl_totals = parse_pnl_text(pnl_text, source_file="pnl_sample.txt")

        normalize_transactions(transactions)
        classify_transactions(transactions, parse_rule_dicts([]))

        result = validate_against_pnl(
            transactions=transactions,
            pnl_totals=pnl_totals,
            variance_threshold=1.0,
            use_absolute_amounts=True,
        )

        by_category = {line.category_name: line for line in result.lines}
        self.assertEqual(by_category["Services"].status, "matched")
        self.assertEqual(by_category["Bank Charges"].status, "missing_in_ledger")
        self.assertEqual(by_category["Taxes Paid"].status, "variance")

        self.assertGreaterEqual(len(result.non_pnl_transactions), 1)
        self.assertTrue(any("likely_non_pnl" in txn.flags for txn in result.non_pnl_transactions))
        self.assertTrue(any("possible_duplicate" in txn.flags for txn in result.flagged_transactions))

    def test_validator_ignores_bank_for_category_variance(self) -> None:
        ledger_txn = Transaction(
            date=date(2025, 1, 10),
            raw_description="Client payment",
            normalized_description="client payment",
            amount=100.0,
            direction="credit",
            source_account="Business Checking",
            source_file="gl.pdf",
            original_category="Services",
            resolved_category="Services",
            confidence=1.0,
            flags=[],
            source_type="ledger",
        )
        bank_txn = Transaction(
            date=date(2025, 1, 10),
            raw_description="Client payment",
            normalized_description="client payment",
            amount=10000.0,
            direction="credit",
            source_account="Business Checking",
            source_file="bank.pdf",
            original_category="Deposits",
            resolved_category="Services",
            confidence=1.0,
            flags=[],
            source_type="bank",
        )
        pnl = [
            PnLCategoryTotal(
                category_name="Services",
                amount=100.0,
                period="2025",
                section="Income",
                source_file="pnl.pdf",
            )
        ]

        result = validate_against_pnl(
            transactions=[ledger_txn, bank_txn],
            pnl_totals=pnl,
            variance_threshold=1.0,
            use_absolute_amounts=True,
        )
        by_category = {line.category_name: line for line in result.lines}
        self.assertEqual(by_category["Services"].ledger_total, 100.0)
        self.assertEqual(by_category["Services"].status, "matched")

    def test_bank_supplemental_outputs(self) -> None:
        ledger_txn = Transaction(
            date=date(2025, 1, 5),
            raw_description="Google Workspace Subscription",
            normalized_description="google workspace subscription",
            amount=-31.43,
            direction="debit",
            source_account="Business Checking",
            source_file="gl.pdf",
            original_category="Bank Charges",
            resolved_category="Bank Charges",
            confidence=1.0,
            flags=[],
            source_type="ledger",
        )
        bank_match = Transaction(
            date=date(2025, 1, 5),
            raw_description="POS Purchase Google Workspace",
            normalized_description="pos purchase google workspace",
            amount=-31.43,
            direction="debit",
            source_account="Business Checking",
            source_file="bank.pdf",
            original_category="POS Purchases",
            resolved_category="NON_PNL",
            confidence=0.5,
            flags=["likely_non_pnl"],
            source_type="bank",
        )
        bank_unmatched = Transaction(
            date=date(2025, 1, 12),
            raw_description="Withdrawal 003236710",
            normalized_description="withdrawal",
            amount=-20000.0,
            direction="debit",
            source_account="Business Checking",
            source_file="bank.pdf",
            original_category="Other Deductions",
            resolved_category="NON_PNL",
            confidence=0.5,
            flags=["likely_non_pnl"],
            source_type="bank",
        )
        pnl = [
            PnLCategoryTotal(
                category_name="Bank Charges",
                amount=31.43,
                period="2025",
                section="Expenses",
                source_file="pnl.pdf",
            )
        ]

        result = validate_against_pnl(
            transactions=[ledger_txn, bank_match, bank_unmatched],
            pnl_totals=pnl,
            variance_threshold=1.0,
            use_absolute_amounts=True,
        )
        self.assertEqual(result.bank_vs_ledger_coverage_pct, 50.0)
        self.assertEqual(len(result.unmatched_bank_transactions), 1)
        self.assertEqual(result.unmatched_bank_transactions[0].raw_description, "Withdrawal 003236710")
        self.assertGreaterEqual(len(result.bank_transactions_flagged), 2)
        self.assertEqual(result.direct_matched_bank_transactions, 1)
        self.assertEqual(result.fallback_matched_bank_transactions, 0)
        self.assertEqual(result.aggregate_matched_bank_transactions, 0)

    def test_bank_matcher_direct_then_fallback(self) -> None:
        ledger_txn = Transaction(
            date=date(2025, 1, 10),
            raw_description="Client pmt 12345",
            normalized_description="client pmt",
            amount=500.0,
            direction="credit",
            source_account="PNC",
            source_file="gl.pdf",
            original_category="Services",
            resolved_category="Services",
            confidence=1.0,
            flags=[],
            source_type="ledger",
        )
        bank_txn = Transaction(
            date=date(2025, 1, 11),
            raw_description="Completely different text",
            normalized_description="other text",
            amount=500.0,
            direction="credit",
            source_account="PNC",
            source_file="bank.pdf",
            original_category="Deposits",
            resolved_category="Services",
            confidence=0.2,
            flags=[],
            source_type="bank",
        )
        pnl = [PnLCategoryTotal(category_name="Services", amount=500.0, period="2025", section="Income", source_file="pnl.pdf")]
        result = validate_against_pnl([ledger_txn, bank_txn], pnl, variance_threshold=1.0, use_absolute_amounts=True)
        self.assertEqual(result.fallback_matched_bank_transactions, 1)
        self.assertEqual(result.direct_matched_bank_transactions, 0)
        self.assertEqual(len(result.unmatched_bank_transactions), 0)

    def test_bank_matcher_aggregate_many_to_one(self) -> None:
        ledger_txn = Transaction(
            date=date(2025, 1, 10),
            raw_description="Summarized ACH",
            normalized_description="summarized ach",
            amount=300.0,
            direction="credit",
            source_account="PNC",
            source_file="gl.pdf",
            original_category="Services",
            resolved_category="Services",
            confidence=1.0,
            flags=[],
            source_type="ledger",
        )
        bank_a = Transaction(
            date=date(2025, 1, 10),
            raw_description="ACH item A",
            normalized_description="ach item a",
            amount=100.0,
            direction="credit",
            source_account="PNC",
            source_file="bank.pdf",
            original_category="Ach Additions",
            resolved_category="Services",
            confidence=0.2,
            flags=[],
            source_type="bank",
        )
        bank_b = Transaction(
            date=date(2025, 1, 11),
            raw_description="ACH item B",
            normalized_description="ach item b",
            amount=200.0,
            direction="credit",
            source_account="PNC",
            source_file="bank.pdf",
            original_category="Ach Additions",
            resolved_category="Services",
            confidence=0.2,
            flags=[],
            source_type="bank",
        )
        pnl = [PnLCategoryTotal(category_name="Services", amount=300.0, period="2025", section="Income", source_file="pnl.pdf")]
        result = validate_against_pnl([ledger_txn, bank_a, bank_b], pnl, variance_threshold=1.0, use_absolute_amounts=True)
        self.assertEqual(result.aggregate_matched_bank_transactions, 2)
        self.assertEqual(len(result.unmatched_bank_transactions), 0)

    def test_no_double_use_of_ledger_row_in_matching(self) -> None:
        ledger_txn = Transaction(
            date=date(2025, 1, 10),
            raw_description="Single ledger row",
            normalized_description="single ledger row",
            amount=100.0,
            direction="credit",
            source_account="PNC",
            source_file="gl.pdf",
            original_category="Services",
            resolved_category="Services",
            confidence=1.0,
            flags=[],
            source_type="ledger",
        )
        bank_a = Transaction(
            date=date(2025, 1, 10),
            raw_description="Entry one",
            normalized_description="entry one",
            amount=100.0,
            direction="credit",
            source_account="PNC",
            source_file="bank.pdf",
            original_category="Deposits",
            resolved_category="Services",
            confidence=0.2,
            flags=[],
            source_type="bank",
        )
        bank_b = Transaction(
            date=date(2025, 1, 10),
            raw_description="Entry two",
            normalized_description="entry two",
            amount=100.0,
            direction="credit",
            source_account="PNC",
            source_file="bank.pdf",
            original_category="Deposits",
            resolved_category="Services",
            confidence=0.2,
            flags=[],
            source_type="bank",
        )
        pnl = [PnLCategoryTotal(category_name="Services", amount=100.0, period="2025", section="Income", source_file="pnl.pdf")]
        result = validate_against_pnl([ledger_txn, bank_a, bank_b], pnl, variance_threshold=1.0, use_absolute_amounts=True)
        self.assertEqual(result.matched_bank_transactions, 1)
        self.assertEqual(len(result.unmatched_bank_transactions), 1)


if __name__ == "__main__":
    unittest.main()
