from __future__ import annotations

import copy
import hashlib
import io
import json
from datetime import date
from pathlib import Path
import sys
import zipfile

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from financial_validator_mvp.parsers.bank_statement_parser_pnc import parse_pnc_statement_pdf_with_checks_data
from financial_validator_mvp.parsers.document_classifier import classify_document
from financial_validator_mvp.parsers.pnl_parser import parse_pnl_pdf
from financial_validator_mvp.services.classifier import classify_transactions, detect_rule_conflicts, parse_rule_dicts
from financial_validator_mvp.services.normalization import normalize_transactions
from financial_validator_mvp.services.output_checks import (
    MONEY_TOLERANCE,
    run_daily_balance_reconciliation_checks,
    run_export_reconciliation_checks,
    run_pre_export_reconciliation_checks,
)
from financial_validator_mvp.services.pdf_reports import (
    generate_balance_sheet_pdf,
    generate_monthly_detail_pdf,
    generate_profit_and_loss_pdf,
    generate_period_summary_pdf,
    generate_period_total_by_category_pdf,
)
from financial_validator_mvp.services.persistence import (
    load_rule_dicts,
    save_rule_dicts,
    upsert_learned_rules,
)
from financial_validator_mvp.services.pnl_builder import build_monthly_yearly_pnl
from financial_validator_mvp.services.review_state import (
    apply_review_assignments,
    build_review_rows,
    detect_pending_assignment_conflicts,
    preview_rule_impact,
    review_category_options,
    update_review_state,
)
from financial_validator_mvp.services.reporting import transactions_dataframe
from financial_validator_mvp.services.upload_dedup import deduplicate_uploaded_files

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DEFAULT_OUTPUT_DIR = DATA_DIR / "sample_outputs"
TRUCKING_RULES_PATH = DATA_DIR / "trucking_rules.json"
LEARNED_BANK_RULES_PATH = DATA_DIR / "learned_rules_bank.json"
WORKFLOW_STEPS = {
    1: "Import Statement",
    2: "Review Categories",
    3: "Review Reports",
    4: "Export Outputs",
}
SESSION_RESET_KEYS = [
    "workflow_step",
    "review_completed",
    "reports_completed",
    "pending_review_assignments",
    "bank_transactions",
    "bank_balance_summaries",
    "bank_daily_balances_by_file",
    "duplicate_map",
    "bank_base_rule_dicts",
    "bank_learned_rule_dicts",
    "bank_classification_rules",
    "bank_rule_conflicts",
    "bank_pnl_build",
    "review_include_text",
    "review_exclude_text",
    "bulk_review_assignment_kind",
    "bulk_review_selected_category",
    "selected_rows_assignment_kind",
    "selected_rows_category",
    "review_queue_table",
    "individual_rules_manager",
    "bulk_rules_manager",
    "pending_assignments_table",
    "pending_assignment_detail_key",
    "pending_impacted_transactions_table",
    "step3_failed_check_name",
    "step3_failed_transactions_editor",
    "reference_pnl_totals",
    "reference_pnl_file_names",
    "reference_pnl_documents",
    "reference_pnl_selected_file",
    "unsupported_import_files",
    "rules_backup_zip",
    "rules_backup_imported_signature",
    "manual_asset_entries",
    "asset_name_input",
    "asset_amount_input",
    "manual_assets_editor",
    "reset_asset_inputs_next_run",
]


def _render_help_box(message: str) -> None:
    st.info(message, icon="💡")


def main() -> None:
    st.set_page_config(page_title="Bank to Trucking P&L", layout="wide")
    _inject_ui_styles()
    st.title("Bank to Trucking P&L")
    st.caption("Follow the steps in order. Each step must be completed before the next one opens.")
    _render_help_box(
        "Use the sidebar to upload bank PDFs and move through Steps 1-4 in order. "
        "Each step unlocks only after the previous step is complete."
    )
    _ensure_workflow_state()

    with st.sidebar:
        _render_workflow_sidebar()
        if st.button("Start New Session", use_container_width=True, key="btn_start_new_session"):
            _reset_app_session()
            st.rerun()
        uploaded_files = st.file_uploader(
            "Step 1: Upload bank statement and/or P&L PDFs",
            type=["pdf"],
            accept_multiple_files=True,
            key="bank_pdfs",
            help=(
                "Upload one or more monthly bank statement PDFs. "
                "QuickBooks-style Profit & Loss PDFs are also accepted as reference imports."
            ),
        )
        rules_backup_zip = st.file_uploader(
            "Optional: Upload Rules Backup",
            type=["zip", "csv", "json"],
            accept_multiple_files=False,
            key="rules_backup_zip",
            help=(
                "Restore previously exported rules. Supports ZIP bundle backups plus direct rules CSV/JSON exports."
            ),
        )
        analyze_clicked = st.button(
            "Parse Statements",
            type="primary",
            disabled=st.session_state["workflow_step"] != 1,
            key="btn_parse_statements",
            help="Extracts transactions, daily balances, and statement totals from uploaded PDFs.",
        )

    _maybe_auto_import_rules_backup(rules_backup_zip)

    if analyze_clicked:
        if not uploaded_files:
            st.error("Upload at least one bank statement or P&L PDF.")
            return
        _analyze_bank_files(uploaded_files)

    if "bank_transactions" not in st.session_state:
        st.info("Upload bank statement and/or P&L PDFs and click 'Parse Statements'.")
        return

    if st.session_state.get("duplicate_map"):
        mapping = ", ".join(f"{dup} -> {orig}" for dup, orig in st.session_state["duplicate_map"].items())
        st.warning(f"Skipped duplicate uploads: {mapping}")

    _render_workflow_overview()

    current_step = st.session_state["workflow_step"]
    if current_step == 1:
        _render_import_step()
    elif current_step == 2:
        _render_review_step()
    elif current_step == 3:
        _render_reports_step()
    else:
        _render_export_step()


def _analyze_bank_files(uploaded_files) -> None:
    raw_files = {item.name: item.getvalue() for item in uploaded_files}
    canonical_files, duplicate_map, _ = deduplicate_uploaded_files(raw_files)

    transactions = []
    balance_summaries = []
    daily_balances_by_file: dict[str, list] = {}
    reference_pnl_totals = []
    reference_pnl_file_names: list[str] = []
    reference_pnl_documents: list[dict] = []
    unsupported_files: list[str] = []
    bank_files_parsed = 0
    for file_name, payload in canonical_files.items():
        metadata = classify_document(payload, file_name=file_name)
        if metadata.document_type == "pnl":
            parsed_totals = parse_pnl_pdf(payload, source_file=file_name)
            if parsed_totals:
                reference_pnl_totals.extend(parsed_totals)
                reference_pnl_file_names.append(file_name)
                reference_pnl_documents.append(
                    {
                        "file_name": file_name,
                        "period_start": metadata.period_start.isoformat() if metadata.period_start else None,
                        "period_end": metadata.period_end.isoformat() if metadata.period_end else None,
                        "totals": [item.as_dict() for item in parsed_totals],
                    }
                )
            else:
                unsupported_files.append(f"{file_name} (P&L parsed with 0 totals)")
            continue

        if metadata.document_type in {"bank_statement", "unknown"}:
            parsed_transactions, summary, daily_points = parse_pnc_statement_pdf_with_checks_data(
                payload,
                source_file=file_name,
            )
            if parsed_transactions:
                bank_files_parsed += 1
            transactions.extend(parsed_transactions)
            if summary is not None:
                balance_summaries.append(summary)
            if daily_points:
                daily_balances_by_file[file_name] = daily_points
            if not parsed_transactions and metadata.document_type != "bank_statement":
                unsupported_files.append(f"{file_name} (unsupported PDF format)")
            continue

        unsupported_files.append(f"{file_name} ({metadata.document_type})")

    if not transactions:
        st.session_state["reference_pnl_totals"] = reference_pnl_totals
        st.session_state["reference_pnl_file_names"] = reference_pnl_file_names
        st.session_state["reference_pnl_documents"] = reference_pnl_documents
        st.session_state["unsupported_import_files"] = unsupported_files
        if reference_pnl_totals:
            st.warning(
                f"Parsed {len(reference_pnl_totals)} reference P&L row(s) from {len(reference_pnl_file_names)} file(s), "
                "but no bank transactions were found. Upload at least one bank statement PDF to continue."
            )
        else:
            st.error("No bank transactions were extracted. Confirm the bank PDF format.")
        if unsupported_files:
            st.warning("Some files were not usable:\n- " + "\n- ".join(unsupported_files))
        return

    normalize_transactions(transactions)
    _classify(transactions)

    st.session_state["bank_transactions"] = transactions
    st.session_state["bank_balance_summaries"] = balance_summaries
    st.session_state["bank_daily_balances_by_file"] = daily_balances_by_file
    st.session_state["duplicate_map"] = duplicate_map
    st.session_state["reference_pnl_totals"] = reference_pnl_totals
    st.session_state["reference_pnl_file_names"] = reference_pnl_file_names
    st.session_state["reference_pnl_documents"] = reference_pnl_documents
    st.session_state["unsupported_import_files"] = unsupported_files
    st.session_state["pending_review_assignments"] = {}
    st.session_state["workflow_step"] = 2
    st.session_state["review_completed"] = False
    st.session_state["reports_completed"] = False
    status_bits = [
        f"Parsed {len(transactions)} bank transaction(s) from {bank_files_parsed} bank file(s).",
    ]
    if reference_pnl_totals:
        status_bits.append(
            f"Parsed {len(reference_pnl_totals)} reference P&L total row(s) from {len(reference_pnl_file_names)} P&L file(s)."
        )
    st.success(" ".join(status_bits))
    if unsupported_files:
        st.warning("Some files were not usable:\n- " + "\n- ".join(unsupported_files))


def _classify(transactions) -> None:
    base_rules = load_rule_dicts(TRUCKING_RULES_PATH)
    learned_rules = load_rule_dicts(LEARNED_BANK_RULES_PATH)
    st.session_state["bank_base_rule_dicts"] = base_rules
    st.session_state["bank_learned_rule_dicts"] = learned_rules
    rules = parse_rule_dicts(base_rules + learned_rules)
    st.session_state["bank_classification_rules"] = rules
    st.session_state["bank_rule_conflicts"] = detect_rule_conflicts(rules)
    classify_transactions(transactions, rules)


