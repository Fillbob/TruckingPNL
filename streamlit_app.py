from __future__ import annotations

import copy
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import streamlit as st

# Support running via `streamlit run financial_validator_mvp/streamlit_app.py`
# where Streamlit may set sys.path to the script directory only.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from financial_validator_mvp.models.schemas import BalanceSheetSnapshot, PnLCategoryTotal
from financial_validator_mvp.models.validation_pack import ValidationPack
from financial_validator_mvp.parsers.balance_sheet_parser import parse_balance_sheet_pdf
from financial_validator_mvp.parsers.bank_statement_parser_pnc import parse_pnc_statement_pdf
from financial_validator_mvp.parsers.document_classifier import build_validation_packs, classify_document
from financial_validator_mvp.parsers.general_ledger_parser import parse_general_ledger_pdf
from financial_validator_mvp.parsers.pnl_parser import parse_pnl_pdf
from financial_validator_mvp.services.classifier import classify_transactions, parse_rule_dicts
from financial_validator_mvp.services.normalization import normalize_category_name, normalize_transactions
from financial_validator_mvp.services.persistence import (
    DEFAULT_LEARNED_RULES_PATH,
    DEFAULT_RULES_PATH,
    load_combined_rule_dicts,
    upsert_learned_rules,
)
from financial_validator_mvp.services.reporting import (
    export_validation_artifacts,
    transactions_dataframe,
    validation_lines_dataframe,
)
from financial_validator_mvp.services.upload_dedup import build_document_report_rows, deduplicate_uploaded_files
from financial_validator_mvp.services.validator import validate_against_pnl

DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parent / "data" / "sample_outputs"


def main() -> None:
    st.set_page_config(page_title="Financial Validation MVP", layout="wide")
    st.title("Financial Validation MVP")
    st.caption("Mixed-document routing with entity/period pack validation and supplemental bank checks.")

    with st.sidebar:
        st.subheader("Inputs")
        uploaded_files = st.file_uploader(
            "Upload accounting and bank PDFs",
            type=["pdf"],
            accept_multiple_files=True,
            key="uploaded_documents",
        )
        variance_threshold = st.number_input(
            "Variance threshold ($)",
            min_value=0.0,
            value=1.0,
            step=1.0,
            help="Category variances larger than this amount are marked as variance.",
        )
        analyze_clicked = st.button("Analyze Documents", type="primary")

    if analyze_clicked:
        if not uploaded_files:
            st.error("Upload at least one PDF.")
            return
        _analyze_documents(uploaded_files=uploaded_files, variance_threshold=variance_threshold)

    if "document_metadata" not in st.session_state:
        st.info("Upload PDFs and click 'Analyze Documents' to start.")
        return

    _render_document_tables()
    _render_parse_preview()
    _render_pack_validation_flow()


def _analyze_documents(uploaded_files, variance_threshold: float) -> None:
    raw_file_bytes_map: dict[str, bytes] = {item.name: item.getvalue() for item in uploaded_files}
    file_bytes_map, duplicate_map, all_file_names = deduplicate_uploaded_files(raw_file_bytes_map)

    metadata_list = []
    for file_name, payload in file_bytes_map.items():
        try:
            metadata = classify_document(pdf_bytes=payload, file_name=file_name)
        except Exception as exc:  # pragma: no cover - UI fallback
            st.error(f"Failed to classify {file_name}: {exc}")
            continue
        metadata_list.append(metadata)

    packs = build_validation_packs(metadata_list)
    parsed_payloads = _parse_documents(file_bytes_map=file_bytes_map, metadata_list=metadata_list)
    extracted_records_by_file = _build_extracted_records_map(
        metadata_list=metadata_list,
        parsed_payloads=parsed_payloads,
    )
    document_report_rows = build_document_report_rows(
        all_file_names=all_file_names,
        metadata_list=metadata_list,
        duplicate_map=duplicate_map,
        extracted_records_by_file=extracted_records_by_file,
    )

    st.session_state["file_bytes_map"] = file_bytes_map
    st.session_state["all_uploaded_file_names"] = all_file_names
    st.session_state["duplicate_map"] = duplicate_map
    st.session_state["canonical_file_names"] = list(file_bytes_map.keys())
    st.session_state["document_report_rows"] = document_report_rows
    st.session_state["document_metadata"] = metadata_list
    st.session_state["validation_packs"] = packs
    st.session_state["parsed_transactions_by_file"] = parsed_payloads["transactions_by_file"]
    st.session_state["parsed_pnl_by_file"] = parsed_payloads["pnl_by_file"]
    st.session_state["parsed_balance_by_file"] = parsed_payloads["balance_by_file"]
    st.session_state["variance_threshold"] = variance_threshold
    first_core_pack = next((pack for pack in packs if pack.status == "core_ready"), None)
    st.session_state["selected_pack_id"] = first_core_pack.pack_id if first_core_pack else None
    st.success(f"Analyzed {len(file_bytes_map)} unique document(s) from {len(all_file_names)} upload(s).")


