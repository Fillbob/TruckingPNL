from __future__ import annotations

import re
from pathlib import Path

from financial_validator_mvp.models.schemas import Transaction
from financial_validator_mvp.parsers.common import MONEY_PATTERN, collapse_spaces, parse_money, safe_parse_date
from financial_validator_mvp.parsers.pdf_utils import extract_pdf_text

DATE_PREFIX_PATTERN = re.compile(r"^(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\b")
DATE_ANY_PATTERN = re.compile(r"\b(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\b")
HEADER_HINTS = (
    "general ledger",
    "date",
    "transaction type",
    "num",
    "memo",
    "split",
    "clr",
    "balance",
    "account",
    "total",
    "beginning balance",
    "ending balance",
)
TRAILING_CATEGORY_PATTERN = re.compile(
    (
        r"(services|income|bank charges|taxes paid|cost of labor(?:\s*-\s*cos)?|"
        r"other cost(?:s)? of services(?:\s*-\s*cogs)?|supplies(?:\s*&\s*|\s+and\s+)"
        r"materials(?:\s*-\s*cogs)?|supplies|equipment rental|insurance|utilities|"
        r"legal(?:\s*&\s*|\s+and\s+)professional fees|interest paid|dues(?:\s*&\s*|\s+and\s+)subscriptions|"
        r"travel|office expense|advertising|promotional meals)\s*$"
    ),
    flags=re.IGNORECASE,
)
REFERENCE_PATTERN = re.compile(r"\b(?:check\s*#?\s*)?(?P<ref>\d{4,12})\b", flags=re.IGNORECASE)


def parse_general_ledger_pdf(
    pdf_source: str | Path | bytes,
    source_file: str = "general_ledger.pdf",
) -> list[Transaction]:
    text = extract_pdf_text(pdf_source)
    return parse_general_ledger_text(text=text, source_file=source_file)


def parse_general_ledger_text(text: str, source_file: str = "general_ledger.txt") -> list[Transaction]:
    lines = [line.rstrip() for line in text.splitlines()]
    source_account = _extract_source_account(lines)

    transactions: list[Transaction] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if _is_noise_line(line):
            continue
        if not DATE_ANY_PATTERN.search(line):
            continue

        transaction = _parse_transaction_line(line=line, source_file=source_file, source_account=source_account)
        if transaction:
            transactions.append(transaction)

    return transactions


def _parse_transaction_line(
    line: str,
    source_file: str,
    source_account: str | None,
) -> Transaction | None:
    date_match = DATE_ANY_PATTERN.search(line)
    if not date_match:
        return None
    date_value = safe_parse_date(date_match.group("date"))
    account_prefix = collapse_spaces(line[: date_match.start()]) or None
    remainder = line[date_match.end() :].strip()
    if not remainder:
        return None

    money_matches = list(MONEY_PATTERN.finditer(remainder))
    if not money_matches:
        return None

    if len(money_matches) >= 2:
        amount_match = money_matches[-2]
        balance_match = money_matches[-1]
    else:
        amount_match = money_matches[-1]
        balance_match = None

    amount = parse_money(amount_match.group(0))
    running_balance = parse_money(balance_match.group(0)) if balance_match else None

    detail_segment = remainder[: amount_match.start()].strip()
    if not detail_segment:
        return None

    columns = [collapse_spaces(part) for part in re.split(r"\s{2,}", detail_segment) if part.strip()]

    transaction_type = None
    raw_description = detail_segment
    original_category = None

    if len(columns) >= 3:
        transaction_type = columns[0]
        raw_description = columns[1]
        original_category = columns[2]
    elif len(columns) == 2:
        transaction_type = columns[0]
        raw_description = columns[1]
    elif len(columns) == 1:
        transaction_type, raw_description, original_category = _parse_single_column_detail(columns[0])

    direction = "credit" if amount >= 0 else "debit"
    reference_number = _extract_reference_number(raw_description)

    return Transaction(
        date=date_value,
        raw_description=raw_description,
        normalized_description="",
        amount=amount,
        direction=direction,
        source_account=account_prefix or source_account,
        source_file=source_file,
        original_category=original_category,
        resolved_category=None,
        confidence=0.0,
        flags=[],
        transaction_type=transaction_type,
        running_balance=running_balance,
        source_type="ledger",
        reference_number=reference_number,
    )


def _extract_source_account(lines: list[str]) -> str | None:
    for line in lines:
        lowered = line.lower()
        if "account" not in lowered:
            continue
        match = re.search(r"account(?:\s*name)?\s*[:\-]\s*(.+)", line, flags=re.IGNORECASE)
        if match:
            return collapse_spaces(match.group(1))
    return None


def _is_noise_line(line: str) -> bool:
    lowered = line.lower()
    if lowered.startswith("total "):
        return True
    if any(hint in lowered for hint in HEADER_HINTS):
        if DATE_ANY_PATTERN.search(line):
            return False
        return True
    return False


def _parse_single_column_detail(detail: str) -> tuple[str | None, str, str | None]:
    tokens = detail.split()
    if not tokens:
        return None, detail, None

    transaction_type = tokens[0]
    body = " ".join(tokens[1:]) if len(tokens) > 1 else detail
    body = collapse_spaces(body)
    body_lower = body.lower()

    category_match = TRAILING_CATEGORY_PATTERN.search(body)
    if category_match:
        category = collapse_spaces(category_match.group(1))
        description = collapse_spaces(body[: category_match.start()])
        if description:
            return transaction_type, description, category
        return transaction_type, body, category

    # Heuristic: check-number style rows should map to known ledger flow buckets.
    if re.fullmatch(r"\d{4,6}\s+PNC-\s*\d{3,4}", body, flags=re.IGNORECASE):
        return transaction_type, body, "Other Costs of Services"
    if "ach settlement payments" in body_lower:
        return transaction_type, body, "Other Costs of Services"
    if "mobile deposit" in body_lower or body_lower.startswith("deposit "):
        return transaction_type, body, "Services"

    return transaction_type, body or detail, None


def _extract_reference_number(raw_description: str) -> str | None:
    match = REFERENCE_PATTERN.search(raw_description)
    if not match:
        return None
    return match.group("ref")
