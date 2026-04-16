from __future__ import annotations

from pathlib import Path

import pandas as pd

from financial_validator_mvp.models.schemas import BankStatementBalanceSummary
from financial_validator_mvp.services.pnl_builder import PnlBuildResult

SECTION_ORDER = ["Income", "COGS", "Expenses", "Other Income", "Other Expense", "Unclassified"]


def generate_profit_and_loss_pdf(
    *,
    pnl: PnlBuildResult,
    output_path: Path,
    company_name: str,
    period_label: str,
) -> Path:
    canvas, page_width, page_height = _build_canvas(output_path)
    y = _draw_header(
        canvas=canvas,
        page_width=page_width,
        page_height=page_height,
        company_name=company_name,
        report_title="Profit and Loss",
        subtitle=period_label,
    )

    yearly_detail = pnl.yearly_detail.copy()
    if yearly_detail.empty:
        canvas.drawString(72, y, "No P&L data available.")
        canvas.save()
        return output_path

    yearly_detail["section"] = yearly_detail["section"].astype(str)
    yearly_detail["category"] = yearly_detail["category"].astype(str)
    yearly_detail["amount"] = pd.to_numeric(yearly_detail["amount"], errors="coerce").fillna(0.0)

    for section in SECTION_ORDER:
        section_rows = yearly_detail[yearly_detail["section"] == section]
        if section_rows.empty:
            continue
        y = _ensure_row_space(canvas, y, page_width, page_height, company_name, "Profit and Loss", period_label)
        canvas.setFont("Helvetica-Bold", 11)
        canvas.drawString(72, y, section)
        y -= 16

        section_total = 0.0
        for _, row in section_rows.sort_values("category").iterrows():
            amount = float(row["amount"])
            section_total += amount
            y = _ensure_row_space(canvas, y, page_width, page_height, company_name, "Profit and Loss", period_label)
            canvas.setFont("Helvetica", 10)
            canvas.drawString(90, y, row["category"])
            canvas.drawRightString(page_width - 72, y, _money(amount))
            y -= 14

        y = _ensure_row_space(canvas, y, page_width, page_height, company_name, "Profit and Loss", period_label)
        canvas.setFont("Helvetica-Bold", 10)
        canvas.drawString(90, y, f"Total {section}")
        canvas.drawRightString(page_width - 72, y, _money(section_total))
        y -= 18

    y -= 4
    summary_map = _summary_map(pnl.yearly_summary)
    summary_lines = [
        "Total Income",
        "Total COGS",
        "Gross Profit",
        "Total Expenses",
        "Net Ordinary Income",
        "Total Other Income",
        "Total Other Expense",
        "Net Income",
        "Unclassified (needs review)",
    ]
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(72, y, "Summary")
    y -= 16
    for line_item in summary_lines:
        y = _ensure_row_space(canvas, y, page_width, page_height, company_name, "Profit and Loss", period_label)
        value = float(summary_map.get(line_item, 0.0))
        canvas.setFont("Helvetica-Bold" if line_item in {"Gross Profit", "Net Income"} else "Helvetica", 10)
        canvas.drawString(90, y, line_item)
        canvas.drawRightString(page_width - 72, y, _money(value))
        y -= 14

    canvas.save()
    return output_path


