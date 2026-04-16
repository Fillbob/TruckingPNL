from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

DocumentType = Literal["general_ledger", "pnl", "balance_sheet", "bank_statement", "unknown"]


@dataclass(slots=True)
class DocumentMetadata:
    file_name: str
    document_type: DocumentType
    entity_name: str | None
    period_start: date | None
    period_end: date | None
    account_number: str | None
    parser_confidence: float
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["period_start"] = self.period_start.isoformat() if self.period_start else None
        payload["period_end"] = self.period_end.isoformat() if self.period_end else None
        payload["notes"] = "; ".join(self.notes)
        return payload

