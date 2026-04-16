from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from financial_validator_mvp.models.schemas import BankDailyBalancePoint, BankStatementBalanceSummary, Transaction
from financial_validator_mvp.parsers.common import collapse_spaces, derive_year_for_short_date, parse_money, parse_period_date_range
from financial_validator_mvp.parsers.pdf_utils import extract_pdf_text

SECTION_SIGN: dict[str, int] = {
    "deposits": 1,
    "atm deposits and additions": 1,
    "ach additions": 1,
    "other additions": 1,
    "checks": -1,
    "checks and substitute checks": -1,
    "debit card purchases": -1,
    "pos purchases": -1,
    "atm/misc. debit card transactions": -1,
    "service charges and fees": -1,
    "ach deductions": -1,
    "other deductions": -1,
}

IGNORED_LINES = {
    "date posted",
    "amount",
    "transaction description",
    "reference number",
    "date posted amount transaction description reference number",
    "date posted check number amount reference number",
}

STANDARD_ROW_PATTERN = re.compile(
    r"^(?P<date>\d{1,2}/\d{1,2})\s+(?P<amount>\d[\d,]*\.\d{2})\s+(?P<desc>.+)$"
)
CHECK_ROW_PATTERN = re.compile(
    r"(?P<date>\d{1,2}/\d{1,2})\s+(?P<check>\d{3,8})\s+\*?\s*(?P<amount>\d[\d,]*\.\d{2})\s+(?P<ref>\d{5,12})"
)
DAILY_BALANCE_PATTERN = re.compile(r"(?P<date>\d{1,2}/\d{1,2})\s+(?P<balance>\d[\d,]*\.\d{2})")


def parse_pnc_statement_pdf(pdf_source: str | Path | bytes, source_file: str) -> list[Transaction]:
    text = extract_pdf_text(pdf_source)
    return parse_pnc_statement_text(text=text, source_file=source_file)


def parse_pnc_statement_pdf_with_summary(
    pdf_source: str | Path | bytes,
    source_file: str,
) -> tuple[list[Transaction], BankStatementBalanceSummary | None]:
    text = extract_pdf_text(pdf_source)
    transactions = parse_pnc_statement_text(text=text, source_file=source_file)
    summary = parse_pnc_statement_balance_summary_text(text=text, source_file=source_file)
    return transactions, summary


def parse_pnc_statement_pdf_with_checks_data(
    pdf_source: str | Path | bytes,
    source_file: str,
) -> tuple[list[Transaction], BankStatementBalanceSummary | None, list[BankDailyBalancePoint]]:
    text = extract_pdf_text(pdf_source)
    transactions = parse_pnc_statement_text(text=text, source_file=source_file)
    summary = parse_pnc_statement_balance_summary_text(text=text, source_file=source_file)
    daily_balances = parse_pnc_daily_balances_text(text=text, source_file=source_file)
    return transactions, summary, daily_balances


