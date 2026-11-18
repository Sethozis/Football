#!/usr/bin/env python3
"""Collect upcoming digital movie releases from TMDB and OMDb.

The script keeps movies that:
  * have an exact digital release date
  * have an IMDb rating at or above MIN_IMDB_RATING
  * are released in the current/previous month or in the future window

TMDB is used for release dates, localized titles, genres and posters.
OMDb (by IMDb id from TMDB) is used only for the IMDb rating.

Required environment variables:
  TMDB_API_KEY   TMDB v3 API key or v4 read access token
  OMDB_API_KEY   OMDb API key

The generated movie_releases.json is intended to be committed by GitHub
Actions. No API key is ever shipped to the iOS app.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
OMDB_BASE = "https://www.omdbapi.com/"
DEFAULT_OUTPUT = "movie_releases.json"
DEFAULT_MIN_RATING = 7.0
DEFAULT_MONTHS_AHEAD = 12
DEFAULT_WORKERS = 4
HTTP_TIMEOUT = 25
RATING_CACHE_DAYS = 7
USER_AGENT = "month-calendar-movie-sync/1.0"

MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


@dataclass(frozen=True)
class ReleaseWindow:
    start: date
    end: date


@dataclass
class Candidate:
    tmdb_id: int
    detail: Dict[str, Any]
    digital_date: date
    digital_country: str
    title_ru: str
    title_en: str
    title_original: str
    genre_ids: List[int]
    genres_ru: List[str]
    genres_en: List[str]
    poster_url: Optional[str]
    imdb_id: str


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def request_json(
    url: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = HTTP_TIMEOUT,
) -> Optional[Dict[str, Any]]:
    request_headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Encoding": "identity",
    }
    if headers:
        request_headers.update(headers)

    for attempt in range(3):
        request = urllib.request.Request(url, headers=request_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code == 429:
                retry_after = error.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else 5.0 * (attempt + 1)
                logging.warning("Rate limit; sleeping %.1fs", delay)
                time.sleep(delay)
                continue
            if error.code in (401, 403):
                logging.error("Unauthorized request: %s", error)
                return None
            logging.warning("HTTP %s for %s", error.code, url)
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            logging.warning("Request failed for %s: %s", url, error)

        if attempt < 2:
            time.sleep(1.5 * (attempt + 1))

    return None


def tmdb_auth_params(api_key: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Support both TMDB v3 API keys and v4 bearer tokens."""
    if api_key.startswith("eyJ"):
        return {}, {"Authorization": f"Bearer {api_key}"}
    return {"api_key": api_key}, {}


def tmdb_get(
    path: str,
    params: Dict[str, str],
    api_key: str,
    timeout: int,
) -> Optional[Dict[str, Any]]:
    auth_params, headers = tmdb_auth_params(api_key)
    query = dict(params)
    query.update(auth_params)
    url = f"{TMDB_BASE}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    return request_json(url, headers=headers, timeout=timeout)


def omdb_get(imdb_id: str, api_key: str, timeout: int) -> Optional[Dict[str, Any]]:
    url = f"{OMDB_BASE}?" + urllib.parse.urlencode(
        {"i": imdb_id, "apikey": api_key}
    )
    return request_json(url, timeout=timeout)


def add_months(value: date, months: int) -> date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def last_day_of_month(value: date) -> date:
    next_month = add_months(value, 1)
    return next_month - timedelta(days=1)


def release_window(months_ahead: int) -> ReleaseWindow:
    today = date.today()
    current_month = today.replace(day=1)
    start = add_months(current_month, -1)
    end = last_day_of_month(add_months(current_month, months_ahead))
    return ReleaseWindow(start=start, end=end)


def parse_date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or not value:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})", value)
    if not match:
        return None
    try:
        return date(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )
    except ValueError:
        return None