def _maybe_auto_import_rules_backup(uploaded_file) -> None:
    if uploaded_file is None:
        return
    payload_bytes = uploaded_file.getvalue()
    if not payload_bytes:
        return
    file_name = str(getattr(uploaded_file, "name", "rules_backup")).strip()
    signature = hashlib.sha256(payload_bytes).hexdigest()
    previous_signature = st.session_state.get("rules_backup_imported_signature")
    if previous_signature == signature:
        return

    transactions = st.session_state.get("bank_transactions")
    try:
        restored_rows, mode = _restore_rules_from_uploaded_file(
            payload_bytes=payload_bytes,
            file_name=file_name,
            transactions=transactions,
        )
    except Exception as exc:
        st.session_state["rules_backup_imported_signature"] = signature
        st.error(f"Failed to import rules backup ({file_name}): {exc}")
        return
    st.session_state["rules_backup_imported_signature"] = signature
    if mode == "csv":
        st.success(f"Imported rules backup from CSV: {restored_rows} rule row(s) restored.")
    elif mode == "json":
        st.success(f"Imported rules backup from JSON: {restored_rows} rule row(s) restored.")
    elif mode == "zip_csv":
        st.success(f"Imported rules backup ZIP from split CSV rules: {restored_rows} row(s) restored.")
    elif mode == "zip_json":
        st.success(f"Imported rules backup ZIP from JSON rules: {restored_rows} row(s) restored.")
    else:
        st.warning(
            "Rules backup uploaded, but no supported rule data was found. "
            "Use ZIP with split rules CSVs, or a rules-table CSV/JSON export."
        )


def _render_raw_transactions() -> None:
    st.subheader("Imported Bank Transactions")
    _render_help_box(
        "This is your raw extracted data. Check that transaction rows, categories, and source files look correct "
        "before continuing to category review."
    )
    transactions = st.session_state["bank_transactions"]
    source_counts = pd.Series([txn.category_source or "unknown" for txn in transactions]).value_counts().reset_index()
    source_counts.columns = ["category_source", "count"]
    st.caption("Audit trail by category source")
    _render_table(source_counts)
    _render_table(transactions_dataframe(transactions))


def _render_import_step() -> None:
    st.subheader("Step 1: Import Statement")
    _render_help_box(
        "Step 1 parses uploaded statement PDFs into transactions. "
        "If this section looks right, continue to Step 2 to classify and review categories."
    )
    if "bank_transactions" in st.session_state:
        st.success("Statement parsed. Review the imported transactions below, then continue.")
        _render_raw_transactions()
        _render_reference_pnl_totals()
        if st.button("Continue To Step 2: Review Categories", type="primary", key="btn_continue_step2"):
            st.session_state["workflow_step"] = 2
            st.rerun()


def _render_review_step() -> None:
    st.subheader("Step 2: Review Categories")
    st.caption("Resolve repeated descriptions, review rule impact, then mark this step complete.")
    _render_help_box(
        "Use Individual rules for exact one-off descriptions and Bulk rules for repeating text patterns. "
        "Only move forward when unresolved transactions and pending unsaved assignments are both zero."
    )
    _render_review_assistant()

    unresolved_count = _count_unresolved_transactions(st.session_state["bank_transactions"])
    actionable_unresolved_count = _count_unresolved_transactions(
        st.session_state["bank_transactions"],
        actionable_only=True,
    )
    hidden_unresolved_count = max(0, unresolved_count - actionable_unresolved_count)
    pending_count = len(st.session_state.get("pending_review_assignments", {}))
    if hidden_unresolved_count > 0:
        st.info(
            "Some unresolved rows are not shown in the review queue because they do not have a normalized description. "
            f"Hidden unresolved rows: {hidden_unresolved_count}."
        )
    if unresolved_count > 0 or pending_count > 0:
        st.warning(
            f"You cannot continue yet. Unresolved transactions: {unresolved_count}. "
            f"Pending unsaved assignments: {pending_count}."
        )
        if st.button(
            "Continue To Step 3 Anyway",
            type="primary",
            key="btn_force_continue_step3",
        ):
            st.session_state["review_completed"] = True
            st.session_state["workflow_step"] = 3
            st.rerun()
        return

    if st.button("Complete Step 2 And Continue To Reports", type="primary", key="btn_complete_step2"):
        st.session_state["review_completed"] = True
        st.session_state["workflow_step"] = 3
        st.rerun()


def _render_reports_step() -> None:
    st.subheader("Step 3: Review Reports")
    st.caption("Confirm the P&L and statement control totals before moving to export.")
    _render_help_box(
        "Step 3 rebuilds local CSV/XLSX outputs and runs reconciliation checks. "
        "If checks fail, review the failed rows and correct transactions before approving."
    )
    _render_pnl_outputs()
    monthly_checks_failed = _render_step3_local_outputs_and_checks()
    if st.button("Back To Step 2", key="btn_back_step2"):
        st.session_state["workflow_step"] = 2
        st.rerun()
    if st.button(
        "Approve Reports And Continue To Export",
        type="primary",
        key="btn_approve_step3",
        disabled=monthly_checks_failed,
    ):
        st.session_state["reports_completed"] = True
        st.session_state["workflow_step"] = 4
        st.rerun()


def _render_step3_local_outputs_and_checks() -> bool:
    st.markdown("**Step 3 Local CSV/XLSX Build + Validation**")
    _render_help_box(
        "Tolerance controls how close values must be to pass reconciliation. "
        "Use smaller values for stricter checks."
    )
    output_dir_text = st.text_input(
        "Step 3 local output folder",
        value=str(DEFAULT_OUTPUT_DIR),
        key="step3_output_dir",
        help="Local folder where Step 3 writes CSV/XLSX output and diagnostics.",
    )
    tolerance_value = st.number_input(
        "Step 3 monthly/daily check tolerance",
        min_value=0.0,
        value=float(st.session_state.get("reconciliation_tolerance", MONEY_TOLERANCE)),
        step=0.01,
        format="%.4f",
        key="step3_tolerance",
    )
    st.session_state["reconciliation_tolerance"] = float(tolerance_value)

    output_dir = Path(output_dir_text)
    output_dir.mkdir(parents=True, exist_ok=True)
    transactions = st.session_state["bank_transactions"]
    pnl = st.session_state["bank_pnl_build"]
    full_year = _has_full_year_of_data(transactions)
    export_pairs, export_paths = _write_local_csv_xlsx_outputs(output_dir, transactions, pnl)

    st.caption("Local CSV/XLSX files generated in Step 3.")
    st.code(
        "\n".join(
            str(path)
            for path in [
                export_paths["raw_csv"],
                export_paths["raw_xlsx"],
                export_paths["monthly_csv"],
                export_paths["monthly_xlsx"],
                export_paths["period_total_csv"],
                export_paths["period_total_xlsx"],
                export_paths["period_summary_csv"],
                export_paths["period_summary_xlsx"],
                export_paths["manual_assets_csv"],
                export_paths["manual_assets_xlsx"],
            ]
        ),
        language="text",
    )
    if not full_year:
        st.caption("Period naming is used because the loaded date span is under 12 months.")

    pre_checks_df = run_pre_export_reconciliation_checks(
        transactions=transactions,
        pnl=pnl,
        balance_summaries=st.session_state.get("bank_balance_summaries", []),
        daily_balances_by_file=st.session_state.get("bank_daily_balances_by_file", {}),
        tolerance=float(tolerance_value),
    )
    pre_checks_path = output_dir / "pre_export_reconciliation_report.csv"
    pre_checks_df.to_csv(pre_checks_path, index=False)
    st.markdown("**Pre-export Reconciliation Checks**")
    _render_table(_build_reconciliation_display_frame(pre_checks_df))
    failed_checks = pre_checks_df[pre_checks_df["status"] == "FAIL"].copy()
    _render_failed_check_transaction_editor(failed_checks, transactions)

    monthly_checks = pre_checks_df[pre_checks_df["check_name"].str.startswith("monthly_")].copy()
    monthly_failed = monthly_checks[monthly_checks["status"] == "FAIL"]
    if monthly_failed.empty:
        st.success("Monthly rollup checks passed.")
        return False

    st.error(f"{len(monthly_failed)} monthly check(s) failed. Running daily balance diagnostics.")
    st.markdown("**Monthly Failures**")
    _render_table(_build_failed_reconciliation_frame(monthly_failed))

    daily_checks_df = run_daily_balance_reconciliation_checks(
        transactions=transactions,
        balance_summaries=st.session_state.get("bank_balance_summaries", []),
        daily_balances_by_file=st.session_state.get("bank_daily_balances_by_file", {}),
        tolerance=float(tolerance_value),
    )
    daily_checks_path = output_dir / "daily_balance_diagnostics.csv"
    daily_checks_df.to_csv(daily_checks_path, index=False)
    st.markdown("**Daily Balance Diagnostics**")
    _render_table(_build_reconciliation_display_frame(daily_checks_df))

    daily_failed = daily_checks_df[daily_checks_df["status"] == "FAIL"]
    if daily_failed.empty:
        st.warning(
            "Monthly checks failed but daily balance checks passed. Review monthly mapping/category logic and unresolved items."
        )
    else:
        st.warning(
            "Recommendation: review failed daily balances and correct the related transactions/dates. "
            "After corrections, rerun Step 3 and confirm failures are cleared."
        )
        st.markdown("**Failed Daily Balances To Correct**")
        _render_table(_build_failed_reconciliation_frame(daily_failed))
        st.caption(f"Detailed diagnostics saved to: {daily_checks_path}")
    return True


def _render_failed_check_transaction_editor(failed_checks: pd.DataFrame, transactions) -> None:
    st.markdown("**Failed Check Transaction Review**")
    _render_help_box(
        "Pick a failed check, review the scoped transactions, fix incorrect values, then save to rerun checks."
    )
    if failed_checks.empty:
        st.success("No failed checks to review.")
        return
    selected_check_name = st.selectbox(
        "Select failed check_name",
        options=failed_checks["check_name"].astype(str).tolist(),
        key="step3_failed_check_name",
    )
    source_file, target_date = _failed_check_scope(selected_check_name)
    if target_date:
        st.caption(f"Scope: source file `{source_file}`, posting date `{target_date}`")
    elif source_file:
        st.caption(f"Scope: source file `{source_file}` (all transaction dates in that statement)")
    else:
        st.caption("Scope: full transaction set (check is not date/file scoped).")

    scoped_rows = _transactions_for_failed_check(transactions, selected_check_name)
    if not scoped_rows:
        st.info("No transactions found for this failed check scope.")
        return

    review_frame = pd.DataFrame(scoped_rows)
    edited = st.data_editor(
        review_frame,
        use_container_width=True,
        hide_index=True,
        disabled=["txn_index", "source_file", "source_account", "original_category", "resolved_category"],
        column_config={
            "amount": st.column_config.NumberColumn("amount", format="$%.2f"),
            "running_balance": st.column_config.NumberColumn("running_balance", format="$%.2f"),
        },
        key="step3_failed_transactions_editor",
    )
    if st.button("Save Corrections And Re-run Checks", key="btn_step3_save_failed_corrections"):
        updates = _apply_failed_check_edits(transactions, edited)
        if updates == 0:
            st.warning("No changes detected.")
            return
        normalize_transactions(transactions)
        _classify(transactions)
        st.success(f"Saved {updates} transaction correction(s). Re-running checks.")
        st.rerun()


