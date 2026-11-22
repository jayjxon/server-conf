# Subtitle backfill for the VOD library

Three one-off scripts that pulled free English subtitles for the IPTV VOD library on
`stuff.jjay.dev` (jellyfin, 222,483 `.strm` items: 18,583 movies, 6,324 series,
203,898 episodes). They are kept here as the exact versions that ran on 2026-11-20, so a
re-run behaves the same. The service itself lives in `../compose.yaml`.

## Why this exists

The documented route is bazarr (`README.md`, "Subtitles for the VOD library"), but it can
only recover tracks **embedded** in the file, and it only manages media Sonarr/Radarr index.
Neither covers a `.strm` catalogue, so most items had no subtitle at all.

The free web providers that would cover it are gated:

-   opensubtitles.com needs an account (the legacy `api.opensubtitles.org` anonymous route
    answers `401`, dead);
-   subscene answers `403`, podnapisi is gone, yify wants an IMDb id, addic7ed works but
    crashes subbuzz 1.5.1 on its own duplicate-key bug.

**Subf2m via subbuzz** is the only keyless, quota-free source worth it. It is what these
scripts drive. No account, no API keys bought, nothing paid.

The fulfiller in `../fulfiller` now uses subbuzz's OpenSubtitles.com provider as a fallback
when Subf2m returns no candidates. The account lives in the host's `subbuzz.xml`, not in this
repository; these one-off scripts still select Subf2m only.

The catch that made a backfill necessary: Subf2m (and every subbuzz provider) searches by
**IMDb id**, and this library is TMDB-only — all 222,483 items had none, so every search
returned `Search info or IMDB ID missing`. Hence two scripts, not one.

## The pipeline

Run in this order. Each step is resumable: nothing is redone.

1.  `resolve_imdb.py` — fills `BaseItemProviders(ProviderId='Imdb')` for movies and series,
    using IMDb's keyless suggestion endpoint (no key, no login). Episodes need no id of their
    own: subbuzz inherits the series' id.
2.  `fetch_subs.py` — asks jellyfin's own subtitle downloader for candidates, keeps the
    Subf2m ones, downloads the first. No follow-up refresh call: the downloader already
    registers the file (measured 2026-11-21, 46/46 downloaded items had the stream).
3.  `run_fetch_loop.sh` — reruns step 2 until no un-attempted item is left. Needed because
    step 2 snapshots its work list at start, and step 1 is still adding ids.

State lives in three places on the host, all under `/srv/apps/jellyfin/subs/`:
`attempted.txt` (ids step 2 already tried, one per line, the skip list), `fetch.log` /
`resolve.log` (timestamps + counters), and the subtitles themselves at
`/srv/apps/jellyfin/config/metadata/library/<xx>/<itemid>/<name>.en.srt`.

## Run it

```bash
# one-time: a jellyfin API key. Admin endpoints are 401 even with a valid key;
# these two are the ones the script needs.
sudo python3 -c "
import sqlite3,uuid
c=sqlite3.connect('/srv/apps/jellyfin/config/data/jellyfin.db')
c.execute('insert into ApiKeys(AccessToken,Name) values(?,?)',(uuid.uuid4().hex.replace('-',''),'buffy-temp'))
c.commit()"
docker restart jellyfin            # keys are read at startup
sudo -i python3 -c "
import sqlite3;c=sqlite3.connect('/srv/apps/jellyfin/config/data/jellyfin.db')
print(c.execute(\"select AccessToken from ApiKeys where Name='buffy-temp'\").fetchone()[0])" > /tmp/buffy.key
sudo chmod 600 /tmp/buffy.key      # root-only; every script below runs under sudo

# step 1 (hours), step 2+3 (hours at 24 workers) — setsid so they survive the ssh session
sudo setsid nohup python3 /srv/apps/jellyfin/subs/resolve_imdb.py >> /srv/apps/jellyfin/subs/resolve.out 2>&1 &
sudo setsid nohup /srv/apps/jellyfin/subs/run_fetch_loop.sh >/dev/null 2>&1 &

# watch
sudo tail -3 /srv/apps/jellyfin/subs/fetch.log /srv/apps/jellyfin/subs/resolve.log
sudo tail -3 /srv/apps/jellyfin/subs/loop.log
wc -l /srv/apps/jellyfin/subs/attempted.txt

# stop
sudo pkill -f run_fetch_loop; sudo pkill -f fetch_subs.py; sudo pkill -f resolve_imdb.py

# when finished, drop the temporary key
docker exec jellyfin true   # no-op, keeps the pattern obvious
sudo python3 -c "
import sqlite3;c=sqlite3.connect('/srv/apps/jellyfin/config/data/jellyfin.db')
c.execute(\"delete from ApiKeys where Name='buffy-temp'\");c.commit()"
docker restart jellyfin
```

