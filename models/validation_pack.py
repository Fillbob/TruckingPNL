from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal

PackStatus = Literal["core_ready", "partial", "excluded"]


@dataclass(slots=True)
class ValidationPack:
    pack_id: str
    entity_name: str | None
    period_start: date | None
    period_end: date | None
    ledger_docs: list[str] = field(default_factory=list)
    pnl_docs: list[str] = field(default_factory=list)
    balance_docs: list[str] = field(default_factory=list)
    bank_docs: list[str] = field(default_factory=list)
    status: PackStatus = "partial"
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["period_start"] = self.period_start.isoformat() if self.period_start else None
        payload["period_end"] = self.period_end.isoformat() if self.period_end else None
        payload["ledger_docs"] = ", ".join(self.ledger_docs)
        payload["pnl_docs"] = ", ".join(self.pnl_docs)
        payload["balance_docs"] = ", ".join(self.balance_docs)
        payload["bank_docs"] = ", ".join(self.bank_docs)
        payload["notes"] = "; ".join(self.notes)
        return payload

