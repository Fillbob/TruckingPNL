from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from financial_validator_mvp.models.schemas import PnLCategoryTotal, Transaction, ValidationLine, ValidationOutput
from financial_validator_mvp.services.normalization import build_matching_tokens, normalize_category_name


def validate_against_pnl(
    transactions: list[Transaction],
    pnl_totals: list[PnLCategoryTotal],
    variance_threshold: float = 1.0,
    use_absolute_amounts: bool = True,
) -> ValidationOutput:
    _mark_possible_duplicates(transactions)

    ledger_transactions = [txn for txn in transactions if txn.source_type == "ledger"]
    bank_transactions = [txn for txn in transactions if txn.source_type == "bank"]

    pnl_map: dict[str, PnLCategoryTotal] = {}
    for pnl_total in pnl_totals:
        key = normalize_category_name(pnl_total.category_name)
        pnl_map[key] = pnl_total

    ledger_totals: dict[str, float] = defaultdict(float)
    ledger_counts: dict[str, int] = defaultdict(int)
    ledger_flagged_counts: dict[str, int] = defaultdict(int)

    flagged_transactions: list[Transaction] = []
    non_pnl_transactions: list[Transaction] = []
    unclassified_transactions: list[Transaction] = []

    for transaction in transactions:
        if transaction.flags:
            flagged_transactions.append(transaction)

        if _is_non_pnl(transaction):
            non_pnl_transactions.append(transaction)
            continue

        if transaction.source_type != "ledger":
            continue

        if (transaction.resolved_category or "") == "UNCLASSIFIED":
            unclassified_transactions.append(transaction)

        category = transaction.resolved_category or "UNCLASSIFIED"
        category_key = normalize_category_name(category)
        amount = abs(transaction.amount) if use_absolute_amounts else transaction.amount

        ledger_totals[category_key] += amount
        ledger_counts[category_key] += 1
        if transaction.flags:
            ledger_flagged_counts[category_key] += 1

    lines: list[ValidationLine] = []
    all_category_keys = set(pnl_map.keys()).union(ledger_totals.keys())
    for category_key in sorted(all_category_keys):
        pnl_total = pnl_map.get(category_key)
        ledger_total = ledger_totals.get(category_key, 0.0)
        pnl_amount = pnl_total.amount if pnl_total else 0.0
        difference = ledger_total - pnl_amount
        variance_pct = (difference / pnl_amount * 100) if pnl_amount else None

        if pnl_total and category_key not in ledger_totals:
            status = "missing_in_ledger"
        elif not pnl_total and category_key in ledger_totals:
            status = "missing_in_pnl"
        elif abs(difference) > variance_threshold:
            status = "variance"
        else:
            status = "matched"

        lines.append(
            ValidationLine(
                category_name=pnl_total.category_name if pnl_total else category_key.title(),
                ledger_total=round(ledger_total, 2),
                pnl_total=round(pnl_amount, 2),
                difference=round(difference, 2),
                variance_pct=round(variance_pct, 2) if variance_pct is not None else None,
                matched_transactions=ledger_counts.get(category_key, 0),
                flagged_transactions=ledger_flagged_counts.get(category_key, 0),
                status=status,
            )
        )

    lines.sort(key=lambda item: abs(item.difference), reverse=True)

    bank_coverage = calculate_bank_ledger_coverage(bank_transactions=bank_transactions, ledger_transactions=ledger_transactions)
    unmatched_bank_transactions = bank_coverage["unmatched_bank_transactions"]
    bank_transactions_flagged = [txn for txn in bank_transactions if txn.flags]
    unmatched_diagnostics = analyze_unmatched_bank_transactions(
        unmatched_bank_transactions=unmatched_bank_transactions,
        ledger_transactions=ledger_transactions,
    )

    return ValidationOutput(
        lines=lines,
        flagged_transactions=flagged_transactions,
        non_pnl_transactions=non_pnl_transactions,
        unclassified_transactions=unclassified_transactions,
        unmatched_bank_transactions=unmatched_bank_transactions,
        bank_transactions_flagged=bank_transactions_flagged,
        bank_vs_ledger_coverage_pct=bank_coverage["coverage_pct"],
        matched_bank_transactions=bank_coverage["matched_count"],
        total_bank_transactions=bank_coverage["total_count"],
        direct_matched_bank_transactions=bank_coverage["direct_matched_count"],
        fallback_matched_bank_transactions=bank_coverage["fallback_matched_count"],
        aggregate_matched_bank_transactions=bank_coverage["aggregate_matched_count"],
        match_strategy_breakdown=bank_coverage["strategy_breakdown"],
        unclassified_reason_counts=_build_unclassified_reason_counts(unclassified_transactions),
        bank_unmatched_reason_counts=_count_unmatched_reasons(unmatched_diagnostics),
        bank_unmatched_diagnostics=unmatched_diagnostics,
        source_coverage_summary=_build_source_coverage_summary(
            ledger_transactions=ledger_transactions,
            bank_transactions=bank_transactions,
        ),
    )


