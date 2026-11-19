# server-confs

The docker compose configurations for my extremely hacky and messy server setup.

## What's in here?

The VPS (from here on referred to as president) runs on an Oracle Cloud Free Tier machine,
and is pretty much only used for exposing the services I run on my local machine to the internet.

On the VPS, I have the following services running:

-   [x] Traefik
-   [x] Authelia
-   [x] Homepage
-   [x] Jellyfin
-   [x] Dispatcharr
-   [x] Pingvin Share
-   [x] Static site
-   [x] CrowdSec

On the local machine, I have the following services running:

-   [x] Traefik
-   [x] qbittorrent (& gluetun)
-   [x] aria2
-   [x] Sonarr
-   [x] Radarr
-   [x] Prowlarr
-   [x] flaresolverr
-   [x] cross-seed

## Repository layout

-   `president/` — the public VPS stack (traefik, authelia, homepage, jellyfin, pingvin, static, crowdsec)
-   `media/` — the home server stack (traefik + tailscale nodes, *arrs, qbittorrent/gluetun, aria2, cross-seed)
-   `common/` — socket proxy and traefik definitions shared by both stacks
-   everything outside `president/` and `media/` at the top level is docs or devenv plumbing

## Before you deploy

Everything in this section has to be done once, in order, before the first
`docker compose up -d`.

### 0. Host requirements

-   Docker Engine with Compose v2 (`docker compose version` must work). The configs
    use `include:`, which needs Compose >= 2.20.
-   **president**: a public IP with **TCP 80 and 443** reachable. 80 is only used for the
    http -> https redirect; certificates are issued with the Cloudflare **DNS-01** challenge.
    On Oracle Cloud this also means opening 80/443 in the **VCN security list**: the stock
    image's nftables chain only accepts 22, and published container ports are DNATed past
    that chain, so a closed security list looks like a silent connection timeout.
-   **media**: `/dev/net/tun` must exist and the host must allow `NET_ADMIN` (needed by
    `gluetun` and by both tailscale nodes). The president exit node needs `/dev/net/tun` and
    `NET_ADMIN` as well.
-   A Cloudflare API token with `Zone -> DNS -> Edit` and `Zone -> Zone -> Read` on the zone.
-   DNS records (A/AAAA to the VPS): `DOMAINNAME` (apex), `auth.DOMAINNAME`,
    `traefik.DOMAINNAME`, `jellyfin.DOMAINNAME`, `pingvin.DOMAINNAME`,
    `dispatcharr.DOMAINNAME`.
    For media: `DOMAINNAME` (`media.domain.tld`) and `INTERNAL_DOMAINNAME`
    (`media.internal.domain.tld`) must resolve to the external / internal tailnet addresses.
-   The VPS needs no GPU: jellyfin runs there without hardware transcoding and reads its
    library from `${APP_ROOT}/jellyfin/media`.

### 1. Create the env files

Each stack reads its own `.env` next to the compose file (git-ignored):

```bash
cd president && cp example.env .env
cd ../media && cp example.env .env
```

#### president/.env

| Variable | Required | Notes |
| --- | --- | --- |
| `PUID` / `PGID` | yes | uid/gid every service runs as; owns `APP_ROOT` |
| `TZ` | yes | e.g. `America/New_York` |
| `DOMAINNAME` | yes | bare domain, no scheme or slashes |
| `APP_ROOT` | yes | absolute path to the private app data on the VPS |
| `CF_API_EMAIL` | yes | Cloudflare account email |
| `CF_DNS_API_TOKEN` | yes | token described above |
| `DISPATCHARR_SECRET_KEY` | yes | Django signing key, 50+ random alphanumerics (`openssl rand -hex 32`) |
| `DISPATCHARR_SSO_USER` | no | dispatcharr user everyone signs in as, defaults to `admin` |
| `AUTHELIA_OIDC_HMAC_SECRET` | yes | hmac key for authelia's oidc provider |
| `PINGVIN_OIDC_CLIENT_SECRET` | yes | plaintext client secret handed to pingvin |
| `PINGVIN_OIDC_CLIENT_SECRET_HASH` | yes | pbkdf2 hash of the same secret, single-quoted |
| `AUTHELIA_ADMIN_PASSWORD_HASH` | yes | argon2id digest of the admin password, single-quoted (step 3) |
| `AUTHELIA_SESSION_SECRET` | yes | 32+ random bytes, hex |
| `AUTHELIA_STORAGE_ENCRYPTION_KEY` | yes | 32+ random bytes, hex |
| `CROWDSEC_BOUNCER_LAPI_KEY` | yes | bouncer key, see step 6 |
| `PINGVIN_STORAGE_SIZE_MEGABITS` | only for `setup.sh` | see step 4 |
| `UPTIME_KUMA_SLUG` | no | extra homepage widget |
| `TAILSCALE_EXIT_NODE_AUTHKEY` | only for the exit node | tailscale auth key, see the exit node section |

