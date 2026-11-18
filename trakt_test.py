#!/usr/bin/env python3
"""Diagnostic: how many digital/streaming movie releases Trakt exposes.

This script is intentionally separate from movies.py. It only reads public
calendar data and compares it with the existing movie_releases.json.

Requires TRAKT_CLIENT_ID. A user OAuth token is not needed for the global
calendar endpoints.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

BASE = "https://api.trakt.tv"
DEFAULT_DAYS = 425
CHUNK_DAYS = 30
HTTP_TIMEOUT = 25
USER_AGENT = "month-calendar-trakt-test/1.0"


def request_json(
    path: str,
    client_id: str,
    timeout: int,
) -> Optional[Any]:
    url = f"{BASE}{path}"
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Content-Type": "application/json",
        "trakt-api-version": "2",
        "trakt-api-key": client_id,
    }
    for attempt in range(3):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 429:
                delay = 5 * (attempt + 1)
                logging.warning("Trakt rate limit; sleeping %ss", delay)
                time.sleep(delay)
                continue
            logging.warning("Trakt HTTP %s for %s", error.code, url)
            return None
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            logging.warning("Trakt request failed for %s: %s", url, error)
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    return None


def month_start(value: date) -> date:
    return value.replace(day=1)


def add_months(value: date, months: int) -> date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def default_start() -> date:
    return add_months(month_start(date.today()), -1)


def parse_date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def movie_from_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    movie = item.get("movie")
    if isinstance(movie, dict):
        return movie
    if isinstance(item.get("title"), str) and item.get("ids"):
        return item
    return None


def release_date_from_item(item: Dict[str, Any]) -> Optional[date]:
    for key in ("released", "first_aired", "date"):
        value = item.get(key)
        parsed = parse_date(value)
        if parsed is not None:
            return parsed
    return None


def fetch_calendar(
    kind: str,
    start: date,
    days: int,
    client_id: str,
    timeout: int,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    cursor = start
    remaining = max(0, days)

    while remaining > 0:
        chunk = min(CHUNK_DAYS, remaining)
        path = (
            f"/calendars/all/{kind}/{cursor.isoformat()}/{chunk}"
        )
        payload = request_json(path, client_id, timeout)
        if payload is None:
            break
        if isinstance(payload, list):
            rows.extend(
                item for item in payload
                if isinstance(item, dict)
            )
        logging.info(
            "Trakt %s %s..%s: %s items",
            kind,
            cursor,
            cursor + timedelta(days=chunk - 1),
            len(payload) if isinstance(payload, list) else 0,
        )
        cursor += timedelta(days=chunk)
        remaining -= chunk
        time.sleep(0.15)

    return rows


def summarize(
    kind: str,
    rows: Iterable[Dict[str, Any]],
    start: date,
    end: date,
    existing_tmdb_ids: set[int],
) -> Dict[str, Any]:
    today = date.today()
    movies: Dict[str, Dict[str, Any]] = {}
    dates_in_window: List[date] = []
    future_dates: List[date] = []
    first_of_month = 0
    existing = 0
    new = 0

    for item in rows:
        released = release_date_from_item(item)
        if released is None or released < start or released > end:
            continue

        movie = movie_from_item(item)
        if movie is None:
            continue

        ids = movie.get("ids") or {}
        if not isinstance(ids, dict):
            ids = {}
        key = str(
            ids.get("trakt")
            or ids.get("tmdb")
            or ids.get("imdb")
            or movie.get("title")
            or ""
        )
        if not key:
            continue

        dates_in_window.append(released)
        if released >= today:
            future_dates.append(released)
        if released.day == 1:
            first_of_month += 1

        tmdb_id = ids.get("tmdb")
        if isinstance(tmdb_id, int):
            if tmdb_id in existing_tmdb_ids:
                existing += 1
            else:
                new += 1

        movies.setdefault(
            key,
            {
                "title": movie.get("title"),
                "year": movie.get("year"),
                "tmdb_id": tmdb_id,
                "trakt_id": ids.get("trakt"),
                "imdb_id": ids.get("imdb"),
                "release_date": released.isoformat(),
            },
        )

    dates_in_window.sort()
    return {
        "kind": kind,
        "items": len(list(rows)),
        "unique_movies": len(movies),
        "release_dates_in_window": len(dates_in_window),
        "future_releases": len(future_dates),
        "first_of_month_dates": first_of_month,
        "existing_tmdb_movies": existing,
        "new_tmdb_movies": new,
        "min_date": dates_in_window[0].isoformat() if dates_in_window else None,
        "max_date": dates_in_window[-1].isoformat() if dates_in_window else None,
        "sample": list(movies.values())[:20],
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare Trakt release volume with movie_releases.json."
    )
    parser.add_argument(
        "--client-id",
        default=os.environ.get("TRAKT_CLIENT_ID", ""),
        help="Trakt application Client ID (or TRAKT_CLIENT_ID env)",
    )
    parser.add_argument(
        "--start",
        type=date.fromisoformat,
        default=default_start(),
        help="start date YYYY-MM-DD",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"number of days (default: {DEFAULT_DAYS})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("trakt_test.json"),
        help="diagnostic JSON output",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=HTTP_TIMEOUT,
        help="HTTP timeout in seconds",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if not args.client_id:
        logging.error("TRAKT_CLIENT_ID is not set")
        return 2
    if args.days <= 0:
        logging.error("--days must be positive")
        return 2

    end = args.start + timedelta(days=args.days - 1)
    existing_tmdb_ids: set[int] = set()
    existing_path = Path("movie_releases.json")
    if existing_path.exists():
        try:
            payload = json.loads(existing_path.read_text(encoding="utf-8"))
            for movie in payload.get("movies") or []:
                tmdb_id = movie.get("tmdb_id")
                if isinstance(tmdb_id, int):
                    existing_tmdb_ids.add(tmdb_id)
        except (OSError, ValueError):
            pass

    streaming_rows = fetch_calendar(
        "streaming",
        args.start,
        args.days,
        args.client_id,
        args.timeout,
    )
    all_rows = fetch_calendar(
        "movies",
        args.start,
        args.days,
        args.client_id,
        args.timeout,
    )

    summary = {
        "window": {
            "from": args.start.isoformat(),
            "to": end.isoformat(),
            "days": args.days,
        },
        "existing_tmdb_ids": len(existing_tmdb_ids),
        "streaming": summarize(
            "streaming",
            streaming_rows,
            args.start,
            end,
            existing_tmdb_ids,
        ),
        "all_movies": summarize(
            "movies",
            all_rows,
            args.start,
            end,
            existing_tmdb_ids,
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for kind in ("streaming", "all_movies"):
        block = summary[kind]
        logging.info(
            "%s: %s unique, %s future, %s dates, %s..%s",
            kind,
            block["unique_movies"],
            block["future_releases"],
            block["release_dates_in_window"],
            block["min_date"],
            block["max_date"],
        )
    logging.info("Wrote %s", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
