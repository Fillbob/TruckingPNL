from __future__ import annotations

from collections import defaultdict
from typing import Any

import pandas as pd

from financial_validator_mvp.models.schemas import Transaction
from financial_validator_mvp.services.classifier import (
    EXCLUDED_CATEGORY_OPTIONS,
    ClassificationRule,
    RULE_ORIGIN_MANUAL,
    RULE_ORIGIN_LEARNED,
    RULE_ORIGIN_SYSTEM,
    build_category_suggestions,
    detect_rule_conflicts,
    parse_rule_dicts,
)
from financial_validator_mvp.services.pnl_builder import all_trucking_categories


def build_review_rows(
    transactions: list[Transaction],
    rules: list[ClassificationRule],
    include_text: str = "",
    exclude_text: str = "",
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Transaction]] = defaultdict(list)
    include_value = include_text.strip().lower()
    exclude_value = exclude_text.strip().lower()
    for transaction in transactions:
        key = build_review_group_key(
            normalized_description=transaction.normalized_description,
            include_text=include_value,
            exclude_text=exclude_value,
        )
        if not key:
            continue
        grouped[key].append(transaction)

    rows: list[dict[str, Any]] = []
    for normalized_description, items in grouped.items():
        sample = items[0]
        suggestions = build_category_suggestions(sample, rules)
        resolved_values = {str(item.resolved_category or "UNCLASSIFIED") for item in items}
        source_values = {str(item.category_source or "unknown") for item in items}
        needs_review = any(_transaction_needs_review(item) for item in items)
        rows.append(
            {
                "normalized_description": normalized_description,
                "group_rule_type": "contains" if include_value else "exact",
                "group_pattern": include_value if include_value else normalized_description,
                "group_exclude_pattern": exclude_value,
                "occurrences": len(items),
                "net_amount": round(sum(float(item.amount) for item in items), 2),
                "current_category": resolved_values.pop() if len(resolved_values) == 1 else "MIXED",
                "category_source": source_values.pop() if len(source_values) == 1 else "mixed",
                "example_raw_description": sample.raw_description,
                "suggestion_1": suggestions[0] if len(suggestions) > 0 else "",
                "suggestion_2": suggestions[1] if len(suggestions) > 1 else "",
                "suggestion_3": suggestions[2] if len(suggestions) > 2 else "",
                "suggestions": suggestions,
                "needs_review": needs_review,
                "needs_review_label": "Yes" if needs_review else "No",
            }
        )

    rows.sort(key=lambda row: (not row["needs_review"], row["normalized_description"]))
    return rows


def update_review_state(
    pending_assignments: dict[str, dict[str, Any]],
    *,
    normalized_description: str,
    category: str,
    assignment_kind: str,
    match_type: str = "exact",
    exclude_pattern: str = "",
) -> dict[str, dict[str, Any]]:
    updated = dict(pending_assignments)
    assignment_key = f"{match_type}|{normalized_description}|{exclude_pattern.strip().lower()}"
    updated[assignment_key] = {
        "assignment_key": assignment_key,
        "normalized_description": normalized_description,
        "category": category,
        "assignment_kind": assignment_kind,
        "non_pnl": assignment_kind != "pnl",
        "origin": RULE_ORIGIN_MANUAL,
        "match_type": match_type,
        "exclude_pattern": exclude_pattern.strip().lower(),
    }
    return updated


