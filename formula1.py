#!/usr/bin/env python3
"""Collect the current Formula 1 season schedule from ESPN.

The output contains only the schedule: Grand Prix name, circuit/country and
every practice, qualifying, sprint and race session with its UTC start time.
Results and driver information are intentionally not stored.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports"
DEFAULT_OUTPUT = "f1.json"
DEFAULT_WORKERS = 8
HTTP_TIMEOUT = 20
USER_AGENT = "month-calendar-f1-sync/1.0"

SESSION_TYPES = {
    "FP1": ("practice", "Практика 1", "Practice 1"),
    "FP2": ("practice", "Практика 2", "Practice 2"),
    "FP3": ("practice", "Практика 3", "Practice 3"),
    "Qual": ("qualifying", "Квалификация", "Qualifying"),
    "SS": ("sprint_qualifying", "Спринт-квалификация", "Sprint Qualifying"),
    "SR": ("sprint", "Спринт", "Sprint"),
    "Race": ("race", "Гонка", "Race"),
}

_request_lock = threading.Lock()
_request_count = 0


def next_request_number() -> int:
    global _request_count
    with _request_lock:
        _request_count += 1
        return _request_count


def get_json(url: str) -> Optional[Dict[str, Any]]:
    if url.startswith("http://"):
        url = "https://" + url[len("http://"):]

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }

    for attempt in range(3):
        next_request_number()
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code == 429:
                delay = 5 * (attempt + 1)
                logging.warning("ESPN rate limit; sleeping %ss", delay)
                time.sleep(delay)
                continue
            logging.warning("ESPN HTTP %s for %s", error.code, url)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            logging.warning("ESPN request failed for %s: %s", url, error)

        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))

    return None


def scoreboard_url(season: int) -> str:
    return f"{ESPN_SITE}/racing/f1/scoreboard?dates={season}"


def core_event_url(event_id: str) -> str:
    return (
        f"{ESPN_CORE}/racing/leagues/f1/events/"
        f"{urllib.parse.quote(event_id)}?lang=en&region=us"
    )


def session_info(abbreviation: str) -> tuple[str, str, str, bool]:
    known = SESSION_TYPES.get(abbreviation)
    if known:
        kind, name_ru, name_en = known
        return kind, name_ru, name_en, kind in {"sprint", "sprint_qualifying"}

    value = (abbreviation or "session").strip()
    return "other", value, value, False


def parse_utc(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def country_code_from_flag(url: Any) -> str:
    if not isinstance(url, str):
        return ""
    match = re.search(r"/([A-Za-z]{2,3})\.(?:png|svg)(?:\?|$)", url)
    return match.group(1).upper() if match else ""


def fetch_circuit(circuit_ref: Any) -> Dict[str, str]:
    if not isinstance(circuit_ref, dict):
        return {}
    payload = get_json(circuit_ref.get("$ref", ""))
    if not payload:
        return {}

    address = payload.get("address") or {}
    return {
        "circuit": payload.get("fullName") or "",
        "city": address.get("city") or "",
        "country": address.get("country") or "",
    }


def fetch_venue_country(venue_ref: Any) -> tuple[str, str]:
    if not isinstance(venue_ref, dict):
        return "", ""
    payload = get_json(venue_ref.get("$ref", ""))
    if not payload:
        return "", ""

    address = payload.get("address") or {}
    flag = payload.get("countryFlag") or {}
    return (
        address.get("country") or "",
        country_code_from_flag(flag.get("href")),
    )


def build_event(
    event: Dict[str, Any],
    season: int,
) -> Optional[Dict[str, Any]]:
    event_id = event.get("id")
    if event_id is None:
        return None

    start = parse_utc(event.get("date"))
    end = parse_utc(event.get("endDate"))
    if start is None:
        return None

    name = event.get("name") or event.get("shortName") or ""
    short_name = event.get("shortName") or name
    core = get_json(core_event_url(str(event_id))) or {}

    circuit_info = fetch_circuit(core.get("circuit"))
    country = circuit_info.get("country", "")
    country_code = ""
    if not country:
        venue_refs = core.get("venues") or []
        if venue_refs:
            country, country_code = fetch_venue_country(venue_refs[0])

    sessions: List[Dict[str, Any]] = []
    for competition in event.get("competitions") or []:
        session_start = parse_utc(competition.get("date"))
        if session_start is None:
            session_start = start

        session_type = competition.get("type") or {}
        abbreviation = str(
            session_type.get("abbreviation")
            or session_type.get("name")
            or "Session"
        )
        kind, name_ru, name_en, is_sprint = session_info(abbreviation)

        sessions.append(
            {
                "id": str(competition.get("id") or ""),
                "type": kind,
                "type_name_ru": name_ru,
                "type_name_en": name_en,
                "is_sprint": is_sprint,
                "abbreviation": abbreviation,
                "start_utc": utc_iso(session_start),
            }
        )

    sessions.sort(key=lambda item: item["start_utc"])

    return {
        "id": str(event_id),
        "season": season,
        "name": name,
        "short_name": short_name,
        "circuit": circuit_info.get("circuit", ""),
        "city": circuit_info.get("city", ""),
        "country": country,
        "country_code": country_code,
        "start_utc": utc_iso(start),
        "end_utc": utc_iso(end) if end else "",
        "sessions": sessions,
    }


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the current Formula 1 season schedule from ESPN."
    )
    parser.add_argument(
        "--season",
        type=int,
        default=datetime.now(timezone.utc).year,
        help="Season year (default: current UTC year).",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output JSON file (default: f1.json).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Parallel event requests (default: 8).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    logging.info("Loading Formula 1 %s schedule", args.season)
    scoreboard = get_json(scoreboard_url(args.season))
    if not scoreboard:
        logging.error("Could not load Formula 1 scoreboard")
        return 1

    scoreboard_events = scoreboard.get("events") or []
    if not scoreboard_events:
        logging.error("Formula 1 season %s has no events", args.season)
        return 1

    events: List[Dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {
            executor.submit(build_event, event, args.season): event
            for event in scoreboard_events
        }

        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as error:
                logging.warning("Event parsing failed: %s", error)
                continue
            if result:
                events.append(result)

    events.sort(key=lambda event: event["start_utc"])
    for index, event in enumerate(events, start=1):
        event["round"] = index

    output = {
        "generated_at": utc_iso(datetime.now(timezone.utc)),
        "season": args.season,
        "timezone": "UTC",
        "source": "ESPN",
        "events": events,
    }

    write_json(Path(args.output), output)
    logging.info(
        "Wrote %s Formula 1 events to %s (requests: %s)",
        len(events),
        args.output,
        _request_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
