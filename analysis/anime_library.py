#!/usr/bin/env python3
"""
anime_library.py - a small web app for curating an anime library in Jellyfin.

It reads a /data/* catalogue through catalog.py, caches the JSON on disk, and
writes Jellyfin-ready `.strm` files into a media mount.  The URLs inside those
files point back at this server, which resolves the player page and proxies the
HLS manifest and segments with the Referer/User-Agent the CDN expects.

    python3 anime_library.py \
        --catalog-base http://127.0.0.1:8780 \
        --media-dir /srv/jellyfin/media \
        --public-url https://anime.example.com \
        --data-dir /srv/anime-library

Layout written under the media directory:

    anime/sub/Shows/<Title> (Year)/Season 01/<Title> - S01E01 - Episode.strm
    anime/sub/Movies/<Title> (Year)/<Title> (Year).strm
    anime/dub/...   (same shape)

Sub and dub are separate trees because Jellyfin cannot treat two independent
HLS sources as two versions of one episode.  Add each tree as its own Jellyfin
library; the UI shows the exact paths.

Only the Python standard library is used.  catalog.py must sit next to this
file, or be importable on PYTHONPATH.
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
import uuid
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit
from urllib.request import Request, urlopen

from catalog import Catalog, CatalogError, NoStream, NotFound

DEFAULT_PORT = 8787
URI_ATTR_RE = re.compile(r"""URI=(?:"([^"]+)"|'([^']+)')""")
FILENAME_BAD_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def safe_name(value, limit: int = 120) -> str:
    text = FILENAME_BAD_RE.sub(" ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:limit].rstrip(" .") or "Untitled")


def human_count(value: int) -> str:
    return f"{value:,}"


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _atomic_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


# --------------------------------------------------------------------------- #
# cached catalogue
# --------------------------------------------------------------------------- #

class CachingCatalog(Catalog):
    """Catalog whose `_get` responses survive restarts and respect a TTL."""

    def __init__(self, base: str, cache_dir: Path, ttl: float, **kwargs):
        super().__init__(base, **kwargs)
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.ttl = float(ttl)
        self._cache_lock = threading.Lock()

    def _path_for(self, path: str, params: dict | None) -> Path:
        key = json.dumps([path, sorted((params or {}).items())],
                         separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.cache_dir / f"{digest}.json"

    def _get(self, path: str, params: dict = None, referer: str = None) -> dict:
        cache_path = self._path_for(path, params)
        with self._cache_lock:
            cached = _read_json(cache_path)
            if isinstance(cached, dict) and isinstance(cached.get("body"), dict):
                if time.time() - float(cached.get("at") or 0) < self.ttl:
                    return cached["body"]
        body = super()._get(path, params, referer=referer)
        with self._cache_lock:
            _atomic_json(cache_path, {"at": time.time(), "body": body})
        return body

    def clear(self) -> int:
        removed = 0
        with self._cache_lock:
            for child in self.cache_dir.glob("*.json"):
                try:
                    child.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #

class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.Lock()

    def load(self) -> dict:
        with self.lock:
            data = _read_json(self.path)
            if not isinstance(data, dict):
                return {"selections": []}
            if not isinstance(data.get("selections"), list):
                data["selections"] = []
            return data

    def save(self, data: dict) -> None:
        with self.lock:
            _atomic_json(self.path, data)


# --------------------------------------------------------------------------- #
# signed proxy tokens
# --------------------------------------------------------------------------- #

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
        signature = hmac.new(self.secret, body.encode("ascii"),
                             hashlib.sha256).digest()
        return body + "." + base64.urlsafe_b64encode(signature).decode().rstrip("=")

    def loads(self, token: str) -> dict:
        try:
            body, encoded = token.split(".", 1)
            expected = hmac.new(self.secret, body.encode("ascii"),
                                hashlib.sha256).digest()
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


# --------------------------------------------------------------------------- #
# media writer
# --------------------------------------------------------------------------- #

class LibraryWriter:
    def __init__(self, media_root: Path, public_url: str):
        self.media_root = Path(media_root).expanduser().resolve()
        self.public_url = public_url.rstrip("/")
        self.media_root.mkdir(parents=True, exist_ok=True)

    # -- naming ------------------------------------------------------------

    @staticmethod
    def _is_movie(show) -> bool:
        kind = str(show.get("type") or "").lower()
        if kind in ("movie", "film"):
            return True
        if kind in ("tv", "series", "ona", "ova", "special", "music"):
            return False
        try:
            return not show.episodes and show.episode_count <= 1
        except CatalogError:
            return False

    @staticmethod
    def _episode_number(episode, fallback: int) -> int:
        try:
            return max(0, int(float(episode.number)))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _label(show) -> str:
        title = safe_name(show.title or show.slug)
        year = show.get("year")
        return f"{title} ({year})" if year else title

    def _play_url(self, slug: str, number, lang: str) -> str:
        tail = "movie" if number is None else quote(str(number), safe="")
        return (f"{self.public_url}/play/{quote(str(slug), safe='')}/"
                f"{tail}/{lang}")

    # -- filesystem --------------------------------------------------------

    def _within_root(self, path: Path) -> bool:
        try:
            Path(path).resolve().relative_to(self.media_root)
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _write_strm(path: Path, url: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(url.rstrip("/") + "\n", encoding="utf-8")
        os.replace(temp, path)

    def _cleanup(self, files, dirs) -> None:
        for file_name in files:
            path = Path(file_name)
            if self._within_root(path):
                try:
                    path.unlink()
                except OSError:
                    pass
        for dir_name in sorted(dirs, key=len, reverse=True):
            path = Path(dir_name)
            if not self._within_root(path):
                continue
            try:
                path.rmdir()
            except OSError:
                pass

    # -- public ------------------------------------------------------------

    def write(self, show, langs: list) -> dict:
        title = show.title or show.slug
        movie = self._is_movie(show)
        episodes = [] if movie else show.episodes
        if not movie and not episodes:
            raise CatalogError(f"{title} has no episodes")
        label = self._label(show)
        files, dirs = [], set()
        try:
            for lang in langs:
                root = self.media_root / "anime" / lang
                if movie:
                    target = root / "Movies" / label
                    target.mkdir(parents=True, exist_ok=True)
                    dirs.add(str(target))
                    path = target / f"{label}.strm"
                    self._write_strm(path, self._play_url(show.slug, None, lang))
                    files.append(str(path))
                else:
                    show_dir = root / "Shows" / label
                    dirs.add(str(show_dir))
                    for index, episode in enumerate(episodes, 1):
                        number = self._episode_number(episode, index)
                        season_dir = show_dir / "Season 01"
                        season_dir.mkdir(parents=True, exist_ok=True)
                        dirs.add(str(season_dir))
                        episode_title = safe_name(
                            episode.title or f"Episode {number}", 80)
                        filename = (f"{safe_name(title)} - S01E{number:02d} - "
                                    f"{episode_title}.strm")
                        path = season_dir / filename
                        self._write_strm(path, self._play_url(show.slug, number, lang))
                        files.append(str(path))
        except Exception:
            self._cleanup(files, dirs)
            raise
        return {
            "id": uuid.uuid4().hex,
            "slug": show.slug,
            "title": title,
            "year": show.get("year"),
            "kind": "movie" if movie else "series",
            "langs": langs,
            "files": files,
            "dirs": sorted(dirs, key=len),
            "episodes": 1 if movie else len(episodes),
            "created": time.time(),
            "updated": time.time(),
        }

    def remove(self, selection: dict) -> None:
        self._cleanup(selection.get("files") or [], selection.get("dirs") or [])


# --------------------------------------------------------------------------- #
# application
# --------------------------------------------------------------------------- #

class App:
    def __init__(self, catalog_base: str, media_root: Path, public_url: str,
                 data_dir: Path, state_file: Path, *, timeout: float = 20.0,
                 cache_ttl: float = 6 * 3600, verbose: bool = False):
        self.public_url = public_url.rstrip("/")
        self.timeout = float(timeout)
        self.verbose = verbose
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        cache_dir = self.data_dir / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)

        self.catalog = CachingCatalog(
            catalog_base, cache_dir / "catalog", cache_ttl, timeout=self.timeout)
        self.state = StateStore(Path(state_file).expanduser().resolve())
        self.library = LibraryWriter(media_root, self.public_url)
        self.signer = Signer(cache_dir / "proxy.secret")
        self._mutation_lock = threading.Lock()
        self._resolve_lock = threading.Lock()
        self._resolve_cache = {}

        ui_path = Path(__file__).with_name("anime_library_ui.html")
        if not ui_path.is_file():
            raise CatalogError(f"UI file is missing: {ui_path}")
        self.ui_bytes = ui_path.read_bytes()

    # -- logging -----------------------------------------------------------

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {message}", file=sys.stderr, flush=True)

    # -- catalogue ---------------------------------------------------------

    @staticmethod
    def _image_value(value):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for key in ("url", "path", "src", "file"):
                item = value.get(key)
                if isinstance(item, str) and item.strip():
                    return item.strip()
        return None

    def _poster(self, show):
        for key in ("poster", "image", "cover", "thumbnail",
                    "background_image", "spotlight_bg"):
            value = self._image_value(show.get(key))
            if value:
                absolute = urljoin(self.catalog.base + "/", value)
                token = self.signer.dumps({
                    "u": absolute,
                    "r": self.catalog.base + "/",
                })
                return f"/p/{token}"
        images = show.get("images")
        if isinstance(images, list):
            for image in images:
                value = self._image_value(image)
                if value:
                    absolute = urljoin(self.catalog.base + "/", value)
                    token = self.signer.dumps({
                        "u": absolute,
                        "r": self.catalog.base + "/",
                    })
                    return f"/p/{token}"
        return None

    def _card(self, show) -> dict:
        return {
            "slug": show.slug,
            "title": show.title,
            "year": show.get("year"),
            "type": show.get("type") or "",
            "score": show.get("score"),
            "episodes": show.episode_count,
            "status": show.get("status"),
            "poster": self._poster(show),
        }

    def search(self, query: str, page: int) -> dict:
        shows, has_more = self.catalog.search_page(query, page)
        return {
            "items": [self._card(show) for show in shows],
            "page": page,
            "has_more": bool(has_more),
        }

    def show_detail(self, slug: str) -> dict:
        show = self.catalog.show(slug)
        episodes = show.episodes
        preview = episodes[:200]
        genres = []
        for genre in (show.get("genres") or []):
            name = genre.get("name") if isinstance(genre, dict) else genre
            if name:
                genres.append(str(name))
        detail = self._card(show)
        detail.update({
            "description": show.get("description")
                           or show.get("short_description") or "",
            "genres": genres,
            "episode_total": len(episodes),
            "episodes": [
                {"number": LibraryWriter._episode_number(ep, index),
                 "title": ep.title or ""}
                for index, ep in enumerate(preview, 1)
            ],
            "episode_preview_limited": len(episodes) > len(preview),
        })
        return detail

    # -- selections --------------------------------------------------------

    @staticmethod
    def _normalize_langs(value) -> list:
        if isinstance(value, str):
            value = value.strip().lower()
            if value == "both":
                return ["sub", "dub"]
            if value in ("sub", "dub"):
                return [value]
        if isinstance(value, (list, tuple)):
            lowered = {str(item).strip().lower() for item in value}
            picked = [lang for lang in ("sub", "dub") if lang in lowered]
            if picked:
                return picked
        raise ValueError("lang must be sub, dub or both")

    def selected(self) -> list:
        selections = self.state.load().get("selections") or []
        return sorted(selections, key=lambda item: item.get("updated") or 0,
                      reverse=True)

    def add_selection(self, slug: str, lang) -> dict:
        langs = self._normalize_langs(lang)
        if not slug:
            raise ValueError("slug is required")
        with self._mutation_lock:
            state = self.state.load()
            existing = next((item for item in state["selections"]
                             if item.get("slug") == slug), None)
            if existing:
                self.library.remove(existing)
            try:
                show = self.catalog.show(slug)
                selection = self.library.write(show, langs)
            except Exception:
                if existing:
                    state["selections"] = [item for item in state["selections"]
                                           if item is not existing]
                    self.state.save(state)
                raise
            state["selections"] = [item for item in state["selections"]
                                   if item.get("slug") != slug]
            state["selections"].append(selection)
            self.state.save(state)
            return selection

    def remove_selection(self, selection_id: str) -> bool:
        with self._mutation_lock:
            state = self.state.load()
            found = next((item for item in state["selections"]
                          if item.get("id") == selection_id), None)
            if not found:
                return False
            self.library.remove(found)
            state["selections"] = [item for item in state["selections"]
                                   if item is not found]
            self.state.save(state)
            return True

    # -- config ------------------------------------------------------------

    def config(self) -> dict:
        libraries = {}
        for lang in ("sub", "dub"):
            root = self.library.media_root / "anime" / lang
            libraries[lang] = {
                "root": str(root),
                "shows": str(root / "Shows"),
                "movies": str(root / "Movies"),
            }
        return {
            "catalog_base": self.catalog.base,
            "media_root": str(self.library.media_root),
            "public_url": self.public_url,
            "cache_ttl": self.catalog.ttl,
            "libraries": libraries,
            "selection_count": len(self.selected()),
        }

    def clear_cache(self) -> int:
        with self._resolve_lock:
            self._resolve_cache.clear()
        return self.catalog.clear()

    # -- playback ----------------------------------------------------------

    @staticmethod
    def _referer_for(player_link: str, manifest: str) -> str:
        try:
            player = urlsplit(player_link)
            upstream = urlsplit(manifest)
            if (player.scheme and player.netloc
                    and player.netloc != upstream.netloc):
                return f"{player.scheme}://{player.netloc}/"
        except ValueError:
            pass
        return player_link or manifest

    def resolve_manifest(self, slug: str, number, lang: str):
        if lang not in ("sub", "dub"):
            raise ValueError("lang must be sub or dub")
        key = (slug, str(number), lang)
        now = time.time()
        with self._resolve_lock:
            cached = self._resolve_cache.get(key)
            if cached and now - cached[0] < 120:
                return cached[1], cached[2]

        show = self.catalog.show(slug)
        episode = None
        if str(number).strip().lower() not in ("movie", "", "0"):
            episode = show.episode(number)
        if episode is None:
            episodes = show.episodes
            if not episodes:
                raise NoStream(f"{show.slug} has no episodes")
            episode = episodes[0]
        server = episode.server(lang=lang)
        manifest = self.catalog.resolve(server.url)
        referer = self._referer_for(server.url, manifest)
        with self._resolve_lock:
            self._resolve_cache[key] = (now, manifest, referer)
        return manifest, referer

    def rewrite_manifest(self, text: str, base_url: str, referer: str) -> str:
        """Point every variant, segment, map and key back through /p/."""
        public = self.public_url.rstrip("/")

        def proxy_url(value: str) -> str:
            absolute = urljoin(base_url, value)
            token = self.signer.dumps({"u": absolute, "r": referer})
            return f"{public}/p/{token}"

        output = []
        for line in text.splitlines():
            if not line:
                output.append(line)
                continue
            if line.startswith("#"):
                output.append(URI_ATTR_RE.sub(
                    lambda match: 'URI="' + proxy_url(
                        match.group(1) or match.group(2)) + '"',
                    line))
            else:
                output.append(proxy_url(line.strip()))
        return "\n".join(output) + "\n"


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

class AppServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, app: App):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AnimeLibrary/1.0"

    # -- responses ---------------------------------------------------------

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
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8",
                         head_only=(self.command == "HEAD"))

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

    # -- routing -----------------------------------------------------------

    def do_GET(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        app = self.server.app
        try:
            parsed = urlsplit(self.path)
            path = unquote(parsed.path)
            query = {key: values[0] for key, values in
                     parse_qs(parsed.query, keep_blank_values=True).items()}
            command = self.command

            if command in ("GET", "HEAD"):
                if path in ("/", "/index.html"):
                    return self._serve_ui()
                if path == "/favicon.ico":
                    self.send_response(204)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return None
                if path == "/api/config":
                    return self._json(200, app.config())
                if path == "/api/selected":
                    return self._json(200, {"selections": app.selected()})
                if path == "/api/search":
                    page = max(1, int(query.get("page") or 1))
                    return self._json(200, app.search(query.get("q") or "", page))
                if path == "/api/show":
                    slug = query.get("slug") or ""
                    if not slug:
                        return self._error(400, "slug is required")
                    return self._json(200, app.show_detail(slug))
                if path.startswith("/p/"):
                    return self._proxy(token=path[3:])
                if path.startswith("/play/"):
                    return self._play(path)

            if command == "POST":
                if path == "/api/select":
                    payload = self._body()
                    selection = app.add_selection(
                        str(payload.get("slug") or ""),
                        payload.get("lang") or payload.get("langs") or "sub")
                    return self._json(200, {"selection": selection})
                if path == "/api/refresh":
                    removed = app.clear_cache()
                    return self._json(200, {"ok": True, "removed": removed})

            if command == "DELETE" and path.startswith("/api/select/"):
                selection_id = path.rsplit("/", 1)[-1]
                if app.remove_selection(selection_id):
                    return self._json(200, {"ok": True})
                return self._error(404, "selection not found")

            return self._error(404, "not found")
        except NotFound as exc:
            self._error(404, str(exc))
        except NoStream as exc:
            self._error(502, str(exc))
        except ValueError as exc:
            self._error(400, str(exc))
        except (CatalogError, OSError) as exc:
            app.log(f"request failed: {exc}")
            self._error(502, str(exc))
        except Exception as exc:  # pragma: no cover - last-resort request guard
            app.log(f"unhandled request error: {type(exc).__name__}: {exc}")
            try:
                self._error(500, "internal server error")
            except Exception:
                pass

    def _serve_ui(self):
        body = self.server.app.ui_bytes
        self._send_bytes(200, body, "text/html; charset=utf-8",
                         head_only=(self.command == "HEAD"), cache="no-store")

    # -- playback ----------------------------------------------------------

    def _play(self, path: str):
        app = self.server.app
        parts = [part for part in path.split("/") if part]
        if len(parts) < 4:
            return self._error(400, "expected /play/<slug>/<episode>/<lang>")
        slug, number, lang = parts[1], parts[2], parts[3].lower()
        if lang not in ("sub", "dub"):
            return self._error(400, "lang must be sub or dub")
        manifest, referer = app.resolve_manifest(slug, number, lang)
        return self._proxy_url(manifest, referer)

    def _proxy(self, token: str):
        app = self.server.app
        try:
            payload = app.signer.loads(token)
        except ValueError as exc:
            return self._error(400, str(exc))
        return self._proxy_url(payload["u"], payload.get("r") or "")

    def _proxy_url(self, url: str, referer: str):
        app = self.server.app
        head_only = self.command == "HEAD"
        headers = {
            "User-Agent": app.catalog.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if referer:
            headers["Referer"] = referer
            try:
                origin = urlsplit(referer)
                if origin.scheme and origin.netloc:
                    headers["Origin"] = f"{origin.scheme}://{origin.netloc}"
            except ValueError:
                pass
        if self.headers.get("Range"):
            headers["Range"] = self.headers["Range"]

        # Always use GET upstream: many CDNs answer 501 to HEAD, but Jellyfin
        # still needs the real Content-Length for a HEAD probe.
        try:
            response = urlopen(Request(url, headers=headers, method="GET"),
                               timeout=app.timeout)
        except HTTPError as exc:
            body = exc.read(4000) or b""
            content_type = (exc.headers.get("Content-Type")
                            or "text/plain; charset=utf-8")
            return self._send_bytes(exc.code, body, content_type,
                                    head_only=head_only)
        except (URLError, OSError) as exc:
            app.log(f"proxy upstream failed: {url}: {exc}")
            return self._error(502, f"upstream fetch failed: {exc}")

        with response:
            final_url = response.geturl() or url
            status = response.status
            upstream_headers = response.headers
            content_type = (upstream_headers.get("Content-Type")
                            or "application/octet-stream")
            if head_only:
                self._relay_headers(status, upstream_headers)
                return

            first = response.read(64 * 1024)
            likely_manifest = (urlsplit(final_url).path.lower().endswith(".m3u8")
                               or "mpegurl" in content_type.lower())
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
                        status, body, "application/vnd.apple.mpegurl",
                        cache="no-cache")
                return self._send_bytes(status, raw, content_type)

            # A normal segment/init section: relay headers, then stream.
            self._relay_headers(status, upstream_headers)
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

    def _relay_headers(self, status: int, headers) -> None:
        self.send_response(status)
        content_length = headers.get("Content-Length")
        for name in ("Content-Type", "Content-Length", "Content-Range",
                     "Accept-Ranges", "Last-Modified", "ETag", "Cache-Control",
                     "Content-Encoding"):
            value = headers.get(name)
            if value:
                self.send_header(name, value)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Access-Control-Allow-Origin", "*")
        if not content_length:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _env(*names, default=None):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Curate an anime catalogue into a Jellyfin media mount.")
    parser.add_argument("--catalog-base",
                        default=_env("ANIME_CATALOG_BASE", "CATALOG_BASE"),
                        help="catalogue API origin (env ANIME_CATALOG_BASE)")
    parser.add_argument("--media-dir",
                        default=_env("ANIME_MEDIA_DIR", "JELLYFIN_MEDIA_DIR",
                                     default="./anime-library-media"),
                        help="Jellyfin media mount root (env ANIME_MEDIA_DIR)")
    parser.add_argument("--public-url",
                        default=_env("ANIME_PUBLIC_URL"),
                        help="URL Jellyfin/players use for this server "
                             "(env ANIME_PUBLIC_URL)")
    parser.add_argument("--data-dir",
                        default=_env("ANIME_DATA_DIR",
                                     default="./anime-library-data"),
                        help="cache, state and proxy secret directory")
    parser.add_argument("--state-file", default=None,
                        help="selection state path (default: DATA_DIR/state.json)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--cache-ttl", type=float, default=6 * 3600,
                        help="catalogue cache lifetime in seconds")
    parser.add_argument("--verbose", action="store_true",
                        help="log every proxied request")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if not args.catalog_base:
        print("error: --catalog-base is required (or set ANIME_CATALOG_BASE)",
              file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir).expanduser().resolve()
    state_file = (Path(args.state_file).expanduser().resolve()
                  if args.state_file else data_dir / "state.json")
    listen_host = args.host
    public_host = "127.0.0.1" if listen_host in ("0.0.0.0", "::", "") else listen_host
    public_url = (args.public_url
                  or f"http://{public_host}:{args.port}").rstrip("/")

    app = App(args.catalog_base, Path(args.media_dir), public_url, data_dir,
              state_file, timeout=args.timeout, cache_ttl=args.cache_ttl,
              verbose=args.verbose)
    server = AppServer((args.host, args.port), app)
    print(f"anime library on http://{public_host}:{args.port}")
    print(f"  catalogue: {args.catalog_base}")
    print(f"  media:     {app.library.media_root}")
    print(f"  public:    {app.public_url}")
    if "127.0.0.1" in app.public_url or "localhost" in app.public_url:
        print("  warning: Jellyfin cannot reach a loopback public_url; set "
              "ANIME_PUBLIC_URL to a reachable host.", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