Generate the authelia secrets:

```bash
echo "AUTHELIA_SESSION_SECRET=$(openssl rand -hex 32)" >> .env
echo "AUTHELIA_STORAGE_ENCRYPTION_KEY=$(openssl rand -hex 32)" >> .env
```

`AUTHELIA_ADMIN_PASSWORD_HASH` is generated in step 3, it needs the password itself.

#### media/.env

| Variable | Required | Notes |
| --- | --- | --- |
| `PUID` / `PGID` | yes | uid/gid every service runs as |
| `TZ` | yes | used by every container |
| `DOMAINNAME` | yes | public name of the media stack |
| `INTERNAL_DOMAINNAME` | yes | tailnet-only name for the internal routes |
| `ROOT_DOMAINNAME` | yes | the president domain |
| `APP_ROOT` | yes | absolute path to app data on the media host |
| `DATA_MOUNT_DIR` | yes | where the media library lives |
| `QBITTORRENT_INCOMPLETE_MOUNT` | yes | scratch space for incomplete torrents |
| `TORRENT_PORT` | yes | port forwarded by gluetun for torrent traffic |
| `TP_THEME` | yes | theme.park theme name for the *arrs |
| `WIREGUARD_PROVIDER`, `WIREGUARD_PRIVATE_KEY`, `WIREGUARD_ADDRESSES`, `WIREGUARD_PRESHARED_KEY`, `WIREGUARD_SERVER_CITIES` | yes | gluetun VPN config |
| `CF_API_EMAIL`, `CF_DNS_API_TOKEN` | yes | the media traefik also gets its cert via DNS-01 |
| `ARIA2_RPC_SECRET` | yes | generate, see below |
| `HEADSCALE_INTERNAL_AUTHKEY`, `HEADSCALE_EXTERNAL_AUTHKEY` | yes, see step 7 | pre-auth keys for the two tailscale ingress nodes |

```bash
echo "ARIA2_RPC_SECRET=$(openssl rand -base64 60 | tr -d '\n')" >> .env
```

`media/example.env` also carries optional `LOG_LEVEL`, `LOG_HTML` and `CAPTCHA_SOLVER`
for flaresolverr.

### 2. Create the data directories

Docker will create missing bind-mount sources as root-owned directories, which then breaks
the services that run as `PUID`. Create them up front.

president:

```bash
cd president && . ./.env
mkdir -p "$APP_ROOT"/{traefik/{data,logs,plugins},authelia/config,crowdsec/{config,data,logs},homepage/logs,jellyfin/{config,cache,media},dispatcharr/{data,vods},pingvin/{data,images,uploads/uploads},tailscale/{data,cache}}
chown -R "$PUID:$PGID" "$APP_ROOT"
```

media:

```bash
cd media && . ./.env
mkdir -p "$APP_ROOT"/{aria/{data,session},cross-seed/{config,torrents},prowlarr/config,qbittorrent/config,radarr/{config,scripts},sonarr/{config,scripts},tailscale-internal/state,tailscale-external/state,traefik-tailscale/{data,logs},traefik-external/{data,logs}}
mkdir -p "$DATA_MOUNT_DIR" "$QBITTORRENT_INCOMPLETE_MOUNT"
chown -R "$PUID:$PGID" "$APP_ROOT" "$DATA_MOUNT_DIR" "$QBITTORRENT_INCOMPLETE_MOUNT"
```

Notes:

-   `${APP_ROOT}/authelia/config` holds authelia's state (`db.sqlite3`, `notifications.txt`).
    The configuration and the users database are mounted in from the repo, so only the state
    lives here, and it must stay writable by `PUID`.
-   `${APP_ROOT}/crowdsec/{config,data,logs}` is populated by the crowdsec image on first start.
    crowdsec runs as root (the image bootstraps its config with `rsync` from a root-owned
    `/staging`), so those directories only need to be writable by root.