def parse_pnc_statement_text(text: str, source_file: str) -> list[Transaction]:
    period_start, period_end, _ = parse_period_date_range(text)
    source_account = _extract_account_number(text)

    lines = [collapse_spaces(line) for line in text.splitlines() if line.strip()]
    start_idx = _find_activity_start_index(lines)
    if start_idx is None:
        return []

    transactions: list[Transaction] = []
    current_section: str | None = None

    for idx in range(start_idx, len(lines)):
        line = lines[idx]
        normalized = _normalize_section_name(line)

        if normalized in SECTION_SIGN:
            current_section = normalized
            continue
        if _is_ignored_line(line):
            continue
        if line.lower().startswith("page "):
            continue
        if line.lower().startswith("detail of services used"):
            break
        if not current_section:
            continue

        check_rows = list(CHECK_ROW_PATTERN.finditer(line))
        if check_rows:
            for row in check_rows:
                transaction_date = derive_year_for_short_date(
                    short_date=row.group("date"),
                    period_start=period_start,
                    period_end=period_end,
                )
                amount = -abs(parse_money(row.group("amount")))
                transactions.append(
                    Transaction(
                        date=transaction_date,
                        raw_description=f"Check {row.group('check')}",
                        normalized_description="",
                        amount=amount,
                        direction="debit",
                        source_account=source_account,
                        source_file=source_file,
                        original_category=current_section.title(),
                        resolved_category=None,
                        confidence=0.0,
                        flags=[],
                        transaction_type="Checks",
                        running_balance=None,
                        source_type="bank",
                        reference_number=row.group("ref"),
                    )
                )
            continue

        row_match = STANDARD_ROW_PATTERN.match(line)
        if row_match:
            transaction_date = derive_year_for_short_date(
                short_date=row_match.group("date"),
                period_start=period_start,
                period_end=period_end,
            )
            amount = parse_money(row_match.group("amount"))
            signed_amount = abs(amount) * SECTION_SIGN[current_section]
            description, reference_number = _split_reference_number(row_match.group("desc"))
            transactions.append(
                Transaction(
                    date=transaction_date,
                    raw_description=description,
                    normalized_description="",
                    amount=signed_amount,
                    direction="credit" if signed_amount >= 0 else "debit",
                    source_account=source_account,
                    source_file=source_file,
                    original_category=current_section.title(),
                    resolved_category=None,
                    confidence=0.0,
                    flags=[],
                    transaction_type=current_section.title(),
                    running_balance=None,
                    source_type="bank",
                    reference_number=reference_number,
                )
            )
            continue

        if transactions and _can_attach_to_previous(line):
            transactions[-1].raw_description = collapse_spaces(
                f"{transactions[-1].raw_description} {line}"
            )

    return transactions


def parse_pnc_statement_balance_summary_text(
    text: str,
    source_file: str,
) -> BankStatementBalanceSummary | None:
    period_start, period_end, _ = parse_period_date_range(text)

    beginning_balance: float | None = None
    additions_total: float | None = None
    deductions_total: float | None = None
    ending_balance: float | None = None

    table_amounts = _extract_balance_summary_table_amounts(text)
    if table_amounts is not None:
        beginning_balance, additions_total, deductions_total, ending_balance = table_amounts

    if beginning_balance is None:
        beginning_balance = _extract_summary_amount(
        text=text,
        label_patterns=[r"beginning\s+balance", r"previous\s+balance"],
        )
    if additions_total is None:
        additions_total = _extract_summary_amount(
        text=text,
        label_patterns=[r"deposits?\s+and\s+other\s+additions?", r"total\s+additions?"],
        )
    if deductions_total is None:
        deductions_total = _extract_summary_amount(
        text=text,
        label_patterns=[r"checks?\s+and\s+other\s+deductions?", r"total\s+deductions?"],
        )
    if ending_balance is None:
        ending_balance = _extract_summary_amount(
        text=text,
        label_patterns=[r"ending\s+balance", r"new\s+balance"],
        )

    if all(value is None for value in (beginning_balance, additions_total, deductions_total, ending_balance)):
        return None

    return BankStatementBalanceSummary(
        source_file=source_file,
        period_start=period_start,
        period_end=period_end,
        beginning_balance=beginning_balance,
        additions_total=additions_total,
        deductions_total=deductions_total,
        ending_balance=ending_balance,
    )


def parse_pnc_daily_balances_text(
    text: str,
    source_file: str,
) -> list[BankDailyBalancePoint]:
    period_start, period_end, _ = parse_period_date_range(text)
    lines = [collapse_spaces(line) for line in text.splitlines() if line.strip()]

    daily_points: list[BankDailyBalancePoint] = []
    in_daily_balance_section = False
    for line in lines:
        lowered = line.lower()
        normalized = _normalize_section_name(line)
        if _is_daily_balance_header(normalized):
            in_daily_balance_section = True
            continue
        if not in_daily_balance_section:
            continue
        if lowered.startswith("page "):
            # Daily balance tables can continue onto the next page.
            continue
        if lowered.startswith("date ledger balance"):
            continue
        if lowered.startswith("business checking with interest"):
            # Header rows may appear on continued pages.
            continue
        if lowered.startswith("primary account number"):
            continue
        if lowered.startswith("activity detail"):
            break

        for match in DAILY_BALANCE_PATTERN.finditer(line):
            day = derive_year_for_short_date(
                short_date=match.group("date"),
                period_start=period_start,
                period_end=period_end,
            )
            if day is None:
                continue
            daily_points.append(
                BankDailyBalancePoint(
                    source_file=source_file,
                    date=day,
                    ledger_balance=parse_money(match.group("balance")),
                )
            )

    unique_by_day: dict[str, BankDailyBalancePoint] = {}
    for item in daily_points:
        key = item.date.isoformat() if item.date else ""
        unique_by_day[key] = item
    ordered = sorted(unique_by_day.values(), key=lambda item: item.date or date.min)
    return ordered


