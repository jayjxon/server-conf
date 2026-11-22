#!/usr/bin/env python3
"""CineCat 1080p library adapter for Jellyfin.

It resolves a TMDB id through CineCat's ungated Aphrodite HLS lane, checks that
the master has a 1080p H.264 rendition, and writes Jellyfin-ready `.strm` files.
The URLs in those files point back here; this service resolves a fresh signed
manifest on playback and proxies its segments with CineCat's Referer/Origin.

The service deliberately does not write anything unless a 1080p H.264 rendition
is present. Sources below that are left for the Dispatcharr VOD fallback.

Wire contract:

    POST /api/select
      {tmdb_id, media_type, title, year, seasons, season_counts}
    GET  /play/movie/{tmdb}
    GET  /play/tv/{tmdb}/{season}/{episode}
    GET  /p/{signed-token}

Only the Python standard library is used.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape as xml_escape

DEFAULT_PORT = 8788
DEFAULT_API_BASE = "https://cdn.hls.lol"
DEFAULT_SITE_URL = "https://beta.cinecat.eu"
DEFAULT_SUBS_BASE = "https://sub.vdrk.site"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)
FILENAME_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
URI_ATTR_RE = re.compile(r"""URI=(?:"([^"]+)"|'([^']+)')""")
_RESOLUTION_RE = re.compile(r"\bRESOLUTION=(\d+)x(\d+)\b", re.I)
_BANDWIDTH_RE = re.compile(r"\bBANDWIDTH=(\d+)\b", re.I)


class CinecatError(RuntimeError):
    """CineCat answered, but not with something this adapter can use."""


class NoSource(CinecatError):
    """No 1080p H.264 source exists for the requested title/episode."""


def safe_name(value, limit: int = 120) -> str:
    text = FILENAME_BAD_RE.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .") or "Untitled"


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temp, path)


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return (
        f"{parts.scheme}://{parts.netloc}/"
        if parts.scheme and parts.netloc
        else url
    )