The scripts hardcode `/srv/apps/jellyfin` (i.e. `APP_ROOT=/srv/apps`) and `/tmp/buffy.key`.
Edit `DB`, `BASE_DIR` and `LOG` at the top if the host differs. `--series <BaseItems.Id>`
limits step 2 to one show, `--limit N` to a smoke test, `--workers N` to change parallelism.

## Results measured (2026-11-20, ~22:10 host clock UTC-5, jobs still running)

| Stage | Result |
| --- | --- |
| IMDb ids | 711 → 3,362 in the first 27 min (~80/min); 24,905 to do, so ~4.5 h total |
| Resolver match mix | roughly 55% exact, 25% fuzzy, 20% no match — fuzzy lines are logged for review |
| Subtitles | 381 `.srt` files, 460 items attempted, 304 `ok` / 71 `nosub` / 0 `err` (81% per item) |
| Playback check | `GET /Users/{uid}/Items/{id}` returns `English - SUBRIP - External` for fresh downloads |
| Named titles | *I Want to Eat Your Pancreas* (2018) and Attack on Titan S02/S03 verified playable |

Throughput is **~15 items/min at `--workers 3`**, so a 1,400-item queue takes ~90 min. That
first run was too slow; see "Speeding it up" below. `MediaStreamInfos` row counts are *not* a
progress signal (they are transient), count files or the API instead.

## Speeding it up (measured 2026-11-21)

Three findings, in order of impact.

1.  **The Refresh call was unnecessary.** The download endpoint already registers the file,
    so `fetch_subs.py` no longer POSTs `/Items/{id}/Refresh` afterwards. Removing it saved one
    request per item and nothing else: 46 of 46 items checked through
    `/Users/{uid}/Items/{id}` advertised `English - SUBRIP - External` without it.
2.  **Worker count was the real limit.** A search costs ~10 s of remote latency, a download
    0.02 s, and jellyfin sits at 0.02% CPU, so this is entirely Subf2m I/O. `bench_workers.py`
    (in this directory) timed fresh item slices at several pool sizes:

    | workers | measured (bench, movie slice) | median search | note |
    | --- | --- | --- | --- |
    | 3 | 14.8 items/min | 10.1 s | the original setting |
    | 12 | 34.9 items/min | 15.2 s | |
    | 24 | 45.6 items/min | 29.8 s | chosen |
    | 40 | 45.8 items/min | 33.4 s | no gain; `HttpConnectionPool` connect timeouts in the log |

    Past 24 workers Subf2m stops giving more throughput and jellyfin starts logging
    `A connection could not be established within the configured ConnectTimeout`, so 24 is the
    ceiling. `run_fetch_loop.sh` now passes `--workers 24`.
3.  **Series batches run faster than movies.** The live queue at 24 workers did **~90
    items/min sustained** over an 8-minute window (575 -> 1,275 items, `err 0`), well above the
    45.6/min movie slice: episodes of one show repeat the same Subf2m lookup, so most of the
    page is already warm. Expect the rate to swing between ~45 and ~125/min depending on
    whether the current slice is movies or episodes.

Measured effect on this catalogue: the 11,543-item ready queue went from ~12 h to ~2 h. Two
verified samples: 10 of 14 freshly attempted movies advertised an external subtitle (the
other 4 were `nosub`, no candidate on Subf2m), and `.srt` files rose 2,134 -> 2,707 while the
measurement ran.

To repeat the measurement, stop the loop first (an item can otherwise be downloaded by both
processes), then run arms of `~36` items: `bench_workers.py --workers 3 --limit 36 --verify`,
`--workers 24 --limit 36 --verify`. Both append to `attempted.txt`, so the work is not
wasted.

## Verified how