def _is_daily_balance_header(normalized_line: str) -> bool:
    # Handles "Daily Balance" and "Daily Balance - continued".
    return normalized_line.startswith("daily balance")


def _find_activity_start_index(lines: list[str]) -> int | None:
    for idx, line in enumerate(lines):
        if line.lower() == "activity detail":
            return idx + 1
    return None


def _extract_account_number(text: str) -> str | None:
    match = re.search(r"account number:\s*([A-Z0-9X\-]+)", text, flags=re.IGNORECASE)
    if match:
        return collapse_spaces(match.group(1))
    return None


def _split_reference_number(description: str) -> tuple[str, str | None]:
    value = collapse_spaces(description)
    token_match = re.search(r"\b([A-Z0-9]{6,})$", value)
    if not token_match:
        return value, None

    token = token_match.group(1)
    if not any(char.isdigit() for char in token):
        return value, None
    trimmed = collapse_spaces(value[: token_match.start()])
    return (trimmed or value), token


def _normalize_section_name(line: str) -> str:
    normalized = collapse_spaces(line.lower())
    normalized = normalized.replace("\t", " ")
    normalized = re.sub(r"\s+-\s+continued$", "", normalized)
    return normalized


def _is_ignored_line(line: str) -> bool:
    lowered = line.lower().strip()
    if lowered in IGNORED_LINES:
        return True
    if lowered.startswith("business checking"):
        return True
    if lowered.startswith("for 24-hour account information"):
        return True
    if lowered.startswith("for the period"):
        return True
    if lowered.startswith("primary account number"):
        return True
    return False


def _can_attach_to_previous(line: str) -> bool:
    lowered = line.lower()
    if lowered.startswith("date ") or lowered.startswith("reference "):
        return False
    if re.match(r"^\d{1,2}/\d{1,2}\b", line):
        return False
    if re.match(r"^[A-Za-z ]+\d{1,2}/\d{1,2}", line):
        return False
    return True


def _extract_summary_amount(text: str, label_patterns: list[str]) -> float | None:
    for pattern in label_patterns:
        match = re.search(
            rf"{pattern}(?:\s+on\s+[A-Za-z0-9/, -]+)?\s+(\(?-?\$?\d[\d,]*\.\d{{2}}\)?)",
            text,
            flags=re.IGNORECASE,
        )
        if match:
            return parse_money(match.group(1))
    return None


def _extract_balance_summary_table_amounts(text: str) -> tuple[float, float, float, float] | None:
    flattened = collapse_spaces(text)
    match = re.search(
        (
            r"balance\s+summary.*?"
            r"beginning\s+balance.*?"
            r"deposits?\s+and\s+other\s+additions?.*?"
            r"checks?\s+and\s+other\s+deductions?.*?"
            r"ending\s+balance\s+"
            r"(\(?-?\$?\d[\d,]*\.\d{2}\)?)\s+"
            r"(\(?-?\$?\d[\d,]*\.\d{2}\)?)\s+"
            r"(\(?-?\$?\d[\d,]*\.\d{2}\)?)\s+"
            r"(\(?-?\$?\d[\d,]*\.\d{2}\)?)"
        ),
        flattened,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    return (
        parse_money(match.group(1)),
        parse_money(match.group(2)),
        parse_money(match.group(3)),
        parse_money(match.group(4)),
    )
