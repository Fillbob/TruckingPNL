from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

from financial_validator_mvp.models.schemas import Transaction

RULE_ORIGIN_SYSTEM = "system_rule"
RULE_ORIGIN_LEARNED = "learned_rule"
RULE_ORIGIN_MANUAL = "manual_user_selection"
RULE_ORIGIN_SOURCE = "source_category"
RULE_ORIGIN_FALLBACK = "fallback_category"
RULE_ORIGIN_KEYWORD = "keyword_non_pnl"
RULE_ORIGIN_UNCLASSIFIED = "unclassified"

NON_PNL_KEYWORDS = (
    "transfer",
    "wire between",
    "owner contribution",
    "owner draw",
    "loan",
    "line of credit",
    "capital contribution",
    "equity injection",
)
EXCLUDED_CATEGORY_OPTIONS = ["TRANSFER", "BALANCE_SHEET", "IGNORE", "NON_PNL"]
FALLBACK_CATEGORY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmobile deposit\b"), "Services"),
    (re.compile(r"\bdeposit\b"), "Services"),
    (re.compile(r"\bcredit memo\b"), "Services"),
    (re.compile(r"\bach settlement payments?\b"), "Other Costs of Services"),
    (re.compile(r"\binterest payment\b"), "Interest Paid"),
    (re.compile(r"\bpilotdraft\b"), "Supplies & Materials- COGS"),
    (re.compile(r"\bequipment rental\b"), "Equipment Rental"),
]
CATEGORY_ALIASES = {
    "other costs of services": "Other Costs of Services",
    "other costs of services-cogs": "Other Costs of Services-COGS",
    "cost of labor cos": "Cost of Labor- COS",
    "cost of labor": "Cost of Labor- COS",
    "supplies materials cogs": "Supplies & Materials- COGS",
    "supplies and materials cogs": "Supplies & Materials- COGS",
}


@dataclass(slots=True)
class ClassificationRule:
    id: str
    match_type: str
    pattern: str
    category: str
    non_pnl: bool = False
    field: str = "normalized_description"
    exclude_pattern: str = ""
    confidence: float = 0.9
    priority: int = 0
    origin: str = RULE_ORIGIN_SYSTEM


def rule_from_dict(payload: dict[str, Any]) -> ClassificationRule:
    return ClassificationRule(
        id=str(payload.get("id", "")),
        match_type=str(payload.get("match_type", "contains")).lower(),
        pattern=str(payload.get("pattern", "")),
        category=str(payload.get("category", "UNCLASSIFIED")),
        non_pnl=bool(payload.get("non_pnl", False)),
        field=str(payload.get("field", "normalized_description")),
        exclude_pattern=str(payload.get("exclude_pattern", "")),
        confidence=float(payload.get("confidence", 0.9)),
        priority=int(payload.get("priority", 0)),
        origin=str(payload.get("origin", _infer_rule_origin(str(payload.get("id", ""))))),
    )


def rule_to_dict(rule: ClassificationRule) -> dict[str, Any]:
    return {
        "id": rule.id,
        "match_type": rule.match_type,
        "pattern": rule.pattern,
        "category": rule.category,
        "non_pnl": rule.non_pnl,
        "field": rule.field,
        "exclude_pattern": rule.exclude_pattern,
        "confidence": rule.confidence,
        "priority": rule.priority,
        "origin": rule.origin,
    }


def build_exact_rule(
    normalized_description: str,
    category: str,
    non_pnl: bool,
    confidence: float = 1.0,
    priority: int = 100,
    origin: str = RULE_ORIGIN_LEARNED,
) -> ClassificationRule:
    suffix = hashlib.md5(normalized_description.encode("utf-8")).hexdigest()[:10]
    return ClassificationRule(
        id=f"learned_exact_{suffix}",
        match_type="exact",
        pattern=normalized_description,
        category=category,
        non_pnl=non_pnl,
        field="normalized_description",
        exclude_pattern="",
        confidence=confidence,
        priority=priority,
        origin=origin,
    )


def build_contains_rule(
    pattern: str,
    category: str,
    non_pnl: bool,
    exclude_pattern: str = "",
    confidence: float = 1.0,
    priority: int = 100,
    origin: str = RULE_ORIGIN_LEARNED,
) -> ClassificationRule:
    suffix = hashlib.md5(f"{pattern}|{exclude_pattern}".encode("utf-8")).hexdigest()[:10]
    return ClassificationRule(
        id=f"learned_contains_{suffix}",
        match_type="contains",
        pattern=pattern.strip().lower(),
        category=category,
        non_pnl=non_pnl,
        field="normalized_description",
        exclude_pattern=exclude_pattern.strip().lower(),
        confidence=confidence,
        priority=priority,
        origin=origin,
    )


def parse_rule_dicts(rule_dicts: list[dict[str, Any]]) -> list[ClassificationRule]:
    parsed_rules: list[ClassificationRule] = []
    for payload in rule_dicts:
        if not payload.get("pattern"):
            continue
        parsed_rules.append(rule_from_dict(payload))
    parsed_rules.sort(key=lambda item: item.priority, reverse=True)
    return parsed_rules


def classify_transactions(
    transactions: list[Transaction],
    rules: list[ClassificationRule],
) -> list[Transaction]:
    for transaction in transactions:
        classify_transaction(transaction=transaction, rules=rules)
    return transactions