def _mark_possible_duplicates(transactions: list[Transaction]) -> None:
    grouped: dict[tuple[str | None, float, str], list[Transaction]] = defaultdict(list)
    for transaction in transactions:
        key = (
            transaction.date.isoformat() if transaction.date else None,
            round(abs(transaction.amount), 2),
            transaction.normalized_description,
        )
        grouped[key].append(transaction)

    for group in grouped.values():
        if len(group) <= 1:
            continue
        for transaction in group:
            if "possible_duplicate" not in transaction.flags:
                transaction.flags.append("possible_duplicate")


def _is_non_pnl(transaction: Transaction) -> bool:
    if (transaction.resolved_category or "").upper() == "NON_PNL":
        return True
    if "non_pnl" in transaction.flags:
        return True
    if "likely_non_pnl" in transaction.flags:
        return True
    return False


def calculate_bank_ledger_coverage(
    bank_transactions: list[Transaction],
    ledger_transactions: list[Transaction],
) -> dict[str, float | int | list[Transaction]]:
    if not bank_transactions:
        return {
            "coverage_pct": None,
            "unmatched_bank_transactions": [],
            "matched_count": 0,
            "total_count": 0,
            "direct_matched_count": 0,
            "fallback_matched_count": 0,
            "aggregate_matched_count": 0,
            "strategy_breakdown": {},
        }

    matched_ledger_indexes: set[int] = set()
    matched_bank_indexes: set[int] = set()
    unmatched: list[Transaction] = []
    direct_count = 0
    fallback_count = 0
    aggregate_count = 0

    for bank_idx, bank_txn in enumerate(bank_transactions):
        match_index = _find_ledger_match_direct(
            bank_transaction=bank_txn,
            ledger_transactions=ledger_transactions,
            used_indexes=matched_ledger_indexes,
        )
        if match_index is not None:
            matched_ledger_indexes.add(match_index)
            matched_bank_indexes.add(bank_idx)
            direct_count += 1
            continue

        fallback_match_index = _find_ledger_match_fallback(
            bank_transaction=bank_txn,
            ledger_transactions=ledger_transactions,
            used_indexes=matched_ledger_indexes,
        )
        if fallback_match_index is not None:
            matched_ledger_indexes.add(fallback_match_index)
            matched_bank_indexes.add(bank_idx)
            fallback_count += 1

    aggregate_groups = _aggregate_match_groups(
        bank_transactions=bank_transactions,
        ledger_transactions=ledger_transactions,
        used_bank_indexes=matched_bank_indexes,
        used_ledger_indexes=matched_ledger_indexes,
    )
    for bank_indexes, ledger_index in aggregate_groups:
        matched_bank_indexes.update(bank_indexes)
        matched_ledger_indexes.add(ledger_index)
        aggregate_count += len(bank_indexes)

    for bank_idx, bank_txn in enumerate(bank_transactions):
        if bank_idx not in matched_bank_indexes:
            unmatched.append(bank_txn)

    matched_count = direct_count + fallback_count + aggregate_count
    coverage_pct = round((matched_count / len(bank_transactions)) * 100, 2)
    strategy_breakdown = {
        "direct": direct_count,
        "fallback": fallback_count,
        "aggregate": aggregate_count,
    }
    return {
        "coverage_pct": coverage_pct,
        "unmatched_bank_transactions": unmatched,
        "matched_count": matched_count,
        "total_count": len(bank_transactions),
        "direct_matched_count": direct_count,
        "fallback_matched_count": fallback_count,
        "aggregate_matched_count": aggregate_count,
        "strategy_breakdown": strategy_breakdown,
    }


