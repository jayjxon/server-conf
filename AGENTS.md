# AGENTS.md

Working notes for anyone (human or agent) changing this repository. It describes how the
stacks are wired and the rules that keep them consistent — not the specifics of any one host.

## Ground rules

-   **No AI attribution in commits.** No `Co-Authored-By:` for an assistant, no "Generated
    with …" footer, no emoji trailer. The commit message ends with its own last line.
-   **Do not push without permission.** Commit locally, show what is staged, and wait to be
    told. The deployed hosts pull from this repository, so a push is a deployment trigger.
-   **No secrets in the repo.** Stack config lives in `.env` files next to each
    `compose.yaml`; those are git-ignored. Only `example.env` is tracked, and it holds empty
    values with a note on how to generate each one.
-   **Ask before anything destructive or shared**: rewriting published history, force-pushing,
    removing data directories, changing DNS or firewall rules outside this repo.

## Layout

-   `president/` — the public stack (traefik, authelia, homepage, jellyfin, dispatcharr,
    pingvin, static, crowdsec, exit node).
-   `media/` — the home stack (traefik on two tailscale ingress nodes, the *arrs, qbittorrent,
    aria2, cross-seed).
-   `common/` — definitions shared by both: the docker socket proxy and the traefik base.
-   Each stack has one `compose.yaml` at its root that pulls service directories in with
    `include:`, plus its own `.env`. Both stacks name their networks explicitly
    (`socket_proxy`, `traefik` with a fixed subnet) because traefik attaches fixed addresses.

## How configuration reaches a container

There is no init script or renderer sidecar anywhere in this repo. The three patterns below
cover every case; prefer them in this order before inventing anything else.

1.  **Bind mount the file read-only and let the program expand it.** Traefik reads its rules
    from `common/traefik/rules/`; authelia reads `president/authelia/config/configuration.yml`
    with `X_AUTHELIA_CONFIG_FILTERS=expand-env` and expands `${DOMAINNAME}` itself.
2.  **Compose `configs:` with inline `content:`** for small generated files that need a value
    from `.env`. Compose interpolates `${VAR}` into the content and mounts it in. Used for the
    authelia users database and the two `/.well-known` JSON files.
3.  **The program's own environment variables** for secrets and scalar settings, e.g.
    `AUTHELIA_SESSION_SECRET`, `AUTHELIA_STORAGE_ENCRYPTION_KEY`.

Two things that follow from this:

-   A file mounted through `configs:` (or a bind mount) cannot be written to, and Docker cannot
    create mount points inside a read-only bind mount. Mount generated files on the container's
    own filesystem and point the program's `root`/`path` at them, instead of mounting into a
    read-only directory.
-   Changing a value that ends up in a `configs:` payload or a mounted file **does not**
    recreate the running container. `docker compose up -d` succeeds and keeps the old config;
    use `docker compose up -d --force-recreate <service>`.

## `.env` gotchas

-   Compose interpolates `.env` values. Any value containing `$` — an argon2 digest is the usual
    case — must be **single-quoted**, or every `$` doubled. Unquoted, `$argon2id$v=19$…` is
    silently reduced to its tail, and the failure looks like a wrong password.
-   `${VAR:?message}` makes a variable required: `docker compose config -q` fails without it.
    `${VAR:-}` keeps a feature optional, so the rest of the stack still comes up. Use the first
    for things the stack cannot run without, the second for opt-in services.
-   `docker compose config -q` is the cheap validation step; it catches missing variables and
    malformed includes without touching anything running.

## Authelia

-   `configuration.yml` is mounted read-only; placeholders are `${...}`, expanded by authelia.
-   The users database is a compose config built from `AUTHELIA_ADMIN_PASSWORD_HASH`, an argon2id
    digest. Generate it with:

    ```bash
    docker run --rm --entrypoint authelia authelia/authelia:4.39.28 \
        authelia crypto hash generate argon2 --password 'the-password'
    ```

    Note `--entrypoint authelia`: the image's entrypoint script `exec`s its arguments, so
    `docker run … crypto hash …` fails with "crypto: not found".
