# Posters for the VOD library

One-off script kept next to the subtitle backfill (`../subs/`), for the same reason: it is the
exact version that ran on 2026-11-20.

## What it does

Lists every movie and series in jellyfin that has no `Primary` image, keeps the ones that
already carry a TMDB or IMDb id, and asks jellyfin to fetch the missing poster
(`POST /Items/{id}/Refresh?imageRefreshMode=FullRefresh&replaceAllImages=false`). Resumable:
processed ids go to `done.txt`, `--limit N` is a smoke test, `--workers N` changes parallelism.
An item counts as `ok` only when `ImageTags.Primary` exists afterwards — a `204` from jellyfin
means "accepted", not "downloaded", the same rule as the subtitle scripts.

## Audit result (2026-11-20)

| Library | Items | No poster | Of those, with a TMDB/IMDb id |
| --- | --- | --- | --- |
| Movies | 18,583 | 3,039 | 2,968 |
| Series | 6,324 | 24 | 23 |

Backdrops are missing for *every* item (18,583 / 6,324) — that is how the catalogue arrives
from dispatcharr, not drift. This script only targets posters.

## Why the run fetched nothing

The run walked 2,991 items in ~90 s and reported `{'ok': 0, 'noimage': 2991, 'err': 0}`. That
is not a script failure; it is the library configuration:

-   `config/root/default/Movies/options.xml` and `Shows/options.xml` both have
    `EnableInternetProviders` `false` and empty `ImageFetchers` / `MetadataFetchers` lists;
-   `config/plugins/configurations/Jellyfin.Plugin.Tmdb.xml` has an empty `<TmdbApiKey />`.

So jellyfin accepts the refresh, has no provider to ask, and saves nothing (its log shows
`ProviderManager.SaveImage` failing to connect). Both settings are host state, not repo state.

To make posters possible: put a free TMDB API key in that plugin config and enable remote
images for the two libraries. That is **not** a one-line change to make casually — the same
switch (`EnableInternetProviders`) also enables metadata fetching, which would overwrite the
titles dispatcharr imported for 222k `.strm` items. Test it on one folder before applying it
library-wide, then `rm /srv/apps/jellyfin/images/done.txt` and re-run so the 2,991 skipped ids
are retried.

## Run it

```bash
# one item, to confirm the plumbing before queueing 3k refreshes
sudo python3 /srv/apps/jellyfin/images/fetch_posters.py --limit 1 --workers 1

# the real run (logs to posters.log / posters.out, survives the ssh session)
sudo setsid sh -c 'python3 /srv/apps/jellyfin/images/fetch_posters.py --workers 2 \
    >> /srv/apps/jellyfin/images/posters.out 2>&1 < /dev/null &'

# watch / stop
sudo tail -3 /srv/apps/jellyfin/images/posters.log
sudo grep -c . /srv/apps/jellyfin/images/done.txt
sudo pkill -f fetch_posters.py
```

It reads the API key from `/tmp/buffy.key` (same temporary `buffy-temp` key as the subtitle
scripts, created with the commands in `../subs/README.md`) and finds the container address with
`docker inspect jellyfin`. Run it under `sudo`; the key is root-only.

Timing: the refresh itself is fire-and-forget (2,991 items in ~90 s at two workers). Real
download time only shows up once a provider is configured, and will be dominated by TMDB
latency — expect roughly 0.3–1 s per image, so 15–50 min for the 3k backlog.
