"""The committed bank calendar and the generator that writes it."""

import json
from datetime import date
from typing import Any

import pytest

from dispute_casework.calendar_build import CALENDAR, render

SATURDAY_HOLIDAY_FRIDAYS = ["2026-07-03", "2027-06-18", "2027-12-24", "2027-12-31"]
SUNDAY_HOLIDAY_MONDAY = "2027-07-05"


def generated() -> dict[str, Any]:
    calendar: dict[str, Any] = json.loads(render())
    return calendar


def committed() -> dict[str, Any]:
    calendar: dict[str, Any] = json.loads(CALENDAR.read_text(encoding="utf-8"))
    return calendar


@pytest.mark.parametrize("calendar", [generated, committed])
def test_saturday_holiday_not_observed_friday(calendar: Any) -> None:
    closed = calendar()["closed_weekdays"]
    assert closed.keys().isdisjoint(SATURDAY_HOLIDAY_FRIDAYS)
    assert SUNDAY_HOLIDAY_MONDAY in closed


def test_the_committed_calendar_is_what_the_generator_writes() -> None:
    assert CALENDAR.read_text(encoding="utf-8") == render(), (
        "run python -m dispute_casework.calendar_build"
    )


def test_no_closed_day_is_a_weekend() -> None:
    closed = [date.fromisoformat(day) for day in committed()["closed_weekdays"]]
    assert all(day.weekday() < 5 for day in closed)


def test_the_calendar_covers_exactly_its_two_years() -> None:
    calendar = committed()
    first = date.fromisoformat(calendar["first_day"])
    last = date.fromisoformat(calendar["last_day"])
    assert (first, last) == (date(2026, 1, 1), date(2027, 12, 31))
    closed = [date.fromisoformat(day) for day in calendar["closed_weekdays"]]
    assert all(first <= day <= last for day in closed)