-   Verify a password without a browser: `POST /api/firstfactor` returns `200` for a correct
    password and `401` for a wrong one.
-   Users live in the file backend; the second factor (TOTP) enrolment is stored in
    `db.sqlite3` under `APP_ROOT`, so a lost authenticator means clearing that identity, not
    resetting the password.
-   `identity_providers.oidc` makes authelia the provider for pingvin share. The signing key is
    generated once at `APP_ROOT/authelia/config/oidc/issuer.pem` and handed over as
    `AUTHELIA_IDENTITY_PROVIDERS_OIDC_ISSUER_PRIVATE_KEY_FILE`; the hmac secret and the client
    hash are plain variables in `.env`.
-   A variable referenced as `${...}` inside `configuration.yml` has to be in the authelia
    container's environment — the `expand-env` filter sees nothing else. Referencing an
    unset one expands to an empty string, which surfaces as `option 'x' is required`.
-   OIDC client secrets are **pbkdf2 hashes**, not plaintext: the plaintext value goes to the
    client, the hash into `configuration.yml`. Both contain `$`, so the hash is single-quoted
    in `.env` like the argon2 digest.

## Pingvin Share

-   Its entire configuration is a compose config mounted at `/opt/app/config.yaml`: the values
    that used to be set in the admin UI plus the OIDC client. While that file exists the UI
    shows those settings read-only, so the compose file is the source of truth.
-   A YAML config **replaces** the values instead of merging into them, and
    `ConfigService.migrateInitUser` reads `initUser` straight out of the parsed file. Leave a
    section out and the backend exits with `Cannot read properties of undefined (reading
    'enabled')`; traefik then serves the catch-all error-page router, so the symptom looks like
    a routing fault rather than a crash. `initUser.enabled: 'false'` is enough to satisfy it.

## Traefik and CrowdSec

-   CrowdSec is **disabled** right now: the `include:` in `president/compose.yaml` is commented
    out and `middlewares-crowdsec@file` is absent from
    `TRAEFIK_ENTRYPOINTS_WEBSECURE_HTTP_MIDDLEWARES`. Its `crowdsecurity/http-probing` scenario
    banned the owner's own address because dispatcharr answers 404 for uncached artwork
    (`/api/vod/vodlogos/<id>/cache/`), so enforcement was turned off instead of tuned. Config,
    plugin and the decision database are untouched; restoring the two lines re-enables it.

-   The bouncer plugin (`maxlerebourg/crowdsec-bouncer-traefik-plugin`) is downloaded from the
    catalogue on first start and cached in `APP_ROOT/traefik/plugins`; without egress traefik
    will not start.
-   Traefik requires `CROWDSEC_BOUNCER_LAPI_KEY`, but the key only exists after crowdsec has
    run once. On a fresh host the order is: `config -q` with a throwaway key → `up -d
    socket-proxy crowdsec` → `cscli bouncers add traefik-bouncer` → write the real key → `up -d`.
-   Certificates come from ACME with the Cloudflare DNS-01 challenge, so the DNS records do not
    need to exist for issuance — but the token needs `Zone → DNS → Edit` on the zone.
-   CrowdSec reads traefik's access log read-only and only logs the status classes it is
    configured for, so an empty log is not proof that traffic never arrived.

## Dispatcharr

-   It keeps no login of its own. The router carries authelia's forward-auth **plus** a headers
    middleware that sets `Remote-User`; dispatcharr reads the `reverse_proxy_auth` CoreSettings
    row (header `Remote-User`) and its SPA calls `POST /api/accounts/auth/proxy-login/` on load
    to swap the header for a JWT.
-   Both halves live in dispatcharr's own database, i.e. host state rather than repo state, so
    they are created by `dispatcharr-init`: a one-shot sidecar that shares the container's
    network namespace (postgres and redis run inside it, so `localhost` is the only way to
    reach them). It runs on every `up -d`, retries for three minutes, and is a no-op once the
    rows exist. Without it a fresh volume serves the first-run setup page, which then needs an
    allowed IP or a manual `createsuperuser`.
-   The header is only honoured when the peer is in `DISPATCHARR_TRUSTED_PROXIES`; that is why
    the compose file sets `10.120.0.0/24`. Traefik overwrites the header, so a client cannot
    assert an identity of its own — the middleware, not the app, is the gate.
