#!/usr/bin/env python3
"""Collect current-season football league tables from ESPN's public API.

No API key is required. The script writes football_standings.json, which the
iOS app can fetch from GitHub in the same way as football_matches.json.

Coverage is limited to competitions that actually expose a table through the
standings endpoint. Cup competitions and most national-team tournaments are
omitted because ESPN does not provide a league table for them.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

ESPN_STANDINGS = "https://site.api.espn.com/apis/v2/sports/soccer"
DEFAULT_OUTPUT = "football_standings.json"
HTTP_TIMEOUT = 20
USER_AGENT = "month-calendar-standings-sync/1.0"


@dataclass(frozen=True)
class League:
    slug: str
    name: str
    country: str


# Domestic championships and European league-phase tables.
LEAGUES: Tuple[League, ...] = (
    League("eng.1", "Premier League", "England"),
    League("eng.2", "EFL Championship", "England"),
    League("esp.1", "La Liga", "Spain"),
    League("esp.2", "LaLiga 2", "Spain"),
    League("ita.1", "Serie A", "Italy"),
    League("ita.2", "Serie B", "Italy"),
    League("ger.1", "Bundesliga", "Germany"),
    League("ger.2", "2. Bundesliga", "Germany"),
    League("fra.1", "Ligue 1", "France"),
    League("fra.2", "Ligue 2", "France"),
    League("rus.1", "Russian Premier League", "Russia"),
    League("ksa.1", "Saudi Pro League", "Saudi Arabia"),
    League("uefa.champions", "UEFA Champions League", "Europe"),
    League("uefa.europa", "UEFA Europa League", "Europe"),
    League("uefa.europa.conf", "UEFA Conference League", "Europe"),
)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def get_json(url: str, timeout: int) -> Optional[Dict[str, Any]]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }

    for attempt in range(3):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code == 429:
                delay = 5 * (attempt + 1)
                logging.warning("ESPN rate limit for %s; sleeping %ss", url, delay)
                time.sleep(delay)
                continue
            logging.warning("ESPN HTTP %s for %s", error.code, url)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            logging.warning("ESPN request failed for %s: %s", url, error)

        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))

    return None


def numeric_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("+", ""))
    except ValueError:
        return None


def int_value(value: Any) -> int:
    number = numeric_value(value)
    if number is None:
        return 0
    return int(round(number))


def stat_value(stats: Iterable[Dict[str, Any]], name: str) -> Any:
    for stat in stats:
        if stat.get("name") != name:
            continue
        value = stat.get("value")
        if value is not None:
            return value
        display = stat.get("displayValue")
        if display is not None:
            return display
    return None


def team_code(team: Dict[str, Any]) -> str:
    raw = (
        team.get("abbreviation")
        or team.get("shortDisplayName")
        or team.get("displayName")
        or ""
    )
    letters = re.sub(r"[^A-Za-zА-Яа-яЁё0-9]", "", str(raw)).upper()
    if not letters:
        return "XXX"
    return letters[:3].ljust(3, "X")


def team_logo(team: Dict[str, Any]) -> Optional[str]:
    logos = team.get("logos") or []
    if not isinstance(logos, list):
        return None
    for logo in logos:
        href = logo.get("href") if isinstance(logo, dict) else None
        if href:
            return href
    return None


def entry_row(
    entry: Dict[str, Any],
    index: int,
) -> Optional[Dict[str, Any]]:
    team = entry.get("team")
    if not isinstance(team, dict):
        return None

    team_id = team.get("id")
    if team_id is None:
        return None

    stats = entry.get("stats") or []
    if not isinstance(stats, list):
        stats = []

    note = entry.get("note") or {}
    rank = int_value(note.get("rank")) if isinstance(note, dict) else 0
    if rank <= 0:
        rank = index + 1

    games_played = int_value(stat_value(stats, "gamesPlayed"))
    goals_for = int_value(stat_value(stats, "pointsFor"))
    goals_against = int_value(stat_value(stats, "pointsAgainst"))
    points = int_value(stat_value(stats, "points"))
    goal_difference = stat_value(stats, "pointDifferential")
    if goal_difference is None:
        goal_difference = goals_for - goals_against

    return {
        "rank": rank,
        "team_id": str(team_id),
        "team_name": (
            team.get("displayName")
            or team.get("shortDisplayName")
            or team.get("name")
            or ""
        ),
        "team_code": team_code(team),
        "team_logo": team_logo(team),
        "played": games_played,
        "wins": int_value(stat_value(stats, "wins")),
        "draws": int_value(stat_value(stats, "ties")),
        "losses": int_value(stat_value(stats, "losses")),
        "goals_for": goals_for,
        "goals_against": goals_against,
        "goal_difference": int_value(goal_difference),
        "points": points,
        "note": note.get("description") if isinstance(note, dict) else None,
    }


def extract_groups(payload: Dict[str, Any]) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Return (group name, entries) pairs from ESPN's standings payload."""
    groups: List[Tuple[str, List[Dict[str, Any]]]] = []

    def add_group(name: Any, standings: Any) -> None:
        if not isinstance(standings, dict):
            return
        entries = standings.get("entries")
        if isinstance(entries, list) and entries:
            groups.append((str(name or ""), entries))

    children = payload.get("children")
    if isinstance(children, list) and children:
        for child in children:
            if not isinstance(child, dict):
                continue
            name = (
                child.get("name")
                or child.get("abbreviation")
                or child.get("shortName")
                or ""
            )
            add_group(name, child.get("standings"))

            nested = child.get("children")
            if isinstance(nested, list):
                for subchild in nested:
                    if not isinstance(subchild, dict):
                        continue
                    subname = (
                        subchild.get("name")
                        or subchild.get("abbreviation")
                        or name
                    )
                    add_group(subname, subchild.get("standings"))

    if not groups:
        add_group("", payload.get("standings"))

    return groups


