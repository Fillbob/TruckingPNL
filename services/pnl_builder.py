from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Literal

import pandas as pd

from financial_validator_mvp.models.schemas import Transaction

PnlSection = Literal["Income", "COGS", "Expenses", "Other Income", "Other Expense", "Unclassified"]

TRUCKING_PNL_SECTIONS: dict[PnlSection, list[str]] = {
    "Income": [
        "Gross Trucking Income",
        "Fuel Surcharge",
        "Other Income",
    ],
    "COGS": [
        "Fuel for Hired Vehicles",
        "Travel Expenses for Drivers",
        "Truck Maintenance Costs",
        "Sub-Contractor",
        "Cost of Goods Sold",
    ],
    "Expenses": [
        "Advertising and Promotion",
        "Automobile Expense",
        "Bank Service Charges",
        "Business Licenses and Permits",
        "Computer and Internet Expenses",
        "Insurance Expense",
        "Interest Expense",
        "Lease",
        "Meals and Entertainment",
        "Miscellaneous Expense",
        "Office Supplies",
        "Payroll Expenses",
        "Payroll Taxes",
        "Professional Fees",
        "Rent Expense",
        "Repairs and Maintenance",
        "Scale",
        "Small Tools and Equipment",
        "Telephone Expense",
        "Tires",
        "Tolls",
        "Truck/Trailer Wash",
        "Uncategorized Expenses",
        "Utilities",
    ],
    "Other Income": [
        "Other Income",
    ],
    "Other Expense": [
        "Ask My Accountant",
        "Reconciliation Discrepancies",
        "Other Expense",
    ],
    "Unclassified": ["UNCLASSIFIED"],
}

EXCLUDED_REVIEW_CATEGORIES = ["NON_PNL", "TRANSFER", "BALANCE_SHEET", "IGNORE"]

CATEGORY_TO_SECTION: dict[str, PnlSection] = {
    category: section
    for section, categories in TRUCKING_PNL_SECTIONS.items()
    for category in categories
}


@dataclass(slots=True)
class PnlBuildResult:
    monthly_detail: pd.DataFrame
    yearly_detail: pd.DataFrame
    monthly_pivot: pd.DataFrame
    yearly_summary: pd.DataFrame


def all_trucking_categories() -> list[str]:
    categories = []
    for items in TRUCKING_PNL_SECTIONS.values():
        categories.extend(items)
    # Preserve order, remove duplicates
    return list(dict.fromkeys(categories))


def infer_section_for_category(category: str | None) -> PnlSection:
    if not category:
        return "Unclassified"
    return CATEGORY_TO_SECTION.get(category, "Unclassified")


def pnl_amount_for_category(amount: float, category: str | None) -> float:
    section = infer_section_for_category(category)
    return _pnl_amount_for_section(amount, section)


def build_monthly_yearly_pnl(transactions: list[Transaction]) -> PnlBuildResult:
    monthly_rollup: dict[tuple[str, str, str], float] = defaultdict(float)
    yearly_rollup: dict[tuple[str, str], float] = defaultdict(float)

    for txn in transactions:
        if (txn.resolved_category or "").upper() in EXCLUDED_REVIEW_CATEGORIES:
            continue
        txn_date = txn.date
        month_key = _month_key(txn_date)
        category = txn.resolved_category or "UNCLASSIFIED"
        section = infer_section_for_category(category)
        amount = pnl_amount_for_category(txn.amount, category)

        monthly_rollup[(month_key, section, category)] += amount
        yearly_rollup[(section, category)] += amount

    monthly_rows = [
        {"month": month, "section": section, "category": category, "amount": round(amount, 2)}
        for (month, section, category), amount in monthly_rollup.items()
    ]
    monthly_detail = pd.DataFrame(monthly_rows)
    if monthly_detail.empty:
        monthly_detail = pd.DataFrame(columns=["month", "section", "category", "amount"])
    else:
        monthly_detail = monthly_detail.sort_values(by=["month", "section", "category"]).reset_index(drop=True)

    yearly_rows = [
        {"section": section, "category": category, "amount": round(amount, 2)}
        for (section, category), amount in yearly_rollup.items()
    ]
    yearly_detail = pd.DataFrame(yearly_rows)
    if yearly_detail.empty:
        yearly_detail = pd.DataFrame(columns=["section", "category", "amount"])
    else:
        yearly_detail = yearly_detail.sort_values(by=["section", "category"]).reset_index(drop=True)

    monthly_pivot = (
        monthly_detail.pivot_table(index=["section", "category"], columns="month", values="amount", aggfunc="sum")
        .fillna(0.0)
        .reset_index()
    )
    if monthly_pivot.empty:
        monthly_pivot = pd.DataFrame(columns=["section", "category"])

    yearly_summary = _build_yearly_summary(yearly_detail)
    return PnlBuildResult(
        monthly_detail=monthly_detail,
        yearly_detail=yearly_detail,
        monthly_pivot=monthly_pivot,
        yearly_summary=yearly_summary,
    )


def _month_key(txn_date: date | None) -> str:
    if txn_date is None:
        return "UNKNOWN"
    return f"{txn_date.year:04d}-{txn_date.month:02d}"


def _pnl_amount_for_section(amount: float, section: PnlSection) -> float:
    # Preserve signed activity for all categories so each category nets debits and credits.
    if section in {"Income", "COGS", "Expenses", "Other Income", "Other Expense", "Unclassified"}:
        return amount
    return amount


def _build_yearly_summary(yearly_detail: pd.DataFrame) -> pd.DataFrame:
    if yearly_detail.empty:
        return pd.DataFrame(columns=["line_item", "amount"])

    section_totals = yearly_detail.groupby("section", as_index=False)["amount"].sum()
    by_section = {row["section"]: float(row["amount"]) for _, row in section_totals.iterrows()}

    total_income = by_section.get("Income", 0.0)
    total_cogs = by_section.get("COGS", 0.0)
    gross_profit = total_income - total_cogs
    total_expenses = by_section.get("Expenses", 0.0)
    net_ordinary_income = gross_profit - total_expenses
    other_income = by_section.get("Other Income", 0.0)
    other_expense = by_section.get("Other Expense", 0.0)
    net_income = net_ordinary_income + other_income - other_expense
    unclassified = by_section.get("Unclassified", 0.0)

    rows = [
        {"line_item": "Total Income", "amount": round(total_income, 2)},
        {"line_item": "Total COGS", "amount": round(total_cogs, 2)},
        {"line_item": "Gross Profit", "amount": round(gross_profit, 2)},
        {"line_item": "Total Expenses", "amount": round(total_expenses, 2)},
        {"line_item": "Net Ordinary Income", "amount": round(net_ordinary_income, 2)},
        {"line_item": "Total Other Income", "amount": round(other_income, 2)},
        {"line_item": "Total Other Expense", "amount": round(other_expense, 2)},
        {"line_item": "Net Income", "amount": round(net_income, 2)},
        {"line_item": "Unclassified (needs review)", "amount": round(unclassified, 2)},
    ]
    return pd.DataFrame(rows)