def localized_title(
    detail: Dict[str, Any],
    language: str,
) -> Optional[str]:
    translations = detail.get("translations", {})
    if not isinstance(translations, dict):
        return None
    entries = translations.get("translations")
    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("iso_639_1") != language:
            continue
        data = entry.get("data")
        if not isinstance(data, dict):
            continue
        title = data.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def alternative_title(
    detail: Dict[str, Any],
    country_code: str,
) -> Optional[str]:
    alternative = detail.get("alternative_titles", {})
    if not isinstance(alternative, dict):
        return None
    entries = alternative.get("titles")
    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("iso_3166_1") != country_code:
            continue
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def choose_digital_release(
    detail: Dict[str, Any],
    window: ReleaseWindow,
) -> Optional[Tuple[date, str]]:
    release_dates = detail.get("release_dates", {})
    if not isinstance(release_dates, dict):
        return None
    results = release_dates.get("results")
    if not isinstance(results, list):
        return None

    best: Optional[Tuple[date, int, str]] = None
    country_priority = {
        "US": 0, "GB": 1, "CA": 2, "AU": 3, "NZ": 4,
        "IE": 5, "DE": 6, "FR": 7, "IT": 8, "ES": 9,
        "JP": 10, "RU": 11,
    }

    for country in results:
        if not isinstance(country, dict):
            continue
        country_code = str(country.get("iso_3166_1") or "")
        releases = country.get("release_dates")
        if not isinstance(releases, list):
            continue

        for release in releases:
            if not isinstance(release, dict):
                continue
            if release.get("type") != 4:
                continue
            released = parse_date(release.get("release_date"))
            if released is None:
                continue
            if released < window.start or released > window.end:
                continue

            priority = country_priority.get(country_code, 99)
            candidate = (released, priority, country_code)
            if best is None or candidate < best:
                best = candidate

    if best is None:
        return None
    return best[0], best[2]


def collect_candidates(
    window: ReleaseWindow,
    api_key: str,
    timeout: int,
    limit: Optional[int],
) -> List[int]:
    ids: List[int] = []
    seen = set()
    page = 1
    total_pages = 1

    while page <= total_pages and page <= 500:
        payload = tmdb_get(
            "/discover/movie",
            {
                "language": "en-US",
                "sort_by": "primary_release_date.asc",
                "with_release_type": "4",
                "release_date.gte": window.start.isoformat(),
                "release_date.lte": window.end.isoformat(),
                "include_adult": "false",
                "page": str(page),
            },
            api_key,
            timeout,
        )
        if payload is None:
            break

        total_pages = int(payload.get("total_pages") or 1)
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            break

        for result in results:
            if not isinstance(result, dict):
                continue
            movie_id = result.get("id")
            if isinstance(movie_id, int) and movie_id not in seen:
                seen.add(movie_id)
                ids.append(movie_id)
                if limit is not None and len(ids) >= limit:
                    return ids

        page += 1
        time.sleep(0.05)

    logging.info(
        "TMDB discover: %s candidates in %s..%s",
        len(ids),
        window.start,
        window.end,
    )
    return ids


def fetch_detail(
    movie_id: int,
    api_key: str,
    timeout: int,
) -> Optional[Dict[str, Any]]:
    return tmdb_get(
        f"/movie/{movie_id}",
        {
            "language": "en-US",
            "append_to_response": (
                "release_dates,translations,alternative_titles,external_ids"
            ),
        },
        api_key,
        timeout,
    )


