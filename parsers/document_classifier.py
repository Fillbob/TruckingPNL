from __future__ import annotations

import re
from collections import defaultdict
from datetime import date

from financial_validator_mvp.models.document_metadata import DocumentMetadata
from financial_validator_mvp.models.validation_pack import ValidationPack
from financial_validator_mvp.parsers.common import collapse_spaces, parse_period_date_range, periods_overlap
from financial_validator_mvp.parsers.pdf_utils import extract_pdf_first_page_text, extract_pdf_text

TITLE_KEYWORDS = {
    "general_ledger": ("general ledger",),
    "pnl": ("profit and loss", "profit & loss"),
    "balance_sheet": ("balance sheet",),
    "bank_statement": ("business checking", "for the period"),
}

ENTITY_SUFFIXES = ("INC", "LLC", "LTD", "CORP", "CORPORATION", "CO")


def classify_document(pdf_bytes: bytes, file_name: str) -> DocumentMetadata:
    first_page_text = extract_pdf_first_page_text(pdf_bytes)
    full_text = extract_pdf_text(pdf_bytes)
    return classify_document_text(
        first_page_text=first_page_text,
        full_text=full_text,
        file_name=file_name,
    )


def classify_document_text(
    first_page_text: str,
    full_text: str,
    file_name: str,
) -> DocumentMetadata:
    first_page_lower = first_page_text.lower()
    full_lower = full_text.lower()

    notes: list[str] = []
    document_type = _detect_document_type(first_page_lower=first_page_lower, full_lower=full_lower)
    confidence = _document_confidence(document_type=document_type)
    if document_type == "unknown":
        notes.append("Unsupported or unrecognized PDF structure.")

    entity_name = _extract_entity_name(first_page_text)
    if not entity_name:
        notes.append("Entity name could not be confidently extracted.")
        confidence = min(confidence, 0.55)

    period_start, period_end, period_confidence = parse_period_date_range(full_text)
    confidence = min(confidence, period_confidence if period_confidence > 0 else confidence)
    if not period_start or not period_end:
        notes.append("Period could not be confidently extracted.")

    account_number = _extract_account_number(first_page_text)
    if document_type == "bank_statement" and not account_number:
        notes.append("Bank account number was not found.")
        confidence = min(confidence, 0.75)

    return DocumentMetadata(
        file_name=file_name,
        document_type=document_type,
        entity_name=entity_name,
        period_start=period_start,
        period_end=period_end,
        account_number=account_number,
        parser_confidence=round(confidence, 2),
        notes=notes,
    )


def build_validation_packs(metadata_list: list[DocumentMetadata]) -> list[ValidationPack]:
    grouped_by_entity: dict[str, list[DocumentMetadata]] = defaultdict(list)
    unknown_docs: list[DocumentMetadata] = []

    for metadata in metadata_list:
        key = normalize_entity_name(metadata.entity_name)
        if not key:
            unknown_docs.append(metadata)
            continue
        grouped_by_entity[key].append(metadata)

    packs: list[ValidationPack] = []
    for entity_key in sorted(grouped_by_entity.keys()):
        entity_docs = grouped_by_entity[entity_key]
        packs.extend(_build_entity_packs(entity_docs))

    for metadata in unknown_docs:
        packs.append(
            ValidationPack(
                pack_id=f"excluded-{_safe_pack_token(metadata.file_name)}",
                entity_name=metadata.entity_name,
                period_start=metadata.period_start,
                period_end=metadata.period_end,
                status="excluded",
                notes=["No entity detected; excluded from pack matching."],
                ledger_docs=[metadata.file_name] if metadata.document_type == "general_ledger" else [],
                pnl_docs=[metadata.file_name] if metadata.document_type == "pnl" else [],
                balance_docs=[metadata.file_name] if metadata.document_type == "balance_sheet" else [],
                bank_docs=[metadata.file_name] if metadata.document_type == "bank_statement" else [],
            )
        )

    packs.sort(key=lambda pack: (pack.status != "core_ready", pack.entity_name or "", pack.pack_id))
    return packs


def normalize_entity_name(entity_name: str | None) -> str:
    if not entity_name:
        return ""
    value = re.sub(r"[^A-Za-z0-9 ]+", " ", entity_name.upper())
    return collapse_spaces(value)