class Source:
    """The ungated CineCat content lookup plus its HLS master."""

    def __init__(
        self,
        api_base: str = DEFAULT_API_BASE,
        site_url: str = DEFAULT_SITE_URL,
        subs_base: str = DEFAULT_SUBS_BASE,
        timeout: float = 20.0,
    ):
        self.api_base = api_base.rstrip("/")
        self.site_url = site_url.rstrip("/")
        self.subs_base = subs_base.rstrip("/")
        self.timeout = float(timeout)

    def _headers(self, accept: str = "application/json") -> dict:
        return {
            "Accept": accept,
            "Accept-Encoding": "identity",
            "User-Agent": USER_AGENT,
            "Referer": self.site_url + "/",
            "Origin": self.site_url,
        }

    def _allowed_url(self, url: str) -> bool:
        """Only HTTPS CineCat/CDN hosts may be fetched by this service."""
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host:
            return False
        api_host = (urlsplit(self.api_base).hostname or "").lower()
        suffixes = ("totallyacdn.cc", "vdrk.site", "hls.lol")
        return host == api_host or any(
            host == suffix or host.endswith("." + suffix)
            for suffix in suffixes
        )

    def _fetch(self, url: str, accept: str = "application/json", limit: int = 0):
        if not self._allowed_url(url):
            raise CinecatError(f"blocked untrusted upstream URL: {url}")
        request = Request(url, headers=self._headers(accept))
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = response.read(limit) if limit else response.read()
                return response.status, response.headers, body
        except HTTPError as exc:
            if exc.code == 404:
                raise NoSource(f"HTTP 404 for {url}") from exc
            raise CinecatError(f"HTTP {exc.code} for {url}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise CinecatError(f"{type(exc).__name__} for {url}: {exc}") from exc

    def _json(self, url: str):
        _, _, body = self._fetch(url)
        try:
            payload = json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise CinecatError(f"expected JSON from {url}") from exc
        return payload

    def _text(self, url: str) -> str:
        _, _, body = self._fetch(url, "application/vnd.apple.mpegurl,*/*")
        return body.decode("utf-8", "replace")

    def _lookup(self, media_type: str, tmdb_id: int, season=None, episode=None) -> str:
        if media_type == "movie":
            path = f"/content/movie/{int(tmdb_id)}"
        elif media_type == "tv":
            if season is None or episode is None:
                raise NoSource("season and episode are required for TV")
            path = f"/content/tv/{int(tmdb_id)}/{int(season)}/{int(episode)}"
        else:
            raise NoSource(f"unsupported media type {media_type!r}")
        data = self._json(self.api_base + path)
        if not isinstance(data, dict) or not data.get("found"):
            raise NoSource(f"no CineCat source for {path}")
        url = str(data.get("url") or "").strip()
        if not url:
            raise NoSource(f"CineCat returned no URL for {path}")
        if str(data.get("type") or "hls").lower() != "hls":
            raise NoSource(f"CineCat returned a non-HLS source for {path}")
        return url

    def choose_1080p(self, media_type: str, tmdb_id: int, season=None, episode=None) -> str:
        """Return the best 1080p H.264 variant, or raise NoSource."""
        master_url = self._lookup(media_type, tmdb_id, season, episode)
        text = self._text(master_url)
        if not text.lstrip().startswith("#EXTM3U"):
            raise NoSource(f"not an HLS master: {master_url}")

        lines = text.splitlines()
        candidates = []
        for index, line in enumerate(lines):
            if not line.startswith("#EXT-X-STREAM-INF:"):
                continue
            if "avc1" not in line.lower():
                continue
            resolution = _RESOLUTION_RE.search(line)
            if not resolution or int(resolution.group(2)) != 1080:
                continue
            variant = None
            for following in lines[index + 1 :]:
                following = following.strip()
                if not following or following.startswith("#"):
                    continue
                variant = following
                break
            if not variant:
                continue
            bandwidth = _BANDWIDTH_RE.search(line)
            candidates.append(
                (
                    int(bandwidth.group(1)) if bandwidth else 0,
                    urljoin(master_url, variant),
                )
            )
        if not candidates:
            raise NoSource(
                f"no 1080p H.264 variant for {media_type} {tmdb_id}"
                + (f" S{int(season):02d}E{int(episode):02d}" if season and episode else "")
            )
        variant_url = max(candidates, key=lambda item: item[0])[1]
        playlist = self._text(variant_url)
        if "#EXTINF" not in playlist:
            raise NoSource(f"1080p playlist has no segments: {variant_url}")
        return variant_url

    def english_vtt(self, media_type: str, tmdb_id: int, season=None, episode=None):
        """Return the first English VTT track, if CineCat has one."""
        if not self.subs_base:
            return None
        if media_type == "movie":
            path = f"/v1/movie/{int(tmdb_id)}"
        elif media_type == "tv":
            path = f"/v1/tv/{int(tmdb_id)}/{int(season or 1)}/{int(episode or 1)}"
        else:
            return None
        try:
            tracks = self._json(self.subs_base + path)
        except CinecatError:
            return None
        if not isinstance(tracks, list):
            return None
        chosen = None
        for track in tracks:
            if not isinstance(track, dict):
                continue
            label = str(track.get("label") or "").strip().lower()
            url = str(track.get("file") or "").strip()
            if url and (label == "english" or (chosen is None and label.startswith("english"))):
                chosen = url
                if label == "english":
                    break
        if not chosen:
            return None
        # The movie endpoint returns absolute cache URLs; the TV endpoint can
        # return a site-relative path containing spaces. Normalise before the
        # standard library builds the request.
        chosen = quote(
            urljoin(self.subs_base + "/", chosen), safe=":/?&=%._-~"
        )
        try:
            _, _, body = self._fetch(chosen, "*/*", limit=2_000_000)
        except CinecatError:
            return None
        if body.startswith(b"\xef\xbb\xbf"):
            body = body[3:]
        head = body.lstrip()[:128]
        if not (head.startswith(b"WEBVTT") or b"-->" in head):
            return None
        return body


class Signer:
    """Tamper-proof tokens carrying an upstream URL and its Referer."""

    def __init__(self, secret_path: Path):
        self.path = Path(secret_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            secret = self.path.read_bytes()
        except OSError:
            secret = b""
        if not secret:
            secret = secrets.token_bytes(32)
            temp = self.path.with_name(self.path.name + ".tmp")
            temp.write_bytes(secret)
            os.replace(temp, self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        self.secret = secret

    def dumps(self, payload: dict) -> str:
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        body = base64.urlsafe_b64encode(raw.encode("utf-8")).decode().rstrip("=")
        signature = hmac.new(
            self.secret, body.encode("ascii"), hashlib.sha256
        ).digest()
        return body + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    def loads(self, token: str) -> dict:
        try:
            body, encoded = token.split(".", 1)
            expected = hmac.new(
                self.secret, body.encode("ascii"), hashlib.sha256
            ).digest()
            actual = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if not hmac.compare_digest(expected, actual):
                raise ValueError("bad signature")
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid proxy token") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("u"), str):
            raise ValueError("invalid proxy token")
        return payload


class LibraryWriter:
    """Writes CineCat entries into the existing Jellyfin request tree."""

    def __init__(self, media_root: Path, public_url: str, state_path: Path):
        self.root = Path(media_root).expanduser().resolve() / "requests"
        self.public_url = public_url.rstrip("/")
        self.state_path = Path(state_path)
        self.lock = threading.Lock()
        self.root.mkdir(parents=True, exist_ok=True)
        state = _read_json(self.state_path)
        self.state = state if isinstance(state, dict) else {}
        if not isinstance(self.state.get("selections"), dict):
            self.state["selections"] = {}

    def _save(self) -> None:
        _atomic_json(self.state_path, self.state)

    def _within_root(self, path: Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.root)
            return True
        except (OSError, ValueError):
            return False

    def _write(self, path: Path, body) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        if isinstance(body, bytes):
            temp.write_bytes(body)
        else:
            temp.write_text(str(body), encoding="utf-8")
        os.replace(temp, path)

    def _cleanup(self, files, dirs) -> None:
        for name in files:
            path = Path(name)
            if self._within_root(path):
                try:
                    path.unlink()
                except OSError:
                    pass
        for name in sorted(dirs, key=len, reverse=True):
            path = Path(name)
            if not self._within_root(path):
                continue
            try:
                path.rmdir()
            except OSError:
                pass

    def _remove(self, selection: dict) -> None:
        self._cleanup(selection.get("files") or [], selection.get("dirs") or [])

    def _strm(self, path: Path, url: str) -> None:
        self._write(path, url.rstrip("/") + "\n")

    def rewrite_stream_urls(self, selections) -> int:
        """Repoint existing .strm files after a public URL/path change."""
        rewritten = 0
        for selection in selections:
            if not isinstance(selection, dict):
                continue
            media_type = str(selection.get("media_type") or "")
            tmdb_id = _int(selection.get("tmdb_id"))
            if media_type not in ("movie", "tv") or not tmdb_id:
                continue
            for name in selection.get("files") or []:
                path = Path(name)
                if path.suffix != ".strm" or not self._within_root(path):
                    continue
                if media_type == "movie":
                    url = f"{self.public_url}/play/movie/{tmdb_id}"
                else:
                    match = re.search(r" - S(\d+)E(\d+) - ", path.name)
                    if not match:
                        continue
                    url = (
                        f"{self.public_url}/play/tv/{tmdb_id}/"
                        f"{int(match.group(1))}/{int(match.group(2))}"
                    )
                try:
                    current = path.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                if current != url:
                    self._strm(path, url)
                    rewritten += 1
        return rewritten

    def _tags(self, data: dict) -> str:
        output = []
        for key, value in data.items():
            if value is not None and value != "":
                output.append(f"  <{key}>{xml_escape(str(value))}</{key}>")
        return "\n".join(output)

    def _movie_nfo(self, title: str, year, tmdb_id: int) -> str:
        return (
            "<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\"?>\n"
            "<movie>\n"
            + self._tags(
                {
                    "title": title,
                    "year": year,
                    "tmdbid": tmdb_id,
                    "uniqueid": tmdb_id,
                }
            )
            + "\n</movie>\n"
        )

    def _show_nfo(self, title: str, year, tmdb_id: int) -> str:
        return (
            "<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\"?>\n"
            "<tvshow>\n"
            + self._tags({"title": title, "year": year, "tmdbid": tmdb_id})
            + "\n</tvshow>\n"
        )

    @staticmethod
    def _episode_nfo(title: str, season: int, episode: int) -> str:
        return (
            "<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\"?>\n"
            "<episodedetails>\n"
            f"  <title>{xml_escape(title)}</title>\n"
            f"  <season>{int(season)}</season>\n"
            f"  <episode>{int(episode)}</episode>\n"
            "</episodedetails>\n"
        )

    def _sidecar(self, strm_path: Path, source: Source, media_type: str,
                 tmdb_id: int, season=None, episode=None) -> list[str]:
        body = source.english_vtt(media_type, tmdb_id, season, episode)
        if not body:
            return []
        path = strm_path.with_suffix(".en.vtt")
        self._write(path, body)
        return [str(path)]

    def write(self, fields: dict, source: Source) -> dict:
        media_type = fields["media_type"]
        tmdb_id = int(fields["tmdb_id"])
        title = str(fields["title"]).strip() or f"TMDB {tmdb_id}"
        year = _int(fields.get("year"))
        label = safe_name(title)
        if year:
            label = f"{label} ({year})"
        key = f"{media_type}:{tmdb_id}"
        seasons = sorted(
            {
                number
                for number in (_int(value) for value in fields.get("seasons") or [])
                if number is not None
            }
        )
        counts = {
            _int(key): _int(value)
            for key, value in (fields.get("season_counts") or {}).items()
            if _int(key) is not None and _int(value)
        }
        if media_type == "tv" and not seasons:
            raise ValueError("TV selection requires at least one season")

        with self.lock:
            old = self.state["selections"].get(key)
            if old:
                self._remove(old)
            files, dirs = [], set()
            try:
                if media_type == "movie":
                    folder = self.root / "Movies" / f"{label} [tmdbid-{tmdb_id}]"
                    dirs.add(str(folder))
                    source.choose_1080p("movie", tmdb_id)
                    strm = folder / f"{label}.strm"
                    nfo = folder / f"{label}.nfo"
                    self._strm(
                        strm, f"{self.public_url}/play/movie/{tmdb_id}"
                    )
                    self._write(nfo, self._movie_nfo(title, year, tmdb_id))
                    files.extend([str(strm), str(nfo)])
                    files.extend(
                        self._sidecar(strm, source, "movie", tmdb_id)
                    )
                else:
                    folder = self.root / "Shows" / f"{label} [tmdbid-{tmdb_id}]"
                    dirs.add(str(folder))
                    self._write(folder / "tvshow.nfo", self._show_nfo(title, year, tmdb_id))
                    files.append(str(folder / "tvshow.nfo"))
                    for season in seasons:
                        count = counts.get(season)
                        if not count:
                            raise ValueError(
                                f"missing episodeCount for season {season}"
                            )
                        season_dir = folder / f"Season {season:02d}"
                        dirs.add(str(season_dir))
                        for episode in range(1, count + 1):
                            source.choose_1080p(
                                "tv", tmdb_id, season=season, episode=episode
                            )
                            stem = (
                                f"{safe_name(title)} - S{season:02d}E{episode:02d} - "
                                f"Episode {episode}"
                            )
                            strm = season_dir / f"{stem}.strm"
                            nfo = season_dir / f"{stem}.nfo"
                            self._strm(
                                strm,
                                f"{self.public_url}/play/tv/{tmdb_id}/"
                                f"{season}/{episode}",
                            )
                            self._write(
                                nfo,
                                self._episode_nfo(
                                    f"Episode {episode}", season, episode
                                ),
                            )
                            files.extend([str(strm), str(nfo)])
                            files.extend(
                                self._sidecar(
                                    strm,
                                    source,
                                    "tv",
                                    tmdb_id,
                                    season=season,
                                    episode=episode,
                                )
                            )
            except Exception:
                self._cleanup(files, dirs)
                raise
            selection = {
                "id": f"{key}:{int(time.time())}",
                "kind": "cinecat",
                "media_type": media_type,
                "tmdb_id": tmdb_id,
                "title": title,
                "year": year,
                "seasons": seasons,
                "files": files,
                "dirs": sorted(dirs, key=len),
                "created": time.time(),
                "updated": time.time(),
            }
            self.state["selections"][key] = selection
            self._save()
            return selection


class App:
    def __init__(self, source: Source, writer: LibraryWriter, data_dir: Path):
        self.source = source
        self.writer = writer
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.signer = Signer(self.data_dir / "proxy.secret")
        self._resolve_lock = threading.Lock()
        self._resolve_cache = {}
        self.timeout = source.timeout
        rewritten = self.writer.rewrite_stream_urls(
            self.writer.state["selections"].values()
        )
        if rewritten:
            self.log(
                f"rewrote {rewritten} .strm URLs to {self.writer.public_url}"
            )

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {message}", file=sys.stderr, flush=True)

    def select(self, payload: dict) -> dict:
        media_type = str(payload.get("media_type") or "").strip().lower()
        if media_type not in ("movie", "tv"):
            raise ValueError("media_type must be movie or tv")
        tmdb_id = _int(payload.get("tmdb_id"))
        if not tmdb_id:
            raise ValueError("tmdb_id is required")
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        seasons = payload.get("seasons") or []
        counts = payload.get("season_counts") or {}
        fields = {
            "media_type": media_type,
            "tmdb_id": tmdb_id,
            "title": title,
            "year": _int(payload.get("year")),
            "seasons": seasons,
            "season_counts": counts,
        }
        return self.writer.write(fields, self.source)

    def _resolve(self, media_type: str, tmdb_id: int, season=None, episode=None):
        key = (media_type, int(tmdb_id), _int(season), _int(episode))
        now = time.time()
        with self._resolve_lock:
            cached = self._resolve_cache.get(key)
            if cached and now - cached[0] < 120:
                return cached[1], cached[2]
        variant = self.source.choose_1080p(
            media_type, tmdb_id, season=season, episode=episode
        )
        referer = self.source.site_url + "/"
        with self._resolve_lock:
            self._resolve_cache[key] = (now, variant, referer)
        return variant, referer

    def manifest(self, media_type: str, tmdb_id: int, season=None, episode=None) -> str:
        variant, referer = self._resolve(media_type, tmdb_id, season, episode)
        text = self.source._text(variant)
        if not text.lstrip().startswith("#EXTM3U"):
            raise CinecatError(f"not an HLS playlist: {variant}")
        return self.rewrite_manifest(text, variant, referer)

    def rewrite_manifest(self, text: str, base_url: str, referer: str) -> str:
        public = self.writer.public_url.rstrip("/")

        def proxy_url(value: str, suffix: str = "") -> str:
            absolute = urljoin(base_url, value)
            token = self.signer.dumps({"u": absolute, "r": referer})
            return f"{public}/p/{token}{suffix}"

        output = []
        for line in text.splitlines():
            if not line:
                output.append(line)
                continue
            if line.startswith("#"):
                if line.startswith("#EXT-X-KEY"):
                    suffix = ""
                elif line.startswith("#EXT-X-MAP"):
                    suffix = ".mp4"
                elif line.startswith("#EXT-X-MEDIA"):
                    suffix = ".vtt" if 'TYPE="SUBTITLES"' in line else ".m3u8"
                else:
                    suffix = ".m3u8"
                output.append(
                    URI_ATTR_RE.sub(
                        lambda match: 'URI="'
                        + proxy_url(
                            match.group(1) or match.group(2),
                            suffix,
                        )
                        + '"',
                        line,
                    )
                )
            else:
                output.append(proxy_url(line.strip(), ".ts"))
        return "\n".join(output) + "\n"


class AppServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, app: App):
        self.app = app
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError)):
            return
        self.app.log(
            f"request from {client_address[0]} failed: {type(exc).__name__}: {exc}"
        )


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "CinecatLibrary/1.0"

    def log_message(self, fmt, *args):
        if getattr(self.server.app, "verbose", False):
            super().log_message(fmt, *args)

    def _send_bytes(self, status: int, body: bytes, content_type: str,
                    head_only: bool = False, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        try:
            self.end_headers()
            if not head_only:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            head_only=(self.command == "HEAD"),
        )

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ValueError("invalid Content-Length") from exc
        if length <= 0:
            return {}
        if length > 1_000_000:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise ValueError("body must be JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        return payload

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        app = self.server.app
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            if self.command in ("GET", "HEAD"):
                if path == "/api/health":
                    return self._json(200, {"ok": True})
                if path == "/api/config":
                    return self._json(
                        200,
                        {
                            "api_base": app.source.api_base,
                            "site_url": app.source.site_url,
                            "public_url": app.writer.public_url,
                            "media_root": str(app.writer.root),
                            "min_height": 1080,
                        },
                    )
                if path == "/api/selected":
                    return self._json(
                        200,
                        {"selections": list(app.writer.state["selections"].values())},
                    )
                if path.startswith("/play/"):
                    return self._play(path)
                if path.startswith("/p/"):
                    return self._proxy(path[3:])
            if self.command == "POST" and path == "/api/select":
                return self._json(
                    200,
                    {"selection": app.select(self._body())},
                )
            return self._error(404, "not found")
        except NoSource as exc:
            app.log(f"no 1080p source for {self.path}: {exc}")
            self._error(404, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except (CinecatError, OSError) as exc:
            app.log(f"request failed: {exc}")
            self._error(502, str(exc))
        except Exception as exc:  # pragma: no cover
            app.log(f"unhandled request error: {type(exc).__name__}: {exc}")
            try:
                self._error(500, "internal server error")
            except Exception:
                pass

    def _play(self, path: str):
        app = self.server.app
        parts = [part for part in path.split("/") if part]
        if len(parts) == 3 and parts[1] == "movie":
            media_type, tmdb_id = "movie", _int(parts[2])
            season = episode = None
        elif len(parts) == 5 and parts[1] == "tv":
            media_type, tmdb_id = "tv", _int(parts[2])
            season, episode = _int(parts[3]), _int(parts[4])
        else:
            return self._error(400, "expected /play/movie/<tmdb> or /play/tv/<tmdb>/<season>/<episode>")
        if not tmdb_id or (media_type == "tv" and (season is None or episode is None)):
            return self._error(400, "invalid playback coordinates")
        text = app.manifest(media_type, tmdb_id, season=season, episode=episode)
        self._send_bytes(
            200,
            text.encode("utf-8"),
            "application/vnd.apple.mpegurl",
            head_only=(self.command == "HEAD"),
            cache="no-cache",
        )

    def _proxy(self, token: str):
        app = self.server.app
        raw_path = urlsplit(self.path).path
        segment = raw_path.endswith(".ts")
        clean = re.sub(r"\.(?:m3u8|ts|m4s|mp4|vtt|key)$", "", token)
        try:
            payload = app.signer.loads(clean)
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._proxy_url(
            payload["u"], payload.get("r") or "", segment=segment
        )

    def _proxy_url(self, url: str, referer: str, segment: bool = False):
        app = self.server.app
        if not app.source._allowed_url(url):
            return self._error(403, "upstream host not allowed")
        head_only = self.command == "HEAD"
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Referer": referer or app.source.site_url + "/",
        }
        if referer:
            headers["Origin"] = _origin(referer).rstrip("/")
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]
        try:
            response = urlopen(
                Request(url, headers=headers, method="GET"), timeout=app.timeout
            )
        except HTTPError as exc:
            body = exc.read(4000) or b""
            content_type = exc.headers.get("Content-Type") or "text/plain"
            return self._send_bytes(exc.code, body, content_type, head_only=head_only)
        except (URLError, OSError) as exc:
            return self._error(502, f"upstream fetch failed: {exc}")

        with response:
            final_url = response.geturl() or url
            status = response.status
            upstream_headers = response.headers
            content_type = (
                upstream_headers.get("Content-Type") or "application/octet-stream"
            )
            if head_only:
                self._relay_headers(
                    status,
                    upstream_headers,
                    content_type="video/mp2t" if segment else content_type,
                )
                return
            first = response.read(64 * 1024)
            likely_manifest = (
                urlsplit(final_url).path.lower().endswith(".m3u8")
                or "mpegurl" in content_type.lower()
            )
            if likely_manifest or first.lstrip().startswith(b"#EXTM3U"):
                raw = first + response.read()
                encoding = (upstream_headers.get("Content-Encoding") or "").lower()
                if encoding == "gzip":
                    try:
                        raw = gzip.decompress(raw)
                    except OSError:
                        pass
                elif encoding == "deflate":
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        pass
                text = raw.decode("utf-8", "replace")
                if text.lstrip().startswith("#EXTM3U"):
                    rewritten = app.rewrite_manifest(text, final_url, referer)
                    body = rewritten.encode("utf-8")
                    return self._send_bytes(
                        status,
                        body,
                        "application/vnd.apple.mpegurl",
                        cache="no-cache",
                    )
                return self._send_bytes(status, raw, content_type)
            self._relay_headers(
                status,
                upstream_headers,
                content_type="video/mp2t" if segment else content_type,
            )
            if first:
                self.wfile.write(first)
            try:
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _relay_headers(self, status: int, headers, content_type: str = None) -> None:
        self.send_response(status)
        content_length = headers.get("Content-Length")
        for name in (
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
            "Last-Modified",
            "ETag",
            "Cache-Control",
            "Content-Encoding",
        ):
            value = headers.get(name)
            if value:
                self.send_header(name, value)
        self.send_header(
            "Content-Type", content_type or headers.get("Content-Type") or "application/octet-stream"
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        if not content_length:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()


def _env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Resolve CineCat 1080p HLS into a Jellyfin request library."
    )
    parser.add_argument(
        "--media-dir",
        default=_env("CINECAT_MEDIA_DIR", "JELLYFIN_MEDIA_DIR", default="./cinecat-media"),
    )
    parser.add_argument(
        "--public-url",
        default=_env("CINECAT_PUBLIC_URL"),
    )
    parser.add_argument(
        "--data-dir",
        default=_env("CINECAT_DATA_DIR", default="./cinecat-data"),
    )
    parser.add_argument(
        "--api-base",
        default=_env("CINECAT_API_BASE", default=DEFAULT_API_BASE),
    )
    parser.add_argument(
        "--site-url",
        default=_env("CINECAT_SITE_URL", default=DEFAULT_SITE_URL),
    )
    parser.add_argument(
        "--subs-base",
        default=_env("CINECAT_SUBS_BASE", default=DEFAULT_SUBS_BASE),
    )
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    data_dir = Path(args.data_dir).expanduser().resolve()
    public_host = (
        "127.0.0.1" if args.host in ("0.0.0.0", "::", "") else args.host
    )
    public_url = (
        args.public_url or f"http://{public_host}:{args.port}"
    ).rstrip("/")
    source = Source(
        api_base=args.api_base,
        site_url=args.site_url,
        subs_base=args.subs_base,
        timeout=args.timeout,
    )
    writer = LibraryWriter(
        Path(args.media_dir), public_url, data_dir / "state.json"
    )
    app = App(source, writer, data_dir)
    app.verbose = args.verbose
    server = AppServer((args.host, args.port), app)
    print(f"cinecat library on http://{public_host}:{args.port}")
    print(f"  api:       {source.api_base}")
    print(f"  media:     {writer.root}")
    print(f"  public:    {public_url}")
    if "127.0.0.1" in public_url or "localhost" in public_url:
        print(
            "  warning: Jellyfin cannot reach a loopback public_url; set "
            "CINECAT_PUBLIC_URL to a reachable host.",
            file=sys.stderr,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
