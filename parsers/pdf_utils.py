from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import BinaryIO


def extract_pdf_pages_text(pdf_source: str | Path | bytes | BinaryIO) -> list[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'pypdf'. Install dependencies with: "
            "pip install -r financial_validator_mvp/requirements.txt"
        ) from exc

    if isinstance(pdf_source, (str, Path)):
        reader = PdfReader(str(pdf_source))
    elif isinstance(pdf_source, bytes):
        reader = PdfReader(BytesIO(pdf_source))
    else:
        data = pdf_source.read()
        if hasattr(pdf_source, "seek"):
            pdf_source.seek(0)
        reader = PdfReader(BytesIO(data))

    return [page.extract_text() or "" for page in reader.pages]


def extract_pdf_text(pdf_source: str | Path | bytes | BinaryIO) -> str:
    return "\n".join(extract_pdf_pages_text(pdf_source))


def extract_pdf_first_page_text(pdf_source: str | Path | bytes | BinaryIO) -> str:
    pages = extract_pdf_pages_text(pdf_source)
    return pages[0] if pages else ""