def generate_balance_sheet_pdf(
    *,
    pnl: PnlBuildResult,
    balance_summaries: list[BankStatementBalanceSummary],
    output_path: Path,
    company_name: str,
    as_of_label: str,
) -> Path:
    canvas, page_width, page_height = _build_canvas(output_path)
    y = _draw_header(
        canvas=canvas,
        page_width=page_width,
        page_height=page_height,
        company_name=company_name,
        report_title="Balance Sheet",
        subtitle=f"As of {as_of_label}",
    )

    net_income = float(_summary_map(pnl.yearly_summary).get("Net Income", 0.0))
    assets_cash = _latest_total_cash(balance_summaries)
    total_assets = assets_cash
    liabilities_total = 0.0
    opening_equity = total_assets - net_income
    total_equity = opening_equity + net_income
    total_liabilities_and_equity = liabilities_total + total_equity

    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(72, y, "ASSETS")
    y -= 16
    canvas.setFont("Helvetica", 10)
    canvas.drawString(90, y, "Cash and Bank")
    canvas.drawRightString(page_width - 72, y, _money(assets_cash))
    y -= 14
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(90, y, "Total Assets")
    canvas.drawRightString(page_width - 72, y, _money(total_assets))
    y -= 24

    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(72, y, "LIABILITIES AND EQUITY")
    y -= 16
    canvas.setFont("Helvetica", 10)
    canvas.drawString(90, y, "Total Liabilities")
    canvas.drawRightString(page_width - 72, y, _money(liabilities_total))
    y -= 18
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(90, y, "Equity")
    y -= 14
    canvas.setFont("Helvetica", 10)
    canvas.drawString(108, y, "Opening Equity (derived)")
    canvas.drawRightString(page_width - 72, y, _money(opening_equity))
    y -= 14
    canvas.drawString(108, y, "Net Income")
    canvas.drawRightString(page_width - 72, y, _money(net_income))
    y -= 14
    canvas.setFont("Helvetica-Bold", 10)
    canvas.drawString(90, y, "Total Equity")
    canvas.drawRightString(page_width - 72, y, _money(total_equity))
    y -= 18
    canvas.drawString(90, y, "Total Liabilities and Equity")
    canvas.drawRightString(page_width - 72, y, _money(total_liabilities_and_equity))
    y -= 20

    canvas.setFont("Helvetica-Oblique", 9)
    canvas.drawString(
        72,
        y,
        "Note: This is a bank-based balance sheet draft and excludes non-bank assets/liabilities not present in statements.",
    )

    canvas.save()
    return output_path


def generate_monthly_detail_pdf(
    *,
    pnl: PnlBuildResult,
    output_path: Path,
    company_name: str,
    period_label: str,
) -> Path:
    canvas, page_width, page_height = _build_canvas(output_path)
    title = "Monthly Detail"
    y = _draw_header(
        canvas=canvas,
        page_width=page_width,
        page_height=page_height,
        company_name=company_name,
        report_title=title,
        subtitle=period_label,
    )
    monthly = pnl.monthly_detail.copy()
    if monthly.empty:
        canvas.drawString(72, y, "No monthly detail data available.")
        canvas.save()
        return output_path
    monthly["month"] = monthly["month"].astype(str)
    monthly["section"] = monthly["section"].astype(str)
    monthly["category"] = monthly["category"].astype(str)
    monthly["amount"] = pd.to_numeric(monthly["amount"], errors="coerce").fillna(0.0)
    y = _draw_tabular_lines(
        canvas=canvas,
        y=y,
        page_width=page_width,
        page_height=page_height,
        company_name=company_name,
        report_title=title,
        subtitle=period_label,
        header="Month | Section | Category | Amount",
        rows=[
            f"{row['month']} | {row['section']} | {row['category']} | {_money(float(row['amount']))}"
            for _, row in monthly.sort_values(by=["month", "section", "category"]).iterrows()
        ],
    )
    canvas.save()
    return output_path


def generate_period_total_by_category_pdf(
    *,
    pnl: PnlBuildResult,
    output_path: Path,
    company_name: str,
    period_label: str,
) -> Path:
    canvas, page_width, page_height = _build_canvas(output_path)
    title = "Period Total by Category"
    y = _draw_header(
        canvas=canvas,
        page_width=page_width,
        page_height=page_height,
        company_name=company_name,
        report_title=title,
        subtitle=period_label,
    )
    detail = pnl.yearly_detail.copy()
    if detail.empty:
        canvas.drawString(72, y, "No period totals available.")
        canvas.save()
        return output_path
    detail["section"] = detail["section"].astype(str)
    detail["category"] = detail["category"].astype(str)
    detail["amount"] = pd.to_numeric(detail["amount"], errors="coerce").fillna(0.0)
    y = _draw_tabular_lines(
        canvas=canvas,
        y=y,
        page_width=page_width,
        page_height=page_height,
        company_name=company_name,
        report_title=title,
        subtitle=period_label,
        header="Section | Category | Amount",
        rows=[
            f"{row['section']} | {row['category']} | {_money(float(row['amount']))}"
            for _, row in detail.sort_values(by=["section", "category"]).iterrows()
        ],
    )
    canvas.save()
    return output_path


