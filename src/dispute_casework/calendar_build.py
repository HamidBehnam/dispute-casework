"""The bank's two-year calendar: the weekdays its offices are closed.

The bank keeps the Federal Reserve Banks' holidays. A holiday that falls on a
Saturday leaves the preceding Friday a business day, as at the Reserve Banks;
holidays' XFED market applies that rule itself. Saturdays and Sundays are
never business days. Run `python -m dispute_casework.calendar_build` to
rewrite the committed file.
"""

import json
from datetime import date
from importlib.metadata import version
from pathlib import Path

import holidays

YEARS = (2026, 2027)
MARKET = "XFED"
CALENDAR = Path(__file__).parent / "calendar" / f"bank-{YEARS[0]}-{YEARS[-1]}.json"


def render() -> str:
    # expand=False keeps the calendar to these years; a holiday outside them
    # would otherwise be added on lookup, or read as a working day.
    reserve_banks = holidays.financial_holidays(MARKET, years=YEARS, expand=False)
    calendar = {
        "first_day": date(YEARS[0], 1, 1).isoformat(),
        "last_day": date(YEARS[-1], 12, 31).isoformat(),
        "business_days": "Monday to Friday, except the closed weekdays",
        "closed_weekdays": {
            day.isoformat(): name
            for day, name in reserve_banks.items()
            if not reserve_banks.is_weekend(day)
        },
        "source": f"holidays {version('holidays')}, market {MARKET}",
    }
    return json.dumps(calendar, indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    CALENDAR.write_text(render(), encoding="utf-8")
