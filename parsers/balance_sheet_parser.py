from __future__ import annotations

import re
from pathlib import Path

from financial_validator_mvp.models.schemas import BalanceSheetSnapshot
from financial_validator_mvp.parsers.common import MONEY_PATTERN, collapse_spaces, parse_money, parse_statement_period
from financial_validator_mvp.parsers.pdf_utils import extract_pdf_text


def parse_balance_sheet_pdf(
    pdf_source: str | Path | bytes,
    source_file: str = "balance_sheet.pdf",
) -> BalanceSheetSnapshot:
    text = extract_pdf_text(pdf_source)
    return parse_balance_sheet_text(text=text, source_file=source_file)


def parse_balance_sheet_text(text: str, source_file: str = "balance_sheet.txt") -> BalanceSheetSnapshot:
    period = parse_statement_period(text)
    lines = [collapse_spaces(line) for line in text.splitlines() if line.strip()]

    ending_cash = _find_metric(lines, includes=("total cash",))
    if ending_cash is None:
        ending_cash = _find_metric(lines, includes=("cash and cash equivalents",))
    if ending_cash is None:
        ending_cash = _find_metric(lines, includes=("cash",), excludes=("non cash",))

    net_income = _find_metric(lines, includes=("net income",))

    return BalanceSheetSnapshot(
        period=period,
        ending_cash=ending_cash,
        net_income=net_income,
        source_file=source_file,
    )


def _find_metric(
    lines: list[str],
    includes: tuple[str, ...],
    excludes: tuple[str, ...] = (),
) -> float | None:
    for line in lines:
        lowered = line.lower()
        if any(required not in lowered for required in includes):
            continue
        if any(excluded in lowered for excluded in excludes):
            continue
        value = _extract_trailing_amount(line)
        if value is not None:
            return value
    return None


def _extract_trailing_amount(line: str) -> float | None:
    matches = list(MONEY_PATTERN.finditer(line))
    if not matches:
        return None
    last_match = matches[-1]
    if last_match.end() != len(line):
        return None
    return parse_money(last_match.group(0))

