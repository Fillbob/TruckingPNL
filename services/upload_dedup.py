from __future__ import annotations

import hashlib

from financial_validator_mvp.models.document_metadata import DocumentMetadata


def deduplicate_uploaded_files(
    file_bytes_map: dict[str, bytes],
) -> tuple[dict[str, bytes], dict[str, str], list[str]]:
    canonical_by_hash: dict[str, str] = {}
    canonical_file_bytes: dict[str, bytes] = {}
    duplicate_map: dict[str, str] = {}
    all_file_names = list(file_bytes_map.keys())

    for file_name, payload in file_bytes_map.items():
        file_hash = hashlib.sha256(payload).hexdigest()
        if file_hash in canonical_by_hash:
            duplicate_map[file_name] = canonical_by_hash[file_hash]
            continue
        canonical_by_hash[file_hash] = file_name
        canonical_file_bytes[file_name] = payload

    return canonical_file_bytes, duplicate_map, all_file_names


def build_document_report_rows(
    all_file_names: list[str],
    metadata_list: list[DocumentMetadata],
    duplicate_map: dict[str, str],
    extracted_records_by_file: dict[str, int] | None = None,
) -> list[dict[str, object]]:
    metadata_by_file = {item.file_name: item for item in metadata_list}
    extracted_records_by_file = extracted_records_by_file or {}

    rows: list[dict[str, object]] = []
    for file_name in all_file_names:
        duplicate_of = duplicate_map.get(file_name, "")
        is_duplicate = bool(duplicate_of)
        canonical_file = duplicate_of or file_name
        metadata = metadata_by_file.get(canonical_file)

        row = {
            "file_name": file_name,
            "document_type": metadata.document_type if metadata else "unknown",
            "entity_name": metadata.entity_name if metadata else None,
            "period_start": metadata.period_start.isoformat() if metadata and metadata.period_start else None,
            "period_end": metadata.period_end.isoformat() if metadata and metadata.period_end else None,
            "account_number": metadata.account_number if metadata else None,
            "parser_confidence": metadata.parser_confidence if metadata else 0.0,
            "notes": "; ".join(metadata.notes) if metadata else "No metadata available.",
            "is_duplicate": is_duplicate,
            "duplicate_of": duplicate_of,
            "included_in_analysis": not is_duplicate,
            "extracted_records": 0 if is_duplicate else int(extracted_records_by_file.get(file_name, 0)),
        }
        rows.append(row)

    return rows