def parse_league(
    league: League,
    season: Optional[int],
    timeout: int,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    params: Dict[str, str] = {}
    if season is not None:
        params["season"] = str(season)

    url = f"{ESPN_STANDINGS}/{league.slug}/standings"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    payload = get_json(url, timeout)
    if payload is None:
        return [], "standings request failed"

    groups = extract_groups(payload)
    if not groups:
        return [], "standings table is empty"

    season_data = payload.get("season") or {}
    season_year = season_data.get("year") if isinstance(season_data, dict) else None
    season_display = (
        season_data.get("displayName") if isinstance(season_data, dict) else None
    )
    multiple_groups = len(groups) > 1
    tables: List[Dict[str, Any]] = []

    for group_name, entries in groups:
        rows: List[Dict[str, Any]] = []
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            row = entry_row(entry, index)
            if row is not None:
                rows.append(row)

        if not rows:
            continue

        rows.sort(key=lambda row: (row["rank"], row["team_name"]))
        table: Dict[str, Any] = {
            "league_id": league.slug,
            "league_slug": league.slug,
            "league_name": league.name,
            "country": league.country,
            "season_year": season_year,
            "season_display": season_display,
            "rows": rows,
        }
        if multiple_groups and group_name:
            table["group"] = group_name
        tables.append(table)

    if not tables:
        return [], "no usable rows"

    return tables, None


def collect(
    leagues: Iterable[League],
    season: Optional[int],
    timeout: int,
    workers: int,
) -> Dict[str, Any]:
    league_list = list(leagues)
    results: Dict[str, Tuple[List[Dict[str, Any]], Optional[str]]] = {}

    with ThreadPoolExecutor(max_workers=max(1, min(workers, 8))) as executor:
        futures = {
            executor.submit(parse_league, league, season, timeout): league
            for league in league_list
        }
        for future in as_completed(futures):
            league = futures[future]
            try:
                results[league.slug] = future.result()
            except Exception as error:  # pragma: no cover - defensive
                logging.exception("Unexpected error for %s", league.slug)
                results[league.slug] = ([], str(error))

    tables: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    for league in league_list:
        league_tables, error = results.get(
            league.slug,
            ([], "not collected"),
        )
        tables.extend(league_tables)
        if error:
            errors.append(
                {
                    "league_id": league.slug,
                    "league_name": league.name,
                    "error": error,
                }
            )

    return {
        "generated_at": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "tables": tables,
        "errors": errors,
    }


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect football standings from ESPN."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT),
        help=f"output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--league",
        action="append",
        dest="leagues",
        metavar="SLUG",
        help="limit to a league slug; may be repeated",
    )
    parser.add_argument(
        "--season",
        type=int,
        default=None,
        help="ESPN season year; by default the current season is used",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=HTTP_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {HTTP_TIMEOUT})",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="parallel league requests (default: 4)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    requested = set(args.leagues or [])
    leagues = [
        league for league in LEAGUES
        if not requested or league.slug in requested
    ]
    if requested:
        known = {league.slug for league in LEAGUES}
        unknown = sorted(requested - known)
        if unknown:
            logging.warning("Unknown league slugs: %s", ", ".join(unknown))

    if not leagues:
        logging.error("No leagues selected")
        return 2

    payload = collect(
        leagues,
        season=args.season,
        timeout=max(1, args.timeout),
        workers=args.workers,
    )
    write_json(args.output, payload)

    for error in payload["errors"]:
        logging.warning(
            "%s: %s",
            error["league_name"],
            error["error"],
        )

    table_count = sum(len(table["rows"]) for table in payload["tables"])
    logging.info(
        "Wrote %s tables (%s rows) to %s",
        len(payload["tables"]),
        table_count,
        args.output,
    )

    return 0 if payload["tables"] else 1


if __name__ == "__main__":
    sys.exit(main())
