from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from financial_validator_mvp.models.schemas import BankStatementBalanceSummary
from financial_validator_mvp.parsers.bank_statement_parser_pnc import parse_pnc_statement_pdf_with_checks_data
from financial_validator_mvp.services.classifier import classify_transactions, parse_rule_dicts
from financial_validator_mvp.services.normalization import normalize_transactions
from financial_validator_mvp.services.output_checks import (
    build_daily_rollforward_frame,
    run_export_reconciliation_checks,
)
from financial_validator_mvp.services.persistence import load_rule_dicts
from financial_validator_mvp.services.pnl_builder import build_monthly_yearly_pnl

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
TRUCKING_RULES_PATH = DATA_DIR / "trucking_rules.json"
LEARNED_BANK_RULES_PATH = DATA_DIR / "learned_rules_bank.json"


def _sanitize_frame(frame: pd.DataFrame) -> list[dict[str, Any]]:
    if frame.empty:
        return []
    cleaned = frame.where(pd.notnull(frame), None)
    return cleaned.to_dict(orient="records")


def _summary_payload(summary: BankStatementBalanceSummary | None) -> dict[str, Any]:
    if summary is None:
        return {}
    return summary.as_dict()


def process_statement(statement_pdf: Path, source_file: str) -> dict[str, Any]:
    transactions, summary, daily_balances = parse_pnc_statement_pdf_with_checks_data(
        statement_pdf,
        source_file=source_file,
    )
    if not transactions:
        raise ValueError("No transactions were extracted from this statement.")

    normalize_transactions(transactions)
    base_rules = load_rule_dicts(TRUCKING_RULES_PATH)
    learned_rules = load_rule_dicts(LEARNED_BANK_RULES_PATH)
    classify_transactions(transactions, parse_rule_dicts(base_rules + learned_rules))

    pnl = build_monthly_yearly_pnl(transactions)
    source_daily_balances = {source_file: daily_balances} if daily_balances else {}
    reconciliation_checks = run_export_reconciliation_checks(
        transactions=transactions,
        pnl=pnl,
        export_pairs={},
        balance_summaries=[summary] if summary is not None else [],
        daily_balances_by_file=source_daily_balances,
    )
    daily_rollforward = build_daily_rollforward_frame(transactions, daily_balances, summary)
    transaction_count = len(transactions)
    dates = sorted(txn.date for txn in transactions if txn.date is not None)
    period_start = dates[0].isoformat() if dates else None
    period_end = dates[-1].isoformat() if dates else None

    return {
        "period_start": period_start,
        "period_end": period_end,
        "parsed_summary": _summary_payload(summary),
        "transaction_count": transaction_count,
        "monthly_detail": _sanitize_frame(pnl.monthly_detail),
        "period_total_by_category": _sanitize_frame(pnl.yearly_detail),
        "period_summary": _sanitize_frame(pnl.yearly_summary),
        "reconciliation_checks": _sanitize_frame(reconciliation_checks),
        "daily_balance_diagnostics": _sanitize_frame(daily_rollforward),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Process a single bank statement into trucking P&L JSON payload.")
    parser.add_argument("--input-pdf", required=True, help="Absolute path to the statement PDF.")
    parser.add_argument("--source-file", required=False, help="Original source filename.")
    args = parser.parse_args()

    statement_pdf = Path(args.input_pdf).expanduser().resolve()
    if not statement_pdf.exists():
        raise FileNotFoundError(f"Input PDF not found: {statement_pdf}")
    source_file = args.source_file or statement_pdf.name
    payload = process_statement(statement_pdf=statement_pdf, source_file=source_file)
    print(json.dumps(payload, separators=(",", ":"), ensure_ascii=True))


if __name__ == "__main__":
    main()
