#!/usr/bin/env python3
"""Collect the current Formula 1 season schedule and standings from ESPN.

The schedule contains Grand Prix name, circuit/country and every practice,
qualifying, sprint and race session with its UTC start time. The standings
file contains the drivers' championship and constructors' championship with
localized Russian and English names and citizenship.
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
DEFAULT_STANDINGS_OUTPUT = "f1_standings.json"
DEFAULT_WORKERS = 8
HTTP_TIMEOUT = 20
USER_AGENT = "month-calendar-f1-sync/1.0"

DRIVER_NAMES_RU: Dict[str, tuple[str, str]] = {
    "Kimi Antonelli": ("Кими", "Антонелли"),
    "Max Verstappen": ("Макс", "Ферстаппен"),
    "George Russell": ("Джордж", "Расселл"),
    "Lewis Hamilton": ("Льюис", "Хэмилтон"),
    "Charles Leclerc": ("Шарль", "Леклер"),
    "Lando Norris": ("Ландо", "Норрис"),
    "Oscar Piastri": ("Оскар", "Пиастри"),
    "Isack Hadjar": ("Изак", "Аджар"),
    "Liam Lawson": ("Лиам", "Лоусон"),
    "Pierre Gasly": ("Пьер", "Гасли"),
    "Arvid Lindblad": ("Арвид", "Линдблад"),
    "Franco Colapinto": ("Франко", "Колапинто"),
    "Oliver Bearman": ("Оливер", "Берман"),
    "Gabriel Bortoleto": ("Габриэль", "Бортолето"),
    "Nico Hülkenberg": ("Нико", "Хюлькенберг"),
    "Esteban Ocon": ("Эстебан", "Окон"),
    "Fernando Alonso": ("Фернандо", "Алонсо"),
    "Carlos Sainz": ("Карлос", "Сайнс"),
    "Alexander Albon": ("Александер", "Албон"),
    "Yuki Tsunoda": ("Юки", "Цунода"),
    "Ryo Hirakawa": ("Рё", "Хиракава"),
    "Lance Stroll": ("Лэнс", "Стролл"),
    "Valtteri Bottas": ("Валттери", "Боттас"),
    "Sergio Pérez": ("Серхио", "Перес"),
    "Rafael Câmara": ("Рафаэл", "Камара")
}

TEAM_NAMES_RU: Dict[str, str] = {
    "Mercedes": "Мерседес",
    "Ferrari": "Феррари",
    "Red Bull": "Ред Булл",
    "McLaren": "Макларен",
    "Racing Bulls": "Рейсинг Буллз",
    "Alpine": "Альпин",
    "Haas": "Хаас",
    "Audi": "Ауди",
    "Williams": "Уильямс",
    "Aston Martin": "Астон Мартин",
    "Cadillac": "Кэдиллак"
}

TEAM_CODES: Dict[str, str] = {
    "Mercedes": "MER",
    "Ferrari": "FER",
    "Red Bull": "RBR",
    "McLaren": "MCL",
    "Racing Bulls": "VRB",
    "Alpine": "ALP",
    "Haas": "HAA",
    "Audi": "AUD",
    "Williams": "WIL",
    "Aston Martin": "AMR",
    "Cadillac": "CAD"
}

TEAM_COUNTRIES: Dict[str, tuple[str, str, str]] = {
    "Mercedes": ("Germany", "DEU", "Германия"),
    "Ferrari": ("Italy", "ITA", "Италия"),
    "Red Bull": ("Austria", "AUT", "Австрия"),
    "McLaren": ("Britain", "GBR", "Великобритания"),
    "Racing Bulls": ("Italy", "ITA", "Италия"),
    "Alpine": ("France", "FRA", "Франция"),
    "Haas": ("USA", "USA", "США"),
    "Audi": ("Germany", "DEU", "Германия"),
    "Williams": ("Britain", "GBR", "Великобритания"),
    "Aston Martin": ("Britain", "GBR", "Великобритания"),
    "Cadillac": ("USA", "USA", "США")
}

COUNTRY_NAMES_RU: Dict[str, str] = {
    "Italy": "Италия",
    "Netherlands": "Нидерланды",
    "Britain": "Великобритания",
    "United Kingdom": "Великобритания",
    "Monaco": "Монако",
    "Australia": "Австралия",
    "France": "Франция",
    "New Zealand": "Новая Зеландия",
    "Argentina": "Аргентина",
    "Brazil": "Бразилия",
    "Germany": "Германия",
    "Spain": "Испания",
    "Thailand": "Таиланд",
    "Japan": "Япония",
    "Canada": "Канада",
    "Finland": "Финляндия",
    "Mexico": "Мексика",
    "Austria": "Австрия",
    "USA": "США",
    "United States": "США",
    "Switzerland": "Швейцария"
}

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


def core_athlete_url(season: int, athlete_id: str) -> str:
    return (
        f"{ESPN_CORE}/racing/leagues/f1/seasons/{season}/athletes/"
        f"{urllib.parse.quote(athlete_id)}?lang=en&region=us"
    )


def fetch_driver_team_name(
    season: int,
    athlete_id: str
) -> str:
    payload = get_json(core_athlete_url(season, athlete_id))
    if not payload:
        return ""
    vehicles = payload.get("vehicles") or []
    for vehicle in vehicles:
        if isinstance(vehicle, dict):
            team = vehicle.get("team")
            if isinstance(team, str) and team.strip():
                return team.strip()
    return ""


def standings_url(season: int) -> str:
    return (
        "https://site.api.espn.com/apis/v2/sports/racing/f1/standings"
        f"?season={season}&lang=en&region=us"
    )


def stat_number(entry: Dict[str, Any], name: str) -> Optional[float]:
    for stat in entry.get("stats") or []:
        if stat.get("name") != name:
            continue
        value = stat.get("value")
        if isinstance(value, (int, float)):
            return float(value)
        display = stat.get("displayValue")
        if isinstance(display, str):
            try:
                return float(display.replace(",", "."))
            except ValueError:
                return None
    return None


def split_driver_name(value: str) -> tuple[str, str]:
    parts = value.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return " ".join(parts[:-1]), parts[-1]


def country_from_flag(flag: Any) -> tuple[str, str]:
    if not isinstance(flag, dict):
        return "", ""
    name = flag.get("alt") or ""
    code = country_code_from_flag(flag.get("href"))
    return name, code


def localized_country(name: str) -> str:
    return COUNTRY_NAMES_RU.get(name, name)


def parse_driver_standings(
    payload: Dict[str, Any]
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    children = payload.get("children") or []
    standings = None
    for child in children:
        if child.get("id") == "0" or child.get("abbreviation") == "Driver":
            standings = (child.get("standings") or {}).get("entries") or []
            break
    if standings is None and children:
        standings = (children[0].get("standings") or {}).get("entries") or []

    for entry in standings:
        athlete = entry.get("athlete") or {}
        full_name = (
            athlete.get("name")
            or athlete.get("displayName")
            or ""
        ).strip()
        first_name, last_name = split_driver_name(full_name)
        translated = DRIVER_NAMES_RU.get(full_name)
        if translated:
            first_ru, last_ru = translated
        else:
            first_ru, last_ru = first_name, last_name
            logging.warning(
                "No Russian driver name mapping for %s", full_name
            )

        country, country_code = country_from_flag(athlete.get("flag"))
        entries.append({
            "position": int(stat_number(entry, "rank") or 0),
            "athlete_id": str(athlete.get("id") or ""),
            "first_name": first_name,
            "last_name": last_name,
            "first_name_ru": first_ru,
            "last_name_ru": last_ru,
            "full_name": full_name,
            "full_name_ru": " ".join(
                part for part in (first_ru, last_ru) if part
            ),
            "country": country,
            "country_ru": localized_country(country),
            "country_code": country_code,
            "team_name": "",
            "team_code": "",
            "points": stat_number(entry, "championshipPts") or 0.0
        })

    entries.sort(key=lambda item: (
        item["position"] if item["position"] > 0 else 9999,
        -item["points"],
        item["last_name"]
    ))
    return entries


def parse_constructor_standings(
    payload: Dict[str, Any]
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    children = payload.get("children") or []
    standings = None
    for child in children:
        if child.get("id") == "1" or child.get("abbreviation") == "constructor":
            standings = (child.get("standings") or {}).get("entries") or []
            break
    if standings is None and len(children) > 1:
        standings = (children[1].get("standings") or {}).get("entries") or []

    for entry in standings:
        team = entry.get("team") or {}
        name = (team.get("name") or team.get("displayName") or "").strip()
        country, country_code, country_ru = TEAM_COUNTRIES.get(
            name,
            ("", "", "")
        )
        if not country:
            logging.warning(
                "No constructor country mapping for %s", name
            )

        entries.append({
            "position": int(stat_number(entry, "rank") or 0),
            "team_id": str(team.get("id") or ""),
            "team_name": name,
            "team_name_ru": TEAM_NAMES_RU.get(name, name),
            "country": country,
            "country_ru": country_ru or localized_country(country),
            "country_code": country_code,
            "points": stat_number(entry, "points") or 0.0
        })

    entries.sort(key=lambda item: (
        item["position"] if item["position"] > 0 else 9999,
        -item["points"],
        item["team_name"]
    ))
    return entries


def build_standings(season: int) -> Optional[Dict[str, Any]]:
    payload = get_json(standings_url(season))
    if not payload:
        return None

    drivers = parse_driver_standings(payload)
    for driver in drivers:
        athlete_id = driver.get("athlete_id", "")
        team_name = fetch_driver_team_name(season, athlete_id)
        driver["team_name"] = team_name
        driver["team_code"] = TEAM_CODES.get(team_name, "")
        if team_name and not driver["team_code"]:
            logging.warning(
                "No team code mapping for %s (%s)",
                team_name,
                athlete_id
            )

    return {
        "generated_at": utc_iso(datetime.now(timezone.utc)),
        "season": season,
        "source": "ESPN",
        "drivers": drivers,
        "constructors": parse_constructor_standings(payload)
    }


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
        "--standings-output",
        default=DEFAULT_STANDINGS_OUTPUT,
        help=(
            "Standings JSON file "
            f"(default: {DEFAULT_STANDINGS_OUTPUT})."
        ),
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
        "Wrote %s Formula 1 events to %s",
        len(events),
        args.output,
    )

    standings = build_standings(args.season)
    if standings is None:
        logging.error("Could not load Formula 1 standings")
        return 1

    write_json(Path(args.standings_output), standings)
    logging.info(
        "Wrote %s drivers and %s constructors to %s (requests: %s)",
        len(standings["drivers"]),
        len(standings["constructors"]),
        args.standings_output,
        _request_count,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
