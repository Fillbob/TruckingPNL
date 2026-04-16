from __future__ import annotations

import re
from datetime import date, datetime, timedelta

MONEY_PATTERN = re.compile(r"\(?-?\$?\d[\d,]*\.\d{2}\)?")
MONTH_LOOKUP = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def parse_money(token: str) -> float:
    cleaned = token.strip()
    negative = False

    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1]

    cleaned = cleaned.replace("$", "").replace(",", "")
    if cleaned.startswith("-"):
        negative = True
        cleaned = cleaned[1:]

    value = float(cleaned)
    return -value if negative else value


def safe_parse_date(value: str) -> datetime.date | None:
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def collapse_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_statement_period(text: str) -> str | None:
    date_range_match = re.search(
        (
            r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
            r"[a-z]*\s+\d{1,2},?\s+\d{4}\s*(?:-|to)\s*"
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
            r"[a-z]*\s+\d{1,2},?\s+\d{4})"
        ),
        text,
        flags=re.IGNORECASE,
    )
    if date_range_match:
        return collapse_spaces(date_range_match.group(1))

    month_range_match = re.search(
        (
            r"((?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
            r"[a-z]*\s+\d{4}\s*(?:-|to)\s*"
            r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
            r"[a-z]*\s+\d{4})"
        ),
        text,
        flags=re.IGNORECASE,
    )
    if month_range_match:
        return collapse_spaces(month_range_match.group(1))

    years = sorted(set(re.findall(r"\b20\d{2}\b", text)))
    if len(years) == 1:
        return years[0]
    if len(years) >= 2:
        return f"{years[0]}-{years[-1]}"
    return None


def parse_period_date_range(text: str) -> tuple[date | None, date | None, float]:
    cleaned = collapse_spaces(text)
    lowered = cleaned.lower()

    explicit_dates = re.search(
        r"(\d{1,2}/\d{1,2}/\d{4})\s*(?:-|to)\s*(\d{1,2}/\d{1,2}/\d{4})",
        cleaned,
        flags=re.IGNORECASE,
    )
    if explicit_dates:
        start = safe_parse_date(explicit_dates.group(1))
        end = safe_parse_date(explicit_dates.group(2))
        if start and end:
            return start, end, 1.0

    month_day_range = re.search(
        (
            r"(?P<m1>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+"
            r"(?P<d1>\d{1,2})\s*(?:-|to)\s*"
            r"(?P<m2>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+"
            r"(?P<d2>\d{1,2}),?\s*(?P<y>\d{4})"
        ),
        lowered,
    )
    if month_day_range:
        year = int(month_day_range.group("y"))
        start = date(year, MONTH_LOOKUP[month_day_range.group("m1")[:3]], int(month_day_range.group("d1")))
        end = date(year, MONTH_LOOKUP[month_day_range.group("m2")[:3]], int(month_day_range.group("d2")))
        if start <= end:
            return start, end, 0.95

    month_year_range = re.search(
        (
            r"(?P<m1>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s*(?:-|to)\s*"
            r"(?P<m2>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*,?\s*(?P<y>\d{4})"
        ),
        lowered,
    )
    if month_year_range:
        year = int(month_year_range.group("y"))
        start = date(year, MONTH_LOOKUP[month_year_range.group("m1")[:3]], 1)
        end_month = MONTH_LOOKUP[month_year_range.group("m2")[:3]]
        end = _month_end(year=year, month=end_month)
        return start, end, 0.9

    as_of_match = re.search(
        (
            r"as of\s+"
            r"(?P<m>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+"
            r"(?P<d>\d{1,2}),?\s*(?P<y>\d{4})"
        ),
        lowered,
    )
    if as_of_match:
        day = date(
            int(as_of_match.group("y")),
            MONTH_LOOKUP[as_of_match.group("m")[:3]],
            int(as_of_match.group("d")),
        )
        return day, day, 0.9

    year_matches = sorted(set(re.findall(r"\b20\d{2}\b", lowered)))
    if len(year_matches) == 1:
        year = int(year_matches[0])
        return date(year, 1, 1), date(year, 12, 31), 0.6
    if len(year_matches) > 1:
        year = int(year_matches[0])
        return date(year, 1, 1), date(year, 12, 31), 0.4

    return None, None, 0.0


def periods_overlap(
    start_a: date | None,
    end_a: date | None,
    start_b: date | None,
    end_b: date | None,
) -> bool:
    if not all([start_a, end_a, start_b, end_b]):
        return False
    return max(start_a, start_b) <= min(end_a, end_b)


def derive_year_for_short_date(
    short_date: str,
    period_start: date | None,
    period_end: date | None,
) -> date | None:
    match = re.match(r"(?P<m>\d{1,2})/(?P<d>\d{1,2})$", short_date.strip())
    if not match:
        return safe_parse_date(short_date)

    month = int(match.group("m"))
    day = int(match.group("d"))
    if period_start and period_end:
        year = period_start.year
        if period_end.year > period_start.year and month < period_start.month:
            year = period_end.year
        try:
            return date(year, month, day)
        except ValueError:
            return None

    if period_start:
        try:
            return date(period_start.year, month, day)
        except ValueError:
            return None
    return None


def _month_end(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    next_month = date(year, month + 1, 1)
    return next_month - timedelta(days=1)
