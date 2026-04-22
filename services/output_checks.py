from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from financial_validator_mvp.models.schemas import BankDailyBalancePoint, BankStatementBalanceSummary, Transaction
from financial_validator_mvp.services.pnl_builder import PnlBuildResult, pnl_amount_for_category

MONEY_TOLERANCE = 0.01


@dataclass(slots=True)
class ReconciliationCheck:
    check_name: str
    status: str
    expected: str
    actual: str
    difference: float
    tolerance: float
    details: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "check_name": self.check_name,
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
            "difference": round(self.difference, 4),
            "tolerance": self.tolerance,
            "details": self.details,
        }


def run_export_reconciliation_checks(
    transactions: list[Transaction],
    pnl: PnlBuildResult,
    export_pairs: dict[str, tuple[Path, Path]],
    balance_summaries: list[BankStatementBalanceSummary] | None = None,
    daily_balances_by_file: dict[str, list[BankDailyBalancePoint]] | None = None,
    tolerance: float = MONEY_TOLERANCE,
) -> pd.DataFrame:
    checks = _build_pre_export_checks(
        transactions=transactions,
        pnl=pnl,
        balance_summaries=balance_summaries,
        daily_balances_by_file=daily_balances_by_file,
        tolerance=tolerance,
    )
    checks.extend(_build_file_parity_checks(export_pairs))
    return pd.DataFrame([check.as_dict() for check in checks])


def run_pre_export_reconciliation_checks(
    transactions: list[Transaction],
    pnl: PnlBuildResult,
    balance_summaries: list[BankStatementBalanceSummary] | None = None,
    daily_balances_by_file: dict[str, list[BankDailyBalancePoint]] | None = None,
    tolerance: float = MONEY_TOLERANCE,
) -> pd.DataFrame:
    checks = _build_pre_export_checks(
        transactions=transactions,
        pnl=pnl,
        balance_summaries=balance_summaries,
        daily_balances_by_file=daily_balances_by_file,
        tolerance=tolerance,
    )
    return pd.DataFrame([check.as_dict() for check in checks])


def run_daily_balance_reconciliation_checks(
    transactions: list[Transaction],
    balance_summaries: list[BankStatementBalanceSummary] | None = None,
    daily_balances_by_file: dict[str, list[BankDailyBalancePoint]] | None = None,
    tolerance: float = MONEY_TOLERANCE,
) -> pd.DataFrame:
    checks = _build_daily_balance_checks(
        transactions=transactions,
        summaries=balance_summaries or [],
        daily_balances_by_file=daily_balances_by_file or {},
        tolerance=tolerance,
    )
    return pd.DataFrame([check.as_dict() for check in checks])


def _build_pre_export_checks(
    transactions: list[Transaction],
    pnl: PnlBuildResult,
    balance_summaries: list[BankStatementBalanceSummary] | None = None,
    daily_balances_by_file: dict[str, list[BankDailyBalancePoint]] | None = None,
    tolerance: float = MONEY_TOLERANCE,
) -> list[ReconciliationCheck]:
    checks: list[ReconciliationCheck] = []
    summaries = balance_summaries or []
    checks.extend(_build_pnl_arithmetic_checks(transactions, pnl, tolerance=tolerance))
    checks.extend(_build_balance_summary_checks(transactions, summaries, tolerance=tolerance))
    checks.extend(
        _build_daily_balance_checks(
            transactions=transactions,
            summaries=summaries,
            daily_balances_by_file=daily_balances_by_file or {},
            tolerance=tolerance,
        )
    )
    return checks