def _find_ledger_match_direct(
    bank_transaction: Transaction,
    ledger_transactions: list[Transaction],
    used_indexes: set[int],
) -> int | None:
    bank_date = bank_transaction.date
    bank_amount = round(abs(bank_transaction.amount), 2)
    bank_tokens = _description_tokens(bank_transaction.normalized_description or bank_transaction.raw_description)

    for idx, ledger_txn in enumerate(ledger_transactions):
        if idx in used_indexes:
            continue
        if ledger_txn.date is None or bank_date is None:
            continue
        if abs((ledger_txn.date - bank_date).days) > 3:
            continue

        ledger_amount = round(abs(ledger_txn.amount), 2)
        if abs(ledger_amount - bank_amount) > 0.01:
            continue

        ledger_tokens = _description_tokens(ledger_txn.normalized_description or ledger_txn.raw_description)
        if not bank_tokens or not ledger_tokens:
            continue
        if bank_tokens.intersection(ledger_tokens):
            return idx

    return None


def _find_ledger_match_fallback(
    bank_transaction: Transaction,
    ledger_transactions: list[Transaction],
    used_indexes: set[int],
) -> int | None:
    bank_date = bank_transaction.date
    bank_amount = round(abs(bank_transaction.amount), 2)
    for idx, ledger_txn in enumerate(ledger_transactions):
        if idx in used_indexes:
            continue
        if ledger_txn.date is None or bank_date is None:
            continue
        if abs((ledger_txn.date - bank_date).days) > 3:
            continue
        ledger_amount = round(abs(ledger_txn.amount), 2)
        if abs(ledger_amount - bank_amount) > 0.01:
            continue
        return idx
    return None


def _aggregate_match_groups(
    bank_transactions: list[Transaction],
    ledger_transactions: list[Transaction],
    used_bank_indexes: set[int],
    used_ledger_indexes: set[int],
) -> list[tuple[list[int], int]]:
    results: list[tuple[list[int], int]] = []

    for ledger_index, ledger_txn in enumerate(ledger_transactions):
        if ledger_index in used_ledger_indexes:
            continue
        if ledger_txn.date is None:
            continue
        ledger_amount = round(abs(ledger_txn.amount), 2)
        if ledger_amount <= 0:
            continue

        candidates = []
        for bank_index, bank_txn in enumerate(bank_transactions):
            if bank_index in used_bank_indexes:
                continue
            if bank_txn.date is None:
                continue
            if abs((bank_txn.date - ledger_txn.date).days) > 5:
                continue
            if (bank_txn.amount >= 0) != (ledger_txn.amount >= 0):
                continue
            candidates.append((bank_index, bank_txn))

        if len(candidates) < 2:
            continue

        # Conservative aggregate matching: try pairs/triples only.
        candidate_indexes = [item[0] for item in candidates]
        matched_group = None
        for group_size in (2, 3):
            if len(candidate_indexes) < group_size:
                continue
            for combo in combinations(candidate_indexes, group_size):
                combo_total = round(sum(abs(bank_transactions[idx].amount) for idx in combo), 2)
                if abs(combo_total - ledger_amount) <= 0.01:
                    matched_group = list(combo)
                    break
            if matched_group:
                break

        if matched_group:
            results.append((matched_group, ledger_index))
            used_bank_indexes.update(matched_group)
            used_ledger_indexes.add(ledger_index)

    return results


def _description_tokens(value: str) -> set[str]:
    return build_matching_tokens(value)


def _build_unclassified_reason_counts(unclassified_transactions: list[Transaction]) -> dict[str, int]:
    reasons: dict[str, int] = defaultdict(int)
    for txn in unclassified_transactions:
        if not txn.original_category:
            reasons["missing_split_or_category"] += 1
        if txn.reference_number and txn.raw_description.isnumeric():
            reasons["parser_uncertainty"] += 1
        if "fallback_category" not in txn.flags:
            reasons["fallback_no_match"] += 1
    return dict(reasons)