def _parse_documents(file_bytes_map: dict[str, bytes], metadata_list) -> dict[str, Any]:
    transactions_by_file = {}
    pnl_by_file = {}
    balance_by_file = {}

    for metadata in metadata_list:
        payload = file_bytes_map[metadata.file_name]
        if metadata.document_type == "general_ledger":
            transactions_by_file[metadata.file_name] = parse_general_ledger_pdf(
                payload,
                source_file=metadata.file_name,
            )
        elif metadata.document_type == "bank_statement":
            transactions_by_file[metadata.file_name] = parse_pnc_statement_pdf(
                payload,
                source_file=metadata.file_name,
            )
        elif metadata.document_type == "pnl":
            pnl_by_file[metadata.file_name] = parse_pnl_pdf(payload, source_file=metadata.file_name)
        elif metadata.document_type == "balance_sheet":
            balance_by_file[metadata.file_name] = parse_balance_sheet_pdf(
                payload,
                source_file=metadata.file_name,
            )

    return {
        "transactions_by_file": transactions_by_file,
        "pnl_by_file": pnl_by_file,
        "balance_by_file": balance_by_file,
    }


def _render_document_tables() -> None:
    duplicate_map: dict[str, str] = st.session_state.get("duplicate_map", {})
    packs = st.session_state["validation_packs"]
    document_report_rows = st.session_state.get("document_report_rows", [])

    if duplicate_map:
        mapping_text = ", ".join(f"{dup} -> {canonical}" for dup, canonical in duplicate_map.items())
        st.warning(f"Skipped {len(duplicate_map)} duplicate upload(s): {mapping_text}")

    st.subheader("Document Classification")
    st.dataframe(pd.DataFrame(document_report_rows), hide_index=True, use_container_width=True)

    st.subheader("Generated Validation Packs")
    pack_rows = [item.as_dict() for item in packs]
    st.dataframe(pd.DataFrame(pack_rows), hide_index=True, use_container_width=True)


def _render_parse_preview() -> None:
    st.subheader("Parse Preview")
    rows = [
        {
            "file_name": row["file_name"],
            "document_type": row["document_type"],
            "entity_name": row["entity_name"],
            "period_start": row["period_start"],
            "period_end": row["period_end"],
            "is_duplicate": row["is_duplicate"],
            "duplicate_of": row["duplicate_of"],
            "included_in_analysis": row["included_in_analysis"],
            "extracted_records": row["extracted_records"],
        }
        for row in st.session_state.get("document_report_rows", [])
    ]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def _render_pack_validation_flow() -> None:
    packs: list[ValidationPack] = st.session_state["validation_packs"]
    core_ready_packs = [pack for pack in packs if pack.status == "core_ready"]
    partial_packs = [pack for pack in packs if pack.status == "partial"]
    excluded_packs = [pack for pack in packs if pack.status == "excluded"]

    if partial_packs:
        st.warning(f"{len(partial_packs)} partial pack(s) detected and excluded from core variance runs.")
    if excluded_packs:
        st.warning(f"{len(excluded_packs)} excluded pack(s) detected.")

    if not core_ready_packs:
        st.error("No core-ready packs found. Upload a matching GL + P&L pair for the same entity and period.")
        return

    pack_map = {pack.pack_id: pack for pack in core_ready_packs}
    pack_options = [pack.pack_id for pack in core_ready_packs]
    current_selected_pack = st.session_state.get("selected_pack_id")
    if current_selected_pack not in pack_map:
        current_selected_pack = pack_options[0]
        st.session_state["selected_pack_id"] = current_selected_pack

    selected_pack_id = st.selectbox(
        "Select core pack",
        options=pack_options,
        index=pack_options.index(current_selected_pack),
        format_func=lambda value: _pack_label(pack_map[value]),
        key="selected_pack_id",
    )
    selected_pack = pack_map.get(selected_pack_id)
    if selected_pack is None:
        st.error("Selected pack is no longer available. Re-select a core pack.")
        return

    runtime = _build_runtime_for_pack(
        selected_pack=selected_pack,
        variance_threshold=st.session_state["variance_threshold"],
    )
    if runtime is None:
        st.error("Selected pack could not be prepared.")
        return

    st.session_state["current_runtime"] = runtime
    _render_overrides_editor(runtime=runtime)
    _render_validation_tables(runtime=runtime)
    _render_balance_snapshot(runtime=runtime)
    _render_export_controls(runtime=runtime)