-   `${APP_ROOT}/traefik/data` holds `acme.json`; traefik creates it with `0600` itself.
-   `${APP_ROOT}/traefik/plugins` caches the crowdsec bouncer plugin.
-   `${APP_ROOT}/dispatcharr/data` is everything dispatcharr owns — its postgres and redis
    run inside the container, so nothing else on the host or network is needed.

### 3. Configure Authelia

Authelia reads its configuration straight out of the repo, there is no render step:

-   `president/authelia/config/configuration.yml` is mounted read-only at
    `/config/configuration.yml`, and authelia expands its `${...}` placeholders itself
    (`X_AUTHELIA_CONFIG_FILTERS=expand-env`);
-   the session secret and the storage encryption key are set through authelia's own
    environment variables, `AUTHELIA_SESSION_SECRET` and
    `AUTHELIA_STORAGE_ENCRYPTION_KEY`;
-   `/config/users_database.yml` is a compose config built from
    `AUTHELIA_ADMIN_PASSWORD_HASH`, i.e. the `admin` user with `admin@DOMAINNAME` in `admins`.

Generate the digest and append it to `.env` in **single quotes**:

```bash
cd president
docker run --rm authelia/authelia:4.39.28 \
    authelia crypto hash generate argon2 --password 'your-password' |
    sed -n 's/^Digest: //p' |
    xargs -I{} echo "AUTHELIA_ADMIN_PASSWORD_HASH='{}'" >> .env
```

The quoting matters: compose interpolates `.env` values, so an unquoted
`$argon2id$v=19$...` loses everything after the first `$`. Single quotes (or doubling every
`$`) keep it intact. To change the password later, regenerate the hash and recreate the
container — a plain `up -d` leaves it on the config it was created with, so the new hash is
only picked up by a recreate:

```bash
docker compose up -d --force-recreate authelia
```

#### OIDC for pingvin share

Authelia is the OIDC provider. The client is declared in `configuration.yml` (id `pingvin`,
callback `https://pingvin.DOMAINNAME/api/oauth/callback/oidc`) and its secrets live in `.env`,
so nothing is clicked in a UI. Two things must exist before authelia starts:

```bash
cd president && . ./.env

# signing key: generated once, kept next to authelia's state
mkdir -p "$APP_ROOT/authelia/config/oidc"
openssl genrsa -out "$APP_ROOT/authelia/config/oidc/issuer.pem" 4096
chmod 600 "$APP_ROOT/authelia/config/oidc/issuer.pem"

# client secret: plaintext for pingvin, pbkdf2 hash for authelia
SECRET=$(openssl rand -hex 32)
echo "PINGVIN_OIDC_CLIENT_SECRET=$SECRET" >> .env
docker run --rm --entrypoint authelia authelia/authelia:4.39.28 \
    crypto hash generate pbkdf2 --password "$SECRET" |
    sed -n 's/^Digest: //p' |
    xargs -I{} echo "PINGVIN_OIDC_CLIENT_SECRET_HASH='{}'" >> .env

echo "AUTHELIA_OIDC_HMAC_SECRET=$(openssl rand -hex 32)" >> .env
```

Pingvin's own settings are a compose config mounted at `/opt/app/config.yaml`: the app name,
URL, cookie and registration flags that were set in its UI, plus the OIDC client. While that
file exists, pingvin's admin UI is read-only for those keys — change the values in
`president/pingvin/compose.yaml` and recreate the container instead.

1.  Authelia protects **three** routes — the homepage at `DOMAINNAME`, the traefik dashboard at
    `traefik.DOMAINNAME` and dispatcharr at `dispatcharr.DOMAINNAME`. Dispatcharr is narrower
    than the other two: its access-control rule requires the `admins` group, so any other
    authelia user is refused with 403. That group is the `groups:` list on the user in
    `president/authelia/compose.yaml`.

    `jellyfin.`, `pingvin.`, `static` (`.well-known`) and the `auth.` portal itself are public;
    jellyfin enforces its own auth and pingvin signs in through authelia's OIDC provider.

2.  The first login also enrols a TOTP device (the `totp` section is enabled), so have an
    authenticator app ready.

### 4. Pingvin storage image (optional)

`president/setup.sh` preallocates `PINGVIN_STORAGE_SIZE_MEGABITS` MiB at
`${APP_ROOT}/pingvin/uploads/pingvin.img` and runs `mkfs.ext4` on it:

