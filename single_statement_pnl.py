from __future__ import annotations

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from financial_validator_mvp.services.single_statement_converter import convert_single_statement_to_pnl_workbook

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DEFAULT_OUTPUT_DIR = DATA_DIR / "sample_outputs"
TRUCKING_RULES_PATH = DATA_DIR / "trucking_rules.json"
LEARNED_BANK_RULES_PATH = DATA_DIR / "learned_rules_bank.json"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert a single PNC bank statement PDF into monthly P&L outputs with statement back checks."
    )
    parser.add_argument(
        "--input-pdf",
        required=True,
        help="Absolute path to the bank statement PDF.",
    )
    parser.add_argument(
        "--output-xlsx",
        required=False,
        help="Output workbook path. Default: financial_validator_mvp/data/sample_outputs/<statement_name>_pnl_crosscheck.xlsx",
    )
    args = parser.parse_args()

    input_pdf = Path(args.input_pdf).expanduser().resolve()
    if not input_pdf.exists():
        raise FileNotFoundError(f"Input PDF not found: {input_pdf}")

    if args.output_xlsx:
        output_xlsx = Path(args.output_xlsx).expanduser().resolve()
    else:
        output_xlsx = DEFAULT_OUTPUT_DIR / f"{input_pdf.stem}_pnl_crosscheck.xlsx"

    artifacts = convert_single_statement_to_pnl_workbook(
        statement_pdf=input_pdf,
        output_xlsx=output_xlsx,
        trucking_rules_path=TRUCKING_RULES_PATH,
        learned_rules_path=LEARNED_BANK_RULES_PATH,
    )

    print("Created statement conversion artifacts:")
    for name, path in artifacts.items():
        print(f"- {name}: {path}")


if __name__ == "__main__":
    main()
