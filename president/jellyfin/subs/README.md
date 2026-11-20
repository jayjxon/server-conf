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

The catch that made a backfill necessary: Subf2m (and every subbuzz provider) searches by
**IMDb id**, and this library is TMDB-only — all 222,483 items had none, so every search
returned `Search info or IMDB ID missing`. Hence two scripts, not one.

## The pipeline

Run in this order. Each step is resumable: nothing is redone.

1.  `resolve_imdb.py` — fills `BaseItemProviders(ProviderId='Imdb')` for movies and series,
    using IMDb's keyless suggestion endpoint (no key, no login). Episodes need no id of their
    own: subbuzz inherits the series' id.
2.  `fetch_subs.py` — asks jellyfin's own subtitle downloader for candidates, keeps the
    Subf2m ones, downloads the first, then refreshes the item so the new file is registered.
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
sudo chmod 644 /tmp/buffy.key

# step 1 (hours), step 2+3 (days) — setsid so they survive the ssh session
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

Throughput is **~17 items/min sustained at `--workers 3`**, so a 1,400-item queue takes ~85 min
and the full catalogue would take **~9 days** if every series resolves. Both jobs were left
running; `MediaStreamInfos` row counts are *not* a progress signal (they are transient), count
files or the API instead.

Timing is worth stating plainly: step 1 is hours, step 2/3 is days, and it is I/O to Subf2m,
not CPU. Do not raise `--workers` past 3 without measuring — Subf2m starts returning zero
candidates for concurrent searches of the same series, and the IMDb endpoint throttles (at 6
workers it returned empty `d`, logged as `http_fail`).

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
-   Addic7ed's crash is inside the compiled plugin, unfixable here; the candidate filter
    (`"subf2m" in Id`) skips it deliberately.
-   Subf2m may throttle or start answering with Cloudflare challenges over a multi-day run.
    If `ok` stops climbing, restart the loop after a pause.