def _build_pnl_arithmetic_checks(
    transactions: list[Transaction],
    pnl: PnlBuildResult,
    tolerance: float = MONEY_TOLERANCE,
) -> list[ReconciliationCheck]:
    checks: list[ReconciliationCheck] = []
    monthly_total = float(pnl.monthly_detail["amount"].sum()) if not pnl.monthly_detail.empty else 0.0
    yearly_total = float(pnl.yearly_detail["amount"].sum()) if not pnl.yearly_detail.empty else 0.0
    checks.append(
        _money_check(
            "monthly_total_equals_yearly_total",
            expected=yearly_total,
            actual=monthly_total,
            details="Sum(monthly_pnl_detail.amount) must equal Sum(yearly_pnl_detail.amount).",
            tolerance=tolerance,
        )
    )

    monthly_by_group = (
        pnl.monthly_detail.groupby(["section", "category"], as_index=False)["amount"].sum()
        if not pnl.monthly_detail.empty
        else pd.DataFrame(columns=["section", "category", "amount"])
    )
    yearly_by_group = (
        pnl.yearly_detail.groupby(["section", "category"], as_index=False)["amount"].sum()
        if not pnl.yearly_detail.empty
        else pd.DataFrame(columns=["section", "category", "amount"])
    )
    merged = monthly_by_group.merge(
        yearly_by_group,
        how="outer",
        on=["section", "category"],
        suffixes=("_monthly", "_yearly"),
    ).fillna(0.0)
    max_group_delta = 0.0
    if not merged.empty:
        max_group_delta = float((merged["amount_monthly"] - merged["amount_yearly"]).abs().max())
    checks.append(
        _money_check(
            "monthly_vs_yearly_group_consistency",
            expected=0.0,
            actual=max_group_delta,
            details="Each (section, category) monthly rollup must tie to yearly detail.",
            tolerance=tolerance,
        )
    )

    summary_map = _summary_map(pnl.yearly_summary)
    checks.append(
        _money_check(
            "summary_assets_consistency",
            expected=float(
                pnl.yearly_detail.loc[pnl.yearly_detail["section"] == "Assets", "amount"].sum()
            ) if not pnl.yearly_detail.empty else 0.0,
            actual=summary_map["Total Assets"],
            details="Summary assets line must equal yearly detail assets section total.",
            tolerance=tolerance,
        )
    )
    checks.append(
        _money_check(
            "summary_gross_profit_formula",
            expected=summary_map["Total Income"] + summary_map["Total COGS"],
            actual=summary_map["Gross Profit"],
            details="Gross Profit = Total Income + Total COGS (signed section totals).",
            tolerance=tolerance,
        )
    )
    checks.append(
        _money_check(
            "summary_net_ordinary_income_formula",
            expected=summary_map["Gross Profit"] + summary_map["Total Expenses"],
            actual=summary_map["Net Ordinary Income"],
            details="Net Ordinary Income = Gross Profit + Total Expenses (signed section totals).",
            tolerance=tolerance,
        )
    )
    checks.append(
        _money_check(
            "summary_net_income_formula",
            expected=summary_map["Net Ordinary Income"] + summary_map["Total Other Income"] + summary_map["Total Other Expense"],
            actual=summary_map["Net Income"],
            details="Net Income = Net Ordinary Income + Total Other Income + Total Other Expense (signed section totals).",
            tolerance=tolerance,
        )
    )

    unclassified_total = float(
        pnl.yearly_detail.loc[pnl.yearly_detail["section"] == "Unclassified", "amount"].sum()
    ) if not pnl.yearly_detail.empty else 0.0
    checks.append(
        _money_check(
            "summary_unclassified_consistency",
            expected=unclassified_total,
            actual=summary_map["Unclassified (needs review)"],
            details="Summary unclassified line must equal yearly detail unclassified section total.",
            tolerance=tolerance,
        )
    )

    rebuilt_total = 0.0
    for txn in transactions:
        if (txn.resolved_category or "").upper() == "NON_PNL":
            continue
        rebuilt_total += pnl_amount_for_category(txn.amount, txn.resolved_category or "UNCLASSIFIED")
    checks.append(
        _money_check(
            "transaction_rollup_equals_yearly_total",
            expected=yearly_total,
            actual=rebuilt_total,
            details="Recomputed P&L total from transactions must equal yearly detail total.",
            tolerance=tolerance,
        )
    )

    return checks


