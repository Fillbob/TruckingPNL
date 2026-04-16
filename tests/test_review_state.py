from __future__ import annotations

import unittest
from datetime import date

from financial_validator_mvp.models.schemas import Transaction
from financial_validator_mvp.services.classifier import (
    ClassificationRule,
    RULE_ORIGIN_MANUAL,
    RULE_ORIGIN_SYSTEM,
    detect_rule_conflicts,
    match_rule_value,
    rule_matches_transaction,
)
from financial_validator_mvp.services.normalization import normalize_description
from financial_validator_mvp.services.review_state import (
    apply_review_assignments,
    build_review_group_key,
    build_review_rows,
    detect_pending_assignment_conflicts,
    find_transactions_for_review_row,
    preview_rule_impact,
    update_review_state,
)


class ReviewStateTests(unittest.TestCase):
    def test_description_normalization_is_stable(self) -> None:
        normalized = normalize_description("ACH Debit REF: 123456 PILOTDRAFT 998877")
        self.assertEqual(normalized, "ach pilotdraft")

    def test_rule_matching_supports_exact_and_contains(self) -> None:
        transaction = Transaction(
            date=date(2025, 1, 2),
            raw_description="Mobile Deposit 12345",
            normalized_description="mobile deposit",
            amount=100.0,
            direction="credit",
            source_account="acct",
            source_file="bank.pdf",
            original_category=None,
            resolved_category=None,
            confidence=0.0,
        )
        exact_rule = ClassificationRule(
            id="r1",
            match_type="exact",
            pattern="mobile deposit",
            category="Gross Trucking Income",
            origin=RULE_ORIGIN_SYSTEM,
        )
        contains_rule = ClassificationRule(
            id="r2",
            match_type="contains",
            pattern="deposit",
            category="Gross Trucking Income",
            origin=RULE_ORIGIN_SYSTEM,
        )
        self.assertTrue(rule_matches_transaction(exact_rule, transaction))
        self.assertTrue(rule_matches_transaction(contains_rule, transaction))
        self.assertTrue(match_rule_value(contains_rule, "mobile deposit"))

    def test_conflict_detection_finds_same_pattern_different_category(self) -> None:
        rules = [
            ClassificationRule(
                id="rule_a",
                match_type="exact",
                pattern="pilot",
                category="Fuel for Hired Vehicles",
                origin=RULE_ORIGIN_SYSTEM,
            ),
            ClassificationRule(
                id="rule_b",
                match_type="exact",
                pattern="pilot",
                category="Truck Maintenance Costs",
                origin=RULE_ORIGIN_MANUAL,
            ),
        ]
        conflicts = detect_rule_conflicts(rules)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["pattern"], "pilot")

    def test_transaction_review_state_updates_and_preview(self) -> None:
        transactions = [
            Transaction(
                date=date(2025, 1, 2),
                raw_description="Pilot Fuel",
                normalized_description="pilot fuel",
                amount=-100.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category=None,
                resolved_category="UNCLASSIFIED",
                confidence=0.0,
                category_source="unclassified",
            ),
            Transaction(
                date=date(2025, 1, 3),
                raw_description="Pilot Fuel",
                normalized_description="pilot fuel",
                amount=-200.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category=None,
                resolved_category="UNCLASSIFIED",
                confidence=0.0,
                category_source="unclassified",
            ),
        ]
        pending = update_review_state(
            {},
            normalized_description="pilot fuel",
            category="Fuel for Hired Vehicles",
            assignment_kind="pnl",
        )
        preview = preview_rule_impact(transactions, pending)
        self.assertEqual(int(preview.iloc[0]["impacted_transactions"]), 2)
        self.assertEqual(float(preview.iloc[0]["impacted_net_amount"]), -300.0)

        apply_review_assignments(transactions, pending)
        self.assertEqual(transactions[0].resolved_category, "Fuel for Hired Vehicles")
        self.assertEqual(transactions[0].category_source, RULE_ORIGIN_MANUAL)

    def test_build_review_rows_and_pending_conflicts(self) -> None:
        transactions = [
            Transaction(
                date=date(2025, 1, 2),
                raw_description="Transfer To Savings",
                normalized_description="transfer to savings",
                amount=-100.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category=None,
                resolved_category="UNCLASSIFIED",
                confidence=0.0,
                category_source="unclassified",
            )
        ]
        rules = [
            ClassificationRule(
                id="base_transfer",
                match_type="contains",
                pattern="transfer",
                category="TRANSFER",
                origin=RULE_ORIGIN_SYSTEM,
            )
        ]
        rows = build_review_rows(transactions, rules)
        self.assertEqual(rows[0]["suggestion_1"], "TRANSFER")

        pending = update_review_state(
            {},
            normalized_description="transfer to savings",
            category="IGNORE",
            assignment_kind="balance_transfer_ignore",
        )
        conflicts = detect_pending_assignment_conflicts(
            pending,
            [
                {
                    "id": "existing_rule",
                    "match_type": "exact",
                    "pattern": "transfer to savings",
                    "category": "TRANSFER",
                    "non_pnl": True,
                    "field": "normalized_description",
                    "origin": RULE_ORIGIN_SYSTEM,
                }
            ],
        )
        self.assertEqual(len(conflicts), 1)

    def test_include_exclude_group_key_and_contains_assignment(self) -> None:
        self.assertEqual(
            build_review_group_key(
                normalized_description="5571 debit card purchase sunpass acc123",
                include_text="sunpass",
                exclude_text="",
            ),
            "sunpass",
        )
        self.assertEqual(
            build_review_group_key(
                normalized_description="5571 debit card purchase sunpass refund",
                include_text="sunpass",
                exclude_text="refund",
            ),
            "",
        )

        transactions = [
            Transaction(
                date=date(2025, 1, 2),
                raw_description="Sunpass Toll 1",
                normalized_description="5571 debit card purchase sunpass acc1",
                amount=-10.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category=None,
                resolved_category="UNCLASSIFIED",
                confidence=0.0,
                category_source="unclassified",
            ),
            Transaction(
                date=date(2025, 1, 3),
                raw_description="Sunpass Toll 2",
                normalized_description="5571 debit card purchase sunpass acc2",
                amount=-15.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category=None,
                resolved_category="UNCLASSIFIED",
                confidence=0.0,
                category_source="unclassified",
            ),
            Transaction(
                date=date(2025, 1, 4),
                raw_description="Wayfair",
                normalized_description="5571 debit card purchase wayfair",
                amount=-20.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category=None,
                resolved_category="UNCLASSIFIED",
                confidence=0.0,
                category_source="unclassified",
            ),
        ]

        rows = build_review_rows(transactions, [], include_text="sunpass")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["occurrences"], 2)
        self.assertEqual(rows[0]["group_rule_type"], "contains")

        impacted = find_transactions_for_review_row(transactions, rows[0])
        self.assertEqual(len(impacted), 2)

        pending = update_review_state(
            {},
            normalized_description="sunpass",
            category="Tolls",
            assignment_kind="pnl",
            match_type="contains",
        )
        preview = preview_rule_impact(transactions, pending)
        self.assertEqual(int(preview.iloc[0]["impacted_transactions"]), 2)

        apply_review_assignments(transactions, pending)
        self.assertEqual(transactions[0].resolved_category, "Tolls")
        self.assertEqual(transactions[1].resolved_category, "Tolls")
        self.assertEqual(transactions[2].resolved_category, "UNCLASSIFIED")

    def test_queue_includes_non_rule_classified_rows(self) -> None:
        transactions = [
            Transaction(
                date=date(2025, 1, 2),
                raw_description="ACH item",
                normalized_description="ach item",
                amount=-100.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category="Ach Deductions",
                resolved_category="Ach Deductions",
                confidence=0.4,
                category_source="source_category",
            ),
            Transaction(
                date=date(2025, 1, 3),
                raw_description="Fuel item",
                normalized_description="fuel item",
                amount=-25.0,
                direction="debit",
                source_account="acct",
                source_file="bank.pdf",
                original_category=None,
                resolved_category="Fuel for Hired Vehicles",
                confidence=0.9,
                category_source="learned_rule",
            ),
        ]
        rows = build_review_rows(transactions, [])
        by_desc = {row["normalized_description"]: row for row in rows}
        self.assertTrue(by_desc["ach item"]["needs_review"])
        self.assertFalse(by_desc["fuel item"]["needs_review"])


if __name__ == "__main__":
    unittest.main()