def preview_rule_impact(
    transactions: list[Transaction],
    pending_assignments: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for assignment in pending_assignments.values():
        impacted = _find_impacted_transactions(transactions, assignment)
        rows.append(
            {
                "normalized_description": assignment["normalized_description"],
                "preview_category": assignment["category"],
                "assignment_kind": assignment["assignment_kind"],
                "match_type": assignment["match_type"],
                "exclude_pattern": assignment.get("exclude_pattern", ""),
                "impacted_transactions": len(impacted),
                "impacted_net_amount": round(sum(float(txn.amount) for txn in impacted), 2),
                "existing_categories": ", ".join(sorted({str(txn.resolved_category or "UNCLASSIFIED") for txn in impacted})),
                "existing_sources": ", ".join(sorted({str(txn.category_source or "unknown") for txn in impacted})),
            }
        )
    return pd.DataFrame(rows)


def apply_review_assignments(
    transactions: list[Transaction],
    pending_assignments: dict[str, dict[str, Any]],
) -> list[Transaction]:
    assignments = list(pending_assignments.values())
    for transaction in transactions:
        for assignment in assignments:
            if not _assignment_matches_transaction(transaction, assignment):
                continue
            transaction.resolved_category = assignment["category"]
            transaction.category_source = RULE_ORIGIN_MANUAL
            transaction.category_rule_id = None
            transaction.category_note = "preview assignment"
            transaction.confidence = 1.0
            break
    return transactions


def detect_pending_assignment_conflicts(
    pending_assignments: dict[str, dict[str, Any]],
    existing_rule_dicts: list[dict[str, Any]],
) -> list[dict[str, str]]:
    candidate_rule_dicts = list(existing_rule_dicts)
    for assignment in pending_assignments.values():
        candidate_rule_dicts.append(
            {
                "id": f"preview_{assignment['assignment_key']}",
                "match_type": assignment["match_type"],
                "pattern": assignment["normalized_description"],
                "category": assignment["category"],
                "non_pnl": assignment["non_pnl"],
                "field": "normalized_description",
                "exclude_pattern": assignment.get("exclude_pattern", ""),
                "confidence": 1.0,
                "priority": 100,
                "origin": RULE_ORIGIN_MANUAL,
            }
        )
    return detect_rule_conflicts(parse_rule_dicts(candidate_rule_dicts))


def review_category_options() -> dict[str, list[str]]:
    return {
        "pnl": all_trucking_categories(),
        "balance_transfer_ignore": EXCLUDED_CATEGORY_OPTIONS,
    }


def build_review_group_key(
    *,
    normalized_description: str,
    include_text: str = "",
    exclude_text: str = "",
) -> str:
    candidate = normalized_description.strip().lower()
    if not candidate:
        return ""
    include_value = include_text.strip().lower()
    exclude_values = _split_exclude_patterns(exclude_text)
    if include_value:
        if include_value not in candidate:
            return ""
        if any(exclude_value in candidate for exclude_value in exclude_values):
            return ""
        return include_value
    return candidate


def _assignment_matches_transaction(transaction: Transaction, assignment: dict[str, Any]) -> bool:
    candidate = transaction.normalized_description.strip().lower()
    pattern = str(assignment["normalized_description"]).strip().lower()
    exclude_patterns = _split_exclude_patterns(str(assignment.get("exclude_pattern", "")))
    if any(exclude_pattern in candidate for exclude_pattern in exclude_patterns):
        return False
    if assignment.get("match_type") == "contains":
        return pattern in candidate
    return candidate == pattern


def _find_impacted_transactions(transactions: list[Transaction], assignment: dict[str, Any]) -> list[Transaction]:
    return [txn for txn in transactions if _assignment_matches_transaction(txn, assignment)]


def find_transactions_for_review_row(
    transactions: list[Transaction],
    review_row: dict[str, Any],
) -> list[Transaction]:
    assignment = {
        "normalized_description": review_row["group_pattern"],
        "match_type": review_row["group_rule_type"],
        "exclude_pattern": review_row.get("group_exclude_pattern", ""),
    }
    return _find_impacted_transactions(transactions, assignment)


def _split_exclude_patterns(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace("\n", "||").replace(",", "||")
    return [item.strip().lower() for item in normalized.split("||") if item.strip()]


def _transaction_needs_review(transaction: Transaction) -> bool:
    source = str(transaction.category_source or "").strip().lower()
    if source in {RULE_ORIGIN_SYSTEM, RULE_ORIGIN_LEARNED, RULE_ORIGIN_MANUAL}:
        return False
    return True