def _build_file_parity_checks(export_pairs: dict[str, tuple[Path, Path]]) -> list[ReconciliationCheck]:
    checks: list[ReconciliationCheck] = []
    for name, (csv_path, xlsx_path) in export_pairs.items():
        csv_df = pd.read_csv(csv_path)
        xlsx_df = pd.read_excel(xlsx_path)
        checks.append(
            _row_count_check(
                f"{name}_csv_xlsx_row_count",
                expected=int(len(csv_df)),
                actual=int(len(xlsx_df)),
            )
        )
        checks.append(
            _record_equality_check(
                f"{name}_csv_xlsx_record_match",
                csv_df=csv_df,
                xlsx_df=xlsx_df,
            )
        )
    return checks


def _build_balance_summary_checks(
    transactions: list[Transaction],
    summaries: list[BankStatementBalanceSummary],
    tolerance: float = MONEY_TOLERANCE,
) -> list[ReconciliationCheck]:
    checks: list[ReconciliationCheck] = []
    if not summaries:
        return checks

    for summary in summaries:
        file_txns = [txn for txn in transactions if txn.source_file == summary.source_file]
        tx_net_change = sum(float(txn.amount) for txn in file_txns)
        tx_additions = sum(float(txn.amount) for txn in file_txns if txn.amount > 0)
        tx_deductions = sum(abs(float(txn.amount)) for txn in file_txns if txn.amount < 0)

        if summary.beginning_balance is not None and summary.ending_balance is not None:
            expected = summary.ending_balance - summary.beginning_balance
            checks.append(
                _money_check(
                    f"balance_summary_net_change_match__{summary.source_file}",
                    expected=expected,
                    actual=tx_net_change,
                    details=f"{summary.source_file}: Statement ending-beginning must match parsed transaction net.",
                    tolerance=tolerance,
                )
            )

        if (
            summary.beginning_balance is not None
            and summary.additions_total is not None
            and summary.deductions_total is not None
            and summary.ending_balance is not None
        ):
            expected_ending = summary.beginning_balance + summary.additions_total - summary.deductions_total
            checks.append(
                _money_check(
                    f"balance_summary_equation__{summary.source_file}",
                    expected=summary.ending_balance,
                    actual=expected_ending,
                    details=f"{summary.source_file}: Beginning + Additions - Deductions must equal Ending.",
                    tolerance=tolerance,
                )
            )

        if summary.additions_total is not None:
            checks.append(
                _money_check(
                    f"balance_summary_additions_match__{summary.source_file}",
                    expected=summary.additions_total,
                    actual=tx_additions,
                    details=f"{summary.source_file}: Parsed positive transaction sum must match statement additions.",
                    tolerance=tolerance,
                )
            )

        if summary.deductions_total is not None:
            checks.append(
                _money_check(
                    f"balance_summary_deductions_match__{summary.source_file}",
                    expected=summary.deductions_total,
                    actual=tx_deductions,
                    details=f"{summary.source_file}: Parsed negative transaction sum must match statement deductions.",
                    tolerance=tolerance,
                )
            )

    return checks