def _build_entity_packs(entity_docs: list[DocumentMetadata]) -> list[ValidationPack]:
    ledgers = [item for item in entity_docs if item.document_type == "general_ledger"]
    pnls = [item for item in entity_docs if item.document_type == "pnl"]
    balances = [item for item in entity_docs if item.document_type == "balance_sheet"]
    banks = [item for item in entity_docs if item.document_type == "bank_statement"]
    unknown = [item for item in entity_docs if item.document_type == "unknown"]

    used_file_names: set[str] = set()
    packs: list[ValidationPack] = []
    entity_name = _best_entity_name(entity_docs)

    for ledger in ledgers:
        overlapping_pnls = [
            pnl
            for pnl in pnls
            if periods_overlap(ledger.period_start, ledger.period_end, pnl.period_start, pnl.period_end)
        ]
        if not overlapping_pnls:
            continue

        candidate_dates: list[tuple[date | None, date | None]] = [
            (ledger.period_start, ledger.period_end),
            *[(pnl.period_start, pnl.period_end) for pnl in overlapping_pnls],
        ]
        period_start, period_end = _union_dates(candidate_dates)

        overlapping_balances = [
            item
            for item in balances
            if periods_overlap(period_start, period_end, item.period_start, item.period_end)
        ]
        overlapping_banks = [
            item
            for item in banks
            if periods_overlap(period_start, period_end, item.period_start, item.period_end)
        ]

        pack_id = (
            f"{_safe_pack_token(entity_name or 'entity')}-"
            f"{(period_start.isoformat() if period_start else 'na')}-"
            f"{(period_end.isoformat() if period_end else 'na')}"
        )
        packs.append(
            ValidationPack(
                pack_id=pack_id,
                entity_name=entity_name,
                period_start=period_start,
                period_end=period_end,
                ledger_docs=[ledger.file_name],
                pnl_docs=sorted(item.file_name for item in overlapping_pnls),
                balance_docs=sorted(item.file_name for item in overlapping_balances),
                bank_docs=sorted(item.file_name for item in overlapping_banks),
                status="core_ready",
                notes=[],
            )
        )

        used_file_names.add(ledger.file_name)
        used_file_names.update(item.file_name for item in overlapping_pnls)
        used_file_names.update(item.file_name for item in overlapping_balances)
        used_file_names.update(item.file_name for item in overlapping_banks)

    leftovers = [item for item in entity_docs if item.file_name not in used_file_names]
    for metadata in leftovers:
        notes = []
        if metadata.document_type == "unknown":
            notes.append("Document type unsupported for MVP parsers.")
            status = "excluded"
        else:
            notes.append("No overlapping core GL+P&L pack found for this document.")
            status = "partial"

        packs.append(
            ValidationPack(
                pack_id=f"{status}-{_safe_pack_token(metadata.file_name)}",
                entity_name=entity_name,
                period_start=metadata.period_start,
                period_end=metadata.period_end,
                status=status,
                notes=notes + metadata.notes,
                ledger_docs=[metadata.file_name] if metadata.document_type == "general_ledger" else [],
                pnl_docs=[metadata.file_name] if metadata.document_type == "pnl" else [],
                balance_docs=[metadata.file_name] if metadata.document_type == "balance_sheet" else [],
                bank_docs=[metadata.file_name] if metadata.document_type == "bank_statement" else [],
            )
        )

    return packs


def _best_entity_name(metadata_list: list[DocumentMetadata]) -> str | None:
    for item in metadata_list:
        if item.entity_name:
            return item.entity_name
    return None


def _document_confidence(document_type: str) -> float:
    if document_type == "unknown":
        return 0.4
    if document_type == "bank_statement":
        return 0.95
    return 0.9


def _detect_document_type(first_page_lower: str, full_lower: str) -> str:
    if any(keyword in first_page_lower for keyword in TITLE_KEYWORDS["general_ledger"]):
        return "general_ledger"
    if any(keyword in first_page_lower for keyword in TITLE_KEYWORDS["pnl"]):
        return "pnl"
    if any(keyword in first_page_lower for keyword in TITLE_KEYWORDS["balance_sheet"]):
        return "balance_sheet"
    if (
        any(keyword in first_page_lower for keyword in TITLE_KEYWORDS["bank_statement"])
        and "pnc bank" in first_page_lower
        and "activity detail" in full_lower
    ):
        return "bank_statement"
    return "unknown"


def _extract_account_number(text: str) -> str | None:
    match = re.search(r"account number\s*:\s*([A-Z0-9X\-]+)", text, flags=re.IGNORECASE)
    if match:
        return collapse_spaces(match.group(1))

    match = re.search(r"primary account number\s*:\s*([A-Z0-9X\-]+)", text, flags=re.IGNORECASE)
    if match:
        return collapse_spaces(match.group(1))
    return None


def _extract_entity_name(first_page_text: str) -> str | None:
    lines = [collapse_spaces(line) for line in first_page_text.splitlines() if line.strip()]
    for line in lines:
        upper = line.upper()
        if any(
            keyword in upper
            for keyword in (
                "PNC BANK",
                "BUSINESS CHECKING",
                "PROFIT AND LOSS",
                "GENERAL LEDGER",
                "BALANCE SHEET",
                "ACCOUNT NUMBER",
                "FOR THE PERIOD",
                "SUMMARY",
            )
        ):
            continue
        candidate = _extract_entity_candidate(upper)
        if candidate:
            return candidate

    flat_upper = collapse_spaces(first_page_text.upper())
    match = re.search(
        r"(?:GENERAL LEDGER|PROFIT AND LOSS|BALANCE SHEET)\s+([A-Z0-9&.,' -]+?(?:INC|LLC|LTD|CORP|CORPORATION|CO))\b",
        flat_upper,
    )
    if match:
        return collapse_spaces(match.group(1))
    return None


def _union_dates(date_pairs: list[tuple[date | None, date | None]]) -> tuple[date | None, date | None]:
    starts = [item[0] for item in date_pairs if item[0] is not None]
    ends = [item[1] for item in date_pairs if item[1] is not None]
    if not starts or not ends:
        return None, None
    return min(starts), max(ends)


def _safe_pack_token(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:60]


def _extract_entity_candidate(line: str) -> str | None:
    if not re.search(r"[A-Z]", line):
        return None
    match = re.search(
        r"([A-Z0-9&.,' -]+?\b(?:INC|LLC|LTD|CORP(?:ORATION)?|CO)\b)",
        line,
    )
    if not match:
        return None
    return collapse_spaces(match.group(1))
