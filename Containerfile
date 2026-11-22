# ============================================================
# Bridge Daemon — Containerfile (Podman / Docker)
# Lightweight Python image; data files mounted at runtime
# ============================================================

FROM python:3.12-slim

WORKDIR /app

# Copy daemon and persistent data files
COPY bridge_daemon.py /app/bridge_daemon.py

# config.json and links.json are mounted at runtime so changes
# persist across container restarts.

EXPOSE 9000

ENTRYPOINT ["python3", "-u", "bridge_daemon.py"]