def _build_runtime_for_pack(
    selected_pack: ValidationPack,
    variance_threshold: float,
) -> dict[str, Any] | None:
    txns_by_file = st.session_state["parsed_transactions_by_file"]
    pnl_by_file = st.session_state["parsed_pnl_by_file"]
    balance_by_file = st.session_state["parsed_balance_by_file"]

    transactions = []
    for file_name in selected_pack.ledger_docs + selected_pack.bank_docs:
        transactions.extend(copy.deepcopy(txns_by_file.get(file_name, [])))
    pnl_totals = _merge_pnl_totals(
        pnl_docs=[copy.deepcopy(pnl_by_file.get(name, [])) for name in selected_pack.pnl_docs]
    )

    if not transactions or not pnl_totals:
        return None

    normalize_transactions(transactions)
    _reset_classification(transactions)
    rules = parse_rule_dicts(
        load_combined_rule_dicts(
            base_rules_path=DEFAULT_RULES_PATH,
            learned_rules_path=DEFAULT_LEARNED_RULES_PATH,
        )
    )
    classify_transactions(transactions=transactions, rules=rules)
    validation_output = validate_against_pnl(
        transactions=transactions,
        pnl_totals=pnl_totals,
        variance_threshold=variance_threshold,
        use_absolute_amounts=True,
    )

    balance_snapshots: list[BalanceSheetSnapshot] = [
        copy.deepcopy(balance_by_file[name])
        for name in selected_pack.balance_docs
        if name in balance_by_file
    ]

    return {
        "selected_pack": selected_pack,
        "transactions": transactions,
        "pnl_totals": pnl_totals,
        "validation_output": validation_output,
        "balance_snapshots": balance_snapshots,
    }


def _reset_classification(transactions) -> None:
    for txn in transactions:
        txn.resolved_category = None
        txn.confidence = 0.0
        txn.flags = [
            flag
            for flag in txn.flags
            if flag not in {"possible_duplicate", "likely_non_pnl", "non_pnl", "unclassified"}
        ]


def _merge_pnl_totals(pnl_docs: list[list[PnLCategoryTotal]]) -> list[PnLCategoryTotal]:
    merged = {}
    for totals in pnl_docs:
        for item in totals:
            key = normalize_category_name(item.category_name)
            if key not in merged:
                merged[key] = copy.deepcopy(item)
                continue
            merged[key].amount += item.amount
    return list(merged.values())


def _render_overrides_editor(runtime: dict[str, Any]) -> None:
    st.subheader("Unresolved Review")
    unresolved_rows = _build_unresolved_rows(runtime["transactions"])
    if not unresolved_rows:
        st.caption("No unresolved transactions for this pack.")
        return

    unresolved_frame = pd.DataFrame(unresolved_rows)
    category_options = sorted({item.category_name for item in runtime["pnl_totals"]}) + ["NON_PNL"]
    edited_frame = st.data_editor(
        unresolved_frame,
        hide_index=True,
        use_container_width=True,
        column_config={
            "normalized_description": st.column_config.TextColumn("Normalized Description", disabled=True),
            "example_raw_description": st.column_config.TextColumn("Example Description", disabled=True),
            "source_type": st.column_config.TextColumn("Source", disabled=True),
            "assigned_category": st.column_config.SelectboxColumn(
                "Assign Category",
                options=[""] + category_options,
                required=False,
            ),
            "mark_non_pnl": st.column_config.CheckboxColumn("Mark Non-P&L"),
        },
        key="unresolved_editor",
    )

    if st.button("Save Overrides and Re-Validate"):
        assignments = []
        for row in edited_frame.to_dict(orient="records"):
            category = str(row.get("assigned_category", "")).strip()
            mark_non_pnl = bool(row.get("mark_non_pnl", False))
            if mark_non_pnl:
                category = "NON_PNL"
            if not category:
                continue
            assignments.append(
                {
                    "normalized_description": row["normalized_description"],
                    "category": category,
                    "non_pnl": mark_non_pnl or category == "NON_PNL",
                }
            )

        if not assignments:
            st.warning("No overrides selected.")
            return

        upsert_learned_rules(assignments=assignments, learned_rules_path=DEFAULT_LEARNED_RULES_PATH)
        st.success(f"Saved {len(assignments)} override rule(s).")
        st.rerun()


