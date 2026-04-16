from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from financial_validator_mvp.services.classifier import ClassificationRule, build_contains_rule, build_exact_rule, rule_to_dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_RULES_PATH = DATA_DIR / "rules.json"
DEFAULT_LEARNED_RULES_PATH = DATA_DIR / "learned_rules.json"


def ensure_rules_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(json.dumps({"version": 1, "rules": []}, indent=2), encoding="utf-8")


def load_rule_dicts(path: Path) -> list[dict[str, Any]]:
    ensure_rules_file(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rules = payload.get("rules", [])
    return [rule for rule in rules if isinstance(rule, dict)]


def save_rule_dicts(path: Path, rules: list[dict[str, Any]]) -> None:
    ensure_rules_file(path)
    payload = {"version": 1, "rules": rules}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_combined_rule_dicts(
    base_rules_path: Path = DEFAULT_RULES_PATH,
    learned_rules_path: Path = DEFAULT_LEARNED_RULES_PATH,
) -> list[dict[str, Any]]:
    return load_rule_dicts(base_rules_path) + load_rule_dicts(learned_rules_path)


def upsert_learned_rules(
    assignments: list[dict[str, Any]],
    learned_rules_path: Path = DEFAULT_LEARNED_RULES_PATH,
) -> list[dict[str, Any]]:
    existing_rules = load_rule_dicts(learned_rules_path)
    existing_by_key = {
        _rule_key(rule): rule
        for rule in existing_rules
    }

    for assignment in assignments:
        normalized_description = str(assignment.get("normalized_description", "")).strip().lower()
        category = str(assignment.get("category", "")).strip()
        non_pnl = bool(assignment.get("non_pnl", False))
        match_type = str(assignment.get("match_type", "exact")).strip().lower()
        exclude_pattern = str(assignment.get("exclude_pattern", "")).strip().lower()
        if not normalized_description or not category:
            continue

        if match_type == "contains":
            learned_rule = build_contains_rule(
                pattern=normalized_description,
                category=category,
                non_pnl=non_pnl,
                exclude_pattern=exclude_pattern,
                origin="learned_rule",
            )
        else:
            learned_rule = build_exact_rule(
                normalized_description=normalized_description,
                category=category,
                non_pnl=non_pnl,
                origin="learned_rule",
            )
        learned_rule_dict = rule_to_dict(learned_rule)
        existing_by_key[_rule_key(learned_rule_dict)] = learned_rule_dict

    merged_rules = list(existing_by_key.values())
    merged_rules.sort(key=lambda item: int(item.get("priority", 0)), reverse=True)
    save_rule_dicts(path=learned_rules_path, rules=merged_rules)
    return merged_rules


def export_learned_rules(
    output_dir: Path,
    learned_rules_path: Path = DEFAULT_LEARNED_RULES_PATH,
) -> Path:
    ensure_rules_file(learned_rules_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "learned_rules.json"
    destination.write_text(learned_rules_path.read_text(encoding="utf-8"), encoding="utf-8")
    return destination


def _rule_key(rule: dict[str, Any]) -> str:
    return "|".join(
        [
            str(rule.get("field", "normalized_description")),
            str(rule.get("match_type", "contains")),
            str(rule.get("pattern", "")).lower().strip(),
            str(rule.get("exclude_pattern", "")).lower().strip(),
        ]
    )
