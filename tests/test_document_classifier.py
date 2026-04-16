from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

from financial_validator_mvp.models.document_metadata import DocumentMetadata
from financial_validator_mvp.parsers.document_classifier import build_validation_packs, classify_document_text

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


class DocumentClassifierTests(unittest.TestCase):
    def test_document_classifier_detects_types_and_metadata(self) -> None:
        gl_text = (FIXTURES_DIR / "general_ledger_qb_sample.txt").read_text(encoding="utf-8")
        pnl_text = """
        Cash Basis
        Profit and Loss
        AVERLY LOGISTICS INC
        January-December, 2025
        Income
        Services 187,237.28
        """
        bank_text = (FIXTURES_DIR / "bank_statement_pnc_sample.txt").read_text(encoding="utf-8")

        gl_meta = classify_document_text(first_page_text=gl_text, full_text=gl_text, file_name="gl.pdf")
        pnl_meta = classify_document_text(first_page_text=pnl_text, full_text=pnl_text, file_name="pnl.pdf")
        bank_meta = classify_document_text(first_page_text=bank_text, full_text=bank_text, file_name="bank.pdf")

        self.assertEqual(gl_meta.document_type, "general_ledger")
        self.assertEqual(gl_meta.entity_name, "ANDI GROUP TRUCKING INC")
        self.assertEqual(gl_meta.period_start, date(2026, 1, 1))
        self.assertEqual(gl_meta.period_end, date(2026, 3, 25))

        self.assertEqual(pnl_meta.document_type, "pnl")
        self.assertEqual(pnl_meta.entity_name, "AVERLY LOGISTICS INC")
        self.assertEqual(pnl_meta.period_start, date(2025, 1, 1))
        self.assertEqual(pnl_meta.period_end, date(2025, 12, 31))

        self.assertEqual(bank_meta.document_type, "bank_statement")
        self.assertEqual(bank_meta.entity_name, "ANDI GROUP TRUCKING INC")
        self.assertEqual(bank_meta.period_start, date(2025, 1, 1))
        self.assertEqual(bank_meta.period_end, date(2025, 1, 31))
        self.assertEqual(bank_meta.account_number, "XX-XXXX-9333")

    def test_pack_builder_mixed_uploads_run_matched_subset_only(self) -> None:
        metadata = [
            DocumentMetadata(
                file_name="andi_gl_q1_2026.pdf",
                document_type="general_ledger",
                entity_name="ANDI GROUP TRUCKING INC",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                account_number=None,
                parser_confidence=0.95,
            ),
            DocumentMetadata(
                file_name="andi_pnl_q1_2026.pdf",
                document_type="pnl",
                entity_name="ANDI GROUP TRUCKING INC",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 3, 31),
                account_number=None,
                parser_confidence=0.95,
            ),
            DocumentMetadata(
                file_name="andi_bank_jan_2025.pdf",
                document_type="bank_statement",
                entity_name="ANDI GROUP TRUCKING INC",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                account_number="XX-XXXX-9333",
                parser_confidence=0.95,
            ),
            DocumentMetadata(
                file_name="averly_pnl_2025.pdf",
                document_type="pnl",
                entity_name="AVERLY LOGISTICS INC",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                account_number=None,
                parser_confidence=0.9,
            ),
        ]

        packs = build_validation_packs(metadata)
        core = [pack for pack in packs if pack.status == "core_ready"]
        partial = [pack for pack in packs if pack.status == "partial"]

        self.assertEqual(len(core), 1)
        self.assertEqual(core[0].ledger_docs, ["andi_gl_q1_2026.pdf"])
        self.assertEqual(core[0].pnl_docs, ["andi_pnl_q1_2026.pdf"])
        self.assertEqual(core[0].bank_docs, [])
        self.assertGreaterEqual(len(partial), 2)

    def test_end_to_end_partial_pack_behavior(self) -> None:
        metadata = [
            DocumentMetadata(
                file_name="bank_only.pdf",
                document_type="bank_statement",
                entity_name="ENTITY INC",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                account_number="XX-XXXX-1234",
                parser_confidence=0.95,
            ),
            DocumentMetadata(
                file_name="pnl_only.pdf",
                document_type="pnl",
                entity_name="ENTITY INC",
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                account_number=None,
                parser_confidence=0.9,
            ),
        ]

        packs = build_validation_packs(metadata)
        self.assertFalse(any(pack.status == "core_ready" for pack in packs))
        self.assertTrue(all(pack.status in {"partial", "excluded"} for pack in packs))


if __name__ == "__main__":
    unittest.main()

