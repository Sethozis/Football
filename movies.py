#!/usr/bin/env python3
"""Collect upcoming digital movie releases from TMDB and OMDb.

TMDB is used for the regional digital release calendar, titles, localised
overviews, genres, posters and IMDb ids. OMDb supplies the IMDb rating and
vote count. No Trakt client is required.
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

TMDB_BASE = "https://api.themoviedb.org/3"
OMDB_BASE = "https://www.omdbapi.com/"
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"
DEFAULT_OUTPUT = "movie_releases.json"
DEFAULT_MIN_RATING = 7.0
DEFAULT_MIN_VOTES = 500
DEFAULT_MONTHS_AHEAD = 12
DEFAULT_WORKERS = 8
HTTP_TIMEOUT = 25
RATING_CACHE_DAYS = 7
USER_AGENT = "month-calendar-movies-sync/2.0"

# Major digital-store regions.  The search is repeated per region because
# TMDB stores release dates by country.
DISCOVERY_REGIONS = [
    "US", "GB", "CA", "AU", "DE", "FR", "IT", "ES",
    "JP", "RU", "NL", "BR", "MX", "PL", "KR", "CN",
]
PAGE_CAP = 10


@dataclass(frozen=True)
class ReleaseWindow:
    start: date
    end: date


@dataclass(frozen=True)
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
            with urllib.request.urlopen(
                request,
                timeout=timeout,
            ) as response:
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
                logging.error("Unauthorized TMDB request: %s", error)
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


def omdb_get(
    imdb_id: str,
    api_key: str,
    timeout: int,
) -> Optional[Dict[str, Any]]:
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


def localized_title(detail: Dict[str, Any], language: str) -> Optional[str]:
    translations = detail.get("translations")
    if not isinstance(translations, dict):
        return None
    entries = translations.get("translations")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("iso_639_1") != language:
            continue
        data = entry.get("data")
        if isinstance(data, dict):
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


def localized_overview(detail: Dict[str, Any], language: str) -> Optional[str]:
    translations = detail.get("translations")
    if not isinstance(translations, dict):
        return None
    entries = translations.get("translations")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("iso_639_1") != language:
            continue
        data = entry.get("data")
        if isinstance(data, dict):
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
    result: Dict[int, str] = {}
    if not isinstance(payload, dict):
        return result
    for genre in payload.get("genres") or []:
        if not isinstance(genre, dict):
            continue
        genre_id = genre.get("id")
        name = genre.get("name")
        if isinstance(genre_id, int) and isinstance(name, str):
            result[genre_id] = name
    return result


def select_digital_release(
    detail: Dict[str, Any],
    window: ReleaseWindow,
) -> Optional[Tuple[date, str]]:
    release_dates = detail.get("release_dates")
    if not isinstance(release_dates, dict):
        return None
    results = release_dates.get("results")
    if not isinstance(results, list):
        return None

    priority = {
        "us": 0, "gb": 1, "ca": 2, "au": 3, "nz": 4,
        "ie": 5, "de": 6, "fr": 7, "it": 8, "es": 9,
        "jp": 10, "ru": 11,
    }
    best: Optional[Tuple[date, int, str]] = None

    for country_entry in results:
        if not isinstance(country_entry, dict):
            continue
        country = str(country_entry.get("iso_3166_1") or "").upper()
        dates = country_entry.get("release_dates")
        if not isinstance(dates, list):
            continue
        for release in dates:
            if not isinstance(release, dict):
                continue
            release_type = release.get("type")
            if isinstance(release_type, str) and release_type.isdigit():
                release_type = int(release_type)
            if release_type != 4:
                continue
            released = parse_date(release.get("release_date"))
            if released is None or released < window.start or released > window.end:
                continue
            candidate = (
                released,
                priority.get(country.lower(), 99),
                country,
            )
            if best is None or candidate < best:
                best = candidate

    if best is None:
        return None
    return best[0], best[2]


def discover_ids_for_region(
    region: str,
    window: ReleaseWindow,
    api_key: str,
    timeout: int,
) -> List[int]:
    # Old catalogue re-releases also have type 4 dates.  Search from the
    # previous year and sort by primary release date descending so new films
    # appear before decades-old catalogue titles.
    discovery_start = date(window.start.year - 1, 1, 1)
    result: List[int] = []
    seen = set()
    page = 1
    total_pages = 1

    while page <= total_pages and page <= PAGE_CAP:
        payload = tmdb_get(
            "/discover/movie",
            {
                "region": region,
                "with_release_type": "4",
                "release_date.gte": discovery_start.isoformat(),
                "release_date.lte": window.end.isoformat(),
                "sort_by": "primary_release_date.desc",
                "include_adult": "false",
                "language": "en-US",
                "page": str(page),
            },
            api_key,
            timeout,
        )
        if not isinstance(payload, dict):
            break
        total_pages = int(payload.get("total_pages") or 1)
        for movie in payload.get("results") or []:
            if not isinstance(movie, dict):
                continue
            movie_id = movie.get("id")
            if isinstance(movie_id, int) and movie_id not in seen:
                seen.add(movie_id)
                result.append(movie_id)
        page += 1
        time.sleep(0.08)

    logging.info(
        "TMDB discover %s: %s movies",
        region,
        len(result),
    )
    return result


def discover_movie_ids(
    window: ReleaseWindow,
    api_key: str,
    timeout: int,
    workers: int,
) -> List[int]:
    ids: List[int] = []
    seen = set()
    with ThreadPoolExecutor(max_workers=max(1, min(workers, 6))) as pool:
        futures = {
            pool.submit(
                discover_ids_for_region,
                region,
                window,
                api_key,
                timeout,
            ): region
            for region in DISCOVERY_REGIONS
        }
        for future in as_completed(futures):
            try:
                region_ids = future.result()
            except Exception:
                logging.exception(
                    "TMDB discover failed for %s",
                    futures[future],
                )
                continue
            for movie_id in region_ids:
                if movie_id not in seen:
                    seen.add(movie_id)
                    ids.append(movie_id)
    return ids


def fetch_tmdb_detail(
    tmdb_id: int,
    api_key: str,
    timeout: int,
) -> Optional[Dict[str, Any]]:
    detail = tmdb_get(
        f"/movie/{tmdb_id}",
        {
            "language": "en-US",
            "append_to_response": "translations,alternative_titles,release_dates,external_ids",
        },
        api_key,
        timeout,
    )
    if not isinstance(detail, dict):
        return None
    if not isinstance(detail.get("release_dates"), dict):
        release_dates = tmdb_get(
            f"/movie/{tmdb_id}/release_dates",
            {},
            api_key,
            timeout,
        )
        if isinstance(release_dates, dict):
            detail["release_dates"] = release_dates
    return detail


def build_candidate(
    detail: Dict[str, Any],
    digital: Tuple[date, str],
    genres_ru: Dict[int, str],
    genres_en: Dict[int, str],
    min_year: int,
) -> Optional[Candidate]:
    external = detail.get("external_ids")
    imdb_id = ""
    if isinstance(external, dict):
        value = external.get("imdb_id")
        if isinstance(value, str):
            imdb_id = value.strip()
    if not imdb_id:
        value = detail.get("imdb_id")
        if isinstance(value, str):
            imdb_id = value.strip()
    if not imdb_id.startswith("tt"):
        return None

    title_en = str(
        detail.get("title")
        or detail.get("original_title")
        or ""
    ).strip()
    title_original = str(detail.get("original_title") or "").strip()
    title_ru = (
        localized_title(detail, "ru")
        or alternative_title(detail, "RU")
        or title_en
    )
    overview_en = (
        localized_overview(detail, "en")
        or str(detail.get("overview") or "").strip()
    )
    overview_ru = localized_overview(detail, "ru") or overview_en

    release_date = parse_date(detail.get("release_date"))
    year = release_date.year if release_date else None
    if year is not None and year < min_year:
        return None

    genre_ids: List[int] = []
    for genre in detail.get("genres") or []:
        if isinstance(genre, dict) and isinstance(genre.get("id"), int):
            genre_ids.append(genre["id"])

    poster_path = detail.get("poster_path")
    poster_url = (
        f"{IMAGE_BASE}{poster_path}"
        if isinstance(poster_path, str) and poster_path
        else None
    )

    return Candidate(
        tmdb_id=int(detail.get("id") or 0),
        imdb_id=imdb_id,
        title_ru=title_ru,
        title_en=title_en,
        title_original=title_original,
        overview_ru=overview_ru,
        overview_en=overview_en,
        year=year,
        genre_ids=genre_ids,
        genres_ru=[genres_ru[g] for g in genre_ids if g in genres_ru],
        genres_en=[genres_en[g] for g in genre_ids if g in genres_en],
        digital_date=digital[0],
        digital_country=digital[1],
        poster_url=poster_url,
    )


def load_rating_cache(
    path: Path,
) -> Dict[str, Tuple[float, Optional[int], str]]:
    cache: Dict[str, Tuple[float, Optional[int], str]] = {}
    if not path.exists():
        return cache
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return cache
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
        checked = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
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
    if not payload:
        logging.warning("OMDb request failed for %s", imdb_id)
        return None
    if payload.get("Response") == "False":
        logging.warning(
            "OMDb rejected %s: %s",
            imdb_id,
            payload.get("Error") or "unknown error",
        )
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


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect digital movie releases from TMDB and OMDb."
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
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"parallel requests (default: {DEFAULT_WORKERS})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=HTTP_TIMEOUT,
        help=f"HTTP timeout in seconds (default: {HTTP_TIMEOUT})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="limit discovered TMDB ids for a test run",
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
    if args.min_votes < 0:
        logging.error("Minimum vote count must be non-negative")
        return 2
    if args.months_ahead < 0 or args.min_year < 1900:
        logging.error("Invalid year/window")
        return 2

    window = release_window(args.months_ahead)
    genres_ru = load_genres("ru-RU", tmdb_key, args.timeout)
    genres_en = load_genres("en-US", tmdb_key, args.timeout)

    movie_ids = discover_movie_ids(
        window,
        tmdb_key,
        args.timeout,
        args.workers,
    )
    if not movie_ids:
        logging.error("TMDB discover returned no movies")
        if args.output.exists():
            logging.error(
                "Keeping existing %s instead of overwriting it",
                args.output,
            )
            return 0
        return 1
    if args.limit is not None:
        movie_ids = movie_ids[: max(0, args.limit)]

    logging.info("TMDB discover: %s unique movies", len(movie_ids))
    candidates: List[Candidate] = []
    details_done = 0
    digital_found = 0
    build_rejected = 0
    missing_release_dates = 0
    sample_logged = False

    with ThreadPoolExecutor(max_workers=max(1, min(args.workers, 8))) as pool:
        futures = {
            pool.submit(
                fetch_tmdb_detail,
                movie_id,
                tmdb_key,
                args.timeout,
            ): movie_id
            for movie_id in movie_ids
        }
        for future in as_completed(futures):
            tmdb_id = futures[future]
            try:
                detail = future.result()
            except Exception:
                logging.exception("TMDB detail failed for %s", tmdb_id)
                continue
            if not isinstance(detail, dict):
                continue

            if not isinstance(detail.get("release_dates"), dict):
                missing_release_dates += 1
            digital = select_digital_release(detail, window)
            if digital is not None:
                digital_found += 1
                candidate = build_candidate(
                    detail,
                    digital,
                    genres_ru,
                    genres_en,
                    args.min_year,
                )
                if candidate is not None:
                    candidates.append(candidate)
                else:
                    build_rejected += 1
                    if build_rejected == 1:
                        external = detail.get("external_ids")
                        imdb_value = (
                            external.get("imdb_id")
                            if isinstance(external, dict)
                            else None
                        )
                        logging.info(
                            "First candidate rejected: id=%s title=%s "
                            "year=%s imdb=%s",
                            tmdb_id,
                            detail.get("title"),
                            parse_date(detail.get("release_date")),
                            imdb_value,
                        )
            elif not sample_logged:
                sample_logged = True
                logging.info(
                    "No digital date for sample TMDB id %s; release_dates=%s",
                    tmdb_id,
                    json.dumps(
                        detail.get("release_dates"),
                        ensure_ascii=False,
                    )[:900],
                )

            details_done += 1
            if details_done % 25 == 0 or details_done == len(futures):
                logging.info(
                    "TMDB details: %s/%s, %s candidates",
                    details_done,
                    len(futures),
                    len(candidates),
                )

    logging.info(
        "TMDB: %s enriched candidates, %s with digital date, "
        "%s rejected after digital match, %s without release_dates",
        len(candidates),
        digital_found,
        build_rejected,
        missing_release_dates,
    )
    if not candidates:
        logging.error("No TMDB candidates with digital dates")
        if args.output.exists():
            logging.error(
                "Keeping existing %s instead of overwriting it",
                args.output,
            )
            return 0
        return 1

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

    if not movies:
        logging.error("No movies passed the rating/vote filters")
        if args.output.exists():
            logging.error(
                "Keeping existing %s instead of overwriting it",
                args.output,
            )
            return 0
        return 1

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
    return 0


if __name__ == "__main__":
    sys.exit(main())