def build_daily_rollforward_frame(
    transactions: list[Transaction],
    daily_balances: list[BankDailyBalancePoint],
    summary: BankStatementBalanceSummary | None,
) -> pd.DataFrame:
    if not daily_balances:
        return pd.DataFrame(
            columns=[
                "date",
                "tx_count",
                "tx_net_change",
                "computed_ending_balance",
                "statement_ending_balance",
                "difference",
                "status",
                "note",
            ]
        )

    tx_by_day: dict[str, dict[str, float | int]] = {}
    for txn in transactions:
        if txn.date is None:
            continue
        key = txn.date.isoformat()
        bucket = tx_by_day.setdefault(key, {"net": 0.0, "count": 0})
        bucket["net"] = float(bucket["net"]) + float(txn.amount)
        bucket["count"] = int(bucket["count"]) + 1

    ordered = sorted(daily_balances, key=lambda item: item.date or pd.Timestamp.min.date())
    rows: list[dict[str, Any]] = []
    opening_balance: float | None = summary.beginning_balance if summary else None
    previous_statement_balance: float | None = None

    for point in ordered:
        key = point.date.isoformat() if point.date else ""
        day_bucket = tx_by_day.get(key, {"net": 0.0, "count": 0})
        tx_net = float(day_bucket["net"])
        tx_count = int(day_bucket["count"])

        if previous_statement_balance is not None:
            day_opening = previous_statement_balance
        else:
            day_opening = opening_balance
            if day_opening is None:
                day_opening = point.ledger_balance - tx_net

        computed_ending = (day_opening + tx_net) if day_opening is not None else None
        diff = (computed_ending - point.ledger_balance) if computed_ending is not None else None
        status = "PASS" if diff is not None and abs(diff) <= MONEY_TOLERANCE else "FAIL"

        rows.append(
            {
                "date": key,
                "tx_count": tx_count,
                "tx_net_change": round(tx_net, 2),
                "computed_ending_balance": round(computed_ending, 2) if computed_ending is not None else None,
                "statement_ending_balance": round(point.ledger_balance, 2),
                "difference": round(diff, 2) if diff is not None else None,
                "status": status,
                "note": "Computed from statement beginning and parsed line-item totals by posting date.",
            }
        )

        previous_statement_balance = point.ledger_balance

    return pd.DataFrame(rows)


def annotate_line_items_with_daily_checks(
    line_items: pd.DataFrame,
    rollforward_frame: pd.DataFrame,
) -> pd.DataFrame:
    if line_items.empty:
        return line_items
    if rollforward_frame.empty:
        output = line_items.copy()
        output["daily_tx_net_change"] = None
        output["daily_statement_ending_balance"] = None
        output["daily_computed_ending_balance"] = None
        output["daily_difference"] = None
        output["daily_status"] = "MISSING_DAILY_BALANCE"
        return output

    mapping = {
        str(row["date"]): {
            "daily_tx_net_change": row["tx_net_change"],
            "daily_statement_ending_balance": row["statement_ending_balance"],
            "daily_computed_ending_balance": row["computed_ending_balance"],
            "daily_difference": row["difference"],
            "daily_status": row["status"],
        }
        for _, row in rollforward_frame.iterrows()
    }
    output = line_items.copy()
    date_values = output["date"].fillna("").astype(str)
    output["daily_tx_net_change"] = date_values.map(lambda value: mapping.get(value, {}).get("daily_tx_net_change"))
    output["daily_statement_ending_balance"] = date_values.map(
        lambda value: mapping.get(value, {}).get("daily_statement_ending_balance")
    )
    output["daily_computed_ending_balance"] = date_values.map(
        lambda value: mapping.get(value, {}).get("daily_computed_ending_balance")
    )
    output["daily_difference"] = date_values.map(lambda value: mapping.get(value, {}).get("daily_difference"))
    output["daily_status"] = date_values.map(lambda value: mapping.get(value, {}).get("daily_status", "MISSING_DAILY_BALANCE"))
    return output