def _build_unresolved_rows(transactions) -> list[dict[str, object]]:
    seen = set()
    rows = []
    for txn in transactions:
        category = (txn.resolved_category or "").upper()
        if category and category != "UNCLASSIFIED":
            continue
        key = txn.normalized_description.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "normalized_description": key,
                "example_raw_description": txn.raw_description,
                "source_type": txn.source_type,
                "assigned_category": "",
                "mark_non_pnl": False,
            }
        )
    rows.sort(key=lambda item: item["normalized_description"])
    return rows


def _render_validation_tables(runtime: dict[str, Any]) -> None:
    validation_output = runtime["validation_output"]
    st.subheader("Category Comparison Summary (Ledger vs P&L)")
    st.dataframe(
        validation_lines_dataframe(validation_output),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Flagged Transactions")
    st.dataframe(
        transactions_dataframe(validation_output.flagged_transactions),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("Bank Supplemental Checks")
    st.write(
        {
            "bank_vs_ledger_coverage_pct": validation_output.bank_vs_ledger_coverage_pct,
            "matched_bank_transactions": validation_output.matched_bank_transactions,
            "direct_matched_bank_transactions": validation_output.direct_matched_bank_transactions,
            "fallback_matched_bank_transactions": validation_output.fallback_matched_bank_transactions,
            "aggregate_matched_bank_transactions": validation_output.aggregate_matched_bank_transactions,
            "total_bank_transactions": validation_output.total_bank_transactions,
            "unmatched_bank_transactions": len(validation_output.unmatched_bank_transactions),
            "bank_transactions_flagged": len(validation_output.bank_transactions_flagged),
        }
    )
    st.caption(
        "Coverage = matched bank transactions / total bank transactions. "
        "Matching uses amount equality, date within 3 days, and description token overlap. "
        "Low coverage often means period mismatch, missing GL scope, weak description overlap, or upload issues."
    )
    st.dataframe(
        transactions_dataframe(validation_output.unmatched_bank_transactions),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Alignment Diagnostics")
    st.dataframe(
        pd.DataFrame(validation_output.source_coverage_summary),
        use_container_width=True,
        hide_index=True,
    )
    st.dataframe(
        pd.DataFrame(
            [
                {"reason": key, "count": value}
                for key, value in validation_output.bank_unmatched_reason_counts.items()
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("Unclassified Diagnostics")
    st.dataframe(
        pd.DataFrame(
            [
                {"reason": key, "count": value}
                for key, value in validation_output.unclassified_reason_counts.items()
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )


def _render_balance_snapshot(runtime: dict[str, Any]) -> None:
    balance_snapshots: list[BalanceSheetSnapshot] = runtime["balance_snapshots"]
    if not balance_snapshots:
        return
    st.subheader("Balance Sheet Cross-Checks")
    rows = [item.as_dict() for item in balance_snapshots]
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


def _render_export_controls(runtime: dict[str, Any]) -> None:
    st.subheader("Export")
    output_path_text = st.text_input("Output folder", value=str(DEFAULT_OUTPUT_DIR))
    if st.button("Export CSV + Learned Rules"):
        artifact_paths = export_validation_artifacts(
            validation_output=runtime["validation_output"],
            output_dir=Path(output_path_text),
            document_metadata=st.session_state["document_metadata"],
            document_rows=st.session_state.get("document_report_rows", []),
            validation_packs=st.session_state["validation_packs"],
            selected_pack_id=runtime["selected_pack"].pack_id,
            duplicate_map=st.session_state.get("duplicate_map", {}),
        )
        st.success(
            "Exported:\n"
            + "\n".join(f"- {name}: {path}" for name, path in artifact_paths.items())
        )


def _pack_label(pack: ValidationPack) -> str:
    entity = pack.entity_name or "Unknown Entity"
    period_start = pack.period_start.isoformat() if pack.period_start else "?"
    period_end = pack.period_end.isoformat() if pack.period_end else "?"
    return f"{entity} | {period_start} to {period_end}"


def _build_extracted_records_map(metadata_list, parsed_payloads: dict[str, Any]) -> dict[str, int]:
    txns_by_file = parsed_payloads["transactions_by_file"]
    pnl_by_file = parsed_payloads["pnl_by_file"]
    balance_by_file = parsed_payloads["balance_by_file"]

    extracted_records = {}
    for metadata in metadata_list:
        file_name = metadata.file_name
        if file_name in txns_by_file:
            extracted_records[file_name] = len(txns_by_file[file_name])
        elif file_name in pnl_by_file:
            extracted_records[file_name] = len(pnl_by_file[file_name])
        elif file_name in balance_by_file:
            extracted_records[file_name] = 1
        else:
            extracted_records[file_name] = 0
    return extracted_records


if __name__ == "__main__":
    main()
