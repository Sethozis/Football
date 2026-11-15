#!/usr/bin/env python3
"""Fetch next year's Russian production-calendar exceptions.

Only exceptions are stored:
  * days off that fall on Monday-Friday;
  * working days that fall on Saturday or Sunday.

One JSON file is written per year to holidays/ru/<year>.json.
Only next year is requested, once a month, and its file is overwritten.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = (
    "https://calendar.legalic.ru/api/v1/calendars/"
    "RU-FEDERAL/export?year={year}&format=json"
)
OUTPUT_DIR = Path("holidays/ru")
WEEKDAY_NAMES = {
    1: "monday",
    2: "tuesday",
    3: "wednesday",
    4: "thursday",
    5: "friday",
    6: "saturday",
    7: "sunday",
}


class CalendarFetchError(RuntimeError):
    pass


def now_moscow() -> datetime:
    return datetime.now(ZoneInfo("Europe/Moscow"))


def utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def fetch_year(year: int) -> dict | None:
    """Return the API document or None when the year is not published yet."""
    url = BASE_URL.format(year=year)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "MonthCalendar-HolidayUpdater/1.0",
        },
    )

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 422:
                # The service explicitly uses 422 for a year it does not have.
                return None
            if error.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(attempt * 5)
                continue
            raise CalendarFetchError(
                f"HTTP {error.code} for {year}: {error.reason}"
            ) from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt < 3:
                time.sleep(attempt * 5)
                continue
            raise CalendarFetchError(
                f"Network error for {year}: {error}"
            ) from error
        except json.JSONDecodeError as error:
            raise CalendarFetchError(
                f"Invalid JSON for {year}: {error}"
            ) from error

    raise CalendarFetchError(f"Could not fetch {year}")


def exception_entry(day: dict) -> dict:
    weekday = int(day["weekday"])
    entry = {
        "date": day["date"],
        "weekday": WEEKDAY_NAMES.get(weekday, str(weekday)),
        "type": day.get("type", ""),
    }

    if day.get("holidayName"):
        entry["holidayName"] = day["holidayName"]
    if day.get("transferredFrom"):
        entry["transferredFrom"] = day["transferredFrom"]
    if day.get("transferredTo"):
        entry["transferredTo"] = day["transferredTo"]
    if day.get("shortened"):
        entry["shortened"] = True

    return entry


def make_payload(year: int, document: dict) -> dict:
    version = document.get("version") or {}
    days = document.get("days") or []
    if not days:
        raise CalendarFetchError(f"No days returned for {year}")

    days_off: list[dict] = []
    working_days: list[dict] = []

    for day in days:
        try:
            weekday = int(day["weekday"])
            date_value = str(day["date"])
            is_working = bool(day.get("isWorking"))
        except (KeyError, TypeError, ValueError) as error:
            raise CalendarFetchError(
                f"Unexpected day record for {year}: {day!r}"
            ) from error

        if not date_value.startswith(f"{year:04d}-"):
            raise CalendarFetchError(
                f"Day {date_value} does not belong to {year}"
            )

        is_weekend = weekday in (6, 7)
        if not is_working and not is_weekend:
            days_off.append(exception_entry(day))
        elif is_working and is_weekend:
            working_days.append(exception_entry(day))

    days_off.sort(key=lambda item: item["date"])
    working_days.sort(key=lambda item: item["date"])

    status = str(version.get("status") or "")

    return {
        "country": "RU",
        "calendarId": "RU-FEDERAL",
        "year": year,
        "status": status,
        "preliminary": status != "OFFICIAL",
        "versionId": version.get("versionId", ""),
        "contentHash": document.get("contentHash", ""),
        "source": "https://calendar.legalic.ru/",
        "generatedAt": utc_timestamp(),
        "daysOff": days_off,
        "workingDays": working_days,
    }


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    year = now_moscow().year + 1
    print(f"Fetching next year: {year}")

    try:
        document = fetch_year(year)
    except CalendarFetchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if document is None:
        print(f"{year}: not published yet")
        return 0

    try:
        payload = make_payload(year, document)
    except CalendarFetchError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    path = OUTPUT_DIR / f"{year}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(
        f"{year}: wrote {len(payload['daysOff'])} days off, "
        f"{len(payload['workingDays'])} working weekend days"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
