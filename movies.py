#!/usr/bin/env python3
"""Collect digital movie releases from Trakt, enriched with TMDB and OMDb.

Trakt supplies digital release dates across countries. TMDB supplies Russian
and English titles, genres and posters. OMDb supplies the IMDb rating.

Only movies released digitally in the current/previous month or in the future
window are kept. Old catalogue re-releases are filtered by the movie year.

Required environment variables:
  TRAKT_CLIENT_ID  Trakt application Client ID
  TMDB_API_KEY     TMDB v3 API key or v4 read access token
  OMDB_API_KEY     OMDb API key

The generated movie_releases.json is committed by GitHub Actions.
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

TRAKT_BASE = "https://api.trakt.tv"
TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
OMDB_BASE = "https://www.omdbapi.com/"
DEFAULT_OUTPUT = "movie_releases.json"
DEFAULT_MIN_RATING = 7.0
DEFAULT_MONTHS_AHEAD = 12
DEFAULT_WORKERS = 4
HTTP_TIMEOUT = 25
TRAKT_CHUNK_DAYS = 30
RATING_CACHE_DAYS = 7
USER_AGENT = "month-calendar-movie-sync/2.0"


@dataclass(frozen=True)
class ReleaseWindow:
    start: date
    end: date


@dataclass
class TraktMovie:
    trakt_id: int
    tmdb_id: Optional[int]
    imdb_id: str
    title: str
    year: Optional[int]


@dataclass
class Candidate:
    tmdb_id: int
    imdb_id: str
    title_ru: str
    title_en: str
    title_original: str
    overview_ru: str
    overview_en: str
    year: Optional[int]
    genre_ids: List[int]
    genres_ru: List[str]
    genres_en: List[str]
    digital_date: date
    digital_country: str
    poster_url: Optional[str]


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
) -> Optional[Any]:
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
                return json.loads(response.read().decode("utf-8"))
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


def tmdb_auth(api_key: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    if api_key.startswith("eyJ"):
        return {}, {"Authorization": f"Bearer {api_key}"}
    return {"api_key": api_key}, {}


def tmdb_get(
    path: str,
    params: Dict[str, str],
    api_key: str,
    timeout: int,
) -> Optional[Dict[str, Any]]:
    auth_params, headers = tmdb_auth(api_key)
    query = dict(params)
    query.update(auth_params)
    url = f"{TMDB_BASE}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    payload = request_json(url, headers=headers, timeout=timeout)
    return payload if isinstance(payload, dict) else None


def trakt_get(
    path: str,
    client_id: str,
    timeout: int,
) -> Optional[Any]:
    url = f"{TRAKT_BASE}{path}"
    return request_json(
        url,
        headers={
            "trakt-api-version": "2",
            "trakt-api-key": client_id,
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )


def omdb_get(imdb_id: str, api_key: str, timeout: int) -> Optional[Dict[str, Any]]:
    url = f"{OMDB_BASE}?" + urllib.parse.urlencode(
        {"i": imdb_id, "apikey": api_key}
    )
    payload = request_json(url, timeout=timeout)
    return payload if isinstance(payload, dict) else None


def add_months(value: date, months: int) -> date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    return date(year, month, 1)


def last_day_of_month(value: date) -> date:
    return add_months(value, 1) - timedelta(days=1)


def release_window(months_ahead: int) -> ReleaseWindow:
    current = date.today().replace(day=1)
    return ReleaseWindow(
        start=add_months(current, -1),
        end=last_day_of_month(add_months(current, months_ahead)),
    )


def parse_date(value: Any) -> Optional[date]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def integer_year(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def trakt_movie_from_item(item: Dict[str, Any]) -> Optional[TraktMovie]:
    movie = item.get("movie")
    if not isinstance(movie, dict):
        movie = item
    ids = movie.get("ids")
    if not isinstance(ids, dict):
        return None

    trakt_id = ids.get("trakt")
    if not isinstance(trakt_id, int):
        return None

    tmdb_id = ids.get("tmdb")
    if not isinstance(tmdb_id, int):
        tmdb_id = None

    imdb_id = ids.get("imdb")
    if not isinstance(imdb_id, str):
        imdb_id = ""

    title = movie.get("title")
    if not isinstance(title, str):
        title = ""

    return TraktMovie(
        trakt_id=trakt_id,
        tmdb_id=tmdb_id,
        imdb_id=imdb_id if imdb_id.startswith("tt") else "",
        title=title,
        year=integer_year(movie.get("year")),
    )


def fetch_trakt_calendar(
    kind: str,
    window: ReleaseWindow,
    client_id: str,
    timeout: int,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    cursor = window.start
    remaining = (window.end - window.start).days + 1

    while remaining > 0:
        chunk = min(TRAKT_CHUNK_DAYS, remaining)
        payload = trakt_get(
            f"/calendars/all/{kind}/{cursor.isoformat()}/{chunk}",
            client_id,
            timeout,
        )
        if isinstance(payload, list):
            rows.extend(item for item in payload if isinstance(item, dict))
        logging.info(
            "Trakt %s %s..%s: %s items",
            kind,
            cursor,
            cursor + timedelta(days=chunk - 1),
            len(payload) if isinstance(payload, list) else 0,
        )
        cursor += timedelta(days=chunk)
        remaining -= chunk
        time.sleep(0.12)

    return rows


def collect_trakt_movies(
    window: ReleaseWindow,
    client_id: str,
    timeout: int,
    min_year: int,
    limit: Optional[int],
) -> List[TraktMovie]:
    rows = fetch_trakt_calendar(
        "streaming", window, client_id, timeout
    ) + fetch_trakt_calendar(
        "movies", window, client_id, timeout
    )

    movies: Dict[int, TraktMovie] = {}
    for item in rows:
        movie = trakt_movie_from_item(item)
        if movie is None:
            continue
        if movie.year is not None and movie.year < min_year:
            continue
        if not movie.imdb_id and movie.tmdb_id is None:
            continue
        movies[movie.trakt_id] = movie

    result = list(movies.values())
    result.sort(
        key=lambda item: (
            item.year or 0,
            item.title.lower(),
        )
    )
    if limit is not None:
        result = result[:limit]

    logging.info(
        "Trakt: %s candidate movies after year filter (%s+)",
        len(result),
        min_year,
    )
    return result


def select_digital_release(
    releases: Any,
    window: ReleaseWindow,
) -> Optional[Tuple[date, str]]:
    if not isinstance(releases, list):
        return None

    priority = {
        "us": 0, "gb": 1, "ca": 2, "au": 3, "nz": 4,
        "ie": 5, "de": 6, "fr": 7, "it": 8, "es": 9,
        "jp": 10, "ru": 11,
    }
    best: Optional[Tuple[date, int, str]] = None

    for release in releases:
        if not isinstance(release, dict):
            continue
        release_type = str(release.get("release_type") or "").lower()
        if release_type != "digital":
            continue
        released = parse_date(release.get("release_date"))
        if released is None or released < window.start or released > window.end:
            continue

        country = str(release.get("country") or "").lower()
        candidate = (
            released,
            priority.get(country, 99),
            country.upper(),
        )
        if best is None or candidate < best:
            best = candidate

    if best is None:
        return None
    return best[0], best[2]


def load_trakt_releases(
    movies: List[TraktMovie],
    window: ReleaseWindow,
    client_id: str,
    timeout: int,
) -> List[Tuple[TraktMovie, date, str]]:
    result: List[Tuple[TraktMovie, date, str]] = []

    for index, movie in enumerate(movies, start=1):
        releases = trakt_get(
            f"/movies/{movie.trakt_id}/releases",
            client_id,
            timeout,
        )
        digital = select_digital_release(releases, window)
        if digital is not None:
            result.append((movie, digital[0], digital[1]))

        if index % 25 == 0 or index == len(movies):
            logging.info(
                "Trakt releases: %s/%s checked, %s with digital date",
                index,
                len(movies),
                len(result),
            )
        time.sleep(0.12)

    return result


def localized_title(detail: Dict[str, Any], language: str) -> Optional[str]:
    translations = detail.get("translations")
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


def alternative_title(detail: Dict[str, Any], country: str) -> Optional[str]:
    alternative = detail.get("alternative_titles")
    if not isinstance(alternative, dict):
        return None
    entries = alternative.get("titles")
    if not isinstance(entries, list):
        return None

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if entry.get("iso_3166_1") != country:
            continue
        title = entry.get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    return None


def localized_overview(
    detail: Dict[str, Any],
    language: str,
) -> Optional[str]:
    translations = detail.get("translations")
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
        overview = data.get("overview")
        if isinstance(overview, str) and overview.strip():
            return overview.strip()
    return None


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
        if not isinstance(genre, dict):
            continue
        genre_id = genre.get("id")
        name = genre.get("name")
        if isinstance(genre_id, int) and isinstance(name, str):
            result[genre_id] = name
    return result


def find_tmdb_id(
    imdb_id: str,
    api_key: str,
    timeout: int,
) -> Optional[int]:
    if not imdb_id:
        return None
    payload = tmdb_get(
        f"/find/{urllib.parse.quote(imdb_id)}",
        {"external_source": "imdb_id"},
        api_key,
        timeout,
    )
    if not payload:
        return None
    results = payload.get("movie_results")
    if not isinstance(results, list) or not results:
        return None
    first = results[0]
    if not isinstance(first, dict):
        return None
    movie_id = first.get("id")
    return movie_id if isinstance(movie_id, int) else None


def fetch_tmdb_detail(
    movie: TraktMovie,
    api_key: str,
    timeout: int,
) -> Optional[Tuple[TraktMovie, Dict[str, Any]]]:
    tmdb_id = movie.tmdb_id
    if tmdb_id is None:
        tmdb_id = find_tmdb_id(movie.imdb_id, api_key, timeout)
    if tmdb_id is None:
        return None

    detail = tmdb_get(
        f"/movie/{tmdb_id}",
        {
            "language": "en-US",
            "append_to_response": (
                "translations,alternative_titles,external_ids"
            ),
        },
        api_key,
        timeout,
    )
    if detail is None:
        return None

    update = TraktMovie(
        trakt_id=movie.trakt_id,
        tmdb_id=tmdb_id,
        imdb_id=movie.imdb_id,
        title=movie.title,
        year=movie.year,
    )
    return update, detail


def build_candidate(
    movie: TraktMovie,
    detail: Dict[str, Any],
    digital: Tuple[date, str],
    genre_names_ru: Dict[int, str],
    genre_names_en: Dict[int, str],
) -> Optional[Candidate]:
    external = detail.get("external_ids")
    imdb_id = movie.imdb_id
    if isinstance(external, dict):
        value = external.get("imdb_id")
        if isinstance(value, str) and value.startswith("tt"):
            imdb_id = value
    if not imdb_id:
        return None

    tmdb_id = movie.tmdb_id
    if tmdb_id is None:
        value = detail.get("id")
        if isinstance(value, int):
            tmdb_id = value
    if tmdb_id is None:
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
        or movie.title
    )
    title_ru = (
        localized_title(detail, "ru")
        or alternative_title(detail, "RU")
        or title_en
    )
    title_original = (
        str(detail.get("original_title") or "").strip()
        or title_en
    )
    overview_en = (
        localized_overview(detail, "en")
        or str(detail.get("overview") or "").strip()
    )
    overview_ru = localized_overview(detail, "ru") or overview_en

    poster_path = detail.get("poster_path")
    poster_url = (
        f"{TMDB_IMAGE_BASE}{poster_path}"
        if isinstance(poster_path, str) and poster_path
        else None
    )

    return Candidate(
        tmdb_id=tmdb_id,
        imdb_id=imdb_id,
        title_ru=title_ru,
        title_en=title_en,
        title_original=title_original,
        overview_ru=overview_ru,
        overview_en=overview_en,
        year=movie.year,
        genre_ids=genre_ids,
        genres_ru=genres_ru,
        genres_en=genres_en,
        digital_date=digital[0],
        digital_country=digital[1],
        poster_url=poster_url,
    )


def load_rating_cache(
    path: Path,
) -> Dict[str, Tuple[float, Optional[int], str]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}

    cache: Dict[str, Tuple[float, Optional[int], str]] = {}
    for movie in payload.get("movies") or []:
        if not isinstance(movie, dict):
            continue
        imdb_id = movie.get("imdb_id")
        rating = movie.get("imdb_rating")
        checked_at = movie.get("rating_checked_at")
        votes = movie.get("imdb_votes")
        if not isinstance(votes, int):
            votes = None
        if (
            isinstance(imdb_id, str)
            and isinstance(rating, (int, float))
            and isinstance(checked_at, str)
        ):
            cache[imdb_id] = (float(rating), votes, checked_at)
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
        description="Collect digital movie releases from Trakt + TMDB + OMDb."
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
        "--min-votes",
        type=int,
        default=int(os.environ.get("MIN_IMDB_VOTES", "500")),
        help="minimum IMDb vote count (default: 500)",
    )
    parser.add_argument(
        "--min-year",
        type=int,
        default=date.today().year - 1,
        help="minimum movie release year",
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
        help="limit Trakt candidates for a test run",
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

    trakt_key = os.environ.get("TRAKT_CLIENT_ID", "").strip()
    tmdb_key = os.environ.get("TMDB_API_KEY", "").strip()
    omdb_key = os.environ.get("OMDB_API_KEY", "").strip()
    if not trakt_key:
        logging.error("TRAKT_CLIENT_ID is not set")
        return 2
    if not tmdb_key:
        logging.error("TMDB_API_KEY is not set")
        return 2
    if not omdb_key:
        logging.error("OMDB_API_KEY is not set")
        return 2

    if not 0 <= args.min_rating <= 10:
        logging.error("Minimum rating must be between 0 and 10")
        return 2
    if args.min_votes < 0:
        logging.error("Minimum vote count must be non-negative")
        return 2
    if args.months_ahead < 0 or args.min_year < 1900:
        logging.error("Invalid year/window")
        return 2

    window = release_window(args.months_ahead)
    genres_ru = load_genres("ru-RU", tmdb_key, args.timeout)
    genres_en = load_genres("en-US", tmdb_key, args.timeout)

    trakt_movies = collect_trakt_movies(
        window,
        trakt_key,
        args.timeout,
        args.min_year,
        args.limit,
    )
    if not trakt_movies:
        logging.error("No Trakt candidates after year filter")
        return 1

    digital_movies = load_trakt_releases(
        trakt_movies,
        window,
        trakt_key,
        args.timeout,
    )
    logging.info("%s movies with digital release dates", len(digital_movies))

    candidates: List[Candidate] = []
    details_done = 0
    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        futures = {
            pool.submit(
                fetch_tmdb_detail,
                movie,
                tmdb_key,
                args.timeout,
            ): (movie, digital_date, digital_country)
            for movie, digital_date, digital_country in digital_movies
        }
        for future in as_completed(futures):
            movie, digital_date, digital_country = futures[future]
            try:
                fetched = future.result()
            except Exception:
                logging.exception("TMDB detail failed for %s", movie.trakt_id)
                continue
            if fetched is None:
                continue
            updated_movie, detail = fetched
            candidate = build_candidate(
                updated_movie,
                detail,
                (digital_date, digital_country),
                genres_ru,
                genres_en,
            )
            if candidate is not None:
                candidates.append(candidate)

            details_done += 1
            if details_done % 25 == 0 or details_done == len(futures):
                logging.info(
                    "TMDB details: %s/%s, %s candidates",
                    details_done,
                    len(futures),
                    len(candidates),
                )

    logging.info("TMDB: %s enriched candidates", len(candidates))

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

        if (
            cached
            and cached[1] is not None
            and cached_rating_is_fresh(cached[2])
        ):
            rating = cached[0]
            votes = cached[1]
            checked_at = cached[2]
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
                votes = cached[1]
                checked_at = cached[2]
            time.sleep(0.05)

        if rating is None or rating < args.min_rating:
            continue
        if args.min_votes > 0 and (
            votes is None or votes < args.min_votes
        ):
            continue

        movies.append(
            {
                "tmdb_id": candidate.tmdb_id,
                "imdb_id": candidate.imdb_id,
                "title_ru": candidate.title_ru,
                "title_en": candidate.title_en,
                "title_original": candidate.title_original,
                "overview_ru": candidate.overview_ru,
                "overview_en": candidate.overview_en,
                "year": candidate.year,
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
        "minimum_imdb_votes": args.min_votes,
        "minimum_year": args.min_year,
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