def generate_period_summary_pdf(
    *,
    pnl: PnlBuildResult,
    output_path: Path,
    company_name: str,
    period_label: str,
    summary_title: str = "Period Summary",
) -> Path:
    canvas, page_width, page_height = _build_canvas(output_path)
    y = _draw_header(
        canvas=canvas,
        page_width=page_width,
        page_height=page_height,
        company_name=company_name,
        report_title=summary_title,
        subtitle=period_label,
    )
    summary = pnl.yearly_summary.copy()
    if summary.empty:
        canvas.drawString(72, y, "No summary data available.")
        canvas.save()
        return output_path
    summary["line_item"] = summary["line_item"].astype(str)
    summary["amount"] = pd.to_numeric(summary["amount"], errors="coerce").fillna(0.0)
    y = _draw_tabular_lines(
        canvas=canvas,
        y=y,
        page_width=page_width,
        page_height=page_height,
        company_name=company_name,
        report_title=summary_title,
        subtitle=period_label,
        header="Line Item | Amount",
        rows=[
            f"{row['line_item']} | {_money(float(row['amount']))}"
            for _, row in summary.iterrows()
        ],
    )
    canvas.save()
    return output_path


def _build_canvas(output_path: Path):
    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas as report_canvas
    except ImportError as exc:
        raise RuntimeError("reportlab is required for PDF export. Install with: pip install reportlab") from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas = report_canvas.Canvas(str(output_path), pagesize=letter)
    page_width, page_height = letter
    return canvas, float(page_width), float(page_height)


def _draw_header(
    *,
    canvas,
    page_width: float,
    page_height: float,
    company_name: str,
    report_title: str,
    subtitle: str,
) -> float:
    y = page_height - 54
    canvas.setFont("Helvetica-Bold", 14)
    canvas.drawCentredString(page_width / 2, y, company_name or "Company")
    y -= 18
    canvas.setFont("Helvetica-Bold", 12)
    canvas.drawCentredString(page_width / 2, y, report_title)
    y -= 16
    canvas.setFont("Helvetica", 10)
    canvas.drawCentredString(page_width / 2, y, subtitle)
    y -= 20
    canvas.line(72, y, page_width - 72, y)
    return y - 20


def _ensure_row_space(canvas, y: float, page_width: float, page_height: float, company_name: str, title: str, subtitle: str) -> float:
    if y >= 72:
        return y
    canvas.showPage()
    return _draw_header(
        canvas=canvas,
        page_width=page_width,
        page_height=page_height,
        company_name=company_name,
        report_title=title,
        subtitle=subtitle,
    )


def _draw_tabular_lines(
    *,
    canvas,
    y: float,
    page_width: float,
    page_height: float,
    company_name: str,
    report_title: str,
    subtitle: str,
    header: str,
    rows: list[str],
) -> float:
    canvas.setFont("Helvetica-Bold", 10)
    y = _ensure_row_space(canvas, y, page_width, page_height, company_name, report_title, subtitle)
    canvas.drawString(72, y, header)
    y -= 14
    canvas.setFont("Helvetica", 9)
    for row in rows:
        y = _ensure_row_space(canvas, y, page_width, page_height, company_name, report_title, subtitle)
        canvas.drawString(72, y, row[:140])
        y -= 12
    return y


def _summary_map(summary_frame: pd.DataFrame) -> dict[str, float]:
    if summary_frame.empty:
        return {}
    result = {}
    for _, row in summary_frame.iterrows():
        result[str(row["line_item"])] = float(row["amount"])
    return result


def _latest_total_cash(balance_summaries: list[BankStatementBalanceSummary]) -> float:
    if not balance_summaries:
        return 0.0
    dated = [item for item in balance_summaries if item.period_end is not None and item.ending_balance is not None]
    if not dated:
        return 0.0
    latest_date = max(item.period_end for item in dated if item.period_end is not None)
    return float(
        sum(
            item.ending_balance or 0.0
            for item in dated
            if item.period_end == latest_date
        )
    )


def _money(value: float) -> str:
    if value < 0:
        return f"({abs(value):,.2f})"
    return f"{value:,.2f}"
