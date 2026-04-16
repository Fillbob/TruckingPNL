from __future__ import annotations

import re

from financial_validator_mvp.models.schemas import Transaction

ID_PATTERNS = (
    r"\b(?:ref|reference|txn|transaction|trace|confirmation|conf|id)\s*[:#-]?\s*[a-z0-9-]{4,}\b",
    r"\b(?:auth|approval)\s*[:#-]?\s*[a-z0-9-]{4,}\b",
)

VENDOR_STANDARDIZATIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bonline\s+transfer\b"), "transfer"),
    (re.compile(r"\bwire\s+transfer\b"), "transfer"),
    (re.compile(r"\bpos\s+debit\b"), "debit"),
    (re.compile(r"\bach\s+debit\b"), "ach"),
]


def normalize_description(raw: str) -> str:
    value = raw.lower().strip()

    for pattern in ID_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)

    # Strip long purely numeric tokens that are usually reference IDs.
    value = re.sub(r"\b\d{5,}\b", " ", value)
    value = re.sub(r"\bcheck\s*#?\s*\d+\b", "check", value, flags=re.IGNORECASE)

    for pattern, replacement in VENDOR_STANDARDIZATIONS:
        value = pattern.sub(replacement, value)

    value = re.sub(r"[^a-z0-9&/\- ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def normalize_category_name(category: str) -> str:
    value = category.lower().strip()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def normalize_for_matching(raw: str) -> str:
    value = raw.lower().strip()
    value = re.sub(r"\bcheck\s*#?\s*(\d+)\b", r"check \1", value, flags=re.IGNORECASE)
    value = re.sub(r"[^a-z0-9 ]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def build_matching_tokens(raw: str) -> set[str]:
    value = normalize_for_matching(raw)
    tokens = set()
    for token in value.split(" "):
        if len(token) >= 4:
            tokens.add(token)
            continue
        if token.isdigit() and len(token) >= 3:
            tokens.add(token)
    return tokens


def normalize_transactions(transactions: list[Transaction]) -> list[Transaction]:
    for transaction in transactions:
        transaction.normalized_description = normalize_description(transaction.raw_description)
    return transactions