-   Because everyone arrives as that one account, access is limited in authelia: its
    access-control rule for `dispatcharr.DOMAINNAME` carries `subject: group:admins`. A user
    outside the group gets a 403 from the portal and never reaches the app.

## What is public

Only `traefik` publishes ports (80/443); every other container uses `expose`, so nothing is
reachable except through a router. Everything the internet can reach without a session:

-   `auth.DOMAINNAME` — the portal itself, `bypass` by necessity, throttled by authelia's
    regulation (3 tries / 2 min, 5 min ban);
-   `jellyfin.DOMAINNAME` — **no** authelia middleware at all: jellyfin's own login is the gate,
    and `/Users/Public` lists the accounts to anyone;
-   `pingvin.DOMAINNAME` — OIDC through authelia, deliberately, plus its public share links
    (share registration and unauthenticated shares are both off in its config);
-   `dispatcharr.DOMAINNAME/proxy/vod/**` — `bypass`, because every `.strm` points a player at
    it and a player cannot complete a redirect. Traversal does not widen it: traefik
    normalises the path before authelia matches, so `/proxy/vod/../api/…` still redirects to
    the portal (verified 2026-11-20), and everything else on that host is `group:admins`;
-   `/.well-known/*` on the apex (matrix + OpenID discovery, for federation that does not
    exist yet) and the error-page catch-all, which answers any unknown Host.

Known gaps, currently deliberate rather than fixed:

-   `access_control.default_policy` is `one_factor`, so TOTP is enrolled but never *demanded*:
    a password alone opens the homepage, the dashboard, dispatcharr and bazarr. `two_factor` is
    the one-line change if that is not wanted.
-   CrowdSec is disabled (above), so nothing bans abusive IPs at the edge; jellyfin and the
    portal face brute force with authelia's regulation only. Re-enabling needs a whitelist for
    your own address and an exclusion for dispatcharr's artwork 404s.
-   Every container on the `traefik` network can reach `dispatcharr:9191`, and
    `DISPATCHARR_TRUSTED_PROXIES` is that whole subnet — so homepage or pingvin, if compromised,
    can assert `Remote-User: admin`. Segmentation (a dispatcharr-only network) is the fix.
-   Images carry `latest`/`stable` tags (`jellyfin`, `caddy`, `dispatcharr`, `bazarr`,
    `error-pages`, `pingvin`, `gluetun`, `tailscale`), so any `pull` is an unreviewed upgrade.

## Host firewall

There is no `ufw`. Do not add one: `ufw enable` flushes the tables, taking docker's
`DOCKER-*` chains, tailscale's `ts-input`/`ts-forward` and the Oracle image's
`InstanceServices` metadata rules with it. The firewall is the file-based iptables ruleset the
Oracle image ships, loaded by `netfilter-persistent` (`/etc/iptables/rules.v4`,
`rules.v6`), and that file is the source of truth.

-   `rules.v4`: `INPUT` allows loopback, established traffic, icmp, `22/tcp` and jumps
    `ts-input` for tailscale; the last rule rejects everything else. There is deliberately no
    `80`/`443` rule — docker's DNAT sends published ports through `FORWARD`/`DOCKER`, never
    through `INPUT`, so adding them only widens what a host process could bind.
-   `rules.v6`: originally `-P INPUT ACCEPT` with no rules (the cloud image ships an empty
    file), i.e. wide open if the instance ever got a global IPv6 address. It now mirrors v4:
    loopback, established, icmpv6, `tailscale0`, `22/tcp`, reject the rest. Applied
    append-only in the live table so docker and tailscale chains are untouched.
-   `rpcbind` is masked. Nothing here mounts nfs/cifs, and port `111` was the only listener
    that nothing needed.
-   Changing these files does not change the live rules: `iptables-restore < file` (or
    `systemctl restart netfilter-persistent`) applies them. Test with `--test` first; a restore
    flushes the table, which is why the live edits were appended instead.