```bash
cd president && ./setup.sh
```

The active compose file mounts `${APP_ROOT}/pingvin/uploads/uploads` as a plain directory.
The image is only used if you uncomment the `volumes:` block at the bottom of
`president/pingvin/compose.yaml` that mounts `pingvin.img`. Skipping `setup.sh` is fine if
you keep the directory mount.

### 5. Start the VPS stack

`traefik` requires `CROWDSEC_BOUNCER_LAPI_KEY`, but that key can only be created after
crowdsec has run once. On a fresh deploy, bootstrap in three moves:

```bash
cd president
# 1. validate the config and start what crowdsec needs, with a throwaway key for traefik
CROWDSEC_BOUNCER_LAPI_KEY=placeholder docker compose config -q   # fail fast on missing env
CROWDSEC_BOUNCER_LAPI_KEY=placeholder docker compose up -d socket-proxy crowdsec

# 2. register the bouncer (step 6) and write the real key into .env

# 3. now everything else can come up, traefik and authelia included
docker compose up -d
docker compose ps
```

Order matters in one place: `traefik` needs `socket-proxy` (compose enforces it). `authelia`
has no render step, it starts straight from the mounted config.

Dispatcharr sits behind authelia, so the first visit redirects to `auth.DOMAINNAME`. It keeps no
login of its own: the router adds a `Remote-User` header, and dispatcharr exchanges that header
for a session on every page load. It needs `DISPATCHARR_SECRET_KEY` in `.env` (mapped to
`DJANGO_SECRET_KEY`): the image generates one into `/data` on first boot, but releases from
0.13.1 on fail to start when a fresh volume has none.

That hand-off is automatic: `dispatcharr-init` in the compose file shares dispatcharr's network
namespace (its postgres and redis live inside that container, so `localhost` is the only route to
them), waits for the database, and ensures the `admin` user and the `reverse_proxy_auth` row
exist. It runs on every `docker compose up -d`, does nothing once they are there, and exits 0. If
it ever fails, `docker compose logs dispatcharr-init` says why. Without that user the UI serves
the first-run setup page instead, which refuses connections from outside the local network.