def build_candidate(
    movie_id: int,
    detail: Dict[str, Any],
    window: ReleaseWindow,
    genre_names_ru: Dict[int, str],
    genre_names_en: Dict[int, str],
) -> Optional[Candidate]:
    imdb_id = str(detail.get("external_ids", {}).get("imdb_id") or "")
    if not imdb_id.startswith("tt"):
        return None

    digital = choose_digital_release(detail, window)
    if digital is None:
        return None

    genres = detail.get("genres")
    if not isinstance(genres, list):
        genres = []
    genre_ids: List[int] = []
    genres_ru: List[str] = []
    genres_en: List[str] = []
    for genre in genres:
        if not isinstance(genre, dict):
            continue
        genre_id = genre.get("id")
        if not isinstance(genre_id, int):
            continue
        genre_ids.append(genre_id)
        if genre_id in genre_names_ru:
            genres_ru.append(genre_names_ru[genre_id])
        if genre_id in genre_names_en:
            genres_en.append(genre_names_en[genre_id])

    title_en = (
        localized_title(detail, "en")
        or str(detail.get("title") or "").strip()
        or str(detail.get("original_title") or "").strip()
    )
    title_ru = (
        localized_title(detail, "ru")
        or alternative_title(detail, "RU")
        or ""
    )
    title_original = str(detail.get("original_title") or "").strip()

    poster_path = detail.get("poster_path")
    poster_url = (
        f"{TMDB_IMAGE_BASE}{poster_path}"
        if isinstance(poster_path, str) and poster_path
        else None
    )

    return Candidate(
        tmdb_id=movie_id,
        detail=detail,
        digital_date=digital[0],
        digital_country=digital[1],
        title_ru=title_ru or title_en,
        title_en=title_en or title_original,
        title_original=title_original or title_en,
        genre_ids=genre_ids,
        genres_ru=genres_ru,
        genres_en=genres_en,
        poster_url=poster_url,
        imdb_id=imdb_id,
    )


def load_genres(
    language: str,
    api_key: str,
    timeout: int,
) -> Dict[int, str]:
    payload = tmdb_get(
        "/genre/movie/list",
        {"language": language},
        api_key,
        timeout,
    )
    if not payload:
        return {}
    result: Dict[int, str] = {}
    for genre in payload.get("genres") or []:
        if isinstance(genre, dict):
            genre_id = genre.get("id")
            name = genre.get("name")
            if isinstance(genre_id, int) and isinstance(name, str):
                result[genre_id] = name
    return result


