# Financial Validator MVP (Bank Statements -> Trucking P&L)

This app is now focused on one workflow:

- Upload bank statement PDFs
- Classify transactions into trucking P&L categories
- Resolve unknown items in-app
- Generate monthly and yearly P&L outputs
- Export raw imported transactions to CSV/Excel

## Quick Start

From repo root:

```bash
python -m pip install -r financial_validator_mvp/requirements.txt
python -m streamlit run financial_validator_mvp/streamlit_bank_pnl_app.py
```

Run tests:

```bash
python -m unittest discover financial_validator_mvp/tests -v
```

## Single-Statement Tool (One Month, One PDF)

For a single bank statement conversion with strict statement back checks:

```bash
python -m financial_validator_mvp.single_statement_pnl --input-pdf "C:\path\to\statement.pdf"
```

Optional output workbook path:

```bash
python -m financial_validator_mvp.single_statement_pnl --input-pdf "C:\path\to\statement.pdf" --output-xlsx "C:\path\to\statement_pnl_crosscheck.xlsx"
```

This generates an Excel workbook with:

- line items (one row per transaction)
- daily rollforward check (line-item totals vs statement daily balances)
- top-of-statement summary cross checks
- monthly/yearly P&L tabs

## Main Workflow

1. Upload one or more bank statement PDFs.
2. Click **Parse Statements**.
3. Review imported transactions.
4. In **Resolve Unknown Categories**, assign category or mark as `NON_PNL`.
5. Click **Save Category Rules** to persist learned mappings.
6. Review generated:
   - yearly summary
   - yearly detail by category
   - monthly pivot
   - monthly detail
7. Click **Export Raw + P&L CSV/XLSX**.
8. Review **Export Reconciliation Checks** in-app (must all pass).
9. Click **Generate PDF Reports** to create report-style P&L and Balance Sheet PDFs.

## Output Files

Written to `financial_validator_mvp/data/sample_outputs` by default:

- `bank_transactions_raw.csv`
- `bank_transactions_raw.xlsx`
- `monthly_pnl_detail.csv`
- `monthly_pnl_detail.xlsx`
- `yearly_pnl_detail.csv`
- `yearly_pnl_detail.xlsx`
- `yearly_pnl_summary.csv`
- `yearly_pnl_summary.xlsx`
- `export_reconciliation_report.csv`
- `balance_summary_validation.csv`
- `learned_rules_bank.json`
- `Profit_and_Loss_report.pdf`
- `Balance_Sheet_report.pdf`

## Back-Check Validation (Adds-Up Guardrail)

On export, the app runs reconciliation checks and writes `export_reconciliation_report.csv`.

Checks include:

- monthly detail total equals yearly detail total
- monthly grouped rollups tie to yearly grouped rollups
- yearly summary formulas tie out (`Gross Profit`, `Net Ordinary Income`, `Net Income`)
- unclassified summary line equals yearly unclassified total
- transaction rollup equals yearly total (excluding `NON_PNL`)
- bank statement balance summary checks (beginning/additions/deductions/ending and net-change tie-out)
- daily balance checks by posting date (computed ending balance vs statement Daily Balance)
- CSV and XLSX parity (row count + normalized record equality)

Any failed check is shown in the app and flagged in the report.

## How Categorization Works

- Base trucking rules: `financial_validator_mvp/data/trucking_rules.json`
- Learned rules: `financial_validator_mvp/data/learned_rules_bank.json`
- Rules are deterministic (no LLM dependency).
- Learned rules are applied on future runs.

If a line cannot be confidently mapped, it remains `UNCLASSIFIED` until you assign it in the UI.

## Duplicate Upload Handling

- Uploads are deduplicated by SHA-256 content hash.
- First instance is used; exact duplicates are skipped.
- The app shows which duplicate file was skipped.

## Trucking P&L Notes

- Income-style categories are reported as positive inflows.
- COGS/expense-style categories are reported as positive spend totals.
- Transactions marked `NON_PNL` are excluded from P&L totals.
- `UNCLASSIFIED` is shown separately so unresolved spend/income is visible.

## Supported Statement Format (Current MVP)

- PNC-style statement parsing is implemented first.
- PDFs must be text-extractable (no OCR in this phase).

## Legacy App

The previous mixed-document validator (GL + P&L + bank supplemental) still exists at:

```bash
python -m streamlit run financial_validator_mvp/streamlit_app.py
```

Use the bank-only app above for the current trucking P&L workflow.