def _render_export_step() -> None:
    st.subheader("Step 4: Export Outputs")
    st.caption("Create the files only after the report review step is complete.")
    _render_help_box(
        "Step 4 runs final pre-export checks, then writes CSV/XLSX and PDF outputs to your selected folder."
    )
    _render_exports()
    if st.button("Back To Step 3", key="btn_back_step3"):
        st.session_state["workflow_step"] = 3
        st.rerun()


def _render_review_assistant() -> None:
    st.subheader("Bookkeeping Review Assistant")
    _render_help_box(
        "This assistant helps you resolve unknown categories and save learned rules for future statements."
    )
    transactions = st.session_state["bank_transactions"]
    rules = st.session_state.get("bank_classification_rules", [])
    review_rows = build_review_rows(transactions, rules)
    queue_rows = [row for row in review_rows if row["needs_review"]]
    queue_count_now = len(queue_rows)

    pending_assignments = st.session_state.get("pending_review_assignments", {})
    queue_count_after_pending = queue_count_now
    if pending_assignments:
        preview_transactions = _clone_transactions(transactions)
        apply_review_assignments(preview_transactions, pending_assignments)
        preview_rows = build_review_rows(preview_transactions, rules)
        queue_count_after_pending = len([row for row in preview_rows if row["needs_review"]])

    metric_col1, metric_col2 = st.columns(2)
    metric_col1.metric("Review Queue Rows", queue_count_now)
    metric_col2.metric("After Pending Save", queue_count_after_pending)

    st.markdown('<div class="section-chip section-individual">2A. Individual Section</div>', unsafe_allow_html=True)
    st.caption("Select specific rows and assign category. This section creates exact individual rules.")
    _render_help_box(
        "Use this for exact matches only. Selected rows become exact rules and are best for unique descriptions."
    )

    if queue_rows:
        queue_frame = pd.DataFrame(queue_rows)[
            [
                "normalized_description",
                "group_rule_type",
                "group_pattern",
                "group_exclude_pattern",
                "occurrences",
                "net_amount",
                "current_category",
                "category_source",
                "suggestion_1",
                "suggestion_2",
                "suggestion_3",
                "needs_review_label",
            ]
        ].copy()
        queue_frame.insert(0, "select_for_assignment", False)
        queue_frame.insert(1, "bulk_group_exclude", False)

        st.markdown(f"**Review Queue ({queue_count_now} rows)**")
        edited_queue = st.data_editor(
            queue_frame,
            use_container_width=True,
            hide_index=True,
            disabled=[
                "normalized_description",
                "group_rule_type",
                "group_pattern",
                "group_exclude_pattern",
                "occurrences",
                "net_amount",
                "current_category",
                "category_source",
                "suggestion_1",
                "suggestion_2",
                "suggestion_3",
                "needs_review_label",
            ],
            column_config={
                "needs_review_label": st.column_config.TextColumn("Needs Review"),
                "group_rule_type": st.column_config.TextColumn("Group Type"),
                "group_pattern": st.column_config.TextColumn("Group Pattern"),
                "group_exclude_pattern": st.column_config.TextColumn("Group Exclude Pattern"),
                "select_for_assignment": st.column_config.CheckboxColumn("Select"),
                "bulk_group_exclude": st.column_config.CheckboxColumn("Bulk Group Exclude"),
                "occurrences": st.column_config.NumberColumn("occurrences", format="%d"),
                "net_amount": st.column_config.NumberColumn("net_amount", format="$%.2f"),
            },
            key="review_queue_table",
        )
    else:
        st.success("Review queue is empty.")
        edited_queue = pd.DataFrame(columns=["select_for_assignment", "bulk_group_exclude", "normalized_description"])

    selected_rows = [row for row in edited_queue.to_dict(orient="records") if bool(row.get("select_for_assignment", False))]
    selected_assignment_kind = st.radio(
        "Selected rows assignment bucket",
        options=["pnl", "balance_transfer_ignore"],
        format_func=lambda value: "P&L category" if value == "pnl" else "Balance sheet / transfer / ignore",
        horizontal=True,
        key="selected_rows_assignment_kind",
    )
    selected_options = review_category_options()[selected_assignment_kind]
    selected_category = st.selectbox(
        "Selected rows category",
        options=selected_options,
        key="selected_rows_category",
    )
    if st.button("Add Selected Rows To Pending Review", key="btn_add_selected_pending"):
        if not selected_rows:
            st.warning("Select at least one row in Review Queue.")
        else:
            updated = st.session_state.get("pending_review_assignments", {})
            for row in selected_rows:
                updated = update_review_state(
                    updated,
                    normalized_description=str(row["normalized_description"]).strip().lower(),
                    category=selected_category,
                    assignment_kind=selected_assignment_kind,
                    match_type="exact",
                    exclude_pattern="",
                )
            st.session_state["pending_review_assignments"] = updated
            st.success(f"Added {len(selected_rows)} selected row assignment(s) to pending review.")

    _render_individual_rule_manager(transactions)

    st.divider()
    st.markdown('<div class="section-chip section-bulk">2B. Bulk Section</div>', unsafe_allow_html=True)
    st.caption("Create contains rules with include/exclude text. Bulk rules are managed separately.")
    _render_help_box(
        "Use include text for repeated patterns (for example, 'sunpass'). "
        "Use exclude text to skip false matches."
    )

    include_text = st.text_input("Bulk group include text", key="review_include_text")
    exclude_text = st.text_input("Bulk group exclude text", key="review_exclude_text")
    excluded_values = [
        str(row["normalized_description"]).strip().lower()
        for row in edited_queue.to_dict(orient="records")
        if bool(row.get("bulk_group_exclude", False))
    ]
    derived_exclude_text = "||".join(excluded_values)
    if excluded_values:
        st.caption(f"Bulk exclude active from queue: {len(excluded_values)} row(s).")

    bulk_assignment_kind = st.radio(
        "Bulk assignment bucket",
        options=["pnl", "balance_transfer_ignore"],
        format_func=lambda value: "P&L category" if value == "pnl" else "Balance sheet / transfer / ignore",
        horizontal=True,
        key="bulk_review_assignment_kind",
    )
    bulk_options = review_category_options()[bulk_assignment_kind]
    bulk_category = st.selectbox(
        "Bulk category",
        options=bulk_options,
        key="bulk_review_selected_category",
    )
    if include_text.strip():
        effective_exclude_text = _merge_exclude_text(
            manual_exclude_text=exclude_text,
            queue_excluded_values=excluded_values,
        )
        if effective_exclude_text:
            st.caption(f"Effective exclude text: `{effective_exclude_text}`")
        bulk_preview = preview_rule_impact(
            transactions,
            update_review_state(
                {},
                normalized_description=include_text.strip().lower(),
                category=bulk_category,
                assignment_kind=bulk_assignment_kind,
                match_type="contains",
                exclude_pattern=effective_exclude_text,
            ),
        )
        _render_table(bulk_preview)
        if st.button("Add Bulk Contains Rule To Pending Review", key="btn_add_bulk_pending"):
            st.session_state["pending_review_assignments"] = update_review_state(
                st.session_state.get("pending_review_assignments", {}),
                normalized_description=include_text.strip().lower(),
                category=bulk_category,
                assignment_kind=bulk_assignment_kind,
                match_type="contains",
                exclude_pattern=effective_exclude_text,
            )

    _render_bulk_rule_manager(transactions)
    st.divider()
    st.markdown('<div class="section-chip section-pending">2C. Assets Section</div>', unsafe_allow_html=True)
    st.caption("Create asset line items one-by-one. These entries are included in P&L outputs and exports.")
    _render_help_box(
        "Add each asset with name and amount. You can delete entries before moving forward."
    )
    _render_assets_item_entry_section()

    st.divider()
    st.markdown('<div class="section-chip section-pending">2D. Pending Review</div>', unsafe_allow_html=True)
    _render_help_box(
        "Pending Review shows unsaved rule changes and their impact before you commit them."
    )
    _render_pending_preview_and_save(transactions)


def _render_assets_item_entry_section() -> None:
    if st.session_state.pop("reset_asset_inputs_next_run", False):
        st.session_state["asset_name_input"] = ""
        st.session_state["asset_amount_input"] = 0.0

    asset_col1, asset_col2, asset_col3 = st.columns([2.4, 1.2, 1.1])
    with asset_col1:
        asset_name = st.text_input("Asset Name", key="asset_name_input", placeholder="e.g., Trailer #7")
    with asset_col2:
        asset_amount = st.number_input("Asset Amount", key="asset_amount_input", format="%.2f")
    with asset_col3:
        st.write("")
        st.write("")
        if st.button("Add Asset Entry", key="btn_add_asset_entry_step2"):
            name_value = str(asset_name).strip()
            if not name_value:
                st.error("Enter an asset name before adding.")
            else:
                entries = list(st.session_state.get("manual_asset_entries", []))
                entries.append({"asset_name": name_value, "amount": float(asset_amount)})
                st.session_state["manual_asset_entries"] = entries
                st.session_state["reset_asset_inputs_next_run"] = True
                st.rerun()

    asset_rows = list(st.session_state.get("manual_asset_entries", []))
    if not asset_rows:
        st.caption("No asset entries added yet.")
        return

    asset_frame = pd.DataFrame(asset_rows)
    asset_frame.insert(0, "select_delete", False)
    edited_assets = st.data_editor(
        asset_frame,
        use_container_width=True,
        hide_index=True,
        disabled=["asset_name", "amount"],
        column_config={
            "select_delete": st.column_config.CheckboxColumn("Delete"),
            "amount": st.column_config.NumberColumn("amount", format="$%.2f"),
        },
        key="manual_assets_editor",
    )
    if st.button("Delete Selected Asset Entries", key="btn_delete_asset_entries_step2"):
        keep = [
            row
            for row in edited_assets.to_dict(orient="records")
            if not bool(row.get("select_delete", False))
        ]
        st.session_state["manual_asset_entries"] = [
            {"asset_name": str(row.get("asset_name", "")).strip(), "amount": float(row.get("amount", 0.0))}
            for row in keep
            if str(row.get("asset_name", "")).strip()
        ]
        st.rerun()


def _render_individual_rule_manager(transactions) -> None:
    st.markdown("**Individual Rules Table (Exact Match)**")
    all_rules = load_rule_dicts(LEARNED_BANK_RULES_PATH)
    individual_rules = [rule for rule in all_rules if str(rule.get("match_type", "")).lower() == "exact"]
    frame = pd.DataFrame(individual_rules)
    if frame.empty:
        frame = pd.DataFrame(
            columns=["id", "match_type", "pattern", "exclude_pattern", "category", "non_pnl", "field", "confidence", "priority", "origin"]
        )
    frame.insert(0, "select_delete", False)
    edited = st.data_editor(
        _format_display_frame(frame),
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        key="individual_rules_manager",
        column_config={
            "select_delete": st.column_config.CheckboxColumn("Select"),
        },
    )
    if st.button("Save Individual Rules Table", key="btn_save_individual_rules"):
        rows = edited.to_dict(orient="records")
        cleaned = [item for item in (_sanitize_rule_row(row) for row in rows) if item]
        _save_split_rule_tables(transactions, exact_rules=cleaned, contains_rules=None)
        st.success("Saved individual rules table.")
        st.rerun()
    if st.button("Delete Selected Individual Rules", key="btn_delete_individual_rules"):
        rows = edited.to_dict(orient="records")
        selected = [row for row in rows if bool(row.get("select_delete", False))]
        if not selected:
            st.warning("Select at least one individual rule row to delete.")
        else:
            filtered = [
                item for item in (_sanitize_rule_row(row) for row in rows if not bool(row.get("select_delete", False))) if item
            ]
            _save_split_rule_tables(transactions, exact_rules=filtered, contains_rules=None)
            st.success(f"Deleted {len(selected)} individual rule(s).")
            st.rerun()