def load_rating_cache(path: Path) -> Dict[str, Tuple[float, str]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    cache: Dict[str, Tuple[float, str]] = {}
    for movie in payload.get("movies") or []:
        if not isinstance(movie, dict):
            continue
        imdb_id = movie.get("imdb_id")
        rating = movie.get("imdb_rating")
        checked_at = movie.get("rating_checked_at")
        if (
            isinstance(imdb_id, str)
            and isinstance(rating, (int, float))
            and isinstance(checked_at, str)
        ):
            cache[imdb_id] = (float(rating), checked_at)
    return cache


def cached_rating_is_fresh(checked_at: str) -> bool:
    try:
        checked = datetime.fromisoformat(
            checked_at.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if checked.tzinfo is None:
        checked = checked.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - checked <= timedelta(
        days=RATING_CACHE_DAYS
    )


def fetch_omdb_rating(
    imdb_id: str,
    api_key: str,
    timeout: int,
) -> Optional[Tuple[float, Optional[int]]]:
    payload = omdb_get(imdb_id, api_key, timeout)
    if not payload or payload.get("Response") == "False":
        return None
    raw_rating = payload.get("imdbRating")
    try:
        rating = float(raw_rating)
    except (TypeError, ValueError):
        return None
    votes: Optional[int] = None
    raw_votes = payload.get("imdbVotes")
    if isinstance(raw_votes, str):
        try:
            votes = int(raw_votes.replace(",", ""))
        except ValueError:
            votes = None
    return rating, votes


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect upcoming digital movie releases."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(DEFAULT_OUTPUT),
        help=f"output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--min-rating",
        type=float,
        default=float(os.environ.get("MIN_IMDB_RATING", DEFAULT_MIN_RATING)),
        help=f"minimum IMDb rating (default: {DEFAULT_MIN_RATING})",
    )
    parser.add_argument(
        "--months-ahead",
        type=int,
        default=DEFAULT_MONTHS_AHEAD,
        help=f"future window in months (default: {DEFAULT_MONTHS_AHEAD})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="limit candidate movies for a test run",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"parallel TMDB requests (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=HTTP_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {HTTP_TIMEOUT})",
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

    tmdb_key = os.environ.get("TMDB_API_KEY", "").strip()
    omdb_key = os.environ.get("OMDB_API_KEY", "").strip()
    if not tmdb_key:
        logging.error("TMDB_API_KEY is not set")
        return 2
    if not omdb_key:
        logging.error("OMDB_API_KEY is not set")
        return 2

    if not 0 <= args.min_rating <= 10:
        logging.error("Minimum rating must be between 0 and 10")
        return 2
    if args.months_ahead < 0:
        logging.error("Months ahead must be non-negative")
        return 2

    window = release_window(args.months_ahead)
    genres_ru = load_genres("ru-RU", tmdb_key, args.timeout)
    genres_en = load_genres("en-US", tmdb_key, args.timeout)
    candidate_ids = collect_candidates(
        window,
        tmdb_key,
        args.timeout,
        args.limit,
    )
    if not candidate_ids:
        logging.error("No candidate movies found")
        return 1

    candidates: List[Candidate] = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        futures = {
            pool.submit(fetch_detail, movie_id, tmdb_key, args.timeout):
                movie_id
            for movie_id in candidate_ids
        }
        for future in as_completed(futures):
            movie_id = futures[future]
            try:
                detail = future.result()
            except Exception:
                logging.exception("TMDB detail failed for %s", movie_id)
                continue
            if not detail:
                continue
            candidate = build_candidate(
                movie_id,
                detail,
                window,
                genres_ru,
                genres_en,
            )
            if candidate is not None:
                candidates.append(candidate)

    logging.info("%s candidates have a valid digital date", len(candidates))

    rating_cache = load_rating_cache(args.output)
    movies: List[Dict[str, Any]] = []
    checked_now = datetime.now(timezone.utc).replace(
        microsecond=0
    ).isoformat().replace("+00:00", "Z")
    omdb_requests = 0

    for candidate in sorted(
        candidates,
        key=lambda item: (item.digital_date, item.title_en),
    ):
        cached = rating_cache.get(candidate.imdb_id)
        rating: Optional[float] = None
        votes: Optional[int] = None
        checked_at: Optional[str] = None

        if cached and cached_rating_is_fresh(cached[1]):
            rating = cached[0]
            checked_at = cached[1]
        else:
            rating_payload = fetch_omdb_rating(
                candidate.imdb_id,
                omdb_key,
                args.timeout,
            )
            omdb_requests += 1
            if rating_payload is not None:
                rating, votes = rating_payload
                checked_at = checked_now
            elif cached is not None:
                rating = cached[0]
                checked_at = cached[1]
            time.sleep(0.05)

        if rating is None or rating < args.min_rating:
            continue

        movies.append(
            {
                "tmdb_id": candidate.tmdb_id,
                "imdb_id": candidate.imdb_id,
                "title_ru": candidate.title_ru,
                "title_en": candidate.title_en,
                "title_original": candidate.title_original,
                "genre_ids": candidate.genre_ids,
                "genres_ru": candidate.genres_ru,
                "genres_en": candidate.genres_en,
                "digital_release_date": candidate.digital_date.isoformat(),
                "digital_release_country": candidate.digital_country,
                "poster_url": candidate.poster_url,
                "imdb_rating": round(rating, 1),
                "imdb_votes": votes,
                "rating_checked_at": checked_at,
            }
        )

    payload = {
        "generated_at": checked_now,
        "minimum_imdb_rating": round(args.min_rating, 1),
        "window": {
            "from": window.start.isoformat(),
            "to": window.end.isoformat(),
        },
        "count": len(movies),
        "movies": movies,
    }
    write_json(args.output, payload)

    logging.info(
        "Wrote %s movies to %s (%s OMDb requests)",
        len(movies),
        args.output,
        omdb_requests,
    )
    return 0 if movies else 1


if __name__ == "__main__":
    sys.exit(main())
