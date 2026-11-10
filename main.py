#!/usr/bin/env python3
"""Fetch upcoming football fixtures for selected FIFA/UEFA and domestic competitions.

The script is intended to be run by GitHub Actions every six hours. It writes:

    football_matches.json  — upcoming matches, with UTC kickoff times
    team_codes.json        — cache of three-letter team codes

The API key is read from the API_FOOTBALL_KEY environment variable or from a
local file named API_FOOTBALL_KEY next to this script. Never commit that file.

API: https://www.api-football.com/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

import requests


API_BASE = "https://v3.football.api-sports.io"
API_KEY_FILE = "API_FOOTBALL_KEY"
DEFAULT_OUTPUT = "football_matches.json"
DEFAULT_TEAM_CACHE = "team_codes.json"
DEFAULT_DAYS = 365
MAX_TEAM_GROUPS_PER_RUN = 2
REQUEST_INTERVAL_SECONDS = 7.0

# The order is not important. Keep this list explicit so coverage is
# predictable and the number of API requests stays within the free tier.
#
# National teams:
#   FIFA World Cup, FIFA World Cup qualification, UEFA Euro,
#   UEFA Euro qualification, UEFA Nations League.
#
# Clubs:
#   UEFA Champions League, UEFA Europa League, UEFA Conference League.
#
# Domestic leagues and cup competitions:
#   England: Premier League, FA Cup, EFL Cup
#   Spain: La Liga, Copa del Rey
#   Italy: Serie A, Coppa Italia
#   Germany: Bundesliga, DFB-Pokal
#   France: Ligue 1, Coupe de France
#   Russia: Russian Premier League, Russian Cup
#
# One-off super cups / community shields are intentionally omitted. Add their
# league IDs here if they are needed later.
LEAGUES: Dict[int, str] = {
    1: "FIFA World Cup",
    32: "FIFA World Cup Qualifiers",
    4: "UEFA European Championship",
    960: "UEFA Euro Qualifiers",
    5: "UEFA Nations League",
    2: "UEFA Champions League",
    3: "UEFA Europa League",
    848: "UEFA Conference League",
    39: "Premier League",
    45: "FA Cup",
    48: "EFL Cup",
    140: "La Liga",
    143: "Copa del Rey",
    135: "Serie A",
    137: "Coppa Italia",
    78: "Bundesliga",
    81: "DFB-Pokal",
    61: "Ligue 1",
    66: "Coupe de France",
    235: "Russian Premier League",
    236: "Russian Cup",
}


def load_api_key() -> str:
    key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if key:
        return key

    key_path = Path(__file__).resolve().parent / API_KEY_FILE
    if key_path.is_file():
        key = key_path.read_text(encoding="utf-8").strip()
        if key:
            return key

    raise SystemExit(
        "API-Football key not found. Set API_FOOTBALL_KEY or create "
        f"{API_KEY_FILE!r} next to main.py."
    )


def load_json(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logging.warning("Could not read %s; using empty data", path)
        return default


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_kickoff(value: Any) -> Optional[datetime]:
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


def fallback_code(name: str) -> str:
    letters = re.sub(r"[^A-Za-zА-Яа-яЁё]", "", name).upper()
    if len(letters) >= 3:
        return letters[:3]
    return letters.ljust(3, "X")


class ApiClient:
    def __init__(self, key: str) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-apisports-key": key,
                "Accept": "application/json",
                "User-Agent": "month-calendar-football-sync/1.0",
            }
        )
        self.request_count = 0

    def get(self, path: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        url = f"{API_BASE}/{path.lstrip('/')}"
        last_error: Optional[BaseException] = None

        for attempt in range(3):
            self.request_count += 1
            try:
                response = self.session.get(url, params=params, timeout=30)
                if response.status_code == 429:
                    delay = 5 * (attempt + 1)
                    logging.warning("Rate limited; sleeping %ss", delay)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                last_error = error
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                logging.error("Request failed for %s %s: %s", path, params, error)
                return None
            finally:
                time.sleep(REQUEST_INTERVAL_SECONDS)

            errors = payload.get("errors")
            if errors:
                logging.warning("API warning for %s %s: %s", path, params, errors)
            return payload

        if last_error:
            logging.error("Request failed for %s %s: %s", path, params, last_error)
        return None


def request_all_fixtures_in_range(
    client: ApiClient,
    start_date: datetime,
    end_date: datetime,
) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
    """Try one global date-range request without league/season.

    Free API-Football plans reject ``next`` and require a season when a
    league is supplied. A global date-range query avoids both restrictions.
    """
    payload = client.get(
        "fixtures",
        {
            "from": start_date.date().isoformat(),
            "to": end_date.date().isoformat(),
            "timezone": "UTC",
        },
    )
    if payload is None:
        return None, False

    errors = payload.get("errors")
    if errors:
        logging.warning("Global fixtures request: %s", errors)
        return None, False

    response = payload.get("response")
    if isinstance(response, list):
        return response, True

    return None, False


def request_league_fixtures_in_range(
    client: ApiClient,
    league_id: int,
    season: int,
    start_date: datetime,
    end_date: datetime,
) -> Tuple[Optional[List[Dict[str, Any]]], bool]:
    """Fallback: load one league/season for the requested date range."""
    payload = client.get(
        "fixtures",
        {
            "league": league_id,
            "season": season,
            "from": start_date.date().isoformat(),
            "to": end_date.date().isoformat(),
            "timezone": "UTC",
        },
    )
    if payload is None:
        return None, False

    errors = payload.get("errors")
    if errors:
        logging.warning(
            "Fixtures request for league %s season %s: %s",
            league_id,
            season,
            errors,
        )
        return None, False

    response = payload.get("response")
    if isinstance(response, list):
        return response, True

    return None, False


def team_code_from_api(team: Dict[str, Any]) -> Optional[str]:
    code = team.get("code")
    if isinstance(code, str):
        code = code.strip().upper()
        if len(code) == 3:
            return code
    return None


def fetch_team_codes(
    client: ApiClient,
    groups: Dict[Tuple[int, int], Set[int]],
    cache: Dict[str, str],
    max_groups: int,
) -> None:
    fetched_groups = 0

    for (league_id, season), team_ids in sorted(groups.items()):
        if fetched_groups >= max_groups:
            break

        missing_ids = {
            team_id
            for team_id in team_ids
            if str(team_id) not in cache
        }
        if not missing_ids:
            continue

        logging.info(
            "Fetching team codes for league=%s season=%s (%s teams)",
            league_id,
            season,
            len(missing_ids),
        )
        payload = client.get(
            "teams",
            {"league": league_id, "season": season},
        )
        fetched_groups += 1

        if payload is None or payload.get("errors"):
            continue

        for row in payload.get("response") or []:
            team = row.get("team") or {}
            team_id = team.get("id")
            code = team_code_from_api(team)
            if team_id is None or code is None:
                continue
            cache[str(team_id)] = code


def build_matches(
    fixtures: Iterable[Dict[str, Any]],
    team_codes: Dict[str, str],
    now: datetime,
    horizon: datetime,
) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    seen: Set[int] = set()

    for row in fixtures:
        fixture = row.get("fixture") or {}
        league = row.get("league") or {}
        teams = row.get("teams") or {}
        home = teams.get("home") or {}
        away = teams.get("away") or {}

        fixture_id = fixture.get("id")
        kickoff = parse_kickoff(fixture.get("date"))
        if fixture_id is None or kickoff is None:
            continue
        if fixture_id in seen:
            continue
        if kickoff <= now or kickoff > horizon:
            continue

        league_id = league.get("id")
        league_name = league.get("name") or LEAGUES.get(league_id, "Unknown")
        country = league.get("country") or ""
        round_name = league.get("round") or ""
        season = league.get("season")

        home_id = home.get("id")
        away_id = away.get("id")
        home_name = home.get("name") or ""
        away_name = away.get("name") or ""
        home_code = (
            team_codes.get(str(home_id))
            or team_code_from_api(home)
            or fallback_code(home_name)
        )
        away_code = (
            team_codes.get(str(away_id))
            or team_code_from_api(away)
            or fallback_code(away_name)
        )

        seen.add(fixture_id)
        matches.append(
            {
                "fixture_id": fixture_id,
                "kickoff_utc": utc_iso(kickoff),
                "tournament": league_name,
                "country": country,
                "stage": round_name,
                "season": season,
                "league_id": league_id,
                "home_team_id": home_id,
                "home_team": home_name,
                "home_team_code": home_code,
                "away_team_id": away_id,
                "away_team": away_name,
                "away_team_code": away_code,
            }
        )

    matches.sort(
        key=lambda item: (
            item["kickoff_utc"],
            item["tournament"] or "",
            item["fixture_id"],
        )
    )
    return matches


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch upcoming football matches from API-Football."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=int(os.environ.get("FOOTBALL_UPCOMING_DAYS", DEFAULT_DAYS)),
        help="How many days ahead to include (default: 365).",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output JSON path.",
    )
    parser.add_argument(
        "--team-cache",
        default=DEFAULT_TEAM_CACHE,
        help="Three-letter team-code cache path.",
    )
    parser.add_argument(
        "--max-team-groups",
        type=int,
        default=MAX_TEAM_GROUPS_PER_RUN,
        help=(
            "How many league/season groups to use for team-code lookups "
            "per run. The rest receive a temporary fallback code."
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    api_key = load_api_key()
    output_path = Path(args.output)
    team_cache_path = Path(args.team_cache)

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=args.days)

    team_codes_raw = load_json(team_cache_path, {})
    if not isinstance(team_codes_raw, dict):
        team_codes_raw = {}
    team_codes = {
        str(key): str(value)
        for key, value in team_codes_raw.items()
        if isinstance(value, str) and value.strip()
    }

    client = ApiClient(api_key)
    all_fixtures: List[Dict[str, Any]] = []
    team_groups: Dict[Tuple[int, int], Set[int]] = {}
    successful_fixture_requests = 0

    logging.info(
        "Fetching all fixtures in %s..%s",
        now.date().isoformat(),
        horizon.date().isoformat(),
    )
    global_fixtures, global_succeeded = request_all_fixtures_in_range(
        client,
        now,
        horizon,
    )

    if global_succeeded and global_fixtures is not None:
        successful_fixture_requests = 1
        source_fixtures = [
            row
            for row in global_fixtures
            if (row.get("league") or {}).get("id") in LEAGUES
        ]
        logging.info(
            "Global response: %s fixtures, %s in selected leagues",
            len(global_fixtures),
            len(source_fixtures),
        )
    else:
        # Some plans require a season when a league is supplied. Query each
        # selected league for the current season as a fallback.
        logging.warning(
            "Global fixture request unavailable; falling back to leagues"
        )
        source_fixtures = []
        fallback_season = now.year

        for league_id, league_name in LEAGUES.items():
            logging.info(
                "Fetching league %s (%s), season %s",
                league_id,
                league_name,
                fallback_season,
            )
            fixtures, succeeded = request_league_fixtures_in_range(
                client,
                league_id,
                fallback_season,
                now,
                horizon,
            )
            if succeeded:
                successful_fixture_requests += 1
            if fixtures:
                source_fixtures.extend(fixtures)

    for row in source_fixtures:
        league = row.get("league") or {}
        league_id = league.get("id")
        season = league.get("season")
        fixture = row.get("fixture") or {}
        kickoff = parse_kickoff(fixture.get("date"))
        if kickoff is None or kickoff <= now or kickoff > horizon:
            continue
        if league_id not in LEAGUES:
            continue

        all_fixtures.append(row)

        if isinstance(season, int):
            teams = row.get("teams") or {}
            for side in ("home", "away"):
                team = teams.get(side) or {}
                team_id = team.get("id")
                if team_id is None:
                    continue
                if team_code_from_api(team) is not None:
                    continue
                if str(team_id) in team_codes:
                    continue
                team_groups.setdefault(
                    (league_id, season),
                    set(),
                ).add(int(team_id))

    if successful_fixture_requests == 0:
        logging.error("No successful API requests; keeping the previous JSON.")
        return 1

    fetch_team_codes(
        client=client,
        groups=team_groups,
        cache=team_codes,
        max_groups=max(0, args.max_team_groups),
    )

    matches = build_matches(all_fixtures, team_codes, now, horizon)

    output = {
        "generated_at": utc_iso(now),
        "timezone": "UTC",
        "source": "API-Football",
        "upcoming_days": args.days,
        "matches": matches,
    }

    write_json(output_path, output)
    write_json(team_cache_path, team_codes)

    logging.info(
        "Wrote %s matches to %s (requests: %s)",
        len(matches),
        output_path,
        client.request_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
