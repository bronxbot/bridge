#!/usr/bin/env bash
# ============================================================
# start.sh — Build and start the Bridge Daemon via Podman
# Usage: ./start.sh [--rebuild]
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

CONTAINER_NAME="bridge-daemon"
IMAGE_NAME="bridge-daemon:latest"

REBUILD=false

for arg in "$@"; do
    case "$arg" in
        --rebuild) REBUILD=true ;;
    esac
done

# ── Sanity checks ───────────────────────────────────────────
if ! command -v podman &>/dev/null; then
    echo "[bridge] error: podman is not installed." >&2
    exit 1
fi

# ── Stop existing container if running ──────────────────────
if podman container exists "$CONTAINER_NAME" 2>/dev/null; then
    echo "[bridge] stopping existing container '$CONTAINER_NAME'..."
    podman stop "$CONTAINER_NAME" || true
    podman rm   "$CONTAINER_NAME" || true
fi

# ── Build image ─────────────────────────────────────────────
if $REBUILD || ! podman image exists "$IMAGE_NAME" 2>/dev/null; then
    echo "[bridge] building image '$IMAGE_NAME'..."
    podman build \
        --file "$SCRIPT_DIR/Containerfile" \
        --tag  "$IMAGE_NAME" \
        "$SCRIPT_DIR"
else
    echo "[bridge] using existing image '$IMAGE_NAME' (pass --rebuild to force rebuild)."
fi

# ── Launch container ─────────────────────────────────────────
echo "[bridge] starting container '$CONTAINER_NAME'..."
podman run \
    --detach \
    --name    "$CONTAINER_NAME" \
    --network host \
    --restart on-failure:5 \
    --volume  "$SCRIPT_DIR/config.json:/app/config.json:z" \
    --volume  "$SCRIPT_DIR/links.json:/app/links.json:z" \
    --volume  "$SCRIPT_DIR/status.json:/app/status.json:z" \
    "$IMAGE_NAME"

echo "[bridge] container started."
echo "  podman logs -f $CONTAINER_NAME"
echo ""
echo "[bridge] to stop:"
echo "  podman stop $CONTAINER_NAME"