Bazarr cannot fill this catalogue: it recovers embedded tracks only and manages what
Sonarr/Radarr index, and every free web provider except Subf2m wants an account or answers
403/404. `president/jellyfin/subs/` therefore holds a one-off backfill that drives Subf2m
through jellyfin's own downloader. Nothing there is wired into compose — it is three scripts
plus a README with the run order, the API-key setup, the stop/resume commands and the measured
rates. Read that README before re-running anything.

-   Subf2m (and every subbuzz provider) searches by **IMDb id**, and the library is TMDB-only,
    so `resolve_imdb.py` has to run first. It fills `BaseItemProviders(ProviderId='Imdb')`,
    which is where jellyfin keeps provider ids and where an item refresh will not wipe them;
    episodes inherit the series' id. The ids come from IMDb's keyless suggestion endpoint, not
    Wikidata — Wikidata maps titles to the wrong film.
-   `fetch_subs.py` needs a key in the `ApiKeys` table **plus `docker restart jellyfin`**, the
    table is read at startup. Admin endpoints (`/Plugins`, `/ScheduledTasks`) stay 401 even
    with a good key, so it only uses `Items/{id}` and `Items/{id}/RemoteSearch/Subtitles`.
-   Judge progress by files or by the media-source list, never by the DB: `MediaStreamInfos`
    rows for external subtitles are transient. A `204` from the download means the file was
    written, not that the client will offer it — that is `GET /Users/{uid}/Items/{id}`
    reporting `English - SUBRIP - External`.
-   Host state it depends on: subbuzz's `EnableSubf2m` and `MinScore=20` (Subf2m scores low),
    one `RequirePerfectSubtitleMatch=false` in `root/default/Shows/options.xml` (a duplicated
    element silently drops settings), and `SaveSubtitlesWithMedia=false` because the VOD tree
    is read-only.
-   Limits worth knowing before promising coverage: Subf2m has no per-season page for every
    show — Attack on Titan S1 gets zero candidates while S02/S03 work — about a fifth of titles
    never resolve an IMDb id, and addic7ed crashes subbuzz 1.5.1 internally. Throughput is
    ~17 items/min at 3 workers, i.e. ~9 days for all 222k items; more workers makes Subf2m
    return nothing for concurrent searches of one series.

## Exit node

The exit node runs with `network_mode: host` and `TS_USERSPACE=false`: userspace networking
cannot forward packets, so it cannot act as an exit node. It needs `NET_ADMIN`, `NET_RAW`,
`/dev/net/tun` and the host's `/lib/modules` (tailscaled creates its own iptables chains for
forwarding, and without the modules it logs `modprobe: can't change directory` and every
packet is dropped), plus a Tailscale auth key. Advertising is not enough — the machine has to
be approved in the tailnet, and the exit-node route enabled, before clients can select it.
Until the machine is approved the container exits and restarts in a loop; that is the image's
auth flow, not a crash.

## Deploying

Both hosts deploy from this repository:

```bash
git pull --ff-only
cd president        # or media
docker compose config -q
docker compose up -d --remove-orphans
```

`--remove-orphans` matters when a service is deleted from a compose file: otherwise its
container keeps running and still holds its ports.

## Verifying

```bash
docker compose ps --format '{{.Name}}|{{.Status}}'
curl -sI https://<host> --resolve <host>:443:127.0.0.1    # no -k: a bad cert must fail
curl -s https://auth.<domain>/api/health
docker compose exec crowdsec cscli bouncers list
docker compose logs --tail=50 <service>
```

`--resolve` with the hostname is the honest check: it exercises the real router, the real
certificate and the real middleware without needing public DNS. Omitting `-k` is deliberate —
with TLS handshakes failing due to a missing certificate, the symptom is `000`, not an error
page.

## Reporting the result

After a change, finish with a short account of what actually happened, so the state of the
repository and the hosts is never a mystery:

-   which files changed and why (one line each, not a tool-call log);
-   the commands that verified it, and what they returned;
-   what was deliberately left alone, and anything still blocked, with the reason;
-   anything the reader has to do themselves (approve a tailnet node, add a key, open a port).

Do not describe a check as passing if it was not run, and do not narrate steps that were
abandoned — say what the end state is.
