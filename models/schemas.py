from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any, Literal


@dataclass(slots=True)
class Transaction:
    date: date | None
    raw_description: str
    normalized_description: str
    amount: float
    direction: str
    source_account: str | None
    source_file: str
    original_category: str | None
    resolved_category: str | None
    confidence: float
    flags: list[str] = field(default_factory=list)
    transaction_type: str | None = None
    running_balance: float | None = None
    source_type: Literal["ledger", "bank"] = "ledger"
    reference_number: str | None = None
    category_source: str | None = None
    category_rule_id: str | None = None
    category_note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["date"] = self.date.isoformat() if self.date else None
        return payload


@dataclass(slots=True)
class PnLCategoryTotal:
    category_name: str
    amount: float
    period: str | None
    section: str | None
    source_file: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BalanceSheetSnapshot:
    period: str | None
    ending_cash: float | None
    net_income: float | None
    source_file: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BankStatementBalanceSummary:
    source_file: str
    period_start: date | None
    period_end: date | None
    beginning_balance: float | None
    additions_total: float | None
    deductions_total: float | None
    ending_balance: float | None

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["period_start"] = self.period_start.isoformat() if self.period_start else None
        payload["period_end"] = self.period_end.isoformat() if self.period_end else None
        return payload


@dataclass(slots=True)
class BankDailyBalancePoint:
    source_file: str
    date: date | None
    ledger_balance: float

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["date"] = self.date.isoformat() if self.date else None
        return payload


@dataclass(slots=True)
class ValidationLine:
    category_name: str
    ledger_total: float
    pnl_total: float
    difference: float
    variance_pct: float | None
    matched_transactions: int
    flagged_transactions: int
    status: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ValidationOutput:
    lines: list[ValidationLine]
    flagged_transactions: list[Transaction]
    non_pnl_transactions: list[Transaction]
    unclassified_transactions: list[Transaction]
    unmatched_bank_transactions: list[Transaction] = field(default_factory=list)
    bank_transactions_flagged: list[Transaction] = field(default_factory=list)
    bank_vs_ledger_coverage_pct: float | None = None
    matched_bank_transactions: int = 0
    total_bank_transactions: int = 0
    direct_matched_bank_transactions: int = 0
    fallback_matched_bank_transactions: int = 0
    aggregate_matched_bank_transactions: int = 0
    match_strategy_breakdown: dict[str, int] = field(default_factory=dict)
    unclassified_reason_counts: dict[str, int] = field(default_factory=dict)
    bank_unmatched_reason_counts: dict[str, int] = field(default_factory=dict)
    bank_unmatched_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    source_coverage_summary: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "lines": [line.as_dict() for line in self.lines],
            "flagged_transactions": [txn.as_dict() for txn in self.flagged_transactions],
            "non_pnl_transactions": [txn.as_dict() for txn in self.non_pnl_transactions],
            "unclassified_transactions": [txn.as_dict() for txn in self.unclassified_transactions],
            "unmatched_bank_transactions": [txn.as_dict() for txn in self.unmatched_bank_transactions],
            "bank_transactions_flagged": [txn.as_dict() for txn in self.bank_transactions_flagged],
            "bank_vs_ledger_coverage_pct": self.bank_vs_ledger_coverage_pct,
            "matched_bank_transactions": self.matched_bank_transactions,
            "total_bank_transactions": self.total_bank_transactions,
            "direct_matched_bank_transactions": self.direct_matched_bank_transactions,
            "fallback_matched_bank_transactions": self.fallback_matched_bank_transactions,
            "aggregate_matched_bank_transactions": self.aggregate_matched_bank_transactions,
            "match_strategy_breakdown": self.match_strategy_breakdown,
            "unclassified_reason_counts": self.unclassified_reason_counts,
            "bank_unmatched_reason_counts": self.bank_unmatched_reason_counts,
            "bank_unmatched_diagnostics": self.bank_unmatched_diagnostics,
            "source_coverage_summary": self.source_coverage_summary,
        }