`fetch_subs.py` writes `ok` on a `204`, which means "saved", not "plays". The honest check is
the media-source list jellyfin builds at playback time:

```bash
docker exec jellyfin curl -s -H "Authorization: MediaBrowser Token=\"$(cat /tmp/buffy.key)\"" \
  "http://localhost:8096/Users/<uid>/Items/<itemid>" |
  python3 -c "import json,sys;[print(s.get('DisplayTitle'),s.get('IsExternal'))
    for m in json.load(sys.stdin)['MediaSources'] for s in m['MediaStreams'] if s['Type']=='Subtitle']"
```

Port 8096 is not reachable from the host shell, so go through `docker exec` (or the container
IP, which the script resolves with `docker inspect`).

## Prerequisites on the host (already applied, not in the repo)

These live in `${APP_ROOT}/jellyfin/config`, which is host state, so a fresh deployment needs
them again:

-   `plugins/configurations/subbuzz.xml`: `EnableSubf2m=true`, `EnableAddic7ed=true`,
    `MinScore=20` (was 50 — Subf2m's scores are low).
-   `root/default/Shows/options.xml`: exactly one `RequirePerfectSubtitleMatch`, set `false`;
    a duplicated element makes the serializer drop settings. `Movies/options.xml` keeps
    `SubtitleDownloadLanguages=eng`.
-   `SaveSubtitlesWithMedia` must stay `false`: the VOD tree is mounted read-only, so
    sidecars have to land under `config/metadata/library` instead.

## Known limits

-   **Attack on Titan season 1 gets nothing.** Subf2m keeps a whole show on one page
    (`/subtitles/attack-on-titan-2013`); subbuzz asks for a page named after the season, gets
    none, and reports zero candidates. S02/S03 have real season pages and work. Fixing this
    needs a direct Subf2m scraper or a second source, not a config change.
-   **~20% of titles never get an IMDb id** (obscure, foreign, misnamed), so they are never
    queued — the `READY` SQL in `fetch_subs.py` requires one. Check `resolve.log` for
    `fuzzy <name> [year] -> tt…` lines: those are matches by substring, occasionally wrong.
-   **`resolve_imdb.py` stays at 3 workers.** IMDb's suggestion endpoint answers with an
    empty `d` under more concurrency (seen at 6 workers, logged as `http_fail`), so only the
    subtitle fetcher's pool was widened. Widening the resolver is a separate benchmark.
-   Addic7ed's crash is inside the compiled plugin, unfixable here; the candidate filter
    (`"subf2m" in Id`) skips it deliberately.
-   Subf2m may throttle or start answering with Cloudflare challenges over a multi-day run.
    If `ok` stops climbing, restart the loop after a pause.
-   **A container recreate changes the IP `fetch_subs.py` talks to.** The runner resolves the
    address once at startup, so a `docker compose up -d` during a run makes every search fail
    instantly and be written to `attempted.txt` as `err` (2026-11-21: 50,677 items in under a
    minute, all recoverable only by trimming the file). Since that day `api()` re-resolves the
    address once on a connection error, but the honest rule is still: recreate jellyfin while
    the loop is stopped.

## The missing `node` (2026-11-21)

One addition from the day the backfill was running: subbuzz needs a binary the image does
not ship, so compose mounts one in.

### `setup_node.sh` — the JavaScript challenge solver needs a binary the image lacks

`Providers/Subf2m.cs` answers an `InvalidHtmlException` by running
`node -e "console.log('__arcsjs='+eval(…))"` and setting the resulting cookie. The jellyfin
image has no node, so that path could never work. The script installs a pinned static build
(arm64 or x64, detected), copies it into the running container **without a restart**, and the
compose file mounts the same file over `/usr/local/bin/node` so a recreate keeps it.

```bash
sudo ./setup_node.sh                     # /srv/apps/jellyfin/subs/bin/node, v22.11.0
```

Run it **before** the first `docker compose up -d`: a missing bind-mount source is created as
a directory, which would shadow the binary. Measured on this host: aarch64, node v22.11.0,
110 MB, runs as uid 1001 inside the container. Its effect on real challenges is **not yet
measured** — no `Cookie set` line had appeared at the time of writing, because subbuzz still
serves those pages from its four-hour search cache. Revert by removing the volume, deleting
`/usr/local/bin/node` from the container and recreating; the plugin just logs the old failure.
