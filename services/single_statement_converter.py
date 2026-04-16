from __future__ import annotations

from pathlib import Path

import pandas as pd

from financial_validator_mvp.models.schemas import BankDailyBalancePoint, BankStatementBalanceSummary, Transaction
from financial_validator_mvp.parsers.bank_statement_parser_pnc import parse_pnc_statement_pdf_with_checks_data
from financial_validator_mvp.services.classifier import classify_transactions, parse_rule_dicts
from financial_validator_mvp.services.normalization import normalize_transactions
from financial_validator_mvp.services.output_checks import (
    annotate_line_items_with_daily_checks,
    build_daily_rollforward_frame,
    run_export_reconciliation_checks,
)
from financial_validator_mvp.services.persistence import load_rule_dicts
from financial_validator_mvp.services.pnl_builder import build_monthly_yearly_pnl
from financial_validator_mvp.services.reporting import transactions_dataframe


def convert_single_statement_to_pnl_workbook(
    statement_pdf: Path,
    output_xlsx: Path,
    trucking_rules_path: Path,
    learned_rules_path: Path,
) -> dict[str, Path]:
    source_file = statement_pdf.name
    transactions, summary, daily_balances = parse_pnc_statement_pdf_with_checks_data(statement_pdf, source_file=source_file)
    if not transactions:
        raise ValueError("No transactions were extracted from this statement.")

    _classify_bank_transactions(transactions, trucking_rules_path=trucking_rules_path, learned_rules_path=learned_rules_path)
    pnl = build_monthly_yearly_pnl(transactions)

    line_items_frame = transactions_dataframe(transactions)
    daily_rollforward_frame = build_daily_rollforward_frame(transactions, daily_balances, summary)
    line_items_with_checks = annotate_line_items_with_daily_checks(line_items_frame, daily_rollforward_frame)

    check_frame = run_export_reconciliation_checks(
        transactions=transactions,
        pnl=pnl,
        export_pairs={},
        balance_summaries=[summary] if summary is not None else [],
        daily_balances_by_file={source_file: daily_balances} if daily_balances else {},
    )

    summary_frame = _statement_summary_frame(summary=summary)
    daily_balance_frame = _daily_balance_frame(daily_balances)

    output_xlsx.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(output_xlsx, engine="openpyxl") as writer:
        line_items_with_checks.to_excel(writer, sheet_name="line_items", index=False)
        daily_rollforward_frame.to_excel(writer, sheet_name="daily_rollforward", index=False)
        check_frame.to_excel(writer, sheet_name="reconciliation_checks", index=False)
        summary_frame.to_excel(writer, sheet_name="statement_summary", index=False)
        daily_balance_frame.to_excel(writer, sheet_name="daily_balance_raw", index=False)
        pnl.monthly_detail.to_excel(writer, sheet_name="monthly_pnl_detail", index=False)
        pnl.yearly_detail.to_excel(writer, sheet_name="yearly_pnl_detail", index=False)
        pnl.yearly_summary.to_excel(writer, sheet_name="yearly_pnl_summary", index=False)

    output_dir = output_xlsx.parent
    line_items_csv = output_dir / "statement_line_items_with_checks.csv"
    daily_rollforward_csv = output_dir / "statement_daily_rollforward_check.csv"
    checks_csv = output_dir / "statement_reconciliation_checks.csv"
    summary_csv = output_dir / "statement_top_summary.csv"
    monthly_csv = output_dir / "statement_monthly_pnl_detail.csv"

    line_items_with_checks.to_csv(line_items_csv, index=False)
    daily_rollforward_frame.to_csv(daily_rollforward_csv, index=False)
    check_frame.to_csv(checks_csv, index=False)
    summary_frame.to_csv(summary_csv, index=False)
    pnl.monthly_detail.to_csv(monthly_csv, index=False)

    return {
        "workbook": output_xlsx,
        "line_items_with_checks_csv": line_items_csv,
        "daily_rollforward_csv": daily_rollforward_csv,
        "reconciliation_checks_csv": checks_csv,
        "statement_summary_csv": summary_csv,
        "monthly_pnl_csv": monthly_csv,
    }


def _classify_bank_transactions(
    transactions: list[Transaction],
    trucking_rules_path: Path,
    learned_rules_path: Path,
) -> None:
    normalize_transactions(transactions)
    base_rules = load_rule_dicts(trucking_rules_path)
    learned_rules = load_rule_dicts(learned_rules_path)
    rules = parse_rule_dicts(base_rules + learned_rules)
    classify_transactions(transactions, rules)


def _statement_summary_frame(summary: BankStatementBalanceSummary | None) -> pd.DataFrame:
    if summary is None:
        return pd.DataFrame(
            columns=[
                "source_file",
                "period_start",
                "period_end",
                "beginning_balance",
                "additions_total",
                "deductions_total",
                "ending_balance",
            ]
        )
    return pd.DataFrame([summary.as_dict()])


def _daily_balance_frame(daily_balances: list[BankDailyBalancePoint]) -> pd.DataFrame:
    if not daily_balances:
        return pd.DataFrame(columns=["source_file", "date", "ledger_balance"])
    return pd.DataFrame([item.as_dict() for item in daily_balances])