def _build_daily_balance_checks(
    transactions: list[Transaction],
    summaries: list[BankStatementBalanceSummary],
    daily_balances_by_file: dict[str, list[BankDailyBalancePoint]],
    tolerance: float = MONEY_TOLERANCE,
) -> list[ReconciliationCheck]:
    checks: list[ReconciliationCheck] = []
    if not daily_balances_by_file:
        return checks

    summary_by_file = {item.source_file: item for item in summaries}
    tx_dates_by_file: dict[str, set[str]] = {}
    for txn in transactions:
        if txn.date is None:
            continue
        tx_dates_by_file.setdefault(txn.source_file, set()).add(txn.date.isoformat())

    for source_file, daily_points in daily_balances_by_file.items():
        file_transactions = [txn for txn in transactions if txn.source_file == source_file]
        summary = summary_by_file.get(source_file)
        rollforward = build_daily_rollforward_frame(file_transactions, daily_points, summary)

        for _, row in rollforward.iterrows():
            checks.append(
                _money_check(
                    check_name=f"daily_balance_match__{source_file}__{row['date']}",
                    expected=float(row["statement_ending_balance"]),
                    actual=float(row["computed_ending_balance"]),
                    details=(
                        f"{source_file} {row['date']}: Computed day ending balance "
                        "must match statement Daily Balance."
                    ),
                    tolerance=tolerance,
                )
            )

        daily_dates = {item.date.isoformat() for item in daily_points if item.date is not None}
        tx_dates = tx_dates_by_file.get(source_file, set())
        missing_daily_dates = sorted(tx_dates - daily_dates)
        checks.append(
            ReconciliationCheck(
                check_name=f"daily_balance_date_coverage__{source_file}",
                status="PASS" if not missing_daily_dates else "FAIL",
                expected=str(len(tx_dates)),
                actual=str(len(tx_dates) - len(missing_daily_dates)),
                difference=float(len(missing_daily_dates)),
                tolerance=0.0,
                details=(
                    "All transaction posting dates should exist in statement Daily Balance. "
                    + (f"Missing: {', '.join(missing_daily_dates[:10])}" if missing_daily_dates else "No missing dates.")
                ),
            )
        )

    return checks


def _summary_map(summary_frame: pd.DataFrame) -> dict[str, float]:
    data = {str(row["line_item"]): float(row["amount"]) for _, row in summary_frame.iterrows()} if not summary_frame.empty else {}
    keys = [
        "Total Income",
        "Total Assets",
        "Total COGS",
        "Gross Profit",
        "Total Expenses",
        "Net Ordinary Income",
        "Total Other Income",
        "Total Other Expense",
        "Net Income",
        "Unclassified (needs review)",
    ]
    return {key: data.get(key, 0.0) for key in keys}


def _money_check(
    check_name: str,
    expected: float,
    actual: float,
    details: str,
    tolerance: float = MONEY_TOLERANCE,
) -> ReconciliationCheck:
    diff = actual - expected
    status = "PASS" if abs(diff) <= tolerance else "FAIL"
    return ReconciliationCheck(
        check_name=check_name,
        status=status,
        expected=f"{round(expected, 2):.2f}",
        actual=f"{round(actual, 2):.2f}",
        difference=diff,
        tolerance=tolerance,
        details=details,
    )


def _row_count_check(check_name: str, expected: int, actual: int) -> ReconciliationCheck:
    diff = float(actual - expected)
    status = "PASS" if actual == expected else "FAIL"
    return ReconciliationCheck(
        check_name=check_name,
        status=status,
        expected=str(expected),
        actual=str(actual),
        difference=diff,
        tolerance=0.0,
        details="CSV and XLSX row counts must match.",
    )


def _record_equality_check(check_name: str, csv_df: pd.DataFrame, xlsx_df: pd.DataFrame) -> ReconciliationCheck:
    csv_norm = _normalize_records(csv_df)
    xlsx_norm = _normalize_records(xlsx_df)
    status = "PASS" if csv_norm == xlsx_norm else "FAIL"
    diff = 0.0 if status == "PASS" else float(abs(len(csv_norm) - len(xlsx_norm)))
    return ReconciliationCheck(
        check_name=check_name,
        status=status,
        expected=str(len(csv_norm)),
        actual=str(len(xlsx_norm)),
        difference=diff,
        tolerance=0.0,
        details="CSV and XLSX normalized records must match exactly.",
    )


def _normalize_records(frame: pd.DataFrame) -> list[tuple[str, ...]]:
    if frame.empty:
        return []
    normalized = frame.copy()
    normalized = normalized.reindex(sorted(normalized.columns), axis=1)
    normalized = normalized.fillna("")
    for column in normalized.columns:
        if pd.api.types.is_numeric_dtype(normalized[column]):
            normalized[column] = pd.to_numeric(normalized[column], errors="coerce").fillna(0.0).astype(float).round(2)
        normalized[column] = normalized[column].map(_stringify_cell)
    records = [tuple(row) for row in normalized.astype(str).itertuples(index=False, name=None)]
    records.sort()
    return records


def _stringify_cell(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value).strip()
