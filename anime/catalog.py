#!/usr/bin/env python3
"""
catalog.py - an ergonomic client for a /data/* anime catalogue API.

    from catalog import Catalog

    cat = Catalog("http://127.0.0.1:8780")

    for show in cat.search("my show", limit=5):
        print(show.slug, show.title, show.episode_count)

    show = cat.show("my-show")
    ep = show.episode(2)
    print(ep.title, [s.name for s in ep.servers("dub")])
    print(ep.stream())                       # resolved .m3u8 URL
    ep.download("ep2.mp4", seconds=30)       # remuxed, no re-encode

    # or without a slug lookup
    cat.episode("my-show", 2).download("ep2.mp4", seconds=30)

CLI:

    python3 catalog.py --base URL search "my show"
    python3 catalog.py --base URL show my-show
    python3 catalog.py --base URL episodes my-show --all
    python3 catalog.py --base URL stream my-show 2 --lang dub --server HD-1
    python3 catalog.py --base URL download my-show 2 -o ep2.mp4 -s 30
    python3 catalog.py --base URL probe https://host/player/123
    python3 catalog.py --base URL download my-show 2 -o ep2.mp4 --playlist URL
    CATALOG_BASE=URL python3 catalog.py suggest "my"

If a player page builds its manifest in script, `resolve()` cannot see it: run
`probe` on the page to see what it does return, then pass the manifest with
`--playlist`. `probe` prints the status, content type, manifest candidates,
iframes and endpoint-looking paths found in the body.

If the page carries `data-id` and fetches its source from an API, that endpoint
is queried by default:

    /stream/getSources?id={id}&lang={lang}

Placeholders are filled from the page: {id}, {real_id}, {media_id},
{file_version}, {lang}. Pass sources_endpoint="" to disable it. The JSON
response is searched for a manifest URL. Only the URL is resolved: a body that
is not plaintext HLS is still rejected by download().

Wire contract (all GET, JSON):

    /data/suggest?q=<text>              {anime: [item]}
    /data/browse?keyword=&page=<n>      {anime: [...], ids, page, per, has_more}
    /data/anime?slug=<slug>             {anime: [show], related, seasons, slug, lists}
    /data/servers?chapter=<id>          {ok, chapter_id, s_id, servers: {sub: [...], dub: [...]}}

`Show` forwards unknown attributes to the item, so `show.score`, `show.status` and
`show.year` work without a wrapper per field. `show.episodes` is a list of
`Episode`; the wire field with the same name is the total count, kept as
`show.episode_count` and still reachable through `show.raw["episodes"]`.

Stdlib only. `download()` needs ffmpeg on PATH unless the target ends in `.ts`.
Encrypted playlists (`#EXT-X-KEY`) are rejected rather than guessed at.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote as urlquote, urlencode, urljoin, urlsplit

DEFAULT_TIMEOUT = 20.0
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)

# Player pages that carry data-id fetch their source from this endpoint.
# Override with --sources-endpoint, or disable with an empty string.
DEFAULT_SOURCES_ENDPOINT = "/stream/getSources?id={id}&lang={lang}"


# --------------------------------------------------------------------------- #
# errors
# --------------------------------------------------------------------------- #

class CatalogError(RuntimeError):
    """Anything that stops a request or a download from completing."""


class NotFound(CatalogError):
    """The server answered, but not with the thing you asked for."""


class NoStream(CatalogError):
    """A server entry exists but no playable playlist could be resolved."""


# --------------------------------------------------------------------------- #
# progress
# --------------------------------------------------------------------------- #

def _human_bytes(value: float) -> str:
    value = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def _format_eta(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds:02d}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes:02d}:{seconds:02d}"
    hours, minutes = divmod(minutes, 60)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}"


class _Progress:
    """One-line transfer bar on stderr; silent when stderr is not a TTY."""

    WIDTH = 28

    def __init__(self, total: int, label: str = "downloading",
                 stream=None, enabled: bool = None):
        self.total = max(1, int(total))
        self.done = 0
        self.bytes = 0
        self.label = label
        self.stream = stream or sys.stderr
        self.enabled = self.stream.isatty() if enabled is None else bool(enabled)
        encoding = getattr(self.stream, "encoding", None) or "utf-8"
        try:
            "\u2588".encode(encoding)
            self._filled, self._empty = "\u2588", "\u2591"
        except (LookupError, UnicodeEncodeError):
            self._filled, self._empty = "=", "-"
        self.started = time.monotonic()
        self._last_draw = 0.0
        self._last_length = 0

    def update(self, bytes_delta: int = 0, step: int = 1) -> None:
        self.done = min(self.total, self.done + max(0, int(step)))
        self.bytes += max(0, int(bytes_delta))
        if not self.enabled:
            return
        now = time.monotonic()
        if self.done < self.total and now - self._last_draw < 0.08:
            return
        self._last_draw = now
        self._render(now)

    def _render(self, now: float = None) -> None:
        now = time.monotonic() if now is None else now
        fraction = self.done / self.total
        filled = int(round(fraction * self.WIDTH))
        bar = self._filled * filled + self._empty * (self.WIDTH - filled)
        elapsed = max(1e-9, now - self.started)
        speed = self.bytes / elapsed
        remaining = self.total - self.done
        eta = elapsed / self.done * remaining if self.done else 0.0
        columns = shutil.get_terminal_size((100, 20)).columns
        line = (f"{self.label} [{bar}] {fraction * 100:3.0f}%  "
                f"{self.done}/{self.total}  {_human_bytes(self.bytes)}  "
                f"{_human_bytes(speed)}/s  ETA {_format_eta(eta)}")
        line = line[:max(0, columns - 1)]
        self.stream.write("\r" + line
                          + " " * max(0, self._last_length - len(line)))
        self.stream.flush()
        self._last_length = len(line)

    def close(self) -> None:
        if not self.enabled:
            return
        self._render()
        self.stream.write("\n")
        self.stream.flush()


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #

class Record:
    """A read-only view over one JSON object, with attribute passthrough."""

    def __init__(self, data: dict):
        if not isinstance(data, dict):
            raise TypeError(f"expected an object, got {type(data).__name__}")
        object.__setattr__(self, "_data", dict(data))

    def __getattr__(self, name):
        # Only called when normal lookup fails, so properties always win.
        try:
            return self.__dict__["_data"][name]
        except KeyError:
            raise AttributeError(name) from None

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)

    @property
    def raw(self) -> dict:
        return dict(self._data)

    def to_dict(self) -> dict:
        return dict(self._data)

    def __repr__(self) -> str:
        label = self.get("slug") or self.get("name") or self.get("id")
        return f"<{type(self).__name__} {label}>"


class Server(Record):
    """One playback server for one episode."""

    def __init__(self, data: dict, catalog: "Catalog", lang: str):
        super().__init__(data)
        self._catalog = catalog
        self.lang = lang

    @property
    def name(self) -> str:
        return str(self.get("name") or "?")

    @property
    def url(self) -> str:
        return str(self.get("link") or "")

    def stream(self, playlist: str = None) -> str:
        """Resolve the player page to a playlist URL, or use `playlist` as-is."""
        return self._catalog.resolve(self.url, playlist=playlist)

    def download(self, path: str, seconds=None, start: float = 0.0,
                 playlist: str = None, progress: bool = None) -> str:
        return self._catalog.download(self, path, seconds=seconds, start=start,
                                      playlist=playlist, progress=progress)

    def __repr__(self) -> str:
        return f"<Server {self.lang}/{self.name}>"


class Episode(Record):
    """One chapter, with lazy access to its servers."""

    def __init__(self, data: dict, catalog: "Catalog", show: "Show"):
        super().__init__(data)
        self._catalog = catalog
        self._show = show
        self._server_map = None

    @property
    def id(self):
        return self.get("id")

    @property
    def number(self):
        return self.get("chapter_number")

    @property
    def title(self) -> str:
        return str(self.get("title") or "")

    def _servers(self) -> dict:
        if self._server_map is None:
            self._server_map = self._catalog.servers(self.id)
        return self._server_map

    def servers(self, lang: str = None) -> list:
        """All servers for one language; fetched once per episode."""
        return list(self._servers().get(lang or self._catalog.lang) or [])

    def server(self, name: str = None, lang: str = None) -> "Server":
        pool = self.servers(lang)
        if not pool:
            raise NoStream(f"episode {self.number} has no "
                           f"{lang or self._catalog.lang} servers")
        if name is None:
            return pool[0]
        for candidate in pool:
            if candidate.name == name:
                return candidate
        raise NoStream(f"no server named {name!r}; have "
                       f"{', '.join(s.name for s in pool)}")

    def stream(self, lang: str = None, server: str = None,
               playlist: str = None) -> str:
        return self.server(server, lang).stream(playlist=playlist)

    def download(self, path: str, seconds=None, start: float = 0.0,
                 lang: str = None, server: str = None,
                 playlist: str = None, progress: bool = None) -> str:
        return self.server(server, lang).download(path, seconds=seconds, start=start,
                                                  playlist=playlist,
                                                  progress=progress)

    def __repr__(self) -> str:
        return f"<Episode {self.number}>"


class Show(Record):
    """A catalogue item. `episodes` is a list; `episode_count` is the total."""

    def __init__(self, data: dict, catalog: "Catalog"):
        super().__init__(data)
        self._catalog = catalog
        self._episodes = None

    @property
    def slug(self) -> str:
        return str(self.get("slug") or "")

    @property
    def title(self) -> str:
        return str(self.get("title") or "")

    @property
    def episode_count(self) -> int:
        for key in ("ep_count", "episodes"):
            try:
                return int(self.get(key))
            except (TypeError, ValueError):
                continue
        return len(self.episodes)

    @property
    def episodes(self) -> list:
        if self._episodes is None:
            self._episodes = [
                Episode(ch, self._catalog, self)
                for ch in (self.get("chapters") or [])
                if isinstance(ch, dict)
            ]
        return list(self._episodes)

    def episode(self, number) -> Episode:
        """Look up by chapter number, not by position in the list."""
        for ep in self.episodes:
            if str(ep.number) == str(number):
                return ep
        numbers = [str(e.number) for e in self.episodes]
        if not numbers:
            raise NotFound(f"{self.slug} has no episodes")
        raise NotFound(f"episode {number} not found on {self.slug} "
                       f"(available {numbers[0]}..{numbers[-1]})")

    def refresh(self) -> "Show":
        return self._catalog.show(self.slug, refresh=True)

    def __len__(self) -> int:
        return len(self.episodes)

    def __iter__(self):
        return iter(self.episodes)

    def __getitem__(self, index):
        return self.episodes[index]

    def __repr__(self) -> str:
        return f"<Show {self.slug} episodes={self.episode_count}>"


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #

def _read_response(url: str, headers: dict, timeout: float):
    """Return (status, lowercased headers, body)."""
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return (response.status,
                    {k.lower(): v for k, v in response.headers.items()},
                    response.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise NotFound(f"HTTP 404 for {url}") from exc
        raise CatalogError(f"HTTP {exc.code} for {url}") from exc
    except OSError as exc:
        raise CatalogError(f"{type(exc).__name__} for {url}: {exc}") from exc


def _read(url: str, headers: dict, timeout: float) -> bytes:
    return _read_response(url, headers, timeout)[2]


# A manifest reference anywhere in a body: HTML attribute, inline JSON, header.
_MANIFEST_RE = re.compile(r"[^\s\"'<>\\]+\.m3u8[^\s\"'<>\\]*")
_IFRAME_RE = re.compile(r"""<iframe[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_ENDPOINT_RE = re.compile(r"""["'](/[A-Za-z0-9_\-./?=&%]+)["']""")
_MANIFEST_KEYS = ("file", "src", "url", "hls", "playlist", "manifest",
                  "stream", "source", "video")


def _candidates(text: str) -> list:
    """Every .m3u8-looking reference, in document order, unescaped."""
    text = (text.replace("\\/", "/").replace("\\u002F", "/")
                .replace("\\u002f", "/").replace("&amp;", "&"))
    seen, out = set(), []
    for match in _MANIFEST_RE.finditer(text):
        value = match.group(0).strip()
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


# --------------------------------------------------------------------------- #
# AES-CBC (pure Python, for MegaPlay's encrypted `enc` payload)
# --------------------------------------------------------------------------- #

def _aes_rotl8(value: int, shift: int) -> int:
    return ((value << shift) | (value >> (8 - shift))) & 0xff


def _aes_xtime(value: int) -> int:
    return ((value << 1) ^ (0x1b if value & 0x80 else 0)) & 0xff


def _aes_make_sbox() -> list:
    sbox = [0] * 256
    p = q = 1
    while True:
        p ^= _aes_xtime(p)
        # Divide q by 3 in GF(2^8): plain left shifts, then reduce.
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xff
        if q & 0x80:
            q ^= 0x09
        x = (q ^ _aes_rotl8(q, 1) ^ _aes_rotl8(q, 2)
             ^ _aes_rotl8(q, 3) ^ _aes_rotl8(q, 4))
        sbox[p] = x ^ 0x63
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


def _aes_mul(a: int, b: int) -> int:
    product = 0
    for _ in range(8):
        if b & 1:
            product ^= a
        a = _aes_xtime(a)
        b >>= 1
    return product


_AES_SBOX = _aes_make_sbox()
_AES_INV_SBOX = [0] * 256
for _index, _value in enumerate(_AES_SBOX):
    _AES_INV_SBOX[_value] = _index
_AES_MUL = {factor: [_aes_mul(byte, factor) for byte in range(256)]
            for factor in (9, 11, 13, 14)}


def _aes_round_keys(key: bytes):
    nk = len(key) // 4
    if nk not in (4, 6, 8) or len(key) % 4:
        raise ValueError("AES key must be 16, 24 or 32 bytes")
    nr = nk + 6
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_AES_SBOX[byte] for byte in temp]
            temp[0] ^= rcon
            rcon = _aes_xtime(rcon)
        elif nk > 6 and i % nk == 4:
            temp = [_AES_SBOX[byte] for byte in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return words, nr


def _aes_decrypt_block(block: bytes, words: list, nr: int) -> bytes:
    state = [[block[r + 4 * c] for c in range(4)] for r in range(4)]

    def add_round_key(round_index: int) -> None:
        for c in range(4):
            word = words[round_index * 4 + c]
            for r in range(4):
                state[r][c] ^= word[r]

    add_round_key(nr)
    for round_index in range(nr - 1, 0, -1):
        for r in range(1, 4):
            state[r] = state[r][-r:] + state[r][:-r]
        for r in range(4):
            for c in range(4):
                state[r][c] = _AES_INV_SBOX[state[r][c]]
        add_round_key(round_index)
        for c in range(4):
            a0, a1, a2, a3 = (state[0][c], state[1][c],
                              state[2][c], state[3][c])
            state[0][c] = (_AES_MUL[14][a0] ^ _AES_MUL[11][a1]
                           ^ _AES_MUL[13][a2] ^ _AES_MUL[9][a3])
            state[1][c] = (_AES_MUL[9][a0] ^ _AES_MUL[14][a1]
                           ^ _AES_MUL[11][a2] ^ _AES_MUL[13][a3])
            state[2][c] = (_AES_MUL[13][a0] ^ _AES_MUL[9][a1]
                           ^ _AES_MUL[14][a2] ^ _AES_MUL[11][a3])
            state[3][c] = (_AES_MUL[11][a0] ^ _AES_MUL[13][a1]
                           ^ _AES_MUL[9][a2] ^ _AES_MUL[14][a3])
    for r in range(1, 4):
        state[r] = state[r][-r:] + state[r][:-r]
    for r in range(4):
        for c in range(4):
            state[r][c] = _AES_INV_SBOX[state[r][c]]
    add_round_key(0)
    return bytes(state[r][c] for c in range(4) for r in range(4))


def _aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    if not ciphertext or len(ciphertext) % 16:
        raise ValueError("ciphertext is not a multiple of 16 bytes")
    words, nr = _aes_round_keys(key)
    previous, output = iv, bytearray()
    for offset in range(0, len(ciphertext), 16):
        block = ciphertext[offset:offset + 16]
        plain = _aes_decrypt_block(block, words, nr)
        output.extend(x ^ y for x, y in zip(plain, previous))
        previous = block
    return bytes(output)


def _decrypt_megaplay_enc(value):
    """Decode a MegaPlay-style `enc` field into its JSON payload."""
    if not isinstance(value, str):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return None
    key = b"i?LMTAx0Q6,:}50U".ljust(32, b"\x00")
    iv = b"W0;27ToaUpl_P%'c".ljust(16, b"\x00")
    try:
        padded = _aes_cbc_decrypt(raw, key, iv)
    except ValueError:
        return None
    pad = padded[-1]
    if 1 <= pad <= 16 and padded.endswith(bytes([pad]) * pad):
        padded = padded[:-pad]
    try:
        data = json.loads(padded.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _find_manifest(value, depth: int = 0):
    """Walk a decoded JSON body looking for a manifest URL."""
    if depth > 6:
        return None
    if isinstance(value, str):
        found = _candidates(value)
        return found[0] if found else None
    if isinstance(value, dict):
        if isinstance(value.get("enc"), str):
            decrypted = _decrypt_megaplay_enc(value["enc"])
            if decrypted is not None:
                found = _find_manifest(decrypted, depth + 1)
                if found:
                    return found
        for key in _MANIFEST_KEYS:
            if key in value:
                found = _find_manifest(value[key], depth + 1)
                if found:
                    return found
        for item in value.values():
            found = _find_manifest(item, depth + 1)
            if found:
                return found
    if isinstance(value, list):
        for item in value:
            found = _find_manifest(item, depth + 1)
            if found:
                return found
    return None


def _video_like(head: bytes, content_type: str = "") -> bool:
    """True when these leading bytes look like MPEG-TS or fMP4 media."""
    return (head[:1] == b"G" or b"ftyp" in head[:16]
            or b"styp" in head[:16] or content_type.startswith("video/"))


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return (f"{parts.scheme}://{parts.netloc}/"
            if parts.scheme and parts.netloc else url)


def _variant_refs(text: str) -> list:
    """Variant and I-frame playlist references from a master playlist."""
    refs, lines = [], text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#EXT-X-I-FRAME-STREAM-INF"):
            match = re.search(r'URI="([^"]+)"', stripped)
            if match:
                refs.append(match.group(1))
        elif stripped.startswith("#EXT-X-STREAM-INF"):
            for following in lines[index + 1:]:
                following = following.strip()
                if not following or following.startswith("#"):
                    continue
                refs.append(following)
                break
    return refs


def _segment_urls(text: str) -> list:
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")]


def _playlist_serves_video(url: str, headers: dict, timeout: float) -> bool:
    """Fetch a media playlist and confirm its segments are real video."""
    try:
        _, _, body = _read_response(url, headers, timeout)
    except CatalogError:
        return False
    text = body.decode("utf-8", "replace")
    if not text.lstrip().startswith("#EXTM3U"):
        return False
    segments = _segment_urls(text)
    if not segments:
        return False
    for segment in segments[:3]:
        with_range = dict(headers, Range="bytes=0-15")
        try:
            request = urllib.request.Request(urljoin(url, segment),
                                             headers=with_range)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                head = response.read(16)
                content_type = (response.headers.get("Content-Type") or "").lower()
        except (urllib.error.HTTPError, OSError):
            continue
        if _video_like(head, content_type):
            return True
    return False


# Player pages that hide their source: meta tags, a bare <video>, base64, or
# Dean Edwards packed script.
_VIDEO_TAG_RE = re.compile(r"""<(?:video|source)\b[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_SCRIPT_SRC_RE = re.compile(r"""<script\b[^>]+src\s*=\s*["']([^"']+)["']""", re.I)
_ATOB_RE = re.compile(r"""atob\(\s*["']([A-Za-z0-9+/=\s]{20,})["']\s*\)""")
_PACKER_RE = re.compile(
    r"""\}\(\s*'((?:[^'\\]|\\.)*)'\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*"""
    r"""'((?:[^'\\]|\\.)*)'\s*\.split\(\s*'\|'\s*\)""", re.S)
_URLISH_RE = re.compile(r"""(?:https?://[^\s"'<>]+|/[^\s"'<>]{3,})""")
_BASE62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _unbase62(number: int, base: int) -> str:
    if number < base:
        return _BASE62[number]
    return _unbase62(number // base, base) + _BASE62[number % base]


def _unpack(text: str) -> list:
    """Decode Dean Edwards packed script without executing it."""
    unpacked = []
    for match in _PACKER_RE.finditer(text):
        payload, base, count, words = match.groups()
        payload = payload.replace("\\'", "'").replace("\\\\", "\\")
        words = words.replace("\\'", "'").replace("\\\\", "\\").split("|")
        try:
            base, count = int(base), int(count)
        except ValueError:
            continue
        if base < 2 or base > 62:
            continue
        for index in range(min(count, len(words))):
            if not words[index]:
                continue
            token = _unbase62(index, base)
            payload = re.sub(r"\b" + re.escape(token) + r"\b",
                             lambda _m, w=words[index]: w, payload)
        unpacked.append(payload)
    return unpacked


def _meta_videos(text: str) -> list:
    """og:video / twitter:player:stream values, in document order."""
    found = []
    for match in re.finditer(r"<meta\b[^>]*>", text, re.I):
        tag = match.group(0)
        if not re.search(r"""["'](?:og:video(?::url|:secure_url)?|"""
                         r"""twitter:player:stream)["']""", tag, re.I):
            continue
        value = re.search(r"""content\s*=\s*["']([^"']+)["']""", tag, re.I)
        if value:
            found.append(value.group(1))
    return found


def _decoded_sources(text: str) -> list:
    """Candidates recovered from atob() blobs and packed script."""
    found = []
    for match in _ATOB_RE.finditer(text):
        blob = re.sub(r"\s+", "", match.group(1))
        try:
            decoded = base64.b64decode(blob, validate=False).decode("utf-8", "replace")
        except (ValueError, TypeError):
            continue
        found.extend(_candidates(decoded) or _URLISH_RE.findall(decoded)[:3])
    for payload in _unpack(text):
        found.extend(_candidates(payload) or _URLISH_RE.findall(payload)[:3])
    return found


# Pages that hand their ids to a script and fetch the source from an API.
_DATA_ATTR_RE = re.compile(r"""data-([a-z0-9_-]+)\s*=\s*["']([^"']*)["']""", re.I)
_TYPE_RE = re.compile(r"""type\s*:\s*["']([a-z]{2,4})["']""", re.I)


def _data_attrs(text: str) -> dict:
    return {name.lower(): value for name, value in _DATA_ATTR_RE.findall(text)}


def _lang_hint(text: str, fallback: str = "") -> str:
    match = _TYPE_RE.search(text)
    return match.group(1) if match else fallback


def _fill(template: str, values: dict) -> str:
    return re.sub(r"\{(\w+)\}",
                  lambda m: str(values.get(m.group(1), m.group(0))), template)


# --------------------------------------------------------------------------- #
# playlists
# --------------------------------------------------------------------------- #

def _load_playlist(url: str, headers: dict, timeout: float, depth: int = 0):
    """Return (init_uri, [(duration, uri)], base) following one master variant."""
    if depth > 3:
        raise CatalogError("playlist nesting too deep")
    text = _read(url, headers, timeout).decode("utf-8", "replace")
    if not text.lstrip().startswith("#EXTM3U"):
        raise CatalogError(f"not an M3U8 playlist: {url}")
    if "#EXT-X-KEY" in text:
        raise CatalogError(
            f"playlist is encrypted (#EXT-X-KEY): {url}; "
            "serve an unencrypted rendition or extend _load_playlist")

    lines = [line.strip() for line in text.splitlines() if line.strip()]

    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = dict(re.findall(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', line))
        uri = lines[i + 1] if i + 1 < len(lines) and not lines[i + 1].startswith("#") else None
        if uri:
            try:
                bandwidth = int(attrs.get("BANDWIDTH", "0").strip('"') or 0)
            except ValueError:
                bandwidth = 0
            variants.append((bandwidth, uri))
    if variants:
        variants.sort(reverse=True)
        return _load_playlist(urljoin(url, variants[0][1]), headers, timeout, depth + 1)

    init_uri, segments = None, []
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-MAP"):
            match = re.search(r'URI="([^"]+)"', line)
            if match:
                init_uri = urljoin(url, match.group(1))
        elif line.startswith("#EXTINF"):
            match = re.match(r"#EXTINF:\s*([\d.]+)", line)
            uri = lines[i + 1] if i + 1 < len(lines) and not lines[i + 1].startswith("#") else None
            if uri:
                duration = float(match.group(1)) if match else None
                segments.append((duration, urljoin(url, uri)))
    if not segments:
        raise CatalogError(f"no segments in {url}")
    return init_uri, segments, url


def _select(segments, start: float, seconds):
    """Segments overlapping [start, start+seconds); seconds=None means to the end."""
    length = float("inf") if seconds is None else seconds
    picked, position, window_start = [], 0.0, None
    for duration, uri in segments:
        if duration is None or not uri:
            continue
        segment_start, segment_end = position, position + duration
        position = segment_end
        if segment_end <= start:
            continue
        if window_start is None:
            window_start = segment_start
        picked.append((duration, uri))
        if segment_end >= start + length:
            break
    return picked, (window_start or 0.0)


def _remux(ts_path: str, out_path: str, seconds, offset: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise CatalogError("ffmpeg not found on PATH (needed for MP4 output)")
    command = [ffmpeg, "-y", "-loglevel", "error"]
    if offset > 0:
        command += ["-ss", f"{offset:.3f}"]
    command += ["-i", ts_path]
    if seconds is not None:
        command += ["-t", f"{seconds:.3f}"]
    command += ["-c", "copy", "-movflags", "+faststart", out_path]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise CatalogError(f"ffmpeg failed: {(result.stderr or '').strip()[:400]}")
    if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        raise CatalogError("ffmpeg produced no output")


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #

class Catalog:
    """Entry point. One instance per origin; shows are cached by slug."""

    def __init__(self, base: str, lang: str = "dub", timeout: float = DEFAULT_TIMEOUT,
                 user_agent: str = USER_AGENT,
                 sources_endpoint: str = DEFAULT_SOURCES_ENDPOINT):
        if not base:
            raise ValueError("base URL is required")
        if urlsplit(base).scheme not in ("http", "https"):
            raise ValueError(f"base must be an http(s) URL, got {base!r}")
        self.base = base.rstrip("/")
        self.lang = lang
        self.timeout = timeout
        self.user_agent = user_agent
        self.sources_endpoint = sources_endpoint
        self._shows = {}

    def __enter__(self) -> "Catalog":
        return self

    def __exit__(self, *exc_info) -> bool:
        return False

    def __repr__(self) -> str:
        return f"<Catalog {self.base}>"

    # -- transport ---------------------------------------------------------

    def _headers(self, referer: str = None, accept: str = "application/json") -> dict:
        headers = {"User-Agent": self.user_agent, "Accept": accept,
                   "Accept-Encoding": "identity"}
        if referer:
            headers["Referer"] = referer
        return headers

    def _url(self, path: str, params: dict = None) -> str:
        return self.base + path + (("?" + urlencode(params)) if params else "")

    def _get(self, path: str, params: dict = None, referer: str = None) -> dict:
        url = self._url(path, params)
        raw = _read(url, self._headers(referer), self.timeout)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError as exc:
            raise CatalogError(f"expected JSON from {url}") from exc
        if not isinstance(data, dict):
            raise CatalogError(f"expected an object from {url}")
        return data

    # -- reads -------------------------------------------------------------

    def suggest(self, query: str) -> list:
        """Typeahead: up to ten items, no paging."""
        data = self._get("/data/suggest", {"q": query}, referer=self.base + "/")
        return [Show(item, self) for item in (data.get("anime") or [])
                if isinstance(item, dict)]

    def search(self, query: str, limit: int = None, max_pages: int = 50):
        """Iterate every match, following `has_more` until it stops."""
        page, seen = 1, 0
        while page <= max_pages:
            data = self._get("/data/browse", {"keyword": query, "page": page},
                             referer=self.base + "/search?" + urlencode({"keyword": query}))
            for item in (data.get("anime") or []):
                if isinstance(item, dict):
                    yield Show(item, self)
                    seen += 1
                    if limit is not None and seen >= limit:
                        return
            if not data.get("has_more"):
                return
            page += 1

    def search_page(self, query: str, page: int = 1) -> tuple:
        """One page as (shows, has_more)."""
        data = self._get("/data/browse", {"keyword": query, "page": page},
                         referer=self.base + "/search?" + urlencode({"keyword": query}))
        shows = [Show(item, self) for item in (data.get("anime") or [])
                 if isinstance(item, dict)]
        return shows, bool(data.get("has_more"))

    def show(self, slug: str, refresh: bool = False) -> Show:
        if not refresh and slug in self._shows:
            return self._shows[slug]
        data = self._get("/data/anime", {"slug": slug},
                         referer=self.base + "/watch/" + slug)
        anime = data.get("anime")
        item = anime[0] if isinstance(anime, list) and anime else anime
        if not isinstance(item, dict):
            raise NotFound(f"no show for slug {slug!r}")
        show = Show(item, self)
        self._shows[slug] = show
        return show

    def episode(self, slug: str, number) -> Episode:
        return self.show(slug).episode(number)

    def servers(self, chapter_id) -> dict:
        """{lang: [Server]} for one chapter."""
        data = self._get("/data/servers", {"chapter": chapter_id},
                         referer=self.base + "/")
        if data.get("ok") is False:
            raise NotFound(f"server list refused chapter {chapter_id}")
        out = {}
        for lang, entries in (data.get("servers") or {}).items():
            out[lang] = [Server(entry, self, lang) for entry in entries
                         if isinstance(entry, dict) and entry.get("link")]
        return out

    # -- playback ----------------------------------------------------------

    def resolve(self, link: str, playlist: str = None, _depth: int = 0) -> str:
        """Turn a player URL into a playlist URL.

        Accepts, in order: an explicit `playlist`, a link that already names a
        .m3u8, a response that is itself a playlist, a JSON body holding one, a
        manifest reference in the body, and finally one level of iframe.
        """
        if playlist:
            return playlist
        if not link:
            raise NoStream("server entry has no link")
        if ".m3u8" in link:
            return link

        status, headers, body = _read_response(
            link, self._headers(link, "text/html,application/json,*/*"), self.timeout)
        ctype = (headers.get("content-type") or "").lower()
        text = body.decode("utf-8", "replace")

        # Some servers serve the playlist at a path with no extension.
        if text.lstrip().startswith("#EXTM3U") or "mpegurl" in ctype:
            return link

        found = None
        if text.lstrip().startswith(("{", "[")) or "json" in ctype:
            try:
                found = _find_manifest(json.loads(text))
            except ValueError:
                found = None
        if not found:
            media = _meta_videos(text) + [m.group(1) for m in _VIDEO_TAG_RE.finditer(text)]
            found = media[0] if media else None
        if not found:
            candidates = _candidates(text)
            found = candidates[0] if candidates else None
        if not found:
            candidates = _decoded_sources(text)
            found = candidates[0] if candidates else None
        if not found and self.sources_endpoint:
            found = self._resolve_via_endpoint(link, text)
        if found:
            return self._prefer_playable(urljoin(link, found), link)

        if _depth < 1:
            iframe = _IFRAME_RE.search(text)
            if iframe:
                return self.resolve(urljoin(link, iframe.group(1)), _depth=_depth + 1)

        raise NoStream(self._no_manifest_hint(link, status, ctype, body, text))

    def _prefer_playable(self, manifest: str, player_link: str) -> str:
        """Prefer a media playlist that actually serves video.

        Some hosts hand out a master whose variant server answers with an
        image playlist.  The same variant usually exists on the master's own
        host, so retry there before falling back to the original manifest.
        """
        if not urlsplit(manifest).path.lower().endswith(".m3u8"):
            return manifest
        origin = _origin(player_link)
        headers = self._headers(origin, "*/*")
        headers["Origin"] = origin.rstrip("/")
        try:
            _, _, body = _read_response(manifest, headers, self.timeout)
        except CatalogError:
            return manifest
        text = body.decode("utf-8", "replace")
        if (not text.lstrip().startswith("#EXTM3U")
                or "#EXT-X-STREAM-INF" not in text):
            return manifest
        variants = [urljoin(manifest, ref) for ref in _variant_refs(text)]
        for variant in variants:
            if _playlist_serves_video(variant, headers, self.timeout):
                return variant
        for variant in variants:
            # The decoy lives on another host but keeps the same file name.
            name = urlsplit(variant).path.rsplit("/", 1)[-1]
            alternative = urljoin(manifest, name)
            if alternative == variant:
                continue
            if _playlist_serves_video(alternative, headers, self.timeout):
                return alternative
        return manifest

    def _resolve_via_endpoint(self, link: str, text: str):
        """Fill `sources_endpoint` from the page's data-* ids and read the JSON."""
        attrs = _data_attrs(text)
        if not attrs.get("id") and not attrs.get("mediaid"):
            return None
        endpoint = self.sources_endpoint
        values = {
            "id": attrs.get("id", ""),
            "real_id": attrs.get("realid", ""),
            "media_id": attrs.get("mediaid", ""),
            "file_version": attrs.get("fileversion", ""),
            "lang": _lang_hint(text, self.lang),
        }
        url = _fill(endpoint, values)
        if "{" in url:
            return None
        if not urlsplit(url).scheme:
            url = urljoin(link, url)
        # Do not forward the page's `s=` hint: MegaPlay answers `s=tcdn` with
        # a decoy manifest whose variants only serve ad images.
        headers = self._headers(link, "application/json")
        headers["X-Requested-With"] = "XMLHttpRequest"
        try:
            status, headers, body = _read_response(
                url, headers, self.timeout)
        except CatalogError:
            return None
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError:
            return None
        found = _find_manifest(data)
        return urljoin(url, found) if found else None

    def _no_manifest_hint(self, link, status, ctype, body, text) -> str:
        endpoints = sorted(set(_ENDPOINT_RE.findall(text)))[:6]
        preview = " ".join(text.split())[:160]
        hint = (f"no .m3u8 reference in player page {link} "
                f"(HTTP {status}, {len(body)} B, content-type {ctype or 'unknown'})")
        if endpoints:
            hint += f"; candidate endpoints: {', '.join(endpoints)}"
        if preview:
            hint += f"; body starts: {preview!r}"
        hint += "; pass the manifest directly with --playlist, or save the body "
        hint += "with `probe --dump`"
        if not self.sources_endpoint and _data_attrs(text).get("id"):
            hint += ("; this page carries data-id, so it likely fetches its source "
                     "from an API: try --sources-endpoint")
        return hint

    def probe(self, url: str, preview: int = 240,
              include_body: bool = False) -> dict:
        """Describe what a player page returns, for debugging `resolve`."""
        status, headers, body = _read_response(
            url, self._headers(url, "text/html,application/json,*/*"), self.timeout)
        text = body.decode("utf-8", "replace")
        info = {
            "url": url,
            "status": status,
            "content_type": headers.get("content-type", ""),
            "bytes": len(body),
            "manifests": [urljoin(url, c) for c in _candidates(text)][:10],
            "meta_videos": [urljoin(url, m) for m in _meta_videos(text)][:5],
            "videos": [urljoin(url, m.group(1))
                       for m in _VIDEO_TAG_RE.finditer(text)][:5],
            "iframes": [urljoin(url, m) for m in _IFRAME_RE.findall(text)][:5],
            "scripts": [urljoin(url, s)
                        for s in _SCRIPT_SRC_RE.findall(text)][:10],
            "decoded": [urljoin(url, c) for c in _decoded_sources(text)][:10],
            "packed": bool(_PACKER_RE.search(text)),
            "data_ids": _data_attrs(text),
            "endpoints": sorted(set(_ENDPOINT_RE.findall(text)))[:20],
            "preview": " ".join(text.split())[:preview],
        }
        info["endpoint_manifest"] = None
        if self.sources_endpoint:
            try:
                info["endpoint_manifest"] = self._resolve_via_endpoint(url, text)
            except CatalogError:
                info["endpoint_manifest"] = None
        if include_body:
            info["body"] = body
        return info

    def download(self, server: Server, path: str, seconds=None,
                 start: float = 0.0, playlist: str = None,
                 progress: bool = None) -> str:
        """Fetch the window and write it. `.ts` is written directly; else remuxed."""
        playlist = playlist or server.stream()
        # Some hosts (e.g. megaplay) gate the CDN on the player page origin.
        referer = playlist
        try:
            player_origin = urlsplit(getattr(server, "url", "") or "")
            if (player_origin.scheme and player_origin.hostname
                    and player_origin.hostname != urlsplit(playlist).hostname):
                referer = f"{player_origin.scheme}://{player_origin.netloc}/"
        except (ValueError, AttributeError):
            referer = playlist
        headers = self._headers(referer)
        init_uri, segments, _ = _load_playlist(playlist, headers, self.timeout)
        picked, window_start = _select(segments, start, seconds)
        if not picked:
            raise CatalogError(f"nothing covers {start:.1f}s"
                               f"{'' if seconds is None else f'..{start + seconds:.1f}s'}")

        parts = []
        bar = _Progress(len(picked) + (1 if init_uri else 0), enabled=progress)
        try:
            if init_uri:
                chunk = _read(init_uri, headers, self.timeout)
                parts.append(chunk)
                bar.update(len(chunk))
            for _, uri in picked:
                chunk = _read(uri, headers, self.timeout)
                parts.append(chunk)
                bar.update(len(chunk))
        finally:
            bar.close()
        blob = b"".join(parts)

        if path.endswith(".ts"):
            with open(path, "wb") as handle:
                handle.write(blob)
            return path

        handle, temp_path = tempfile.mkstemp(suffix=".ts", prefix="catalog-")
        try:
            with os.fdopen(handle, "wb") as out:
                out.write(blob)
            _remux(temp_path, path, seconds, offset=max(0.0, start - window_start))
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        return path


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #

def _print_table(shows) -> None:
    print(f"{'#':>3}  {'slug':<30} {'title':<36} {'eps':>5} {'score':>6}  status")
    for index, show in enumerate(shows, 1):
        print(f"{index:>3}  {show.slug[:30]:<30} {show.title[:36]:<36} "
              f"{show.episode_count:>5} {str(show.get('score') or '-'):>6}  "
              f"{show.get('status') or '-'}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Ergonomic client for a /data/* anime catalogue API.",
    )
    parser.add_argument("--base", default=os.environ.get("CATALOG_BASE"),
                        help="API origin (default: $CATALOG_BASE)")
    parser.add_argument("--lang", default="dub", choices=("sub", "dub"),
                        help="preferred server language (default: dub)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    parser.add_argument("--sources-endpoint",
                        default=os.environ.get("CATALOG_SOURCES_ENDPOINT",
                                               DEFAULT_SOURCES_ENDPOINT),
                        metavar="TEMPLATE",
                        help="player API template (default: %(default)s; "
                             "empty string disables)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="search titles")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("suggest", help="typeahead")
    p.add_argument("query")

    p = sub.add_parser("show", help="show detail and episodes")
    p.add_argument("slug")

    p = sub.add_parser("episodes", help="list episode numbers")
    p.add_argument("slug")
    p.add_argument("--all", action="store_true", help="print every episode")

    p = sub.add_parser("stream", help="resolve a playlist URL")
    p.add_argument("slug")
    p.add_argument("episode")
    p.add_argument("--server", default=None)
    p.add_argument("--playlist", default=None,
                   help="use this manifest instead of resolving the player page")

    p = sub.add_parser("download", help="write a clip")
    p.add_argument("slug")
    p.add_argument("episode")
    p.add_argument("-o", "--out", required=True)
    p.add_argument("-s", "--seconds", type=float, default=None)
    p.add_argument("--start", type=float, default=0.0)
    p.add_argument("--server", default=None)
    p.add_argument("--playlist", default=None,
                   help="use this manifest instead of resolving the player page")
    p.add_argument("--no-progress", action="store_true",
                   help="disable the download progress bar")

    p = sub.add_parser("probe", help="inspect a player page and list candidates")
    p.add_argument("url")
    p.add_argument("--dump", default=None, metavar="PATH",
                   help="save the raw body here")
    p.add_argument("--preview", type=int, default=240, metavar="N",
                   help="preview length in characters (default 240)")

    args = parser.parse_args(argv)
    if not args.base:
        print("error: --base is required (or set CATALOG_BASE)", file=sys.stderr)
        return 2

    try:
        catalog = Catalog(args.base, lang=args.lang, timeout=args.timeout,
                          sources_endpoint=args.sources_endpoint)

        if args.command == "search":
            shows = list(catalog.search(args.query, limit=args.limit))
            if args.json:
                print(json.dumps([s.raw for s in shows], indent=2))
            else:
                _print_table(shows)
                if not shows:
                    print("no matches")

        elif args.command == "suggest":
            shows = catalog.suggest(args.query)
            if args.json:
                print(json.dumps([s.raw for s in shows], indent=2))
            else:
                for show in shows:
                    print(f"{show.slug}\t{show.title}")

        elif args.command == "show":
            show = catalog.show(args.slug)
            if args.json:
                print(json.dumps(show.raw, indent=2))
            else:
                print(f"{show.title} [{show.slug}]")
                print(f"  type={show.get('type')} status={show.get('status')} "
                      f"episodes={show.episode_count} year={show.get('year')}")
                genres = [g.get("name") if isinstance(g, dict) else g
                          for g in (show.get("genres") or [])]
                if genres:
                    print(f"  genres: {', '.join(str(g) for g in genres)}")
                for episode in show.episodes[:10]:
                    print(f"  {str(episode.number):>5}  {episode.title[:60]}")
                if len(show) > 10:
                    print(f"  ... {len(show) - 10} more (use `episodes --all`)")

        elif args.command == "episodes":
            show = catalog.show(args.slug)
            chosen = show.episodes if args.all else show.episodes[:20]
            if args.json:
                print(json.dumps([e.raw for e in chosen], indent=2))
            else:
                for episode in chosen:
                    print(f"{episode.number}\t{episode.id}\t{episode.title}")
                if not args.all and len(show) > len(chosen):
                    print(f"... {len(show) - len(chosen)} more (use --all)")

        elif args.command == "stream":
            episode = catalog.episode(args.slug, args.episode)
            print(episode.stream(server=args.server, playlist=args.playlist))

        elif args.command == "download":
            episode = catalog.episode(args.slug, args.episode)
            path = episode.download(args.out, seconds=args.seconds,
                                    start=args.start, server=args.server,
                                    playlist=args.playlist,
                                    progress=False if args.no_progress else None)
            size = os.path.getsize(path)
            print(f"wrote {path} ({size / 1024:.1f} KiB)")

        elif args.command == "probe":
            info = catalog.probe(args.url, preview=args.preview,
                                 include_body=bool(args.dump))
            body = info.pop("body", None)
            if args.dump and body is not None:
                with open(args.dump, "wb") as handle:
                    handle.write(body)
                print(f"dumped {len(body):,} B to {args.dump}", file=sys.stderr)
            if args.json:
                print(json.dumps(info, indent=2))
            else:
                print(f"url         : {info['url']}")
                print(f"status      : {info['status']}")
                print(f"content-type: {info['content_type'] or 'unknown'}")
                print(f"bytes       : {info['bytes']:,}")
                print(f"manifests   : {info['manifests'] or 'none found'}")
                print(f"meta videos : {info['meta_videos'] or 'none'}")
                print(f"videos      : {info['videos'] or 'none'}")
                print(f"iframes     : {info['iframes'] or 'none'}")
                print(f"scripts     : {info['scripts'] or 'none'}")
                print(f"decoded     : {info['decoded'] or 'none'}")
                print(f"packed      : {info['packed']}")
                print(f"data ids    : {info['data_ids'] or 'none'}")
                print(f"endpoints   : {info['endpoints'] or 'none'}")
                if info.get("endpoint_manifest"):
                    print(f"endpoint    : {info['endpoint_manifest']}")
                print(f"preview     : {info['preview']}")

    except (CatalogError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
