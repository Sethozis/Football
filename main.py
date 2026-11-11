#!/usr/bin/env python3
"""Collect upcoming football fixtures from ESPN's public JSON endpoints.

No API key is required. GitHub Actions runs this script every six hours and
commits the generated football_matches.json.

Coverage:
  * FIFA World Cup and its UEFA qualifiers
  * UEFA European Championship and its qualifiers
  * UEFA Nations League
  * UEFA Champions League, Europa League and Conference League
  * Premier League + FA Cup + EFL Cup
  * La Liga + Copa del Rey
  * Serie A + Coppa Italia
  * Bundesliga + DFB-Pokal
  * Ligue 1 + Coupe de France
  * Russian Premier League

ESPN does not expose a Russian Cup competition through these endpoints, so
that competition is omitted for now.

All kickoff times are normalised to UTC. The app can later convert them to the
user's time zone.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports"
DEFAULT_OUTPUT = "football_matches.json"
DEFAULT_DAYS = 365
DEFAULT_WORKERS = 24
HTTP_TIMEOUT = 20
USER_AGENT = "month-calendar-football-sync/3.0"

# slug: (display name, country, national team competition)
LEAGUES: Dict[str, Tuple[str, str, bool]] = {
    "fifa.world": ("FIFA World Cup", "World", True),
    "fifa.worldq.uefa": ("FIFA World Cup Qualifiers", "Europe", True),
    "uefa.euro": ("UEFA European Championship", "Europe", True),
    "uefa.euroq": ("UEFA Euro Qualifiers", "Europe", True),
    "uefa.nations": ("UEFA Nations League", "Europe", True),
    "uefa.champions": ("UEFA Champions League", "Europe", False),
    "uefa.europa": ("UEFA Europa League", "Europe", False),
    "uefa.europa.conf": ("UEFA Conference League", "Europe", False),
    "eng.1": ("Premier League", "England", False),
    "eng.2": ("EFL Championship", "England", False),
    "eng.fa": ("FA Cup", "England", False),
    "eng.league_cup": ("EFL Cup", "England", False),
    "esp.1": ("La Liga", "Spain", False),
    "esp.2": ("LaLiga 2", "Spain", False),
    "esp.copa_del_rey": ("Copa del Rey", "Spain", False),
    "ita.1": ("Serie A", "Italy", False),
    "ita.2": ("Serie B", "Italy", False),
    "ita.coppa_italia": ("Coppa Italia", "Italy", False),
    "ger.1": ("Bundesliga", "Germany", False),
    "ger.2": ("2. Bundesliga", "Germany", False),
    "ger.dfb_pokal": ("DFB-Pokal", "Germany", False),
    "fra.1": ("Ligue 1", "France", False),
    "fra.2": ("Ligue 2", "France", False),
    "fra.coupe_de_france": ("Coupe de France", "France", False),
    "rus.1": ("Russian Premier League", "Russia", False),
    "ksa.1": ("Saudi Pro League", "Saudi Arabia", False),
    "ksa.kings.cup": ("Saudi King's Cup", "Saudi Arabia", False),
}

# Клубы для справочника берём только из матчей чемпионатов.
# Кубковые матчи (включая еврокубки) не должны добавлять клубы в этот список.
CLUB_LEAGUE_SLUGS: Set[str] = {
    "eng.1",
    "eng.2",
    "esp.1",
    "esp.2",
    "ita.1",
    "ita.2",
    "ger.1",
    "ger.2",
    "fra.1",
    "fra.2",
    "rus.1",
    "ksa.1",
}

_request_lock = threading.Lock()
_request_count = 0


def next_request_number() -> int:
    global _request_count
    with _request_lock:
        _request_count += 1
        return _request_count


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def get_json(url: str) -> Optional[Dict[str, Any]]:
    """Fetch JSON with retries. Returns None for 404/failures."""
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


def event_stage(event: Dict[str, Any], competition: Dict[str, Any]) -> str:
    for note in competition.get("notes") or []:
        headline = note.get("headline") or note.get("text")
        if headline:
            return str(headline)

    season_type = event.get("seasonType") or {}
    return str(
        season_type.get("name")
        or season_type.get("abbreviation")
        or ""
    )


def parse_event(
    summary: Dict[str, Any],
    slug: str,
    tournament: str,
    country: str,
    now: datetime,
    horizon: datetime,
) -> Optional[Dict[str, Any]]:
    header = summary.get("header") or {}
    competitions = header.get("competitions") or []
    if not competitions:
        return None
    competition = competitions[0]

    kickoff = parse_kickoff(competition.get("date") or header.get("date"))
    fixture_id = header.get("id") or competition.get("id")
    if kickoff is None or fixture_id is None:
        return None
    if kickoff <= now or kickoff > horizon:
        return None

    home_team: Optional[Dict[str, Any]] = None
    away_team: Optional[Dict[str, Any]] = None

    for competitor in competition.get("competitors") or []:
        team = competitor.get("team") or {}
        if competitor.get("homeAway") == "home":
            home_team = team
        elif competitor.get("homeAway") == "away":
            away_team = team

    if not home_team or not away_team:
        return None

    season = header.get("season") or {}
    league = header.get("league") or {}
    return {
        "fixture_id": str(fixture_id),
        "kickoff_utc": utc_iso(kickoff),
        "tournament": tournament,
        "country": country,
        "stage": season.get("name") or "",
        "season": season.get("year"),
        "league_id": league.get("id"),
        "league_slug": slug,
        "home_team_id": home_team.get("id"),
        "home_team": home_team.get("displayName") or home_team.get("name") or "",
        "home_team_code": team_code(home_team),
        "away_team_id": away_team.get("id"),
        "away_team": away_team.get("displayName") or away_team.get("name") or "",
        "away_team_code": team_code(away_team),
    }


def events_url(slug: str, start: datetime, end: datetime, page: int) -> str:
    dates = f"{start.strftime('%Y%m%d')}-{end.strftime('%Y%m%d')}"
    return (
        f"{ESPN_CORE}/soccer/leagues/{urllib.parse.quote(slug)}/events"
        f"?dates={dates}&limit=1000&page={page}"
    )


def load_event_references(
    slug: str,
    start: datetime,
    end: datetime,
) -> List[str]:
    references: List[str] = []
    page = 1

    while True:
        payload = get_json(events_url(slug, start, end, page))
        if not payload:
            break

        items = payload.get("items") or []
        for item in items:
            reference = item.get("$ref")
            if isinstance(reference, str) and reference:
                references.append(
                    reference.replace("http://", "https://", 1)
                )

        page_count = int(payload.get("pageCount") or 1)
        if page >= page_count:
            break
        page += 1

    return references


def load_event(slug: str, event_id: str) -> Optional[Dict[str, Any]]:
    url = (
        f"{ESPN_SITE}/soccer/{urllib.parse.quote(slug)}/summary"
        f"?event={urllib.parse.quote(event_id)}"
    )
    return get_json(url)


def short_team_code(team: Dict[str, Any]) -> str:
    raw = (
        team.get("abbreviation")
        or team.get("shortDisplayName")
        or team.get("displayName")
        or ""
    )
    letters = re.sub(r"[^A-Za-zА-Яа-яЁё0-9]", "", str(raw)).upper()
    return letters[:3].ljust(3, "X") if letters else "XXX"


def merge_team(
    directory: Dict[str, Dict[str, Any]],
    team: Dict[str, Any],
    country: str,
    is_national: bool,
) -> None:
    team_id = team.get("id")
    if team_id is None:
        return

    key = str(team_id)
    name = team.get("displayName") or team.get("name") or ""
    code = short_team_code(team)
    row = {
        "id": key,
        "name": name,
        "code": code,
        "country": country,
        "is_national": bool(is_national),
    }

    if key not in directory:
        directory[key] = row
        return

    existing = directory[key]
    if not existing.get("country") and country:
        existing["country"] = country
    if not existing.get("name") and name:
        existing["name"] = name
    if not existing.get("code") and code:
        existing["code"] = code
    existing["is_national"] = bool(existing.get("is_national")) or bool(is_national)


def collect_event_teams(
    event: Dict[str, Any],
    slug: str,
    directory: Dict[str, Dict[str, Any]],
    unknown_context: Dict[str, Tuple[str, int]],
) -> None:
    tournament, league_country, is_national = LEAGUES[slug]
    del tournament

    if not is_national and slug not in CLUB_LEAGUE_SLUGS:
        return

    header = event.get("header") or {}
    competitions = header.get("competitions") or []
    if not competitions:
        return
    competition = competitions[0]
    season = (header.get("season") or {}).get("year")
    venue = competition.get("venue") or {}
    venue_country = (venue.get("address") or {}).get("country") or ""

    for competitor in competition.get("competitors") or []:
        team = competitor.get("team") or {}
        if is_national:
            country = team.get("displayName") or team.get("name") or ""
        elif league_country not in ("Europe", "World"):
            country = league_country
        elif competitor.get("homeAway") == "home" and venue_country:
            country = venue_country
        else:
            country = ""

        merge_team(directory, team, country, is_national)

        team_id = team.get("id")
        if (
            not is_national
            and country == ""
            and team_id is not None
            and isinstance(season, int)
        ):
            unknown_context.setdefault(
                str(team_id),
                (slug, season),
            )


def load_team_country(slug: str, season: int, team_id: str) -> str:
    url = (
        f"{ESPN_CORE}/soccer/leagues/{urllib.parse.quote(slug)}"
        f"/seasons/{season}/teams/{urllib.parse.quote(team_id)}"
        "?lang=en&region=us"
    )
    payload = get_json(url)
    if not payload:
        return ""
    venue = payload.get("venue") or {}
    return (venue.get("address") or {}).get("country") or ""


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch upcoming football fixtures from ESPN."
    )
    parser.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help="How many days ahead to include (default: 365).",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help="Output JSON path.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Number of parallel ESPN requests (default: 24).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )

    now = datetime.now(timezone.utc)
    horizon = now + timedelta(days=args.days)
    output_path = Path(args.output)

    references: List[Tuple[str, str]] = []
    seen_events: Set[str] = set()

    for slug in LEAGUES:
        logging.info("Loading event list for %s", slug)
        refs = load_event_references(slug, now, horizon)
        logging.info("%s: %s event references", slug, len(refs))
        for reference in refs:
            marker = "/events/"
            if marker not in reference:
                continue
            event_id = reference.split(marker, 1)[1].split("?", 1)[0]
            if event_id in seen_events:
                continue
            seen_events.add(event_id)
            references.append((slug, event_id))

    logging.info("Fetching %s event details", len(references))

    matches: List[Dict[str, Any]] = []
    team_directory: Dict[str, Dict[str, Any]] = {}
    unknown_team_context: Dict[str, Tuple[str, int]] = {}
    successful_details = 0
    completed = 0
    workers = max(1, min(args.workers, 48))

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(load_event, slug, event_id): slug
            for slug, event_id in references
        }

        for future in as_completed(future_map):
            slug = future_map[future]
            completed += 1
            try:
                event = future.result()
            except Exception as error:  # keep other events usable
                logging.warning("Event request failed: %s", error)
                event = None

            if not event:
                continue
            successful_details += 1

            collect_event_teams(
                event,
                slug,
                team_directory,
                unknown_team_context,
            )

            tournament, country, _ = LEAGUES[slug]
            parsed = parse_event(event, slug, tournament, country, now, horizon)
            if parsed is not None:
                matches.append(parsed)

            if completed % 250 == 0:
                logging.info(
                    "Events: %s/%s, matches: %s",
                    completed,
                    len(references),
                    len(matches),
                )

    missing_team_ids = [
        team_id
        for team_id, row in team_directory.items()
        if not row.get("country") and team_id in unknown_team_context
    ]

    if missing_team_ids:
        logging.info(
            "Fetching countries for %s teams",
            len(missing_team_ids),
        )

        def resolve_country(team_id: str) -> Tuple[str, str]:
            slug, season = unknown_team_context[team_id]
            return team_id, load_team_country(slug, season, team_id)

        with ThreadPoolExecutor(max_workers=16) as executor:
            for team_id, country in executor.map(
                resolve_country,
                missing_team_ids,
            ):
                if country:
                    team_directory[team_id]["country"] = country

    if not matches and successful_details == 0:
        logging.error("No successful ESPN requests; keeping the previous JSON.")
        return 1

    matches.sort(
        key=lambda item: (
            item["kickoff_utc"],
            item["tournament"] or "",
            item["fixture_id"],
        )
    )

    output = {
        "generated_at": utc_iso(now),
        "timezone": "UTC",
        "source": "ESPN",
        "upcoming_days": args.days,
        "teams": sorted(
            team_directory.values(),
            key=lambda row: row.get("name") or "",
        ),
        "matches": matches,
    }

    write_json(output_path, output)
    logging.info(
        "Wrote %s matches to %s (requests: %s)",
        len(matches),
        output_path,
        _request_count,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
