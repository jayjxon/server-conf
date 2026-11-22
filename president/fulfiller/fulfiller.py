#!/usr/bin/env python3
"""Fulfil Seerr requests without Radarr/Sonarr.

The service polls the stock Seerr API for pending and approved requests.  It
looks the title up in the anime catalogue by title/type first, then falls back
to Dispatcharr's VOD database by TMDB id, and writes the corresponding
``.strm``/``.nfo`` files into a small Jellyfin library.  Nothing from the full
Dispatcharr VOD tree is needed.

Run inside the Dispatcharr image with ``network_mode: service:dispatcharr`` so
the Django ORM can reach the database on localhost.  Only the standard library
is used.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

SEERR_URL = os.environ.get("SEERR_URL", "http://seerr:5055")
SEERR_API_KEY = os.environ.get("SEERR_API_KEY", "")
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin:8096")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
DISPATCHARR_PUBLIC_URL = os.environ.get(
    "DISPATCHARR_PUBLIC_URL", "https://dispatcharr.local"
).rstrip("/")
ANIME_LIBRARY_URL = os.environ.get(
    "ANIME_LIBRARY_URL", "http://anime-library:8787"
).rstrip("/")
ANIME_LANG = os.environ.get("ANIME_LANG", "dub").strip().lower()
PRESS_PLAYZ_LIBRARY_URL = os.environ.get(
    "PRESS_PLAYZ_LIBRARY_URL", ""
).rstrip("/")
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/library"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
STATE_FILE = STATE_DIR / "state.json"
POLL_SECONDS = max(10, int(os.environ.get("POLL_SECONDS", "30")))
RECONCILE_SECONDS = max(300, int(os.environ.get("RECONCILE_SECONDS", "21600")))
# hold a pending request this long before retrying it; upstream outages are
# common enough that declining on the first 5xx loses the request for good
RETRY_SECONDS = max(0, int(os.environ.get("RETRY_SECONDS", "300")))
RETRY_ATTEMPTS = max(0, int(os.environ.get("RETRY_ATTEMPTS", "2")))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
SUBTITLE_LANGUAGE = os.environ.get("SUBTITLE_LANGUAGE", "eng").strip() or "eng"
# one subtitle search costs several seconds; small batches keep Seerr polling alive
SUBTITLE_BATCH = max(1, int(os.environ.get("SUBTITLE_BATCH", "6")))
SUBTITLE_MAX_ATTEMPTS = max(
    1, int(os.environ.get("SUBTITLE_MAX_ATTEMPTS", "12"))
)
# bump to make existing "done" entries prove their subtitle tracks again
SUBTITLE_STATE_VERSION = 4

_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_YEAR_IN_TITLE = re.compile(
    r"(?:\(\s*|\[\s*)(?:19|20)\d{2}(?:\s*\)|\s*\])"
)
_TRAILING_YEAR = re.compile(r"\b(?:19|20)\d{2}\b\s*$")

_refresh_warning = False


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def safe_name(value, limit: int = 120) -> str:
    text = _BAD_NAME.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .") or "Untitled"


def normalized_title(value) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = _YEAR_IN_TITLE.sub(" ", text)
    text = _TRAILING_YEAR.sub(" ", text)
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _json_body(raw: bytes):
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return raw.decode("utf-8", "replace")


def http_json(method: str, url: str, payload=None, headers=None, timeout=None):
    """Return (status, parsed body); network errors are raised."""
    body = None
    request_headers = {
        "Accept": "application/json",
        "User-Agent": "seerr-fulfiller/1.0",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(
        url, data=body, method=method, headers=request_headers
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout or HTTP_TIMEOUT
        ) as response:
            return response.status, _json_body(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, _json_body(exc.read())


def request_media_type(request: dict) -> str:
    media = request.get("media") if isinstance(request.get("media"), dict) else {}
    for value in (request.get("type"), media.get("mediaType")):
        kind = str(value or "").strip().lower()
        if kind in ("movie", "film"):
            return "movie"
        if kind in ("tv", "series"):
            return "tv"
    return ""


def request_seasons(request: dict) -> list[int]:
    seasons = []
    for item in request.get("seasons") or []:
        value = item.get("seasonNumber") if isinstance(item, dict) else item
        number = _int(value)
        if number is not None and number not in seasons:
            seasons.append(number)
    return sorted(seasons)


class Seerr:
    def __init__(self, base: str, api_key: str):
        self.base = base.rstrip("/")
        if not self.base.endswith("/api/v1"):
            self.base += "/api/v1"
        self.api_key = api_key

    def call(self, path: str, method: str = "GET", payload=None, timeout=None):
        return http_json(
            method,
            self.base + path,
            payload=payload,
            headers={"X-Api-Key": self.api_key},
            timeout=timeout,
        )

    def requests(self, status_filter: str):
        """Yield every request for one Seerr filter, page by page."""
        skip = 0
        for _ in range(100):
            code, data = self.call(
                f"/request?take=100&skip={skip}&filter={status_filter}"
            )
            if code != 200:
                log(f"seerr request list {status_filter} returned HTTP {code}")
                return
            if not isinstance(data, dict):
                return
            results = data.get("results") or []
            for request in results:
                if isinstance(request, dict):
                    yield request
            if len(results) < 100:
                return
            skip += len(results)

    def details(self, media_type: str, tmdb_id: int):
        kind = "movie" if media_type == "movie" else "tv"
        code, data = self.call(f"/{kind}/{int(tmdb_id)}")
        if code != 200 or not isinstance(data, dict):
            log(f"seerr details {kind}/{tmdb_id} returned HTTP {code}")
            return None
        date = (
            data.get("releaseDate")
            or data.get("firstAirDate")
            or data.get("release_date")
            or data.get("first_air_date")
            or ""
        )
        year = _int(str(date)[:4])
        title = (
            data.get("title")
            or data.get("name")
            or data.get("originalTitle")
            or data.get("originalName")
            or ""
        )
        original = (
            data.get("originalTitle")
            or data.get("originalName")
            or data.get("title")
            or data.get("name")
            or ""
        )
        season_counts = {}
        for season in data.get("seasons") or []:
            if not isinstance(season, dict):
                continue
            number = _int(
                season.get("seasonNumber") or season.get("season_number")
            )
            count = _int(
                season.get("episodeCount") or season.get("episode_count")
            )
            if number is not None and count:
                season_counts[number] = count
        return {
            "media_type": media_type,
            "title": str(title).strip(),
            "original_title": str(original).strip(),
            "year": year,
            "tmdb_id": int(tmdb_id),
            "seasons": _int(data.get("numberOfSeasons") or data.get("number_of_seasons")),
            "season_counts": season_counts,
            "episodes": _int(
                data.get("numberOfEpisodes") or data.get("number_of_episodes")
            ),
        }

    def approve(self, request_id: int) -> bool:
        code, _ = self.call(f"/request/{int(request_id)}/approve", "POST", {})
        if code not in (200, 201, 202):
            log(f"seerr approve {request_id} returned HTTP {code}")
            return False
        return True

    def decline(self, request_id: int) -> bool:
        code, _ = self.call(f"/request/{int(request_id)}/decline", "POST", {})
        if code not in (200, 201, 202):
            log(f"seerr decline {request_id} returned HTTP {code}")
            return False
        return True

    def media_status(self, media_id: int, status: str, seasons=None) -> bool:
        payload = {"is4k": False}
        if seasons:
            payload["seasons"] = [
                {"seasonNumber": int(number)} for number in seasons
            ]
        code, _ = self.call(
            f"/media/{int(media_id)}/{status}", "POST", payload
        )
        if code not in (200, 202):
            log(f"seerr media {media_id} {status} returned HTTP {code}")
            return False
        return True


class VodCatalog:
    def __init__(self):
        from apps.vod.models import (  # type: ignore[import-not-found]
            Episode,
            M3UEpisodeRelation,
            M3UMovieRelation,
            Movie,
            Series,
        )

        self.Episode = Episode
        self.M3UEpisodeRelation = M3UEpisodeRelation
        self.M3UMovieRelation = M3UMovieRelation
        self.Movie = Movie
        self.Series = Series

    @staticmethod
    def _active(relation) -> bool:
        if not getattr(relation, "stream_id", None):
            return False
        account = getattr(relation, "m3u_account", None)
        if account is not None and hasattr(account, "is_active"):
            return bool(account.is_active)
        return True

    def movie(self, tmdb_id: int):
        for movie in self.Movie.objects.filter(tmdb_id=str(tmdb_id))[:20]:
            relations = self.M3UMovieRelation.objects.filter(
                movie=movie
            ).select_related("m3u_account")
            for relation in relations:
                if self._active(relation):
                    return movie, relation
        return None

    def series(self, tmdb_id: int, seasons: list[int]):
        episodes = self.Episode.objects.all()
        if seasons:
            episodes = episodes.filter(season_number__in=seasons)
        for series in self.Series.objects.filter(tmdb_id=str(tmdb_id))[:20]:
            rows = []
            for episode in episodes.filter(series=series).order_by(
                "season_number", "episode_number"
            ):
                relations = self.M3UEpisodeRelation.objects.filter(
                    episode=episode
                ).select_related("m3u_account")
                for relation in relations:
                    if self._active(relation):
                        rows.append((episode, relation))
                        break
            if rows:
                return series, rows
        return None


class Anime:
    def __init__(self, base: str, lang: str):
        self.base = base.rstrip("/")
        self.lang = lang if lang in ("sub", "dub") else "dub"

    def _call(self, path: str, method: str = "GET", payload=None, timeout=None):
        return http_json(
            method,
            self.base + path,
            payload=payload,
            timeout=timeout,
        )

    def search(self, *titles: str) -> list[dict]:
        for title in titles:
            if not title:
                continue
            query = urllib.parse.quote(str(title))
            code, data = self._call(f"/api/search?q={query}&page=1")
            if code != 200 or not isinstance(data, dict):
                log(f"anime search {title!r} returned HTTP {code}")
                continue
            items = data.get("items") or []
            if items:
                return [item for item in items if isinstance(item, dict)]
        return []

    def show(self, slug: str):
        code, data = self._call(
            f"/api/show?slug={urllib.parse.quote(str(slug))}"
        )
        if code != 200 or not isinstance(data, dict):
            return None
        return data

    def select(self, slug: str) -> bool:
        code, data = self._call(
            "/api/select",
            "POST",
            {"slug": slug, "lang": self.lang},
            timeout=180,
        )
        if code != 200:
            detail = data.get("error") if isinstance(data, dict) else ""
            log(f"anime select {slug!r} returned HTTP {code}: {detail}")
            return False
        return True


class PressPlayZ:
    """Client for the local PressPlayZ HLS selector/proxy."""

    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def select(self, details: dict):
        if not self.base:
            return None
        payload = {
            "tmdb_id": details.get("tmdb_id"),
            "media_type": details.get("media_type"),
            "title": details.get("title"),
            "year": details.get("year"),
            "seasons": details.get("_seasons") or [],
            "season_counts": details.get("season_counts") or {},
        }
        try:
            code, data = http_json(
                "POST",
                self.base + "/api/select",
                payload=payload,
                timeout=300,
            )
        except (urllib.error.URLError, OSError) as exc:
            log(f"pressplayz select failed: {exc}")
            return None
        if code == 404:
            return None
        if code != 200 or not isinstance(data, dict):
            detail = data.get("error") if isinstance(data, dict) else ""
            log(f"pressplayz select returned HTTP {code}: {detail}")
            return None
        selection = data.get("selection")
        return selection if isinstance(selection, dict) else None


class Fulfiller:
    def __init__(self):
        self.seerr = Seerr(SEERR_URL, SEERR_API_KEY)
        self.vod = VodCatalog()
        self.anime = Anime(ANIME_LIBRARY_URL, ANIME_LANG)
        self.pressplayz = PressPlayZ(PRESS_PLAYZ_LIBRARY_URL)
        self.state = self._load_state()
        self.state.setdefault("retries", {})
        self._jellyfin_uid = None

    @staticmethod
    def _load_state() -> dict:
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("items"), dict):
                return data
        except (OSError, ValueError):
            pass
        return {"version": 1, "items": {}}

    def _save_state(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temp = STATE_FILE.with_name(STATE_FILE.name + ".tmp")
        temp.write_text(
            json.dumps(self.state, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temp, STATE_FILE)

    @staticmethod
    def _write_if_changed(path: Path, content: str) -> bool:
        if path.is_file():
            try:
                if path.read_text(encoding="utf-8") == content:
                    return False
            except (OSError, UnicodeDecodeError):
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
        return True

    @staticmethod
    def _nfo(root: str, title: str, year, tmdb_id: int) -> str:
        lines = [
            '<?xml version="1.0" encoding="utf-8" standalone="yes"?>',
            f"<{root}>",
            f"  <title>{escape(str(title))}</title>",
        ]
        if year:
            lines.append(f"  <year>{int(year)}</year>")
        lines.extend(
            [
                f"  <tmdbid>{int(tmdb_id)}</tmdbid>",
                f'  <uniqueid type="tmdb" default="true">{int(tmdb_id)}</uniqueid>',
                f"</{root}>",
                "",
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _prune(old_files, keep_files) -> None:
        keep = {str(Path(item).resolve()) for item in keep_files}
        root = LIBRARY_DIR.resolve()
        for item in old_files or []:
            path = Path(str(item)).resolve()
            if str(path) in keep:
                continue
            try:
                path.relative_to(root)
            except ValueError:
                continue
            try:
                path.unlink()
            except OSError:
                continue
            parent = path.parent
            while parent != root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent

    def _vprint(self, media_type: str, tmdb_id: int, details: dict):
        if not details:
            return None
        if media_type == "movie":
            hit = self.vod.movie(tmdb_id)
            if not hit:
                return None
            movie, relation = hit
            title = details.get("title") or movie.name or f"TMDB {tmdb_id}"
            year = details.get("year") or movie.year
            label = safe_name(title)
            if year:
                label += f" ({int(year)})"
            folder = LIBRARY_DIR / "Movies" / f"{label} [tmdbid-{tmdb_id}]"
            url = (
                f"{DISPATCHARR_PUBLIC_URL}/proxy/vod/movie/{movie.uuid}"
                f"?stream_id={relation.stream_id}"
            )
            strm = folder / f"{label}.strm"
            nfo = folder / f"{label}.nfo"
            self._write_if_changed(strm, url + "\n")
            self._write_if_changed(nfo, self._nfo("movie", title, year, tmdb_id))
            return {
                "kind": "vod",
                "media_type": "movie",
                "tmdb_id": tmdb_id,
                "title": title,
                "year": year,
                "seasons": [],
                "files": [str(strm), str(nfo)],
            }

        hit = self.vod.series(tmdb_id, list(details.get("_seasons") or []))
        if not hit:
            return None
        series, rows = hit
        title = details.get("title") or series.name or f"TMDB {tmdb_id}"
        year = details.get("year") or series.year
        label = safe_name(title)
        if year:
            label += f" ({int(year)})"
        folder = LIBRARY_DIR / "Shows" / f"{label} [tmdbid-{tmdb_id}]"
        files = []
        tvshow = folder / "tvshow.nfo"
        self._write_if_changed(tvshow, self._nfo("tvshow", title, year, tmdb_id))
        files.append(str(tvshow))
        for episode, relation in rows:
            season = _int(episode.season_number, 1)
            number = _int(episode.episode_number, 0)
            filename = f"{label} - S{season:02d}E{number:02d}"
            episode_name = safe_name(episode.name, 70)
            if episode_name:
                filename += f" - {episode_name}"
            season_dir = folder / f"Season {season:02d}"
            strm = season_dir / f"{filename}.strm"
            url = (
                f"{DISPATCHARR_PUBLIC_URL}/proxy/vod/episode/{episode.uuid}"
                f"?stream_id={relation.stream_id}"
            )
            self._write_if_changed(strm, url + "\n")
            files.append(str(strm))
        if len(files) == 1:
            return None
        return {
            "kind": "vod",
            "media_type": "tv",
            "tmdb_id": tmdb_id,
            "title": title,
            "year": year,
            "seasons": sorted(
                {_int(ep.season_number, 1) for ep, _ in rows}
            ),
            "files": files,
        }

    @staticmethod
    def _candidate_titles(candidate: dict) -> list[str]:
        names = []
        for key in ("title", "alternative", "titles"):
            value = candidate.get(key)
            if isinstance(value, str):
                names.extend(
                    part.strip()
                    for part in re.split(r"[,;/]", value)
                    if part.strip()
                )
        return names

    @staticmethod
    def _anime_kind(candidate: dict) -> str:
        kind = str(candidate.get("type") or "").strip().lower()
        if kind in ("movie", "film"):
            return "movie"
        if kind in ("tv", "series", "ona", "ova", "special", "music"):
            return "tv"
        episodes = _int(candidate.get("episodes"), 0) or 0
        return "movie" if episodes <= 1 else "tv"

    def _anime_match(self, details: dict):
        if not details.get("title"):
            return None
        candidates = self.anime.search(
            details["title"], details.get("original_title")
        )
        if not candidates:
            return None
        wanted_kind = details["media_type"]
        wanted_titles = {
            normalized_title(details.get("title")),
            normalized_title(details.get("original_title")),
        }
        wanted_titles.discard("")
        matches = []
        for candidate in candidates:
            if self._anime_kind(candidate) != wanted_kind:
                continue
            names = {normalized_title(name) for name in self._candidate_titles(candidate)}
            if names & wanted_titles:
                matches.append(candidate)
        if not matches:
            return None
        if len(matches) == 1:
            return matches[0]

        wanted_episodes = details.get("episodes")
        wanted_seasons = details.get("seasons")
        scored = []
        for candidate in matches[:20]:
            detail = self.anime.show(str(candidate.get("slug") or ""))
            if not detail:
                continue
            candidate_episodes = _int(detail.get("episode_total"))
            candidate_seasons = _int(detail.get("season_total"))
            scored.append(
                (
                    (
                        wanted_episodes is not None
                        and candidate_episodes == wanted_episodes,
                        wanted_seasons is not None
                        and candidate_seasons == wanted_seasons,
                    ),
                    candidate,
                )
            )
        if not scored:
            return None
        both = [candidate for score, candidate in scored if score == (True, True)]
        if len(both) == 1:
            return both[0]
        episodes = [candidate for score, candidate in scored if score[0]]
        if len(episodes) == 1:
            return episodes[0]
        seasons = [candidate for score, candidate in scored if score[1]]
        if len(seasons) == 1:
            return seasons[0]
        return None

    def _anime(self, details: dict):
        candidate = self._anime_match(details)
        if not candidate:
            return None
        slug = str(candidate.get("slug") or "")
        if not slug or not self.anime.select(slug):
            return None
        return {
            "kind": "anime",
            "media_type": details["media_type"],
            "tmdb_id": details.get("tmdb_id"),
            "title": details.get("title") or candidate.get("title"),
            "year": details.get("year") or candidate.get("year"),
            "slug": slug,
            "files": [],
        }

    def _pressplayz(self, details: dict):
        selection = self.pressplayz.select(details)
        if not selection:
            return None
        files = []
        for name in selection.get("files") or []:
            text = str(name)
            # pressplayz-library writes /data/requests; the fulfiller mounts that
            # same tree at /library. Keep state paths in the fulfiller namespace.
            marker = "/data/requests/"
            if text.startswith(marker):
                text = str(LIBRARY_DIR / text[len(marker):])
            files.append(text)
        return {
            "kind": "pressplayz",
            "media_type": details["media_type"],
            "tmdb_id": details.get("tmdb_id"),
            "title": details.get("title") or selection.get("title"),
            "year": details.get("year") or selection.get("year"),
            "seasons": selection.get("seasons") or [],
            "files": files,
        }

    def _has_unmaterialized_season(
        self, tmdb_id: int, requested: list[int], materialized: set[int]
    ) -> bool:
        """True when Dispatcharr has a requested season we have not written yet."""
        hit = self.vod.series(tmdb_id, requested)
        if not hit:
            return False
        available = {_int(episode.season_number, 1) for episode, _ in hit[1]}
        return bool(available - materialized)

    @staticmethod
    def _jellyfin_headers() -> dict:
        return {"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'}

    def _jellyfin_get(self, path: str):
        code, data = http_json(
            "GET",
            JELLYFIN_URL.rstrip("/") + path,
            headers=self._jellyfin_headers(),
            timeout=20,
        )
        if code != 200 or not isinstance(data, dict):
            log(f"jellyfin {path.split('?', 1)[0]} returned HTTP {code}")
            return None
        return data

    def _jellyfin_user_id(self):
        if self._jellyfin_uid:
            return self._jellyfin_uid
        if not JELLYFIN_API_KEY:
            return None
        code, data = http_json(
            "GET",
            JELLYFIN_URL.rstrip("/") + "/Users",
            headers=self._jellyfin_headers(),
            timeout=20,
        )
        if code != 200 or not isinstance(data, list):
            log(f"jellyfin /Users returned HTTP {code}")
            return None
        for user in data:
            if not (user.get("Policy") or {}).get("IsDisabled"):
                self._jellyfin_uid = str(user.get("Id") or "")
                break
        return self._jellyfin_uid

    @staticmethod
    def _strm_files(item: dict) -> list[str]:
        return [
            str(path)
            for path in item.get("files") or []
            if str(path).endswith(".strm")
        ]

    def _with_subtitle_state(self, result, previous: dict):
        """Every .strm addition needs subtitles, whichever source created it."""
        if not result:
            return result
        if previous.get("seerr_status"):
            result["seerr_status"] = previous["seerr_status"]
        if not self._strm_files(result):
            return result
        old_status = previous.get("subtitles")
        files_changed = previous.get("files") != result.get("files")
        attempts = _int(previous.get("subtitle_attempts"), 0) or 0
        if old_status == "failed" and not files_changed:
            result["subtitles"] = "failed"
            result["subtitle_attempts"] = attempts
        elif old_status == "done" and not files_changed:
            result["subtitles"] = "done"
            result.pop("subtitle_attempts", None)
        else:
            result["subtitles"] = "pending"
            result["subtitle_attempts"] = 0 if files_changed else attempts
            if files_changed:
                result.pop("subtitle_cursor", None)
        return result

    @staticmethod
    def _has_subtitle(media: dict) -> bool:
        return any(
            str(stream.get("Type") or "").lower() == "subtitle"
            for stream in media.get("MediaStreams") or []
            if isinstance(stream, dict)
        )

    @staticmethod
    def _imdb_id(title, year):
        title = str(title or "").strip()
        if not title:
            return None
        query = urllib.parse.quote(title)
        try:
            code, data = http_json(
                "GET",
                "https://v3.sg.media-imdb.com/suggestion/x/" + query + ".json",
                headers={"User-Agent": "seerr-fulfiller/1.0"},
            )
        except (urllib.error.URLError, OSError, ValueError) as exc:
            log(f"IMDb lookup for {title!r} failed: {exc}")
            return None
        if code != 200 or not isinstance(data, dict):
            return None
        wanted = normalized_title(title)
        fuzzy = None
        for entry in data.get("d") or []:
            if not isinstance(entry, dict):
                continue
            imdb = str(entry.get("id") or "")
            if not imdb.startswith("tt"):
                continue
            found_year = _int(entry.get("y"))
            if year and found_year and abs(found_year - int(year)) > 1:
                continue
            candidate = normalized_title(entry.get("l"))
            if candidate == wanted:
                return imdb
            if fuzzy is None and (wanted in candidate or candidate in wanted):
                fuzzy = imdb
        return fuzzy

    def _ensure_imdb_ids(self, item: dict, media_items: list[dict]) -> bool:
        """Subbuzz searches only by IMDb id; add one before the subtitle search."""
        uid = self._jellyfin_user_id()
        if not uid:
            return False
        targets = set()
        for media in media_items:
            target = (
                media.get("Id")
                if item.get("media_type") == "movie"
                else media.get("SeriesId")
            )
            if target:
                targets.add(str(target))
        if not targets:
            return False
        for target in targets:
            try:
                code, dto = http_json(
                    "GET",
                    JELLYFIN_URL.rstrip("/") + f"/Users/{uid}/Items/{target}",
                    headers=self._jellyfin_headers(),
                    timeout=20,
                )
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log(f"jellyfin item {target} read failed: {exc}")
                return False
            if code != 200 or not isinstance(dto, dict):
                log(f"jellyfin item {target} returned HTTP {code}")
                return False
            providers = dto.get("ProviderIds")
            if not isinstance(providers, dict):
                providers = {}
            if providers.get("Imdb"):
                continue
            imdb = self._imdb_id(item.get("title"), item.get("year"))
            if not imdb:
                log(f"no IMDb id found for {item.get('title')!r}")
                return False
            providers["Imdb"] = imdb
            dto["ProviderIds"] = providers
            try:
                code, _ = http_json(
                    "POST",
                    JELLYFIN_URL.rstrip("/") + f"/Items/{target}",
                    payload=dto,
                    headers=self._jellyfin_headers(),
                    timeout=20,
                )
            except (urllib.error.URLError, OSError, ValueError) as exc:
                log(f"jellyfin item {target} update failed: {exc}")
                return False
            if code not in (200, 204):
                log(f"jellyfin item {target} update returned HTTP {code}")
                return False
            log(f"IMDb id {imdb} set for {item.get('title')!r}")
        return True

    def _subtitle_attempt(self, item: dict, reason: str) -> bool:
        attempts = (_int(item.get("subtitle_attempts"), 0) or 0) + 1
        item["subtitle_attempts"] = attempts
        if attempts >= SUBTITLE_MAX_ATTEMPTS:
            item["subtitles"] = "failed"
            log(
                f"external subtitles: {reason} for {item.get('title')!r}; "
                f"giving up after {attempts} attempts"
            )
        else:
            item["subtitles"] = "pending"
            log(
                f"external subtitles: {reason} for {item.get('title')!r}; "
                f"retry {attempts}/{SUBTITLE_MAX_ATTEMPTS}"
            )
        return False

    def _jellyfin_media_items(self, item: dict) -> list[dict]:
        """Return the Jellyfin items whose paths are in this state entry."""
        uid = self._jellyfin_user_id()
        if not uid:
            return []
        title = str(item.get("title") or "").strip()
        media_type = item.get("media_type")
        tmdb_id = _int(item.get("tmdb_id"))
        names = {
            Path(str(path)).name
            for path in item.get("files") or []
            if str(path).endswith(".strm")
        }
        if not names or not title:
            return []
        fields = "Path,MediaStreams"
        if media_type == "movie":
            query = urllib.parse.urlencode(
                {
                    "searchTerm": title,
                    "recursive": "true",
                    "includeItemTypes": "Movie",
                    "fields": fields,
                    "limit": "100",
                }
            )
            data = self._jellyfin_get(f"/Users/{uid}/Items?{query}")
            return [
                media
                for media in (data or {}).get("Items") or []
                if Path(str(media.get("Path") or "")).name in names
            ]
        query = urllib.parse.urlencode(
            {
                "searchTerm": title,
                "recursive": "true",
                "includeItemTypes": "Series",
                "fields": "Path",
                "limit": "50",
            }
        )
        data = self._jellyfin_get(f"/Users/{uid}/Items?{query}")
        series_rows = (data or {}).get("Items") or []
        marker = f"[tmdbid-{tmdb_id}]" if tmdb_id else ""
        result = []
        for series in series_rows:
            if marker and marker not in str(series.get("Path") or ""):
                continue
            series_id = str(series.get("Id") or "")
            episode_query = urllib.parse.urlencode(
                {"userId": uid, "fields": fields}
            )
            episodes = self._jellyfin_get(
                f"/Shows/{series.get('Id')}/Episodes?{episode_query}"
            )
            for episode in (episodes or {}).get("Items") or []:
                if Path(str(episode.get("Path") or "")).name in names:
                    episode = dict(episode)
                    episode.setdefault("SeriesId", series_id)
                    result.append(episode)
        return result

    def _ensure_external_subtitles(self, item: dict) -> bool:
        """Download external subtitles for missing .strm files in small batches."""
        if item.get("subtitles") != "pending":
            return False
        if not JELLYFIN_API_KEY:
            item["subtitles"] = "disabled"
            if not _refresh_warning:
                log("JELLYFIN_API_KEY is not set; cannot fetch external subtitles")
            return False
        media_items = self._jellyfin_media_items(item)
        if not media_items:
            return self._subtitle_attempt(
                item, "Jellyfin has not listed the item yet"
            )
        if not self._ensure_imdb_ids(item, media_items):
            return self._subtitle_attempt(item, "no usable IMDb id")
        by_name = {
            Path(str(media.get("Path") or "")).name: media
            for media in media_items
        }
        expected = self._strm_files(item)
        missing = []
        waiting = 0
        for strm in expected:
            media = by_name.get(Path(strm).name)
            if not media:
                waiting += 1
            elif not self._has_subtitle(media):
                missing.append((strm, media))
        if not missing and not waiting:
            item["subtitles"] = "done"
            item.pop("subtitle_attempts", None)
            item.pop("subtitle_cursor", None)
            return False

        downloaded = 0
        unavailable = 0
        failed = 0
        batch = []
        if missing:
            # Rotate through the missing list: a season with no candidates must
            # not starve the episodes after it.
            cursor = (_int(item.get("subtitle_cursor"), 0) or 0) % len(missing)
            batch = (missing[cursor:] + missing[:cursor])[:SUBTITLE_BATCH]
            item["subtitle_cursor"] = (cursor + len(batch)) % len(missing)
        for strm, media in batch:
            item_id = media.get("Id")
            if not item_id:
                failed += 1
                continue
            search_path = (
                f"/Items/{item_id}/RemoteSearch/Subtitles/{SUBTITLE_LANGUAGE}"
            )
            code, candidates = http_json(
                "GET",
                JELLYFIN_URL.rstrip("/") + search_path,
                headers=self._jellyfin_headers(),
                timeout=30,
            )
            if code != 200 or not isinstance(candidates, list):
                failed += 1
                continue
            candidates = [
                candidate
                for candidate in candidates
                if any(
                    provider in str(candidate.get("Id") or "").lower()
                    for provider in ("subf2m", "opensubtitles")
                )
            ]
            # Prefer Subf2m; OpenSubtitles is the fallback for its gaps.
            candidates.sort(
                key=lambda candidate: 0
                if "subf2m" in str(candidate.get("Id") or "").lower()
                else 1
            )
            if not candidates:
                unavailable += 1
                continue
            subtitle_id = urllib.parse.quote(
                str(candidates[0].get("Id") or ""), safe=""
            )
            code, _ = http_json(
                "POST",
                JELLYFIN_URL.rstrip("/")
                + f"/Items/{item_id}/RemoteSearch/Subtitles/{subtitle_id}",
                headers=self._jellyfin_headers(),
                timeout=30,
            )
            if code not in (200, 204):
                failed += 1
                log(
                    f"external subtitle download for {Path(strm).name!r} "
                    f"returned HTTP {code}"
                )
                continue
            # A 204 only means Jellyfin accepted the request. Verify the
            # track or sidecar before counting it, so a no-write provider is
            # retried instead of marking the whole title done.
            uid = self._jellyfin_user_id()
            detail = (
                self._jellyfin_get(f"/Users/{uid}/Items/{item_id}")
                if uid
                else None
            )
            registered = bool(
                detail
                and any(
                    self._has_subtitle(source)
                    for source in detail.get("MediaSources") or []
                )
            )
            sidecar = any(
                Path(strm).with_suffix(suffix).is_file()
                for suffix in (".en.srt", ".en.vtt")
            )
            if not registered and not sidecar:
                failed += 1
                log(
                    f"external subtitle for {Path(strm).name!r} was accepted "
                    "but did not register"
                )
                continue
            downloaded += 1
            log(f"external subtitle saved for {Path(strm).name!r}")

        remaining = (
            waiting + failed + unavailable + max(0, len(missing) - len(batch))
        )
        if remaining <= 0:
            item["subtitles"] = "done"
            item.pop("subtitle_attempts", None)
            item.pop("subtitle_cursor", None)
            log(f"external subtitles complete for {item.get('title')!r}")
            return downloaded > 0
        attempts = (
            0
            if downloaded
            else (_int(item.get("subtitle_attempts"), 0) or 0) + 1
        )
        item["subtitle_attempts"] = attempts
        if attempts >= SUBTITLE_MAX_ATTEMPTS:
            item["subtitles"] = "failed"
            log(
                f"external subtitles: giving up on {item.get('title')!r} "
                f"after {attempts} attempts "
                f"({downloaded} saved, {remaining} still missing)"
            )
        else:
            item["subtitles"] = "pending"
            log(
                f"external subtitles for {item.get('title')!r}: "
                f"{downloaded} saved, {remaining} still missing"
            )
        return downloaded > 0

    def _mark_seerr_available(
        self, request: dict, item: dict, requested_seasons: list[int]
    ) -> bool:
        """Tell Seerr the media is playable; there is no Radarr/Sonarr webhook."""
        if item.get("seerr_status") in ("available", "partial"):
            return False
        media = request.get("media") if isinstance(request.get("media"), dict) else {}
        media_id = _int(media.get("id"))
        if not media_id:
            log(f"seerr media id missing for request {request.get('id')}")
            return False
        if item.get("media_type") == "movie":
            if not self.seerr.media_status(media_id, "available"):
                return False
            item["seerr_status"] = "available"
            return True
        seasons = sorted(
            {
                number
                for number in (
                    _int(value)
                    for value in (item.get("seasons") or requested_seasons)
                )
                if number is not None
            }
        )
        if not seasons:
            log(f"seerr media {media_id}: no seasons to mark available")
            return False
        status = (
            "available"
            if set(seasons).issuperset(set(requested_seasons or seasons))
            else "partial"
        )
        if not self.seerr.media_status(
            media_id, status, seasons if status == "available" else None
        ):
            return False
        item["seerr_status"] = status
        return True

    def _refresh_jellyfin(self) -> None:
        global _refresh_warning
        if not JELLYFIN_API_KEY:
            if not _refresh_warning:
                log("JELLYFIN_API_KEY is not set; relying on Jellyfin's file watcher")
                _refresh_warning = True
            return
        code, _ = http_json(
            "POST",
            JELLYFIN_URL.rstrip("/") + "/Library/Refresh",
            headers={"Authorization": f'MediaBrowser Token="{JELLYFIN_API_KEY}"'},
        )
        if code not in (200, 202, 204):
            log(f"jellyfin refresh returned HTTP {code}")

    def handle(self, request: dict, pending: bool) -> None:
        media = request.get("media") if isinstance(request.get("media"), dict) else {}
        tmdb_id = _int(media.get("tmdbId") or request.get("tmdbId"))
        media_type = request_media_type(request)
        request_id = _int(request.get("id"))
        if request_id is None or not tmdb_id or media_type not in ("movie", "tv"):
            log(f"request {request.get('id')} has no usable media/tmdb id")
            return
        details = self.seerr.details(media_type, tmdb_id)
        if not details:
            return
        requested_seasons = request_seasons(request)
        if media_type == "tv" and not requested_seasons:
            count = details.get("seasons") or 0
            requested_seasons = list(range(1, count + 1))
        details["_seasons"] = requested_seasons
        retries = self.state.setdefault("retries", {})
        retry_key = str(request_id)
        retry = retries.get(retry_key) or {}
        if pending and float(retry.get("at") or 0) > time.time():
            return
        key = f"{media_type}:{tmdb_id}"
        previous = self.state["items"].get(key) or {}
        if not pending and previous.get("kind") == "anime" and previous.get("slug"):
            if self._mark_seerr_available(request, previous, requested_seasons):
                self._save_state()
            return
        materialized = set(previous.get("seasons") or [])
        if (
            not pending
            and previous.get("files")
            and all(Path(item).is_file() for item in previous["files"])
            and (
                media_type == "movie"
                or set(requested_seasons).issubset(materialized)
                or (
                    previous.get("kind") != "pressplayz"
                    and not self._has_unmaterialized_season(
                        tmdb_id, requested_seasons, materialized
                    )
                )
            )
        ):
            if self._mark_seerr_available(request, previous, requested_seasons):
                self._save_state()
            return
        if media_type == "tv":
            details["_seasons"] = sorted(
                set(previous.get("seasons") or []) | set(requested_seasons)
            )
        result = self._anime(details)
        if not result:
            result = self._pressplayz(details)
        if not result:
            result = self._vprint(media_type, tmdb_id, details)
        if result:
            retries.pop(retry_key, None)
            result = self._with_subtitle_state(result, previous)
        if not result:
            if pending:
                attempts = _int(retry.get("attempts"), 0)
                if attempts < RETRY_ATTEMPTS:
                    retries[retry_key] = {
                        "at": time.time() + RETRY_SECONDS,
                        "attempts": attempts + 1,
                    }
                    self._save_state()
                    log(
                        f"no match for {media_type} {tmdb_id}; retry "
                        f"{attempts + 1}/{RETRY_ATTEMPTS} in {RETRY_SECONDS}s"
                    )
                else:
                    retries.pop(retry_key, None)
                    self._save_state()
                    if self.seerr.decline(request_id):
                        log(
                            f"declined {media_type} {tmdb_id}: no "
                            f"anime/Dispatcharr match after {attempts} retries"
                        )
            else:
                log(f"approved {media_type} {tmdb_id}: no anime/Dispatcharr match")
            return
        if previous.get("files"):
            self._prune(previous["files"], result.get("files") or [])
        previous_files = previous.get("files") or []
        self.state["items"][key] = result
        self._save_state()
        if result != previous or not all(
            Path(item).is_file() for item in previous_files
        ):
            self._refresh_jellyfin()
        if pending and not self.seerr.approve(request_id):
            return
        if self._mark_seerr_available(request, result, requested_seasons):
            self._save_state()
        log(
            f"{'approved' if pending else 'materialized'} {media_type} {tmdb_id}: "
            f"{result.get('title')!r} ({result.get('kind')})"
        )

    def ensure_pending_subtitles(self) -> None:
        """Backfill subtitles even when Seerr no longer lists the request."""
        if self.state.get("subtitles_version") != SUBTITLE_STATE_VERSION:
            # OpenSubtitles can now resolve titles Subf2m lacks; revalidate
            # done entries and retry the ones that had already given up.
            for item in self.state["items"].values():
                if not self._strm_files(item):
                    continue
                item.pop("subtitle_attempts", None)
                item.pop("subtitle_cursor", None)
                if item.get("subtitles") in ("done", "failed"):
                    item["subtitles"] = "pending"
            self.state["subtitles_version"] = SUBTITLE_STATE_VERSION
            self._save_state()
        refreshed = False
        for item in list(self.state["items"].values()):
            if not self._strm_files(item):
                continue
            if item.get("subtitles") not in (None, "pending"):
                continue
            if item.get("subtitles") is None:
                item["subtitles"] = "pending"
            downloaded = self._ensure_external_subtitles(item)
            self._save_state()
            refreshed = refreshed or downloaded
        if refreshed:
            self._refresh_jellyfin()

    def reconcile(self) -> None:
        changed = False
        for key, item in list(self.state["items"].items()):
            if item.get("kind") != "vod":
                continue
            media_type = item.get("media_type")
            tmdb_id = _int(item.get("tmdb_id"))
            if media_type not in ("movie", "tv") or not tmdb_id:
                continue
            details = self.seerr.details(media_type, tmdb_id)
            if not details:
                continue
            details["_seasons"] = list(item.get("seasons") or [])
            result = self._anime(details)
            if not result:
                result = self._pressplayz(details)
            if result:
                # The request was fulfilled from VOD before a preferred source
                # was available; replace those files with the new selection.
                self.state["items"][key] = result
                self._save_state()
                self._prune(
                    item.get("files") or [], result.get("files") or []
                )
                changed = True
                detail = result.get("slug") or result.get("kind")
                log(
                    f"reconcile: moved {media_type} {tmdb_id} to "
                    f"{result.get('kind')} {detail!r}"
                )
                continue
            result = self._vprint(media_type, tmdb_id, details)
            if not result:
                log(f"reconcile: {media_type} {tmdb_id} is no longer in Dispatcharr")
                continue
            result = self._with_subtitle_state(result, item)
            self._prune(item.get("files") or [], result.get("files") or [])
            if result != item:
                self.state["items"][key] = result
                self._save_state()
                changed = True
        if changed:
            self._refresh_jellyfin()


def setup_django() -> None:
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "dispatcharr.settings")
    import django  # type: ignore[import-not-found]

    django.setup()


def main() -> int:
    if not SEERR_API_KEY:
        log("SEERR_API_KEY is not set; waiting for Seerr setup")
        while True:
            time.sleep(60)
    setup_django()
    fulfiller = Fulfiller()
    last_reconcile = 0.0
    log(
        "fulfiller started; "
        f"poll={POLL_SECONDS}s reconcile={RECONCILE_SECONDS}s library={LIBRARY_DIR}"
    )
    while True:
        try:
            if time.time() - last_reconcile >= RECONCILE_SECONDS:
                fulfiller.reconcile()
                last_reconcile = time.time()
            for request in fulfiller.seerr.requests("pending"):
                fulfiller.handle(request, pending=True)
            for request in fulfiller.seerr.requests("approved"):
                fulfiller.handle(request, pending=False)
            fulfiller.ensure_pending_subtitles()
        except Exception as exc:  # keep the loop alive
            log(f"unhandled error: {type(exc).__name__}: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
