from __future__ import annotations

from pathlib import Path

import pandas as pd

from financial_validator_mvp.models.document_metadata import DocumentMetadata
from financial_validator_mvp.models.schemas import Transaction, ValidationOutput
from financial_validator_mvp.models.validation_pack import ValidationPack
from financial_validator_mvp.services.persistence import export_learned_rules


def validation_lines_dataframe(validation_output: ValidationOutput) -> pd.DataFrame:
    rows = [line.as_dict() for line in validation_output.lines]
    return pd.DataFrame(rows)


def transactions_dataframe(transactions: list[Transaction]) -> pd.DataFrame:
    rows = [txn.as_dict() for txn in transactions]
    if not rows:
        return pd.DataFrame(
            columns=[
                "date",
                "raw_description",
                "normalized_description",
                "amount",
                "direction",
                "source_account",
                "source_file",
                "original_category",
                "resolved_category",
                "confidence",
                "flags",
                "transaction_type",
                "running_balance",
                "source_type",
                "reference_number",
                "category_source",
                "category_rule_id",
                "category_note",
            ]
        )
    frame = pd.DataFrame(rows)
    frame["flags"] = frame["flags"].apply(lambda value: ", ".join(value))
    return frame


def export_validation_artifacts(
    validation_output: ValidationOutput,
    output_dir: Path,
    document_metadata: list[DocumentMetadata] | None = None,
    document_rows: list[dict[str, object]] | None = None,
    validation_packs: list[ValidationPack] | None = None,
    selected_pack_id: str | None = None,
    duplicate_map: dict[str, str] | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    summary_path = output_dir / "validation_summary.csv"
    flagged_path = output_dir / "flagged_transactions.csv"
    classifications_path = output_dir / "document_classification_report.csv"
    pack_path = output_dir / "pack_selection_report.csv"
    bank_summary_path = output_dir / "bank_supplemental_summary.csv"
    unmatched_bank_path = output_dir / "unmatched_bank_transactions.csv"
    dedup_path = output_dir / "upload_dedup_report.csv"
    diagnostics_path = output_dir / "alignment_diagnostics.csv"
    unmatched_diagnostics_path = output_dir / "unmatched_bank_diagnostics.csv"

    validation_lines_dataframe(validation_output).to_csv(summary_path, index=False)
    transactions_dataframe(validation_output.flagged_transactions).to_csv(flagged_path, index=False)
    transactions_dataframe(validation_output.unmatched_bank_transactions).to_csv(unmatched_bank_path, index=False)

    if document_rows is not None:
        metadata_frame = pd.DataFrame(
            document_rows,
            columns=[
                "file_name",
                "document_type",
                "entity_name",
                "period_start",
                "period_end",
                "account_number",
                "parser_confidence",
                "notes",
                "is_duplicate",
                "duplicate_of",
                "included_in_analysis",
                "extracted_records",
            ],
        )
    else:
        metadata_rows = [item.as_dict() for item in (document_metadata or [])]
        metadata_frame = pd.DataFrame(
            metadata_rows,
            columns=[
                "file_name",
                "document_type",
                "entity_name",
                "period_start",
                "period_end",
                "account_number",
                "parser_confidence",
                "notes",
            ],
        )
        metadata_frame["is_duplicate"] = False
        metadata_frame["duplicate_of"] = ""
        metadata_frame["included_in_analysis"] = True
        metadata_frame["extracted_records"] = None
    metadata_frame.to_csv(classifications_path, index=False)

    pack_rows = [item.as_dict() for item in (validation_packs or [])]
    pack_frame = pd.DataFrame(
        pack_rows,
        columns=[
            "pack_id",
            "entity_name",
            "period_start",
            "period_end",
            "ledger_docs",
            "pnl_docs",
            "balance_docs",
            "bank_docs",
            "status",
            "notes",
        ],
    )
    pack_frame["selected_pack_id"] = selected_pack_id
    pack_frame.to_csv(pack_path, index=False)

    pd.DataFrame(
        [
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
        ]
    ).to_csv(bank_summary_path, index=False)

    pd.DataFrame(validation_output.source_coverage_summary).to_csv(diagnostics_path, index=False)
    pd.DataFrame(validation_output.bank_unmatched_diagnostics).to_csv(unmatched_diagnostics_path, index=False)

    dedup_rows = [
        {"duplicate_file": duplicate_file, "canonical_file": canonical_file}
        for duplicate_file, canonical_file in (duplicate_map or {}).items()
    ]
    pd.DataFrame(dedup_rows, columns=["duplicate_file", "canonical_file"]).to_csv(dedup_path, index=False)

    learned_rules_path = export_learned_rules(output_dir=output_dir)

    return {
        "validation_summary": summary_path,
        "flagged_transactions": flagged_path,
        "document_classification_report": classifications_path,
        "pack_selection_report": pack_path,
        "bank_supplemental_summary": bank_summary_path,
        "unmatched_bank_transactions": unmatched_bank_path,
        "alignment_diagnostics": diagnostics_path,
        "unmatched_bank_diagnostics": unmatched_diagnostics_path,
        "upload_dedup_report": dedup_path,
        "learned_rules": learned_rules_path,
    }
