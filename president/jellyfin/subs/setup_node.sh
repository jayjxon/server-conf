#!/usr/bin/env bash
# Give the jellyfin container a `node` binary so subbuzz's Subf2m provider can solve the
# site's JavaScript challenge.
#
# Why: Providers/Subf2m.cs handles an InvalidHtmlException by running
#   node -e "console.log('__arcsjs='+eval(\"...\"))"
# and setting the resulting cookie. The jellyfin image ships no node (measured 2026-11-21:
# `docker exec jellyfin sh -c 'command -v node'` -> nothing), so that path can never work:
# 1,250 of ~4,800 zero-result searches in one day ended there.
#
# This script installs a pinned static node next to the other subtitle tooling and copies it
# into the running container (no restart). `president/jellyfin/compose.yaml` bind-mounts the
# same file over /usr/local/bin/node so a recreate keeps it. Run this script first: if the
# host file is missing at `docker compose up -d`, docker creates a *directory* at that path.
#
#   sudo ./setup_node.sh                 # APP_ROOT=/srv/apps, node v22.11.0
#   sudo APP_ROOT=/srv/apps NODE_VERSION=v22.11.0 ./setup_node.sh
#
# Revert: docker exec -u 0 jellyfin rm -f /usr/local/bin/node, then remove the compose
# volume and recreate. Nothing else depends on it; the plugin simply falls back to logging
# "Caclulation of javascript challenge failed".
set -euo pipefail

APP_ROOT=${APP_ROOT:-/srv/apps}
NODE_VERSION=${NODE_VERSION:-v22.11.0}
DEST_DIR="$APP_ROOT/jellyfin/subs/bin"
DEST="$DEST_DIR/node"

case "$(uname -m)" in
    aarch64|arm64) ARCH=arm64 ;;
    x86_64|amd64)  ARCH=x64 ;;
    *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

mkdir -p "$DEST_DIR"

if [ ! -x "$DEST" ] || [ "$("$DEST" --version 2>/dev/null || true)" != "$NODE_VERSION" ]; then
    tarball="node-${NODE_VERSION}-linux-${ARCH}"
    url="https://nodejs.org/dist/${NODE_VERSION}/${tarball}.tar.xz"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    echo "downloading $url"
    curl -fsSL --max-time 180 -o "$tmp/node.tar.xz" "$url"
    tar -xJf "$tmp/node.tar.xz" -C "$tmp" "$tarball/bin/node"
    install -m 0755 "$tmp/$tarball/bin/node" "$DEST"
fi

echo "host: $(du -h "$DEST" | cut -f1)  $DEST  $("$DEST" --version)"

# The container's /usr/local/bin is root-owned while jellyfin runs as PUID:PGID, so the copy
# has to go through the daemon rather than `docker exec`.
docker cp "$DEST" jellyfin:/usr/local/bin/node
CID=$(docker inspect -f '{{.Config.User}}' jellyfin)
echo "container user: ${CID:-root}"
docker exec -u 1001 jellyfin /usr/local/bin/node --version
echo "ok: subbuzz will now find node on PATH"