def _render_bulk_rule_manager(transactions) -> None:
    st.markdown("**Bulk Rules Table (Contains Match)**")
    all_rules = load_rule_dicts(LEARNED_BANK_RULES_PATH)
    bulk_rules = [rule for rule in all_rules if str(rule.get("match_type", "")).lower() == "contains"]
    frame = pd.DataFrame(bulk_rules)
    if frame.empty:
        frame = pd.DataFrame(
            columns=["id", "match_type", "pattern", "exclude_pattern", "category", "non_pnl", "field", "confidence", "priority", "origin"]
        )
    frame.insert(0, "select_delete", False)
    edited = st.data_editor(
        _format_display_frame(frame),
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        key="bulk_rules_manager",
        column_config={
            "select_delete": st.column_config.CheckboxColumn("Select"),
        },
    )
    if st.button("Save Bulk Rules Table", key="btn_save_bulk_rules"):
        rows = edited.to_dict(orient="records")
        cleaned = [item for item in (_sanitize_rule_row(row) for row in rows) if item]
        _save_split_rule_tables(transactions, exact_rules=None, contains_rules=cleaned)
        st.success("Saved bulk rules table.")
        st.rerun()
    if st.button("Delete Selected Bulk Rules", key="btn_delete_bulk_rules"):
        rows = edited.to_dict(orient="records")
        selected = [row for row in rows if bool(row.get("select_delete", False))]
        if not selected:
            st.warning("Select at least one bulk rule row to delete.")
        else:
            filtered = [
                item for item in (_sanitize_rule_row(row) for row in rows if not bool(row.get("select_delete", False))) if item
            ]
            _save_split_rule_tables(transactions, exact_rules=None, contains_rules=filtered)
            st.success(f"Deleted {len(selected)} bulk rule(s).")
            st.rerun()


def _render_pending_preview_and_save(transactions) -> None:
    pending_assignments = st.session_state.get("pending_review_assignments", {})
    if not pending_assignments:
        st.caption("No pending assignments.")
        return

    st.markdown("**Pending Rule Impact Preview**")
    preview_frame = preview_rule_impact(transactions, pending_assignments)
    pending_rows = list(pending_assignments.values())
    pending_frame = pd.DataFrame(pending_rows)
    pending_frame.insert(0, "select_pending", False)
    edited_pending = st.data_editor(
        _format_display_frame(pending_frame),
        use_container_width=True,
        hide_index=True,
        disabled=[
            "assignment_key",
            "normalized_description",
            "category",
            "assignment_kind",
            "non_pnl",
            "origin",
            "match_type",
            "exclude_pattern",
        ],
        column_config={
            "select_pending": st.column_config.CheckboxColumn("Select"),
        },
        key="pending_assignments_table",
    )
    selected_pending = [row for row in edited_pending.to_dict(orient="records") if bool(row.get("select_pending", False))]
    if st.button("Remove Selected Pending Assignments", key="btn_remove_pending_assignments"):
        if not selected_pending:
            st.warning("Select at least one pending assignment to remove.")
        else:
            updated = dict(pending_assignments)
            for row in selected_pending:
                assignment_key = str(row.get("assignment_key", ""))
                if assignment_key in updated:
                    del updated[assignment_key]
            st.session_state["pending_review_assignments"] = updated
            st.success(f"Removed {len(selected_pending)} pending assignment(s).")
            st.rerun()

    _render_table(preview_frame)

    assignment_options = [row["assignment_key"] for row in pending_rows]
    selected_assignment_key = st.selectbox(
        "Inspect impacted transactions for pending assignment",
        options=assignment_options,
        key="pending_assignment_detail_key",
    )
    selected_assignment = pending_assignments[selected_assignment_key]
    impacted_transactions = _transactions_for_assignment(transactions, selected_assignment)
    if impacted_transactions:
        impacted_frame = pd.DataFrame([txn.as_dict() for txn in impacted_transactions])
        impacted_frame.insert(0, "exclude_from_pending_rule", False)
        edited_impacted = st.data_editor(
            _format_display_frame(impacted_frame),
            use_container_width=True,
            hide_index=True,
            disabled=[col for col in impacted_frame.columns if col != "exclude_from_pending_rule"],
            key="pending_impacted_transactions_table",
        )
        if st.button("Exclude Selected Transactions From Pending Assignment", key="btn_exclude_pending_transactions"):
            selected_exclusions = [
                str(row.get("normalized_description", "")).strip().lower()
                for row in edited_impacted.to_dict(orient="records")
                if bool(row.get("exclude_from_pending_rule", False))
            ]
            if not selected_exclusions:
                st.warning("Select at least one impacted transaction to exclude.")
            else:
                current_excludes = str(selected_assignment.get("exclude_pattern", "")).strip().lower()
                exclude_set = {item for item in current_excludes.split("||") if item}
                exclude_set.update(selected_exclusions)
                selected_assignment["exclude_pattern"] = "||".join(sorted(exclude_set))
                st.session_state["pending_review_assignments"][selected_assignment_key] = selected_assignment
                st.success(f"Excluded {len(selected_exclusions)} transaction description(s) from this pending assignment.")
                st.rerun()
    else:
        st.caption("No impacted transactions for the selected pending assignment.")

    pending_conflicts = detect_pending_assignment_conflicts(
        pending_assignments,
        st.session_state.get("bank_base_rule_dicts", []) + st.session_state.get("bank_learned_rule_dicts", []),
    )
    if pending_conflicts:
        st.warning("Rule conflicts detected in pending assignments.")
        _render_table(pd.DataFrame(pending_conflicts))

    preview_transactions = _clone_transactions(transactions)
    apply_review_assignments(preview_transactions, pending_assignments)
    preview_sources = pd.Series([txn.category_source or "unknown" for txn in preview_transactions]).value_counts().reset_index()
    preview_sources.columns = ["preview_category_source", "count"]
    st.caption("Preview audit trail after applying pending assignments")
    _render_table(preview_sources)

    if st.button("Save Pending Rules", key="btn_save_pending_rules"):
        assignments = list(pending_assignments.values())
        upsert_learned_rules(assignments, learned_rules_path=LEARNED_BANK_RULES_PATH)
        _classify(transactions)
        st.session_state["pending_review_assignments"] = {}
        st.success(f"Saved {len(assignments)} learned rule(s).")
        st.rerun()


def _save_split_rule_tables(transactions=None, exact_rules=None, contains_rules=None) -> None:
    all_rules = load_rule_dicts(LEARNED_BANK_RULES_PATH)
    other_rules = [rule for rule in all_rules if str(rule.get("match_type", "")).lower() not in {"exact", "contains"}]

    if exact_rules is None:
        exact_rules = [rule for rule in all_rules if str(rule.get("match_type", "")).lower() == "exact"]
    if contains_rules is None:
        contains_rules = [rule for rule in all_rules if str(rule.get("match_type", "")).lower() == "contains"]

    merged = exact_rules + contains_rules + other_rules
    save_rule_dicts(LEARNED_BANK_RULES_PATH, merged)
    if transactions is not None:
        _classify(transactions)


def _render_pnl_outputs() -> None:
    st.subheader("Monthly + Period Trucking P&L")
    transactions = st.session_state["bank_transactions"]
    st.markdown("**Manual Asset Entries (from Step 2C)**")
    asset_rows = list(st.session_state.get("manual_asset_entries", []))
    if asset_rows:
        _render_table(pd.DataFrame(asset_rows))
    else:
        st.caption("No manual assets added.")
    pnl = build_monthly_yearly_pnl(
        transactions,
        asset_entries=st.session_state.get("manual_asset_entries", []),
    )
    st.session_state["bank_pnl_build"] = pnl
    full_year = _has_full_year_of_data(transactions)
    total_label = "Yearly Detail by Category" if full_year else "Period Total by Category"
    summary_label = "Yearly Summary" if full_year else "Period Summary"

    st.markdown(f"**{summary_label}**")
    _render_table(pnl.yearly_summary)
    st.markdown(f"**{total_label}**")
    _render_table(pnl.yearly_detail)

    st.markdown("**Monthly Pivot**")
    _render_table(pnl.monthly_pivot)

    st.markdown("**Monthly Detail**")
    _render_table(pnl.monthly_detail)
    _render_reference_pnl_totals(show_comparison=True, generated_pnl_detail=pnl.yearly_detail)

    if st.session_state.get("bank_balance_summaries"):
        st.markdown("**Parsed Bank Balance Summary (for monthly validation)**")
        summary_rows = [item.as_dict() for item in st.session_state["bank_balance_summaries"]]
        _render_table(pd.DataFrame(summary_rows))

    daily_balances_by_file = st.session_state.get("bank_daily_balances_by_file", {})
    if daily_balances_by_file:
        st.markdown("**Parsed Daily Balances (statement control totals)**")
        daily_rows = []
        for file_name, points in daily_balances_by_file.items():
            for point in points:
                payload = point.as_dict()
                payload["source_file"] = file_name
                daily_rows.append(payload)
        _render_table(pd.DataFrame(daily_rows))