def analyze_unmatched_bank_transactions(
    unmatched_bank_transactions: list[Transaction],
    ledger_transactions: list[Transaction],
) -> list[dict[str, int | str | None | float]]:
    diagnostics: list[dict[str, int | str | None | float]] = []
    for bank_txn in unmatched_bank_transactions:
        absolute_amount = round(abs(bank_txn.amount), 2)
        same_amount = [
            ledger_txn for ledger_txn in ledger_transactions
            if round(abs(ledger_txn.amount), 2) == absolute_amount
        ]
        same_date_window = [
            ledger_txn for ledger_txn in ledger_transactions
            if bank_txn.date is not None
            and ledger_txn.date is not None
            and abs((ledger_txn.date - bank_txn.date).days) <= 3
        ]
        same_amount_and_date = [
            ledger_txn for ledger_txn in same_amount
            if bank_txn.date is not None
            and ledger_txn.date is not None
            and abs((ledger_txn.date - bank_txn.date).days) <= 3
        ]

        reason = "no_same_amount_in_ledger"
        if same_amount_and_date:
            reason = "same_amount_and_date_but_description_diff"
        elif same_amount:
            reason = "same_amount_exists_outside_date_window"
        elif same_date_window:
            reason = "same_date_window_but_amount_diff"

        grouped_candidate_count = _count_grouped_amount_candidates(
            bank_transaction=bank_txn,
            ledger_transactions=ledger_transactions,
        )
        if grouped_candidate_count > 0 and reason == "same_date_window_but_amount_diff":
            reason = "likely_grouped_or_summarized_posting"

        diagnostics.append(
            {
                "date": bank_txn.date.isoformat() if bank_txn.date else None,
                "source_file": bank_txn.source_file,
                "raw_description": bank_txn.raw_description,
                "normalized_description": bank_txn.normalized_description,
                "amount": bank_txn.amount,
                "reason": reason,
                "same_amount_candidates": len(same_amount),
                "same_date_window_candidates": len(same_date_window),
                "same_amount_and_date_candidates": len(same_amount_and_date),
                "grouped_amount_candidates": grouped_candidate_count,
                "reference_number": bank_txn.reference_number,
            }
        )
    return diagnostics


def _count_unmatched_reasons(
    unmatched_diagnostics: list[dict[str, int | str | None | float]],
) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in unmatched_diagnostics:
        reason = str(row.get("reason", "unknown"))
        counts[reason] += 1
    return dict(counts)


def _build_source_coverage_summary(
    ledger_transactions: list[Transaction],
    bank_transactions: list[Transaction],
) -> list[dict[str, int | str | float | None]]:
    summary: list[dict[str, int | str | float | None]] = []
    summary.append(
        {
            "metric": "ledger_transaction_count",
            "value": len(ledger_transactions),
        }
    )
    summary.append(
        {
            "metric": "bank_transaction_count",
            "value": len(bank_transactions),
        }
    )
    summary.append(
        {
            "metric": "count_gap_bank_minus_ledger",
            "value": len(bank_transactions) - len(ledger_transactions),
        }
    )
    if ledger_transactions:
        summary.append(
            {
                "metric": "bank_to_ledger_count_ratio",
                "value": round(len(bank_transactions) / len(ledger_transactions), 4),
            }
        )
    return summary


def _count_grouped_amount_candidates(
    bank_transaction: Transaction,
    ledger_transactions: list[Transaction],
) -> int:
    if bank_transaction.date is None:
        return 0
    bank_amount = round(abs(bank_transaction.amount), 2)
    date_window_candidates = [
        ledger_txn
        for ledger_txn in ledger_transactions
        if ledger_txn.date is not None and abs((ledger_txn.date - bank_transaction.date).days) <= 5
    ]
    count = 0
    amounts = [round(abs(item.amount), 2) for item in date_window_candidates]
    for combo_size in (2, 3):
        if len(amounts) < combo_size:
            continue
        for combo in combinations(amounts, combo_size):
            if abs(round(sum(combo), 2) - bank_amount) <= 0.01:
                count += 1
                if count >= 3:
                    return count
    return count
