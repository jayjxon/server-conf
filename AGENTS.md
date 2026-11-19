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