def _render_exports() -> None:
    st.subheader("Exports")
    _render_help_box(
        "Choose the output folder, run export checks, then generate final CSV/XLSX and PDF reports."
    )
    st.caption(
        "If this app is running on Streamlit Cloud, files are written to the cloud container path, not your local PC. "
        "Use the download buttons below to save files locally."
    )
    output_dir_text = st.text_input("Output folder", value=str(DEFAULT_OUTPUT_DIR))
    company_name = st.text_input("Company Name for PDF Reports", value="Company")
    tolerance_value = st.number_input(
        "Reconciliation tolerance",
        min_value=0.0,
        value=float(st.session_state.get("reconciliation_tolerance", MONEY_TOLERANCE)),
        step=0.01,
        format="%.4f",
        help="Checks must be within this absolute tolerance to pass.",
    )
    st.session_state["reconciliation_tolerance"] = float(tolerance_value)
    if st.button("Export Raw + P&L CSV/XLSX", key="btn_export_csv_xlsx"):
        output_dir = Path(output_dir_text)
        output_dir.mkdir(parents=True, exist_ok=True)
        transactions = st.session_state["bank_transactions"]
        pnl = st.session_state["bank_pnl_build"]
        full_year = _has_full_year_of_data(transactions)
        pre_checks_df = run_pre_export_reconciliation_checks(
            transactions=transactions,
            pnl=pnl,
            balance_summaries=st.session_state.get("bank_balance_summaries", []),
            daily_balances_by_file=st.session_state.get("bank_daily_balances_by_file", {}),
            tolerance=float(tolerance_value),
        )
        pre_failed = pre_checks_df[pre_checks_df["status"] == "FAIL"]
        st.markdown("**Pre-export Reconciliation Checks**")
        _render_table(_build_reconciliation_display_frame(pre_checks_df))
        if not pre_failed.empty:
            st.error(f"{len(pre_failed)} pre-export reconciliation check(s) failed. Export cancelled.")
            st.markdown("**Pre-export Failures: What Broke**")
            _render_table(_build_failed_reconciliation_frame(pre_failed))
            return

        export_pairs, export_paths = _write_local_csv_xlsx_outputs(output_dir, transactions, pnl)
        checks_df = run_export_reconciliation_checks(
            transactions=transactions,
            pnl=pnl,
            export_pairs=export_pairs,
            balance_summaries=st.session_state.get("bank_balance_summaries", []),
            daily_balances_by_file=st.session_state.get("bank_daily_balances_by_file", {}),
            tolerance=float(tolerance_value),
        )
        checks_path = output_dir / "export_reconciliation_report.csv"
        checks_df.to_csv(checks_path, index=False)

        balance_checks = checks_df[
            checks_df["check_name"].str.startswith(("balance_summary_", "daily_balance_"))
        ].copy()
        balance_checks_path = output_dir / "balance_summary_validation.csv"
        balance_checks.to_csv(balance_checks_path, index=False)

        learned_dest = output_dir / "learned_rules_bank.json"
        learned_dest.write_text(LEARNED_BANK_RULES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        individual_rules_csv, bulk_rules_csv, rules_manifest_path = _write_split_rule_csv_exports(output_dir)

        failed_checks = checks_df[checks_df["status"] == "FAIL"]
        st.markdown("**Export Reconciliation Checks**")
        reconciliation_display = _build_reconciliation_display_frame(checks_df)
        _render_table(reconciliation_display)

        exported_message = (
            "Exported:\n"
            f"- {export_paths['raw_csv']}\n"
            f"- {export_paths['raw_xlsx']}\n"
            f"- {export_paths['monthly_csv']}\n"
            f"- {export_paths['monthly_xlsx']}\n"
            f"- {export_paths['period_total_csv']}\n"
            f"- {export_paths['period_total_xlsx']}\n"
            f"- {export_paths['period_summary_csv']}\n"
            f"- {export_paths['period_summary_xlsx']}\n"
            f"- {export_paths['manual_assets_csv']}\n"
            f"- {export_paths['manual_assets_xlsx']}\n"
            f"- {checks_path}\n"
            f"- {balance_checks_path}\n"
            f"- {learned_dest}\n"
            f"- {individual_rules_csv}\n"
            f"- {bulk_rules_csv}\n"
            f"- {rules_manifest_path}"
        )
        if not full_year:
            exported_message += "\n- labels use Period (not Yearly) because the span is under 12 months"
        if failed_checks.empty:
            st.success("All reconciliation checks passed. Export files add up.")
            st.markdown("**How It Was Calculated**")
            _render_table(reconciliation_display)
            st.success(exported_message)
        else:
            st.error(f"{len(failed_checks)} reconciliation check(s) failed. Review export_reconciliation_report.csv.")
            st.markdown("**Failed Checks: What Broke**")
            _render_table(_build_failed_reconciliation_frame(failed_checks))
            st.markdown("**How It Was Calculated**")
            _render_table(reconciliation_display)
            st.warning(exported_message)

        st.session_state["latest_export_files"] = [
            export_paths["raw_csv"],
            export_paths["raw_xlsx"],
            export_paths["monthly_csv"],
            export_paths["monthly_xlsx"],
            export_paths["period_total_csv"],
            export_paths["period_total_xlsx"],
            export_paths["period_summary_csv"],
            export_paths["period_summary_xlsx"],
            export_paths["manual_assets_csv"],
            export_paths["manual_assets_xlsx"],
            checks_path,
            balance_checks_path,
            learned_dest,
            individual_rules_csv,
            bulk_rules_csv,
            rules_manifest_path,
        ]

    if st.button("Generate PDF Reports", key="btn_generate_pdf_reports"):
        output_dir = Path(output_dir_text)
        output_dir.mkdir(parents=True, exist_ok=True)
        transactions = st.session_state["bank_transactions"]
        pnl = st.session_state["bank_pnl_build"]
        balance_summaries = st.session_state.get("bank_balance_summaries", [])

        period_label, as_of_label, period_slug = _derive_pdf_report_labels(transactions)
        pnl_pdf = output_dir / f"Profit_and_Loss_{period_slug}.pdf"
        balance_pdf = output_dir / f"Balance_Sheet_As_of_{as_of_label.replace('/', '-')}.pdf"

        try:
            generate_profit_and_loss_pdf(
                pnl=pnl,
                output_path=pnl_pdf,
                company_name=company_name,
                period_label=period_label,
            )
            generate_balance_sheet_pdf(
                pnl=pnl,
                balance_summaries=balance_summaries,
                output_path=balance_pdf,
                company_name=company_name,
                as_of_label=as_of_label,
            )
        except RuntimeError as error:
            st.error(str(error))
            return

        st.success(
            "Generated PDF reports:\n"
            f"- {pnl_pdf}\n"
            f"- {balance_pdf}"
        )
        prior_files = st.session_state.get("latest_export_files", [])
        st.session_state["latest_export_files"] = [*prior_files, pnl_pdf, balance_pdf]

    _render_export_downloads()


def _render_export_downloads() -> None:
    latest_files = st.session_state.get("latest_export_files", [])
    if not latest_files:
        return

    file_paths = [Path(path) for path in latest_files]
    existing_files = [path for path in file_paths if path.exists() and path.is_file()]
    if not existing_files:
        return

    st.markdown("**Download Export Files**")
    zip_bytes = _build_zip_bytes(existing_files)
    st.download_button(
        "Download All Exports (.zip)",
        data=zip_bytes,
        file_name="trucking_pnl_exports.zip",
        mime="application/zip",
        key="btn_download_exports_zip",
    )

    for path in existing_files:
        st.download_button(
            f"Download {path.name}",
            data=path.read_bytes(),
            file_name=path.name,
            mime="application/octet-stream",
            key=f"btn_download_{path.name}",
        )


def _build_zip_bytes(file_paths: list[Path]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in file_paths:
            archive.writestr(path.name, path.read_bytes())
    buffer.seek(0)
    return buffer.getvalue()


def _write_split_rule_csv_exports(output_dir: Path) -> tuple[Path, Path, Path]:
    all_rules = load_rule_dicts(LEARNED_BANK_RULES_PATH)
    individual_rules = [rule for rule in all_rules if str(rule.get("match_type", "")).lower() == "exact"]
    bulk_rules = [rule for rule in all_rules if str(rule.get("match_type", "")).lower() == "contains"]

    individual_frame = _build_rules_frame(individual_rules)
    bulk_frame = _build_rules_frame(bulk_rules)

    individual_path = output_dir / "rules_individual_exact.csv"
    bulk_path = output_dir / "rules_bulk_contains.csv"
    manifest_path = output_dir / "rules_manifest.json"

    individual_frame.to_csv(individual_path, index=False)
    bulk_frame.to_csv(bulk_path, index=False)
    manifest = {
        "backup_version": 1,
        "rules_restore_priority": [
            "rules_individual_exact.csv",
            "rules_bulk_contains.csv",
            "learned_rules_bank.json",
        ],
        "rules_csv_delineation": {
            "rules_individual_exact.csv": "match_type=exact (individual rules)",
            "rules_bulk_contains.csv": "match_type=contains (bulk rules)",
        },
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return individual_path, bulk_path, manifest_path


def _build_rules_frame(rule_rows: list[dict]) -> pd.DataFrame:
    columns = [
        "id",
        "match_type",
        "pattern",
        "exclude_pattern",
        "category",
        "non_pnl",
        "field",
        "confidence",
        "priority",
        "origin",
    ]
    frame = pd.DataFrame(rule_rows, columns=columns)
    if frame.empty:
        return pd.DataFrame(columns=columns)
    return frame.fillna("")


def _restore_rules_from_uploaded_file(payload_bytes: bytes, file_name: str, transactions=None) -> tuple[int, str]:
    suffix = Path(file_name).suffix.lower().strip()
    if suffix == ".zip":
        rows, mode = _restore_rules_from_zip_bytes(payload_bytes, transactions=transactions)
        if mode == "csv":
            return rows, "zip_csv"
        if mode == "json":
            return rows, "zip_json"
        return rows, mode
    if suffix == ".csv":
        return _restore_rules_from_csv_bytes(payload_bytes, transactions=transactions)
    if suffix == ".json":
        return _restore_rules_from_json_bytes(payload_bytes, transactions=transactions)

    # Fallback: attempt ZIP first, then CSV/JSON.
    rows, mode = _restore_rules_from_zip_bytes(payload_bytes, transactions=transactions)
    if mode != "none":
        return rows, mode
    rows, mode = _restore_rules_from_csv_bytes(payload_bytes, transactions=transactions)
    if mode != "none":
        return rows, mode
    return _restore_rules_from_json_bytes(payload_bytes, transactions=transactions)


def _restore_rules_from_csv_bytes(payload_bytes: bytes, transactions=None) -> tuple[int, str]:
    frame = pd.read_csv(io.BytesIO(payload_bytes))
    if frame.empty:
        return 0, "none"
    rows = []
    for row in frame.to_dict(orient="records"):
        cleaned_row = {key: ("" if pd.isna(value) else value) for key, value in row.items()}
        sanitized = _sanitize_rule_row(cleaned_row)
        if sanitized:
            rows.append(sanitized)
    if not rows:
        return 0, "none"

    exact_rules = [row for row in rows if str(row.get("match_type", "")).strip().lower() == "exact"]
    contains_rules = [row for row in rows if str(row.get("match_type", "")).strip().lower() == "contains"]
    _save_split_rule_tables(
        transactions=transactions,
        exact_rules=exact_rules if exact_rules else None,
        contains_rules=contains_rules if contains_rules else None,
    )
    return len(rows), "csv"


def _restore_rules_from_json_bytes(payload_bytes: bytes, transactions=None) -> tuple[int, str]:
    payload = json.loads(payload_bytes.decode("utf-8"))
    if isinstance(payload, dict):
        rules = payload.get("rules", [])
    elif isinstance(payload, list):
        rules = payload
    else:
        return 0, "none"
    if not isinstance(rules, list):
        return 0, "none"

    sanitized_rows = []
    for row in rules:
        if not isinstance(row, dict):
            continue
        cleaned_row = {key: ("" if pd.isna(value) else value) for key, value in row.items()}
        sanitized = _sanitize_rule_row(cleaned_row)
        if sanitized:
            sanitized_rows.append(sanitized)
    if not sanitized_rows:
        return 0, "none"

    exact_rules = [row for row in sanitized_rows if str(row.get("match_type", "")).strip().lower() == "exact"]
    contains_rules = [row for row in sanitized_rows if str(row.get("match_type", "")).strip().lower() == "contains"]
    _save_split_rule_tables(
        transactions=transactions,
        exact_rules=exact_rules if exact_rules else None,
        contains_rules=contains_rules if contains_rules else None,
    )
    return len(sanitized_rows), "json"


def _restore_rules_from_zip_bytes(zip_bytes: bytes, transactions=None) -> tuple[int, str]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as archive:
        name_lookup = {name.lower(): name for name in archive.namelist()}
        individual_name = _first_existing_name(
            name_lookup,
            ["rules_individual_exact.csv", "individual_rules.csv", "learned_rules_individual.csv"],
        )
        bulk_name = _first_existing_name(
            name_lookup,
            ["rules_bulk_contains.csv", "bulk_rules.csv", "learned_rules_bulk.csv"],
        )
        if individual_name and bulk_name:
            exact_rules = _read_rules_csv(archive, individual_name)
            contains_rules = _read_rules_csv(archive, bulk_name)
            _save_split_rule_tables(transactions=transactions, exact_rules=exact_rules, contains_rules=contains_rules)
            return len(exact_rules) + len(contains_rules), "csv"

        json_name = _first_existing_name(name_lookup, ["learned_rules_bank.json", "learned_rules.json"])
        if json_name:
            payload = json.loads(archive.read(json_name))
            rules = payload.get("rules", [])
            if isinstance(rules, list):
                save_rule_dicts(LEARNED_BANK_RULES_PATH, [rule for rule in rules if isinstance(rule, dict)])
                if transactions is not None:
                    _classify(transactions)
                return len(rules), "json"
    return 0, "none"


def _first_existing_name(name_lookup: dict[str, str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        key = candidate.lower()
        if key in name_lookup:
            return name_lookup[key]
    return None


def _read_rules_csv(archive: zipfile.ZipFile, filename: str) -> list[dict]:
    frame = pd.read_csv(io.BytesIO(archive.read(filename)))
    if frame.empty:
        return []
    rows = []
    for row in frame.to_dict(orient="records"):
        cleaned_row = {key: ("" if pd.isna(value) else value) for key, value in row.items()}
        sanitized = _sanitize_rule_row(cleaned_row)
        if sanitized:
            rows.append(sanitized)
    return rows


def _render_reference_pnl_totals(
    *,
    show_comparison: bool = False,
    generated_pnl_detail: pd.DataFrame | None = None,
) -> None:
    reference_documents = st.session_state.get("reference_pnl_documents", [])
    if not reference_documents:
        reference_totals = st.session_state.get("reference_pnl_totals", [])
        if not reference_totals:
            return
        reference_documents = [
            {
                "file_name": ", ".join(st.session_state.get("reference_pnl_file_names", [])) or "reference_pnl",
                "period_start": None,
                "period_end": None,
                "totals": [item.as_dict() if hasattr(item, "as_dict") else item for item in reference_totals],
            }
        ]

    summary_rows = []
    for document in reference_documents:
        summary_rows.append(
            {
                "file_name": document.get("file_name"),
                "period_start": document.get("period_start"),
                "period_end": document.get("period_end"),
                "category_rows": len(document.get("totals", [])),
            }
        )
    st.markdown("**Imported Reference P&L Documents**")
    _render_table(pd.DataFrame(summary_rows))

    selected_file = _default_reference_pnl_file(reference_documents)
    selected_file = st.selectbox(
        "Reference P&L file for comparison",
        options=[item["file_name"] for item in reference_documents],
        index=[item["file_name"] for item in reference_documents].index(selected_file),
        key="reference_pnl_selected_file",
    )
    selected_doc = next(item for item in reference_documents if item["file_name"] == selected_file)
    selected_totals = selected_doc.get("totals", [])
    if not selected_totals:
        return

    rows = selected_totals
    ref_df = pd.DataFrame(rows)
    st.markdown(f"**Reference P&L Totals: {selected_file}**")
    _render_table(ref_df)

    if not show_comparison or generated_pnl_detail is None or generated_pnl_detail.empty:
        return

    ref_rollup = (
        ref_df.assign(category_key=ref_df["category_name"].astype(str).str.strip().str.lower())
        .groupby("category_key", as_index=False)["amount"]
        .sum()
        .rename(columns={"amount": "reference_amount"})
    )
    gen_rollup = (
        generated_pnl_detail.assign(category_key=generated_pnl_detail["category"].astype(str).str.strip().str.lower())
        .groupby("category_key", as_index=False)["amount"]
        .sum()
        .rename(columns={"amount": "generated_amount"})
    )
    comparison = gen_rollup.merge(ref_rollup, on="category_key", how="outer").fillna(0.0)
    comparison["difference"] = comparison["generated_amount"] - comparison["reference_amount"]
    comparison = comparison.sort_values(by="category_key").reset_index(drop=True)
    st.markdown("**Generated vs Reference P&L Category Comparison**")
    _render_table(comparison)


def _default_reference_pnl_file(reference_documents: list[dict]) -> str:
    if not reference_documents:
        return ""
    transaction_period = _bank_transaction_period(st.session_state.get("bank_transactions", []))
    if transaction_period == (None, None):
        return reference_documents[0]["file_name"]
    txn_start, txn_end = transaction_period
    best_file = reference_documents[0]["file_name"]
    best_overlap = -1
    for document in reference_documents:
        doc_start = _safe_iso_date(document.get("period_start"))
        doc_end = _safe_iso_date(document.get("period_end"))
        overlap = _period_overlap_days(txn_start, txn_end, doc_start, doc_end)
        if overlap > best_overlap:
            best_overlap = overlap
            best_file = document["file_name"]
    return best_file


def _bank_transaction_period(transactions) -> tuple[date | None, date | None]:
    dates = sorted([txn.date for txn in transactions if getattr(txn, "date", None) is not None])
    if not dates:
        return None, None
    return dates[0], dates[-1]


def _safe_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _period_overlap_days(
    left_start: date | None,
    left_end: date | None,
    right_start: date | None,
    right_end: date | None,
) -> int:
    if not all([left_start, left_end, right_start, right_end]):
        return 0
    start = max(left_start, right_start)
    end = min(left_end, right_end)
    if start > end:
        return 0
    return (end - start).days + 1


def _write_local_csv_xlsx_outputs(output_dir: Path, transactions, pnl):
    raw_df = transactions_dataframe(transactions)
    raw_csv = output_dir / "bank_transactions_raw.csv"
    raw_xlsx = output_dir / "bank_transactions_raw.xlsx"
    raw_df.to_csv(raw_csv, index=False)
    raw_df.to_excel(raw_xlsx, index=False)

    monthly_csv = output_dir / "monthly_pnl_detail.csv"
    monthly_xlsx = output_dir / "monthly_pnl_detail.xlsx"
    pnl.monthly_detail.to_csv(monthly_csv, index=False)
    pnl.monthly_detail.to_excel(monthly_xlsx, index=False)

    period_total_csv = output_dir / "period_total_by_category.csv"
    period_total_xlsx = output_dir / "period_total_by_category.xlsx"
    pnl.yearly_detail.to_csv(period_total_csv, index=False)
    pnl.yearly_detail.to_excel(period_total_xlsx, index=False)

    period_summary_csv = output_dir / "period_summary.csv"
    period_summary_xlsx = output_dir / "period_summary.xlsx"
    pnl.yearly_summary.to_csv(period_summary_csv, index=False)
    pnl.yearly_summary.to_excel(period_summary_xlsx, index=False)

    manual_assets = pd.DataFrame(st.session_state.get("manual_asset_entries", []), columns=["asset_name", "amount"])
    manual_assets_csv = output_dir / "manual_asset_entries.csv"
    manual_assets_xlsx = output_dir / "manual_asset_entries.xlsx"
    manual_assets.to_csv(manual_assets_csv, index=False)
    manual_assets.to_excel(manual_assets_xlsx, index=False)

    export_pairs = {
        "bank_transactions_raw": (raw_csv, raw_xlsx),
        "monthly_pnl_detail": (monthly_csv, monthly_xlsx),
        "period_total_by_category": (period_total_csv, period_total_xlsx),
        "period_summary": (period_summary_csv, period_summary_xlsx),
        "manual_asset_entries": (manual_assets_csv, manual_assets_xlsx),
    }
    export_paths = {
        "raw_csv": raw_csv,
        "raw_xlsx": raw_xlsx,
        "monthly_csv": monthly_csv,
        "monthly_xlsx": monthly_xlsx,
        "period_total_csv": period_total_csv,
        "period_total_xlsx": period_total_xlsx,
        "period_summary_csv": period_summary_csv,
        "period_summary_xlsx": period_summary_xlsx,
        "manual_assets_csv": manual_assets_csv,
        "manual_assets_xlsx": manual_assets_xlsx,
    }
    return export_pairs, export_paths


def _derive_period_labels(transactions) -> tuple[str, str]:
    dates = sorted(txn.date for txn in transactions if txn.date is not None)
    if not dates:
        return "For the period", "Unknown Date"
    start = dates[0]
    end = dates[-1]
    period_label = f"For the period {start:%m/%d/%Y} to {end:%m/%d/%Y}"
    as_of_label = f"{end:%m/%d/%Y}"
    return period_label, as_of_label


def _derive_pdf_report_labels(transactions) -> tuple[str, str, str]:
    dates = sorted(txn.date for txn in transactions if txn.date is not None)
    if not dates:
        return "For the period", "Unknown Date", "Period"
    start = dates[0]
    end = dates[-1]
    as_of_label = f"{end:%m/%d/%Y}"
    if start.year == end.year:
        if start.month == end.month:
            label = f"For {start:%B %Y}"
            slug = f"{start:%b_%Y}"
        else:
            label = f"For the period {start:%b}-{end:%b} {start.year}"
            slug = f"{start:%b}-{end:%b}_{start.year}"
    else:
        label = f"For the period {start:%m/%d/%Y} to {end:%m/%d/%Y}"
        slug = f"{start:%Y%m%d}_to_{end:%Y%m%d}"
    return label, as_of_label, slug


def _clone_transactions(transactions):
    return [copy.deepcopy(txn) for txn in transactions]


def _transactions_for_assignment(transactions, assignment):
    pattern = str(assignment.get("normalized_description", "")).strip().lower()
    match_type = str(assignment.get("match_type", "exact")).strip().lower()
    exclude_values = _split_exclude_text(str(assignment.get("exclude_pattern", "")))
    impacted = []
    for txn in transactions:
        normalized = str(txn.normalized_description or "").strip().lower()
        if not normalized:
            continue
        if any(exclude in normalized for exclude in exclude_values):
            continue
        if match_type == "contains":
            if pattern in normalized:
                impacted.append(txn)
            continue
        if normalized == pattern:
            impacted.append(txn)
    return impacted


def _ensure_workflow_state() -> None:
    st.session_state.setdefault("workflow_step", 1)
    st.session_state.setdefault("review_completed", False)
    st.session_state.setdefault("reports_completed", False)
    st.session_state.setdefault("pending_review_assignments", {})
    st.session_state.setdefault("manual_asset_entries", [])


def _render_workflow_sidebar() -> None:
    st.markdown("**Workflow**")
    current_step = st.session_state["workflow_step"]
    for step_number, label in WORKFLOW_STEPS.items():
        if step_number < current_step:
            status = "done"
        elif step_number == current_step:
            status = "active"
        else:
            status = "locked"
        st.write(f"{step_number}. {label} [{status}]")


def _render_workflow_overview() -> None:
    st.markdown("**Workflow Overview**")
    current_step = st.session_state["workflow_step"]
    rows = []
    for step_number, label in WORKFLOW_STEPS.items():
        if step_number < current_step:
            status = "done"
        elif step_number == current_step:
            status = "active"
        else:
            status = "locked"
        rows.append({"step": step_number, "name": label, "status": status})
    frame = pd.DataFrame(rows)
    _render_table(frame)
    st.markdown(
        f'<div class="next-action-box"><strong>Next Action:</strong> {_next_action_message()}</div>',
        unsafe_allow_html=True,
    )


def _next_action_message() -> str:
    current_step = st.session_state["workflow_step"]
    if current_step == 1:
        return "Upload bank statement PDF(s) and click Parse Statements."
    if current_step == 2:
        if "bank_transactions" not in st.session_state:
            return "Parse statements first."
        unresolved_count = _count_unresolved_transactions(st.session_state["bank_transactions"])
        pending_count = len(st.session_state.get("pending_review_assignments", {}))
        if unresolved_count > 0 or pending_count > 0:
            return (
                "Complete category review. "
                f"Unresolved transactions: {unresolved_count}. Pending unsaved assignments: {pending_count}."
            )
        return "Click Complete Step 2 And Continue To Reports."
    if current_step == 3:
        return "Review P&L and control totals, then click Approve Reports And Continue To Export."
    return "Export CSV/XLSX/PDF outputs."


def _split_exclude_text(value: str) -> list[str]:
    if not value:
        return []
    normalized = value.replace("\n", "||").replace(",", "||")
    return [item.strip().lower() for item in normalized.split("||") if item.strip()]


def _merge_exclude_text(manual_exclude_text: str, queue_excluded_values: list[str]) -> str:
    exclude_items = set(_split_exclude_text(manual_exclude_text))
    for value in queue_excluded_values:
        if value.strip():
            exclude_items.add(value.strip().lower())
    return "||".join(sorted(exclude_items))


def _count_unresolved_transactions(transactions, *, actionable_only: bool = False) -> int:
    unresolved = 0
    for txn in transactions:
        if (txn.resolved_category or "").upper() != "UNCLASSIFIED":
            continue
        if actionable_only and not str(getattr(txn, "normalized_description", "") or "").strip():
            continue
        unresolved += 1
    return unresolved


def _count_hidden_unresolved_transactions(transactions) -> int:
    hidden = 0
    for txn in transactions:
        if (txn.resolved_category or "").upper() != "UNCLASSIFIED":
            continue
        if str(getattr(txn, "normalized_description", "") or "").strip():
            continue
        hidden += 1
    return hidden


def _has_full_year_of_data(transactions) -> bool:
    dates = sorted(txn.date for txn in transactions if txn.date is not None)
    if len(dates) < 2:
        return False
    return (dates[-1] - dates[0]).days >= 365


def _coerce_numeric(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    is_parentheses_negative = text.startswith("(") and text.endswith(")")
    cleaned = text.replace("$", "").replace(",", "").replace("(", "").replace(")", "")
    if cleaned.startswith("+"):
        cleaned = cleaned[1:]
    try:
        numeric = float(cleaned)
    except ValueError:
        return None
    if is_parentheses_negative:
        return -abs(numeric)
    return numeric


def _coerce_date(value):
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _failed_check_scope(check_name: str) -> tuple[str | None, str | None]:
    if check_name.startswith("daily_balance_match__"):
        payload = check_name.replace("daily_balance_match__", "", 1)
        if "__" in payload:
            source_file, date_text = payload.rsplit("__", 1)
            return source_file, date_text
    if check_name.startswith("daily_balance_date_coverage__"):
        source_file = check_name.replace("daily_balance_date_coverage__", "", 1)
        return source_file, None
    if check_name.startswith("balance_summary_") and "__" in check_name:
        source_file = check_name.split("__", 1)[1]
        return source_file, None
    return None, None


def _transactions_for_failed_check(transactions, check_name: str) -> list[dict]:
    source_file, target_date = _failed_check_scope(check_name)
    rows: list[dict] = []
    for idx, txn in enumerate(transactions):
        if source_file and txn.source_file != source_file:
            continue
        if target_date:
            txn_date = txn.date.isoformat() if txn.date is not None else ""
            if txn_date != target_date:
                continue
        rows.append(
            {
                "txn_index": idx,
                "date": txn.date.isoformat() if txn.date else "",
                "raw_description": txn.raw_description,
                "amount": float(txn.amount),
                "direction": txn.direction,
                "source_account": txn.source_account,
                "source_file": txn.source_file,
                "original_category": txn.original_category,
                "resolved_category": txn.resolved_category,
                "running_balance": txn.running_balance,
                "reference_number": txn.reference_number,
            }
        )
    return rows


def _apply_failed_check_edits(transactions, edited_frame: pd.DataFrame) -> int:
    changes = 0
    for row in edited_frame.to_dict(orient="records"):
        index_value = _coerce_numeric(row.get("txn_index"))
        if index_value is None:
            continue
        txn_index = int(round(index_value))
        if txn_index < 0 or txn_index >= len(transactions):
            continue
        txn = transactions[txn_index]
        updated = False

        new_date = _coerce_date(row.get("date"))
        if new_date != txn.date:
            txn.date = new_date
            updated = True

        amount_value = _coerce_numeric(row.get("amount"))
        if amount_value is not None and round(float(amount_value), 2) != round(float(txn.amount), 2):
            txn.amount = float(amount_value)
            updated = True

        new_raw_description = str(row.get("raw_description", "")).strip()
        if new_raw_description and new_raw_description != txn.raw_description:
            txn.raw_description = new_raw_description
            updated = True

        new_direction = str(row.get("direction", "")).strip().lower()
        if new_direction in {"debit", "credit"} and new_direction != (txn.direction or "").strip().lower():
            txn.direction = new_direction
            updated = True

        reference_number = str(row.get("reference_number", "")).strip() or None
        if reference_number != txn.reference_number:
            txn.reference_number = reference_number
            updated = True

        if updated:
            changes += 1
    return changes


def _format_currency_value(value) -> str:
    numeric = _coerce_numeric(value)
    if numeric is None:
        return ""
    if abs(numeric) < 0.005:
        numeric = 0.0
    return f"${numeric:,.2f}"


def _format_count_value(value) -> str:
    numeric = _coerce_numeric(value)
    if numeric is None:
        return ""
    return f"{int(round(numeric)):,}"


def _is_currency_column_name(name: str) -> bool:
    lower = name.lower()
    currency_tokens = (
        "amount",
        "balance",
        "total",
        "income",
        "expense",
        "profit",
        "difference",
        "cash",
        "equity",
        "liabilities",
        "assets",
        "net_change",
    )
    return any(token in lower for token in currency_tokens)


def _is_count_column_name(name: str) -> bool:
    lower = name.lower()
    count_tokens = ("count", "occurrences", "transactions", "rows")
    return any(token in lower for token in count_tokens)


def _format_generic_numeric(value) -> str:
    numeric = _coerce_numeric(value)
    if numeric is None:
        return ""
    if abs(numeric) < 0.005:
        numeric = 0.0
    if abs(numeric - round(numeric)) < 1e-9:
        return f"{int(round(numeric)):,}"
    return f"{numeric:,.2f}"


def _format_display_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    output = frame.copy()
    for column in output.columns:
        series = output[column]
        if pd.api.types.is_bool_dtype(series):
            continue
        if _is_currency_column_name(column):
            output[column] = series.map(
                lambda value: _format_currency_value(value) if _coerce_numeric(value) is not None else value
            )
            continue
        if _is_count_column_name(column):
            output[column] = series.map(
                lambda value: _format_count_value(value) if _coerce_numeric(value) is not None else value
            )
            continue
        if not pd.api.types.is_numeric_dtype(series):
            continue
        output[column] = series.map(_format_generic_numeric)
    return output


def _is_count_check_name(check_name: str) -> bool:
    lowered = check_name.lower()
    return "row_count" in lowered or "record_match" in lowered or "date_coverage" in lowered


def _sanitize_rule_row(row: dict) -> dict:
    cleaned = {key: value for key, value in row.items() if key != "select_delete"}
    has_signal = any(
        str(cleaned.get(field, "")).strip()
        for field in ("id", "match_type", "pattern", "category", "field")
    )
    if not has_signal:
        return {}
    confidence = _coerce_numeric(cleaned.get("confidence"))
    priority = _coerce_numeric(cleaned.get("priority"))
    if confidence is not None:
        cleaned["confidence"] = float(confidence)
    if priority is not None:
        cleaned["priority"] = int(round(priority))
    non_pnl = cleaned.get("non_pnl")
    if isinstance(non_pnl, str):
        cleaned["non_pnl"] = non_pnl.strip().lower() in {"true", "1", "yes", "y"}
    return cleaned


def _format_recon_scalar(value, *, money: bool) -> str:
    numeric = _coerce_numeric(value)
    if numeric is None:
        return str(value)
    if abs(numeric) < 0.005:
        numeric = 0.0
    if money:
        return f"${numeric:,.2f}"
    return f"{int(round(numeric)):,}"


def _reconciliation_failure_reason(row: pd.Series, *, is_count: bool) -> str:
    expected = _coerce_numeric(row.get("expected"))
    actual = _coerce_numeric(row.get("actual"))
    difference = _coerce_numeric(row.get("difference"))
    tolerance = _coerce_numeric(row.get("tolerance"))
    details = str(row.get("details", "")).strip()
    if is_count:
        if expected is not None and actual is not None:
            delta = int(round(abs(actual - expected)))
            return f"Count mismatch by {delta}. {details}"
        return details
    if difference is not None and tolerance is not None:
        excess = abs(difference) - tolerance
        if excess < 0:
            excess = 0.0
        return f"Out of tolerance by {_format_currency_value(excess)}. {details}"
    return details


def _build_reconciliation_display_frame(checks_df: pd.DataFrame) -> pd.DataFrame:
    if checks_df.empty:
        return pd.DataFrame(columns=["check_name", "status", "expected", "actual", "difference", "tolerance", "details"])
    rows = []
    for _, row in checks_df.iterrows():
        check_name = str(row.get("check_name", ""))
        is_count = _is_count_check_name(check_name)
        rows.append(
            {
                "check_name": check_name,
                "status": str(row.get("status", "")),
                "expected": _format_recon_scalar(row.get("expected", ""), money=not is_count),
                "actual": _format_recon_scalar(row.get("actual", ""), money=not is_count),
                "difference": _format_recon_scalar(row.get("difference", ""), money=not is_count),
                "tolerance": _format_recon_scalar(row.get("tolerance", ""), money=not is_count),
                "details": str(row.get("details", "")),
            }
        )
    return pd.DataFrame(rows)


def _build_failed_reconciliation_frame(failed_checks: pd.DataFrame) -> pd.DataFrame:
    if failed_checks.empty:
        return pd.DataFrame(columns=["check_name", "status", "expected", "actual", "difference", "tolerance", "failure_reason"])
    rows = []
    for _, row in failed_checks.iterrows():
        check_name = str(row.get("check_name", ""))
        is_count = _is_count_check_name(check_name)
        rows.append(
            {
                "check_name": check_name,
                "status": str(row.get("status", "")),
                "expected": _format_recon_scalar(row.get("expected", ""), money=not is_count),
                "actual": _format_recon_scalar(row.get("actual", ""), money=not is_count),
                "difference": _format_recon_scalar(row.get("difference", ""), money=not is_count),
                "tolerance": _format_recon_scalar(row.get("tolerance", ""), money=not is_count),
                "failure_reason": _reconciliation_failure_reason(row, is_count=is_count),
            }
        )
    return pd.DataFrame(rows)


def _signed_number_style(value) -> str:
    numeric = _coerce_numeric(value)
    if numeric is None:
        return ""
    if numeric > 0:
        return "color: #166534; font-weight: 600;"
    if numeric < 0:
        return "color: #B42318; font-weight: 600;"
    return "color: #334155;"


def _render_table(frame: pd.DataFrame, *, use_container_width: bool = True, hide_index: bool = True) -> None:
    display_frame = _format_display_frame(frame)
    styled = display_frame.style
    if hasattr(styled, "applymap"):
        styled = styled.applymap(_signed_number_style)
    elif hasattr(styled, "map"):
        styled = styled.map(_signed_number_style)
    if hide_index and hasattr(styled, "hide"):
        styled = styled.hide(axis="index")
    st.dataframe(styled, use_container_width=use_container_width)


def _reset_app_session() -> None:
    for key in SESSION_RESET_KEYS:
        if key in st.session_state:
            del st.session_state[key]


def _inject_ui_styles() -> None:
    st.markdown(
        """
        <style>
        :root {
          --flow-accent: #2a5cab;
          --flow-accent-strong: #21498a;
          --flow-accent-soft: #eef4ff;
          --flow-success: #21874b;
          --flow-success-strong: #18663a;
          --flow-danger: #c62828;
          --flow-danger-strong: #991b1b;
          --flow-warning: #d97706;
          --flow-border: #cfd8e3;
          --flow-bg-top: #f6f8fc;
          --flow-bg-bottom: #edf1f7;
          --flow-surface: #ffffff;
          --flow-text: #0f1c2e;
          --flow-muted: #4c5d72;
        }
        .stApp {
          background: linear-gradient(180deg, var(--flow-bg-top) 0%, var(--flow-bg-bottom) 100%);
          color: var(--flow-text);
        }
        section[data-testid="stSidebar"] {
          background: #f4f7fc;
          border-right: 1px solid var(--flow-border);
        }
        .block-container {
          max-width: 1320px;
          background: #ffffff;
          border: 1px solid var(--flow-border);
          border-radius: 14px;
          box-shadow: 0 2px 8px rgba(15, 23, 42, 0.06);
          padding-top: 1.2rem;
          padding-left: 1.4rem;
          padding-right: 1.4rem;
          padding-bottom: 2rem;
        }
        h1, h2, h3, h4 {
          letter-spacing: 0.1px;
          color: var(--flow-text);
        }
        p, label, .stCaption, .stMarkdown, div[data-testid="stMetricLabel"], .stRadio label, .stSelectbox label, .stTextInput label {
          color: var(--flow-text);
        }
        .stCaption {
          color: var(--flow-muted) !important;
        }
        .stMarkdown, .stTextInput, .stSelectbox, .stRadio, .stCheckbox {
          font-size: 0.97rem;
        }
        .stTextInput input, .stSelectbox [data-baseweb="select"] > div {
          background: #ffffff !important;
          border: 1px solid var(--flow-border) !important;
          color: var(--flow-text) !important;
        }
        .stTextInput input:focus, .stSelectbox [data-baseweb="select"] > div:focus-within {
          border-color: var(--flow-accent) !important;
          box-shadow: 0 0 0 1px var(--flow-accent) inset !important;
        }
        .next-action-box {
          border: 1px solid #cddbf4;
          border-left: 6px solid var(--flow-accent-strong);
          background: var(--flow-accent-soft);
          color: #173a75;
          padding: 12px 14px;
          border-radius: 10px;
          margin: 8px 0 16px 0;
          box-shadow: 0 1px 3px rgba(15, 23, 42, 0.07);
        }
        .section-chip {
          display: inline-block;
          padding: 8px 14px;
          border-radius: 999px;
          font-weight: 700;
          margin: 10px 0 6px 0;
          border: 1px solid #b4c5e0;
          background: var(--flow-accent-soft);
          color: var(--flow-accent-strong);
        }
        .stSubheader {
          border-left: 5px solid var(--flow-accent);
          padding-left: 10px;
        }
        .stDivider {
          margin: 1.2rem 0;
        }
        [data-testid="stDataFrame"] {
          border: 1px solid var(--flow-border);
          border-radius: 12px;
          background: var(--flow-surface);
          box-shadow: 0 1px 2px rgba(15, 23, 42, 0.03);
        }
        [data-testid="stDataFrame"] [role="grid"] {
          color: #15253d !important;
          font-size: 0.92rem !important;
        }
        [data-testid="stDataFrame"] [role="columnheader"] {
          background: #f4f7fb !important;
          color: #17335f !important;
          font-weight: 600 !important;
        }
        .stAlert {
          border-radius: 10px;
        }
        div[data-testid="stMetric"] {
          background: #ffffff;
          border: 1px solid var(--flow-border);
          border-radius: 10px;
          padding: 10px 12px;
        }

        /* Save/export actions: green */
        .st-key-btn_save_individual_rules button,
        .st-key-btn_save_bulk_rules button,
        .st-key-btn_save_pending_rules button,
        .st-key-btn_step3_save_failed_corrections button,
        .st-key-btn_export_csv_xlsx button,
        .st-key-btn_generate_pdf_reports button {
          background: var(--flow-success) !important;
          border: 1px solid var(--flow-success-strong) !important;
        }
        .st-key-btn_save_individual_rules button,
        .st-key-btn_save_bulk_rules button,
        .st-key-btn_save_pending_rules button,
        .st-key-btn_step3_save_failed_corrections button,
        .st-key-btn_export_csv_xlsx button,
        .st-key-btn_generate_pdf_reports button,
        .st-key-btn_save_individual_rules button p,
        .st-key-btn_save_bulk_rules button p,
        .st-key-btn_save_pending_rules button p,
        .st-key-btn_step3_save_failed_corrections button p,
        .st-key-btn_export_csv_xlsx button p,
        .st-key-btn_generate_pdf_reports button p {
          color: #ffffff !important;
        }
        .st-key-btn_save_individual_rules button:hover,
        .st-key-btn_save_bulk_rules button:hover,
        .st-key-btn_save_pending_rules button:hover,
        .st-key-btn_step3_save_failed_corrections button:hover,
        .st-key-btn_export_csv_xlsx button:hover,
        .st-key-btn_generate_pdf_reports button:hover {
          background: var(--flow-success-strong) !important;
        }

        /* Delete/remove actions: red */
        .st-key-btn_delete_individual_rules button,
        .st-key-btn_delete_bulk_rules button,
        .st-key-btn_remove_pending_assignments button {
          background: var(--flow-danger) !important;
          border: 1px solid var(--flow-danger-strong) !important;
        }
        .st-key-btn_delete_individual_rules button,
        .st-key-btn_delete_bulk_rules button,
        .st-key-btn_remove_pending_assignments button,
        .st-key-btn_delete_individual_rules button p,
        .st-key-btn_delete_bulk_rules button p,
        .st-key-btn_remove_pending_assignments button p {
          color: #ffffff !important;
        }
        .st-key-btn_delete_individual_rules button:hover,
        .st-key-btn_delete_bulk_rules button:hover,
        .st-key-btn_remove_pending_assignments button:hover {
          background: var(--flow-danger-strong) !important;
        }

        /* Primary flow/add actions: blue */
        .st-key-btn_parse_statements button,
        .st-key-btn_continue_step2 button,
        .st-key-btn_complete_step2 button,
        .st-key-btn_approve_step3 button,
        .st-key-btn_add_selected_pending button,
        .st-key-btn_add_bulk_pending button {
          background: var(--flow-accent) !important;
          border: 1px solid var(--flow-accent-strong) !important;
        }
        .st-key-btn_parse_statements button,
        .st-key-btn_continue_step2 button,
        .st-key-btn_complete_step2 button,
        .st-key-btn_approve_step3 button,
        .st-key-btn_add_selected_pending button,
        .st-key-btn_add_bulk_pending button,
        .st-key-btn_parse_statements button p,
        .st-key-btn_continue_step2 button p,
        .st-key-btn_complete_step2 button p,
        .st-key-btn_approve_step3 button p,
        .st-key-btn_add_selected_pending button p,
        .st-key-btn_add_bulk_pending button p {
          color: #ffffff !important;
        }
        .st-key-btn_parse_statements button:hover,
        .st-key-btn_continue_step2 button:hover,
        .st-key-btn_complete_step2 button:hover,
        .st-key-btn_approve_step3 button:hover,
        .st-key-btn_add_selected_pending button:hover,
        .st-key-btn_add_bulk_pending button:hover {
          background: var(--flow-accent-strong) !important;
        }

        /* Neutral controls */
        .st-key-btn_start_new_session button,
        .st-key-btn_back_step2 button,
        .st-key-btn_back_step3 button {
          border: 1px solid var(--flow-border) !important;
          background: #ffffff !important;
          color: #213247 !important;
        }

        /* Transaction exclusion helper action */
        .st-key-btn_exclude_pending_transactions button {
          background: var(--flow-warning) !important;
          color: #111827 !important;
          border: 1px solid #b25d08 !important;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