The header value is `DISPATCHARR_SSO_USER` (default `admin`) and has to match that username;
change the two together and recreate the container. The header is only believed when the
request comes from `DISPATCHARR_TRUSTED_PROXIES` (`10.120.0.0/24`, traefik's subnet), and
traefik overwrites it on the way in, so a client cannot set it for itself.

#### Showing VOD in jellyfin

Jellyfin's Live TV tuner (HDHR/M3U) only carries dispatcharr's channels — it cannot read the XC
API, so VOD reaches jellyfin as a **STRM library**. The `VOD to Media Library` plugin
(`vod2mlib`, shipped in dispatcharr's plugin catalogue) writes one small `.strm` file per
movie/episode and jellyfin scans that folder as ordinary Movies and Shows libraries.

Both containers mount the same host directory: `${APP_ROOT}/dispatcharr/vods` is `/VODS` in
dispatcharr (read-write, the plugin writes there) and `/data/vods` in jellyfin (read-only, so a
scan can never touch the plugin's output). Those mounts are already in the compose files, so
recreate the two containers once and make the folder readable:

```bash
cd president
docker compose up -d dispatcharr jellyfin
chmod -R a+r "$APP_ROOT/dispatcharr/vods"
```

Then:

1.  Dispatcharr → Plugins → Find Plugins → **VOD to Media Library** → Install → Enable.
2.  Settings: Movies root `/VODS/Movies`, Series root `/VODS/Series`, and **Dispatcharr URL
    `http://dispatcharr:9191`**. Jellyfin loads the same docker network, while the public
    `https://dispatcharr.DOMAINNAME` is answered with authelia's 302 and plays nothing. The
    plugin refuses `localhost` for the same reason.
3.  `[LIBRARY] Catalogue snapshot`, then `[GENERATE] Movies` with the batch size at 10 for a
    first look. Files already generated are skipped on later runs.
4.  Jellyfin → Dashboard → Libraries → add **Movies** at `/data/vods/Movies` and **Shows** at
    `/data/vods/Series`. Untick the metadata downloaders and image fetchers on both *before*
    the first scan: the plugin's NFOs already carry the metadata and a large catalogue
    otherwise pulls tens of GB of artwork.

Two limits worth knowing before turning it loose on the whole catalogue:

-   series import but do not play: `/proxy/vod/episode/...` reaches the provider's series node,
    which answers `400` for every `.mkv`, while movies stream normally. That is upstream of
    the gateway, not a docker or authelia problem.
-   dispatcharr regenerates VOD UUIDs on every M3U refresh, which orphans the URLs inside
    already-generated `.strm` files. Keep the plugin's `?stream_id=` pinning on, run
    `[GENERATE] Full rescan` after a provider refresh, and give the auto-rescan cron a nightly
    slot.

### 6. Register the CrowdSec bouncer

**CrowdSec is currently disabled.** Its `include:` in `president/compose.yaml` is commented out
and `middlewares-crowdsec@file` is not in the traefik entrypoint chain, because the
`crowdsecurity/http-probing` scenario banned the owner's own address over dispatcharr's 404
artwork responses (`/api/vod/vodlogos/<id>/cache/`). Nothing was deleted: the config, the
plugin and the decision database are untouched, so re-enabling is the two commented lines. The
rest of this section is what that re-enabling looks like.

The traefik plugin is enabled and the bouncer middleware is applied to the `websecure`
entrypoint, but it needs an API key before it can read decisions:

```bash
cd president
docker compose exec crowdsec cscli bouncers add traefik-bouncer
# copy the printed key into .env as CROWDSEC_BOUNCER_LAPI_KEY=...
docker compose up -d            # picks up the key and starts traefik + the rest
```

Verification:

```bash
docker compose exec crowdsec cscli bouncers list     # traefik-bouncer should be listed
docker compose exec crowdsec cscli metrics           # should show the traefik acquisition
docker compose logs traefik | grep -i crowdsec       # plugin loading, no auth errors
```

Things to know:

-   Traefik downloads `github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin` v1.8.2 from the
    plugin catalogue on startup. The first start needs outbound internet; after that the
    `/plugins-storage` volume keeps it cached. If the catalogue is unreachable, traefik will
    not start.
-   CrowdSec reads `${APP_ROOT}/traefik/logs/traefik-access.log` (mounted read-only as
    `/var/log/traefik`), so keep traefik's access log enabled.
-   Bans are applied on every route, including the public ones and the authelia portal, and
    visitors get `president/traefik/ban.html`.

### 7. Start the media stack

**Read this first:** `media/traefik/compose.yaml` still starts both tailscale nodes with
`--login-server https://headscale.${ROOT_DOMAINNAME}` and hello `HEADSCALE_INTERNAL_AUTHKEY` /
`HEADSCALE_EXTERNAL_AUTHKEY`. The headscale service was removed from this repository, so the
media ingress will not come up as-is. Pick one before starting:

-   drop `--login-server ...` from both `TS_EXTRA_ARGS` lines to use Tailscale's control plane
    (get the auth keys from the Tailscale admin console instead), or
-   restore a control server and point the `--login-server` flag at it.

Then:

```bash
cd media
docker compose config -q
docker compose up -d socket-proxy traefik-tailscale traefik-external
docker compose up -d
docker compose ps
```

The *arrs and qbittorrent only publish inside the tailnet: `sonarr.`, `radarr.`,
`prowlarr.`, `qbit.` and `aria.INTERNAL_DOMAINNAME`, plus `romm-to-pingvin` — see the
"Not deployed" section. Traefik here serves the external (`traefik-external`) and internal
(`traefik-tailscale`) tailscale nodes, so nothing listens on the LAN directly.

### 8. Post-deploy verification

```bash
curl -sI https://traefik.DOMAINNAME | head -1     # 302 to auth.DOMAINNAME
curl -sI https://DOMAINNAME | head -1             # 302 to auth.DOMAINNAME
curl -sI https://jellyfin.DOMAINNAME | head -1    # 200/302 straight from jellyfin
curl -sI https://pingvin.DOMAINNAME | head -1     # 200 straight from pingvin
curl -s  https://auth.DOMAINNAME/api/health       # {"status":"OK"}
curl -sI https://dispatcharr.DOMAINNAME | head -1 # 302 to auth.DOMAINNAME
```

If traefik answers `404` for a host, the router label is wrong. If it answers `502`, the
service is not on the `traefik` network. If authelia answers `403`, the session cookie domain
does not match `DOMAINNAME`.

### The president stack stands alone

Nothing in `president/` talks to the media host at runtime: no `depends_on` points at a
remote service, traefik only reads the local docker socket proxy, and every bind mount lives
under `APP_ROOT` on the VPS. Powering the media machine off costs you the *arrs, not the VPS
— jellyfin, dispatcharr, pingvin, homepage, authelia and the dashboard keep serving.

Two rules keep it that way when adding services:

-   keep bind mounts on the VPS's own disk; never mount an NFS/SMB share from the media host
    into a president container (`${APP_ROOT}/jellyfin/media` must stay local), because a dead
    mount blocks container start;
-   never add `depends_on` to a container on the other machine, and never route a public
    hostname through the media stack's traefik — put the new container on the `traefik`
    network here instead.

### What is mounted and what is rendered

| Config | How it gets in | To apply a change |
| --- | --- | --- |
| `president/homepage/config/` | mounted straight onto `/app/config` | `docker compose restart homepage` |
| `president/authelia/config/configuration.yml` | bind-mounted read-only, `${...}` expanded by authelia | `docker compose up -d --force-recreate authelia` |
| `AUTHELIA_ADMIN_PASSWORD_HASH` in `.env` | becomes `/config/users_database.yml` through a compose config, see step 3 | `docker compose up -d --force-recreate authelia` |
| `president/crowdsec/config/` | mounted as-is (acquis + whitelist) | `docker compose restart crowdsec` |
| `${APP_ROOT}/dispatcharr/data` | plain bind mount at `/data` | `docker compose restart dispatcharr` |
| `${APP_ROOT}/dispatcharr/vods` | VOD `.strm` tree, `/VODS` in dispatcharr and `/data/vods:ro` in jellyfin | `docker compose up -d dispatcharr jellyfin` |
| `president/static/web/` | bind-mounted read-only, `{{env "DOMAINNAME"}}` expanded by caddy's `templates` directive | `docker compose restart static` |
| `president/pingvin/compose.yaml` (configs) | rendered to `/opt/app/config.yaml`, oidc included | `docker compose up -d --force-recreate pingvin-share` |

## Exit node

`president/exit-node/` advertises the VPS as a regular Tailscale exit node, on Tailscale's own
control plane. It is included by `president/compose.yaml`, so it starts with the rest of the
stack; it only does something useful once it has a key:

1.  create an auth key in the Tailscale admin console (`Settings` → `Keys`) and put it in
    `president/.env` as `TAILSCALE_EXIT_NODE_AUTHKEY`;
2.  `docker compose up -d tailscale-exit-node`;
3.  approve it as an exit node: `Machines` → `outside` → `Edit route settings` → *Use as exit
    node*.

Clients then use `tailscale set --exit-node=outside` (add `--exit-node-allow-lan-access` if
they should still reach their LAN).

The container runs with `TS_USERSPACE=false` and `network_mode: host`: userspace networking
cannot forward packets, so it cannot serve as an exit node, and host networking keeps the tun
device and forwarding rules where the traffic leaves the machine. On Oracle Cloud, if clients
get no connectivity through the node, check that the host forwards (`net.ipv4.ip_forward=1`)
and that the VCN security list allows the outbound traffic — inbound 80/443 has nothing to do
with it.

## Not deployed

The files are kept in the repo but are **not** included by either `compose.yaml`:
-   `media/frpc/` — its `frps` server was removed from `president/` (`FRP_OIDC_ISSUER`,
    `FRP_OIDC_AUDIENCE`, `FRP_OIDC_CLIENT_SECRET`, `FRP_OIDC_TOKEN_ENDPOINT`).
-   `media/upload/` — only knows how to talk to `romm`, which was removed
    (`PINGVIN_UPLOAD_ROMM_USERNAME`, `PINGVIN_UPLOAD_ROMM_PASSWORD`,
    `PINGVIN_UPLOAD_PINGVIN_USERNAME`, `PINGVIN_UPLOAD_PINGVIN_PASSWORD`).

Two more pieces are commented out inside deployed files and are therefore unused too: the
`autobrr` block in `media/downloader/qbit/compose.yaml` (`${APP_ROOT}/autobrr/config`,
`DOCKERLOGGING_MAXFILE`, `DOCKERLOGGING_MAXSIZE`) and the `game-unpackerr` block in
`media/downloader/aria/compose.yaml` (`${APP_ROOT}/unpackerr/logs`). Create their
directories and set their variables only if you re-enable them.

To bring a parked service back, uncomment its `include:` line in the relevant `compose.yaml`
(and restore the service it depends on first).
