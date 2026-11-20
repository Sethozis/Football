#!/usr/bin/env python3
"""Collect upcoming FIDE Candidates and World Championship games.

The script uses Wikipedia's public API as the machine-readable source.  It
searches for the current and next year's tournament pages, extracts the
published schedule and participant list, normalises local times to UTC and
writes chess_events.json.

No API key is required.  The GitHub workflow runs the script once a week.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from lxml import html as lxml_html

WIKI_API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "month-calendar-chess-sync/1.0"
HTTP_TIMEOUT = 25
DEFAULT_OUTPUT = "chess_events.json"

MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

COUNTRY_CODES: Dict[str, str] = {
    "United States": "USA",
    "Netherlands": "NED",
    "Germany": "GER",
    "Uzbekistan": "UZB",
    "China": "CHN",
    "FIDE": "FID",
    "India": "IND",
    "Russia": "RUS",
    "Norway": "NOR",
    "France": "FRA",
    "Armenia": "ARM",
    "Azerbaijan": "AZE",
    "Spain": "ESP",
    "Poland": "POL",
    "Hungary": "HUN",
    "Czech Republic": "CZE",
    "Ukraine": "UKR",
}

COUNTRY_NAMES_RU: Dict[str, str] = {
    "USA": "США",
    "NED": "Нидерланды",
    "GER": "Германия",
    "UZB": "Узбекистан",
    "CHN": "Китай",
    "FID": "FIDE",
    "IND": "Индия",
    "RUS": "Россия",
    "NOR": "Норвегия",
    "FRA": "Франция",
    "ARM": "Армения",
    "AZE": "Азербайджан",
    "ESP": "Испания",
    "POL": "Польша",
    "HUN": "Венгрия",
    "CZE": "Чехия",
    "UKR": "Украина",
}

# Curated Russian spellings for the current cycle.  For future participants
# the English name is kept as a safe fallback.
NAME_RU: Dict[str, Tuple[str, str, str, str]] = {
    "Hikaru Nakamura": ("Hikaru", "Nakamura", "Хикару", "Накамура"),
    "Fabiano Caruana": ("Fabiano", "Caruana", "Фабиано", "Каруана"),
    "Anish Giri": ("Anish", "Giri", "Аниш", "Гири"),
    "Praggnanandhaa R": (
        "Rameshbabu",
        "Praggnanandhaa",
        "Рамешнабху",
        "Прагнанандха",
    ),
    "Wei Yi": ("Wei", "Yi", "Вэй", "И"),
    "Javokhir Sindarov": (
        "Javokhir",
        "Sindarov",
        "Жавохир",
        "Синдаров",
    ),
    "Andrey Esipenko": (
        "Andrey",
        "Esipenko",
        "Андрей",
        "Есипенко",
    ),
    "Matthias Bluebaum": (
        "Matthias",
        "Bluebaum",
        "Маттиас",
        "Блюбаум",
    ),
    "Matthias Blübaum": (
        "Matthias",
        "Blübaum",
        "Маттиас",
        "Блюбаум",
    ),
    "R Praggnanandhaa": (
        "Rameshbabu",
        "Praggnanandhaa",
        "Рамешнабху",
        "Прагнанандха",
    ),
    "Gukesh Dommaraju": (
        "Gukesh",
        "Dommaraju",
        "Гукеш",
        "Доммараджу",
    ),
}


def get_json(url: str) -> Optional[Dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=HTTP_TIMEOUT,
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        logging.warning("Wikipedia request failed for %s: %s", url, error)
        return None


def page_url(title: str) -> str:
    query = urllib.parse.urlencode(
        {
            "action": "parse",
            "page": title,
            "prop": "text|wikitext",
            "format": "json",
            "formatversion": "2",
            "redirects": "1",
        }
    )
    return f"{WIKI_API}?{query}"


def fetch_page(title: str) -> Optional[Tuple[str, str, str]]:
    payload = get_json(page_url(title))
    if not payload or "parse" not in payload:
        return None
    parsed = payload["parse"]
    return (
        parsed.get("title", title),
        parsed.get("text", ""),
        parsed.get("wikitext", ""),
    )


def search_page(query: str, year: int) -> Optional[str]:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "formatversion": "2",
            "srlimit": "10",
        }
    )
    payload = get_json(f"{WIKI_API}?{params}")
    if not payload:
        return None
    results = payload.get("query", {}).get("search", [])
    for result in results:
        title = result.get("title", "")
        if str(year) not in title:
            continue
        lowered = title.lower()
        if "women" in lowered:
            continue
        return title
    return None


def text_of(element: Any) -> str:
    return " ".join("".join(element.itertext()).split())


def find_schedule_table(tree: Any) -> Optional[Any]:
    headings = tree.xpath('//*[@id="Schedule"]')
    if not headings:
        return None
    tables = headings[0].xpath("following::table[1]")
    return tables[0] if tables else None


def parse_date(value: str, year: int) -> Optional[date]:
    cleaned = re.sub(
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s*",
        "",
        value.strip(),
        flags=re.I,
    )
    match = re.match(
        r"(\d{1,2})\s+([A-Za-z]+)",
        cleaned,
    )
    if not match:
        return None
    day = int(match.group(1))
    month = MONTHS.get(match.group(2).lower())
    if month is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_time(value: str, default: str) -> str:
    match = re.search(
        r"(\d{1,2}:\d{2})\s*(?:EEST|EET|CEST|CET|UTC)",
        value,
        flags=re.I,
    )
    return match.group(1) if match else default


def parse_schedule(
    tree: Any,
    year: int,
    tournament: str,
    timezone_name: str,
    default_time: str,
) -> List[Dict[str, Any]]:
    table = find_schedule_table(tree)
    if table is None:
        return []

    page_text = text_of(tree)
    local_time = parse_time(page_text, default_time)
    hour, minute = [int(part) for part in local_time.split(":")]
    zone = ZoneInfo(timezone_name)
    games: List[Dict[str, Any]] = []

    for row in table.xpath(".//tr"):
        cells = [
            text_of(cell)
            for cell in row.xpath("./td|./th")
        ]
        if len(cells) < 2:
            continue
        game_date = parse_date(cells[0], year)
        event_name = cells[-1]
        if game_date is None:
            continue
        if not re.match(
            r"^(Round|Game|Tie[- ]?break)",
            event_name,
            flags=re.I,
        ):
            continue

        # "Tie-breaks (If required), Closing ceremony" -> short stage.
        stage = event_name.split(",")[0].strip()
        required = "if required" in event_name.lower()
        is_tiebreak = stage.lower().startswith("tie")
        time_is_tbd = (
            is_tiebreak
            and tournament == "world_championship"
            and "time is yet to be decided" in page_text.lower()
        )
        if time_is_tbd:
            game_time: Optional[str] = None
            start_utc: Optional[str] = None
        else:
            local_dt = datetime.combine(
                game_date,
                time(hour, minute),
                tzinfo=zone,
            )
            utc_dt = local_dt.astimezone(timezone.utc)
            game_time = local_time
            start_utc = utc_dt.isoformat().replace("+00:00", "Z")

        slug = re.sub(r"[^a-z0-9]+", "-", stage.lower()).strip("-")
        games.append(
            {
                "id": f"{tournament}-{year}-{slug}",
                "tournament": tournament,
                "stage": stage,
                "if_required": required,
                "date": game_date.isoformat(),
                "time": game_time,
                "timezone": timezone_name,
                "start_utc": start_utc,
            }
        )
    return games


def country_for_name(name: str) -> str:
    return COUNTRY_NAMES_RU.get(name, name)


def participant_record(
    display_name: str,
    country_code: str,
) -> Dict[str, Any]:
    localized = NAME_RU.get(display_name)
    if localized:
        first_en, last_en, first_ru, last_ru = localized
    else:
        parts = display_name.split()
        if len(parts) > 1:
            first_en = " ".join(parts[:-1])
            last_en = parts[-1]
        else:
            first_en = display_name
            last_en = ""
        first_ru = first_en
        last_ru = last_en

    country_code = country_code.upper()
    country_en = next(
        (
            name
            for name, code in COUNTRY_CODES.items()
            if code == country_code
        ),
        country_code,
    )
    return {
        "name_en": display_name,
        "name_ru": " ".join(
            part for part in (first_ru, last_ru) if part
        ),
        "first_name_en": first_en,
        "last_name_en": last_en,
        "first_name_ru": first_ru,
        "last_name_ru": last_ru,
        "country_code": country_code,
        "country_en": country_en,
        "country_ru": country_for_name(country_code),
    }


def candidates_participants(tree: Any) -> List[Dict[str, Any]]:
    for table in tree.xpath("//table"):
        rows = table.xpath(".//tr")
        if not rows:
            continue
        header = [text_of(cell) for cell in rows[0].xpath("./th|./td")]
        if not any("Qualification" in cell for cell in header):
            continue

        result: List[Dict[str, Any]] = []
        seen = set()
        for row in rows[1:]:
            anchors = [
                text_of(anchor)
                for anchor in row.xpath(".//a")
            ]
            anchors = [
                value
                for value in anchors
                if value
                and not value.startswith("[")
                and value.lower() not in {
                    "winner",
                    "runner-up",
                }
            ]
            if not anchors:
                continue
            display_name = anchors[-1]
            if display_name in seen:
                continue
            flags = [
                image.get("alt", "")
                for image in row.xpath(".//img")
                if image.get("alt")
            ]
            country_name = flags[0] if flags else ""
            country_code = COUNTRY_CODES.get(country_name, "")
            if not country_code:
                continue
            seen.add(display_name)
            result.append(participant_record(display_name, country_code))
        if result:
            return result
    return []


def world_participants(title: str, wikitext: str) -> List[Dict[str, Any]]:
    def infobox_value(field: str) -> str:
        match = re.search(
            rf"\|\s*{field}\s*=\s*([^\n|]+)",
            wikitext,
        )
        return match.group(1).strip() if match else ""

    def clean(value: str) -> str:
        value = re.sub(r"\[\[[^]|]+\|([^]]+)\]\]", r"\1", value)
        value = re.sub(r"\[\[([^]]+)\]\]", r"\1", value)
        return value.strip()

    players: List[Dict[str, Any]] = []
    for name_field, flag_field in (
        ("comp1", "flag1"),
        ("comp2", "flag2"),
    ):
        name = clean(infobox_value(name_field))
        code = clean(infobox_value(flag_field)).upper()
        if name and code:
            players.append(participant_record(name, code))
    return players


def tournament_payload(
    year: int,
    tournament: str,
    page_title: str,
    tree: Any,
    wikitext: str,
    timezone_name: str,
    default_time: str,
    participants: List[Dict[str, Any]],
    include_past: bool,
) -> Optional[Dict[str, Any]]:
    games = parse_schedule(
        tree,
        year,
        tournament,
        timezone_name,
        default_time,
    )
    if not games:
        return None

    window_start = min(game["date"] for game in games)
    window_end = max(game["date"] for game in games)

    now = datetime.now(timezone.utc)
    if not include_past:
        filtered: List[Dict[str, Any]] = []
        for game in games:
            if game["start_utc"] is None:
                if date.fromisoformat(game["date"]) >= date.today():
                    filtered.append(game)
            elif datetime.fromisoformat(
                game["start_utc"].replace("Z", "+00:00")
            ) >= now:
                filtered.append(game)
        games = filtered
    return {
        "id": f"{tournament}-{year}",
        "tournament": tournament,
        "name_en": (
            "FIDE Candidates Tournament"
            if tournament == "candidates"
            else "World Chess Championship Match"
        ),
        "name_ru": (
            "Турнир претендентов"
            if tournament == "candidates"
            else "Матч за звание чемпиона мира"
        ),
        "year": year,
        "source": f"https://en.wikipedia.org/wiki/{page_title.replace(' ', '_')}",
        "timezone": timezone_name,
        "window_start": window_start,
        "window_end": window_end,
        "participants": participants,
        "games": games,
    }


def collect_year(year: int, include_past: bool) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []

    candidates_title = search_page(
        f"Candidates Tournament {year}",
        year,
    )
    if candidates_title:
        page = fetch_page(candidates_title)
        if page:
            title, html_text, wikitext = page
            tree = lxml_html.fromstring(html_text)
            participants = candidates_participants(tree)
            event = tournament_payload(
                year,
                "candidates",
                title,
                tree,
                wikitext,
                "Asia/Nicosia",
                "15:30",
                participants,
                include_past,
            )
            if event:
                events.append(event)
            logging.info(
                "Candidates %s: %s participants, %s games",
                year,
                len(participants),
                len(event["games"]) if event else 0,
            )
    else:
        logging.info("No Wikipedia Candidates page for %s", year)

    world_title = f"World Chess Championship {year}"
    page = fetch_page(world_title)
    if page:
        title, html_text, wikitext = page
        tree = lxml_html.fromstring(html_text)
        participants = world_participants(title, wikitext)
        event = tournament_payload(
            year,
            "world_championship",
            title,
            tree,
            wikitext,
            "Europe/Zurich",
            "15:00",
            participants,
            include_past,
        )
        if event:
            events.append(event)
        logging.info(
            "World Championship %s: %s participants, %s games",
            year,
            len(participants),
            len(event["games"]) if event else 0,
        )
    else:
        logging.info("No Wikipedia World Championship page for %s", year)

    return events


def schedule_policy_allows_update(path: Path) -> bool:
    if not path.exists():
        return True

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True

    today = datetime.now(timezone.utc).date()
    windows: List[Tuple[date, date]] = []
    for window in payload.get("schedule_windows", []):
        start = window.get("start")
        end = window.get("end")
        if isinstance(start, str) and isinstance(end, str):
            windows.append(
                (date.fromisoformat(start), date.fromisoformat(end))
            )
    for event in payload.get("events", []):
        start = event.get("window_start")
        end = event.get("window_end")
        if isinstance(start, str) and isinstance(end, str):
            windows.append(
                (date.fromisoformat(start), date.fromisoformat(end))
            )

    if not windows:
        return True

    for start, end in windows:
        if (
            start - timedelta(days=1)
            <= today
            <= end + timedelta(days=1)
        ):
            return True
    return today.weekday() == 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect FIDE Candidates and World Championship schedules."
    )
    parser.add_argument(
        "--year",
        type=int,
        default=datetime.now(timezone.utc).year,
        help="First year to fetch (default: current UTC year).",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=2,
        help="Number of seasons to inspect (default: 2).",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Output JSON (default: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--include-past",
        action="store_true",
        help="Include games that have already started.",
    )
    parser.add_argument(
        "--schedule-policy",
        action="store_true",
        help=(
            "Run daily around tournaments and once a week otherwise, "
            "using the existing output as the date window."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the schedule policy and update now.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    output_path = Path(args.output)
    if (
        args.schedule_policy
        and not args.force
        and not schedule_policy_allows_update(output_path)
    ):
        logging.info(
            "Skipping: outside the daily tournament window and not Monday"
        )
        return 0

    all_events: List[Dict[str, Any]] = []
    for year in range(args.year, args.year + max(1, args.years)):
        all_events.extend(collect_year(year, args.include_past))

    schedule_windows = [
        {
            "tournament": event["tournament"],
            "start": event["window_start"],
            "end": event["window_end"],
        }
        for event in all_events
    ]
    events = [event for event in all_events if event["games"]]

    if not events and not schedule_windows and output_path.exists():
        logging.error(
            "No schedule data received; keeping existing %s",
            output_path,
        )
        return 0

    output = {
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "source": "Wikipedia / FIDE",
        "schedule_windows": schedule_windows,
        "events": events,
    }
    output_path.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logging.info(
        "Wrote %s tournaments and %s games to %s",
        len(events),
        sum(len(event["games"]) for event in events),
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
