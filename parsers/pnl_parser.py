from __future__ import annotations

import re
from pathlib import Path

from financial_validator_mvp.models.schemas import PnLCategoryTotal
from financial_validator_mvp.parsers.common import MONEY_PATTERN, collapse_spaces, parse_money, parse_statement_period
from financial_validator_mvp.parsers.pdf_utils import extract_pdf_text

SECTION_ALIASES = {
    "income": "Income",
    "cost of goods sold": "COGS",
    "cogs": "COGS",
    "expenses": "Expenses",
}

SKIP_CATEGORY_PREFIXES = ("total ", "gross ", "net ")


def parse_pnl_pdf(
    pdf_source: str | Path | bytes,
    source_file: str = "profit_and_loss.pdf",
) -> list[PnLCategoryTotal]:
    text = extract_pdf_text(pdf_source)
    return parse_pnl_text(text=text, source_file=source_file)


def parse_pnl_text(text: str, source_file: str = "profit_and_loss.txt") -> list[PnLCategoryTotal]:
    period = parse_statement_period(text)
    lines = [line.rstrip() for line in text.splitlines()]

    category_totals: list[PnLCategoryTotal] = []
    section: str | None = None

    for line in lines:
        trimmed = line.strip()
        if not trimmed:
            continue

        normalized_line = collapse_spaces(trimmed)
        section_alias = SECTION_ALIASES.get(normalized_line.lower())
        if section_alias:
            section = section_alias
            continue
        if _is_noise_line(normalized_line):
            continue

        parsed = _parse_category_amount_line(normalized_line)
        if not parsed:
            continue

        category_name, amount = parsed
        if category_name.lower().startswith(SKIP_CATEGORY_PREFIXES):
            continue
        if category_name.lower() == "net income":
            continue

        category_totals.append(
            PnLCategoryTotal(
                category_name=category_name,
                amount=amount,
                period=period,
                section=section,
                source_file=source_file,
            )
        )

    return category_totals


def _parse_category_amount_line(line: str) -> tuple[str, float] | None:
    money_matches = list(MONEY_PATTERN.finditer(line))
    if not money_matches:
        return None

    last_money = money_matches[-1]
    if last_money.end() != len(line):
        return None

    category_name = collapse_spaces(line[: last_money.start()])
    if not category_name:
        return None
    amount = parse_money(last_money.group(0))
    return category_name, abs(amount)


def _is_noise_line(line: str) -> bool:
    lowered = line.lower()
    if "profit and loss" in lowered:
        return True
    if lowered.startswith("accrual basis") or lowered.startswith("cash basis"):
        return True
    if re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", line):
        return True
    if line.isdigit():
        return True
    return False

