from __future__ import annotations

import csv
import unittest
import uuid
from datetime import date
from pathlib import Path
import shutil

from financial_validator_mvp.models.document_metadata import DocumentMetadata
from financial_validator_mvp.models.schemas import ValidationOutput
from financial_validator_mvp.services.reporting import export_validation_artifacts
from financial_validator_mvp.services.upload_dedup import build_document_report_rows, deduplicate_uploaded_files


class UploadDedupTests(unittest.TestCase):
    def test_duplicate_uploads_are_skipped_by_hash(self) -> None:
        file_bytes_map = {
            "a.pdf": b"same-content",
            "b.pdf": b"same-content",
            "c.pdf": b"different-content",
        }
        canonical, duplicate_map, all_names = deduplicate_uploaded_files(file_bytes_map)

        self.assertEqual(all_names, ["a.pdf", "b.pdf", "c.pdf"])
        self.assertEqual(set(canonical.keys()), {"a.pdf", "c.pdf"})
        self.assertEqual(duplicate_map, {"b.pdf": "a.pdf"})

    def test_duplicate_warning_mapping_is_generated(self) -> None:
        metadata_list = [
            DocumentMetadata(
                file_name="a.pdf",
                document_type="pnl",
                entity_name="ENTITY INC",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                account_number=None,
                parser_confidence=0.9,
            )
        ]
        rows = build_document_report_rows(
            all_file_names=["a.pdf", "b.pdf"],
            metadata_list=metadata_list,
            duplicate_map={"b.pdf": "a.pdf"},
            extracted_records_by_file={"a.pdf": 3},
        )
        row_by_name = {item["file_name"]: item for item in rows}
        self.assertFalse(row_by_name["a.pdf"]["is_duplicate"])
        self.assertTrue(row_by_name["b.pdf"]["is_duplicate"])
        self.assertEqual(row_by_name["b.pdf"]["duplicate_of"], "a.pdf")
        self.assertFalse(row_by_name["b.pdf"]["included_in_analysis"])

    def test_reporting_includes_duplicate_columns(self) -> None:
        output = ValidationOutput(
            lines=[],
            flagged_transactions=[],
            non_pnl_transactions=[],
            unclassified_transactions=[],
            bank_unmatched_reason_counts={"same_amount_and_date_but_description_diff": 2},
            bank_unmatched_diagnostics=[
                {
                    "date": "2025-01-01",
                    "source_file": "bank.pdf",
                    "raw_description": "foo",
                    "normalized_description": "foo",
                    "amount": 10.0,
                    "reason": "same_amount_and_date_but_description_diff",
                    "same_amount_candidates": 1,
                    "same_date_window_candidates": 4,
                    "same_amount_and_date_candidates": 1,
                    "grouped_amount_candidates": 0,
                    "reference_number": None,
                }
            ],
            source_coverage_summary=[
                {"metric": "ledger_transaction_count", "value": 10},
                {"metric": "bank_transaction_count", "value": 25},
            ],
        )
        document_rows = [
            {
                "file_name": "a.pdf",
                "document_type": "pnl",
                "entity_name": "ENTITY INC",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "account_number": None,
                "parser_confidence": 0.9,
                "notes": "",
                "is_duplicate": False,
                "duplicate_of": "",
                "included_in_analysis": True,
                "extracted_records": 3,
            },
            {
                "file_name": "b.pdf",
                "document_type": "pnl",
                "entity_name": "ENTITY INC",
                "period_start": "2025-01-01",
                "period_end": "2025-12-31",
                "account_number": None,
                "parser_confidence": 0.9,
                "notes": "",
                "is_duplicate": True,
                "duplicate_of": "a.pdf",
                "included_in_analysis": False,
                "extracted_records": 0,
            },
        ]

        tmp_dir = Path(__file__).resolve().parent / "tmp" / f"dedup-{uuid.uuid4().hex}"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            artifacts = export_validation_artifacts(
                validation_output=output,
                output_dir=tmp_dir,
                document_rows=document_rows,
                duplicate_map={"b.pdf": "a.pdf"},
            )
            self.assertTrue(artifacts["upload_dedup_report"].exists())
            self.assertTrue(artifacts["alignment_diagnostics"].exists())
            self.assertTrue(artifacts["unmatched_bank_diagnostics"].exists())

            with artifacts["document_classification_report"].open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                headers = reader.fieldnames or []
                self.assertIn("is_duplicate", headers)
                self.assertIn("duplicate_of", headers)
                self.assertIn("included_in_analysis", headers)

            with artifacts["bank_supplemental_summary"].open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                headers = reader.fieldnames or []
                self.assertIn("direct_matched_bank_transactions", headers)
                self.assertIn("fallback_matched_bank_transactions", headers)
                self.assertIn("aggregate_matched_bank_transactions", headers)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
