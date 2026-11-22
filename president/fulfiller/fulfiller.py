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
LIBRARY_DIR = Path(os.environ.get("LIBRARY_DIR", "/library"))
STATE_DIR = Path(os.environ.get("STATE_DIR", "/state"))
STATE_FILE = STATE_DIR / "state.json"
POLL_SECONDS = max(10, int(os.environ.get("POLL_SECONDS", "30")))
RECONCILE_SECONDS = max(300, int(os.environ.get("RECONCILE_SECONDS", "21600")))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "30"))
SUBTITLE_LANGUAGE = os.environ.get("SUBTITLE_LANGUAGE", "eng").strip() or "eng"

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
        return {
            "media_type": media_type,
            "title": str(title).strip(),
            "original_title": str(original).strip(),
            "year": year,
            "tmdb_id": int(tmdb_id),
            "seasons": _int(data.get("numberOfSeasons") or data.get("number_of_seasons")),
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


class Fulfiller:
    def __init__(self):
        self.seerr = Seerr(SEERR_URL, SEERR_API_KEY)
        self.vod = VodCatalog()
        self.anime = Anime(ANIME_LIBRARY_URL, ANIME_LANG)
        self.state = self._load_state()
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
                pass

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
    def _subtitle_sidecar(strm_path) -> str:
        return str(Path(str(strm_path)).with_suffix(".en.srt"))

    def _missing_sidecars(self, item: dict) -> list[str]:
        return [
            str(path)
            for path in item.get("files") or []
            if str(path).endswith(".strm")
            and not Path(self._subtitle_sidecar(path)).is_file()
        ]

    def _with_subtitle_state(self, result, previous: dict):
        """Carry the subtitle work state without making every poll look changed."""
        if not result or result.get("kind") != "vod":
            return result
        old_status = previous.get("subtitles")
        files_changed = previous.get("files") != result.get("files")
        attempts = _int(previous.get("subtitle_attempts"), 0) or 0
        if self._missing_sidecars(result):
            if old_status == "failed" and not files_changed:
                result["subtitles"] = "failed"
                result["subtitle_attempts"] = attempts
            else:
                result["subtitles"] = "pending"
                result["subtitle_attempts"] = 0 if files_changed else attempts
        elif old_status:
            result["subtitles"] = "done"
            result.pop("subtitle_attempts", None)
        return result

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
            episode_query = urllib.parse.urlencode(
                {"userId": uid, "fields": fields}
            )
            episodes = self._jellyfin_get(
                f"/Shows/{series.get('Id')}/Episodes?{episode_query}"
            )
            for episode in (episodes or {}).get("Items") or []:
                if Path(str(episode.get("Path") or "")).name in names:
                    result.append(episode)
        return result

    def _ensure_external_subtitles(self, item: dict) -> bool:
        """Download one external subtitle for each new .strm, if Jellyfin can."""
        if item.get("subtitles") != "pending":
            return False
        if not JELLYFIN_API_KEY:
            item["subtitles"] = "disabled"
            if not _refresh_warning:
                log("JELLYFIN_API_KEY is not set; cannot fetch external subtitles")
            return False
        media_items = self._jellyfin_media_items(item)
        if not media_items:
            attempts = (_int(item.get("subtitle_attempts"), 0) or 0) + 1
            item["subtitle_attempts"] = attempts
            if attempts >= 4:
                item["subtitles"] = "failed"
                log(f"external subtitles: Jellyfin never listed {item.get('title')!r}")
            return False
        by_name = {
            Path(str(media.get("Path") or "")).name: media
            for media in media_items
        }
        expected = [
            str(path)
            for path in item.get("files") or []
            if str(path).endswith(".strm")
        ]
        downloaded = 0
        unavailable = 0
        waiting = 0
        for strm in expected:
            sidecar = self._subtitle_sidecar(strm)
            if Path(sidecar).is_file():
                continue
            media = by_name.get(Path(strm).name)
            if not media:
                waiting += 1
                continue
            # A deleted sidecar can still be listed as an external stream until
            # the next scan, so the file on disk is the source of truth here.
            item_id = media.get("Id")
            search_path = f"/Items/{item_id}/RemoteSearch/Subtitles/{SUBTITLE_LANGUAGE}"
            code, candidates = http_json(
                "GET",
                JELLYFIN_URL.rstrip("/") + search_path,
                headers=self._jellyfin_headers(),
                timeout=30,
            )
            if code != 200 or not isinstance(candidates, list):
                waiting += 1
                continue
            candidates = [
                candidate
                for candidate in candidates
                if "subf2m"
                in str(candidate.get("Id") or "").lower()
            ]
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
                waiting += 1
                log(
                    f"external subtitle download for {Path(strm).name!r} "
                    f"returned HTTP {code}"
                )
                continue
            downloaded += 1
            log(f"external subtitle saved for {Path(strm).name!r}")
        if waiting:
            attempts = (_int(item.get("subtitle_attempts"), 0) or 0) + 1
            item["subtitle_attempts"] = attempts
            if attempts >= 4:
                item["subtitles"] = "failed"
                log(
                    f"external subtitles: gave up on {item.get('title')!r} "
                    f"after {attempts} attempts ({waiting} items waiting)"
                )
            return downloaded > 0
        item["subtitles"] = "done"
        item.pop("subtitle_attempts", None)
        if downloaded or unavailable:
            log(
                f"external subtitles for {item.get('title')!r}: "
                f"{downloaded} saved, {unavailable} unavailable"
            )
        return downloaded > 0

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
        key = f"{media_type}:{tmdb_id}"
        previous = self.state["items"].get(key) or {}
        if not pending and previous.get("kind") == "anime" and previous.get("slug"):
            return
        materialized = set(previous.get("seasons") or [])
        if (
            not pending
            and previous.get("files")
            and all(Path(item).is_file() for item in previous["files"])
            and (
                media_type == "movie"
                or set(requested_seasons).issubset(materialized)
                or not self._has_unmaterialized_season(
                    tmdb_id, requested_seasons, materialized
                )
            )
        ):
            if previous.get("subtitles") == "pending":
                if self._ensure_external_subtitles(previous):
                    self._save_state()
                    self._refresh_jellyfin()
                else:
                    self._save_state()
            return
        if media_type == "tv":
            details["_seasons"] = sorted(
                set(previous.get("seasons") or []) | set(requested_seasons)
            )
        result = self._anime(details)
        if not result:
            result = self._vprint(media_type, tmdb_id, details)
        if result:
            result = self._with_subtitle_state(result, previous)
        if not result:
            if pending:
                if self.seerr.decline(request_id):
                    log(
                        f"declined {media_type} {tmdb_id}: no anime/Dispatcharr match"
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
        log(
            f"{'approved' if pending else 'materialized'} {media_type} {tmdb_id}: "
            f"{result.get('title')!r} ({result.get('kind')})"
        )

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
            if result:
                # The request was fulfilled from VOD before the anime site was
                # preferred; replace those files with the anime selection.
                self.state["items"][key] = result
                self._save_state()
                self._prune(item.get("files") or [], [])
                changed = True
                log(
                    f"reconcile: moved {media_type} {tmdb_id} to anime "
                    f"{result.get('slug')!r} ({self.anime.lang})"
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
        except Exception as exc:  # keep the loop alive
            log(f"unhandled error: {type(exc).__name__}: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