def classify_transaction(
    transaction: Transaction,
    rules: list[ClassificationRule],
) -> Transaction:
    transaction.flags = [flag for flag in transaction.flags if flag not in {"unclassified", "likely_non_pnl"}]
    transaction.category_rule_id = None
    transaction.category_note = None

    for rule in rules:
        if rule_matches_transaction(rule=rule, transaction=transaction):
            transaction.resolved_category = rule.category
            transaction.confidence = rule.confidence
            transaction.category_source = rule.origin
            transaction.category_rule_id = rule.id
            if rule.non_pnl:
                _add_flag(transaction, "non_pnl")
            return transaction

    matched_keywords = [word for word in NON_PNL_KEYWORDS if word in transaction.normalized_description]
    if matched_keywords:
        transaction.resolved_category = "NON_PNL"
        transaction.confidence = 0.55
        transaction.category_source = RULE_ORIGIN_KEYWORD
        transaction.category_note = ", ".join(matched_keywords)
        _add_flag(transaction, "likely_non_pnl")
        return transaction

    if transaction.original_category:
        transaction.resolved_category = _canonicalize_category_name(transaction.original_category)
        transaction.confidence = 0.4
        transaction.category_source = RULE_ORIGIN_SOURCE
        return transaction

    fallback_category = _fallback_category_for_transaction(transaction)
    if fallback_category:
        transaction.resolved_category = fallback_category
        transaction.confidence = 0.3
        transaction.category_source = RULE_ORIGIN_FALLBACK
        _add_flag(transaction, "fallback_category")
        return transaction

    transaction.resolved_category = "UNCLASSIFIED"
    transaction.confidence = 0.0
    transaction.category_source = RULE_ORIGIN_UNCLASSIFIED
    _add_flag(transaction, "unclassified")
    return transaction


def rule_matches_transaction(rule: ClassificationRule, transaction: Transaction) -> bool:
    value = _transaction_field_value(transaction=transaction, field=rule.field)
    return match_rule_value(rule=rule, value=value)


def match_rule_value(rule: ClassificationRule, value: str) -> bool:
    if not value:
        return False
    pattern = rule.pattern.lower().strip()
    candidate = value.lower().strip()
    for exclude_pattern in _split_exclude_patterns(rule.exclude_pattern):
        if exclude_pattern in candidate:
            return False
    if rule.match_type == "exact":
        return candidate == pattern
    if rule.match_type == "contains":
        return pattern in candidate
    if rule.match_type == "regex":
        return re.search(pattern, candidate) is not None
    return False


def _transaction_field_value(transaction: Transaction, field: str) -> str:
    if field == "raw_description":
        return transaction.raw_description.lower().strip()
    if field == "original_category":
        return (transaction.original_category or "").lower().strip()
    return transaction.normalized_description.lower().strip()


def _add_flag(transaction: Transaction, flag: str) -> None:
    if flag not in transaction.flags:
        transaction.flags.append(flag)


def _fallback_category_for_transaction(transaction: Transaction) -> str | None:
    value = transaction.normalized_description.lower()
    for pattern, category in FALLBACK_CATEGORY_PATTERNS:
        if pattern.search(value):
            return category
    return None


def _canonicalize_category_name(category: str) -> str:
    value = re.sub(r"[^a-z0-9]+", " ", category.lower()).strip()
    return CATEGORY_ALIASES.get(value, category)


def build_category_suggestions(
    transaction: Transaction,
    rules: list[ClassificationRule],
    limit: int = 3,
) -> list[str]:
    suggestions: list[str] = []

    for rule in rules:
        if rule_matches_transaction(rule, transaction) and rule.category not in suggestions:
            suggestions.append(rule.category)
        if len(suggestions) >= limit:
            return suggestions

    if transaction.original_category:
        category = _canonicalize_category_name(transaction.original_category)
        if category not in suggestions:
            suggestions.append(category)

    fallback = _fallback_category_for_transaction(transaction)
    if fallback and fallback not in suggestions:
        suggestions.append(fallback)

    if any(word in transaction.normalized_description for word in NON_PNL_KEYWORDS):
        if "TRANSFER" not in suggestions:
            suggestions.append("TRANSFER")

    return suggestions[:limit]


def detect_rule_conflicts(rules: list[ClassificationRule]) -> list[dict[str, str]]:
    seen: dict[str, ClassificationRule] = {}
    conflicts: list[dict[str, str]] = []

    for rule in rules:
        key = f"{rule.field}|{rule.match_type}|{rule.pattern.lower().strip()}|{rule.exclude_pattern.lower().strip()}"
        existing = seen.get(key)
        if existing is None:
            seen[key] = rule
            continue
        if existing.category == rule.category and existing.non_pnl == rule.non_pnl:
            continue
        conflicts.append(
            {
                "field": rule.field,
                "match_type": rule.match_type,
                "pattern": rule.pattern,
                "exclude_pattern": rule.exclude_pattern,
                "existing_rule_id": existing.id,
                "existing_category": existing.category,
                "conflicting_rule_id": rule.id,
                "conflicting_category": rule.category,
            }
        )

    return conflicts


def _infer_rule_origin(rule_id: str) -> str:
    if rule_id.startswith("learned_exact_"):
        return RULE_ORIGIN_LEARNED
    return RULE_ORIGIN_SYSTEM


def _split_exclude_patterns(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace("\n", "||").replace(",", "||")
    return [item.strip().lower() for item in normalized.split("||") if item.strip()]
