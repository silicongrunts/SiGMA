#!/usr/bin/env bash
set -euo pipefail

USERDATA_DIR="${SIGMA_USERDATA_DIR:-/app/userdata}"
DEFAULTS_DIR="/app/docker/userdata-defaults"

mkdir -p "${USERDATA_DIR}"

# Seed userdata on first run (empty directory check).
if [ -z "$(find "${USERDATA_DIR}" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
  echo "[entrypoint] seeding userdata from ${DEFAULTS_DIR}"
  cp -a "${DEFAULTS_DIR}/." "${USERDATA_DIR}/"
fi

mkdir -p "${USERDATA_DIR}/.SiGMA"

# Clean stale Chromium Singleton lock files left behind by a previous
# container. The lock records the prior container's hostname + PID, so a
# fresh container would otherwise refuse to launch chromium ("profile
# appears to be in use by another Chromium process on another computer")
# and the browser service would silently fail.
rm -f "${USERDATA_DIR}/.SiGMA/browser_data/SingletonLock" \
      "${USERDATA_DIR}/.SiGMA/browser_data/SingletonCookie" \
      "${USERDATA_DIR}/.SiGMA/browser_data/SingletonSocket" 2>/dev/null || true

# PUID/PGID: chown the bind-mounted userdata so non-root hosts can write.
# Only relevant on Linux hosts; Docker Desktop (macOS/Windows) ignores this.
if [ -n "${PUID:-}" ] && [ -n "${PGID:-}" ] && [ "$(id -u)" = "0" ]; then
  if getent group "${PGID}" >/dev/null 2>&1; then
    GROUP_NAME=$(getent group "${PGID}" | cut -d: -f1)
  else
    GROUP_NAME="sigma_${PGID}"
    groupadd -g "${PGID}" "${GROUP_NAME}"
  fi
  if id -u "${PUID}" >/dev/null 2>&1; then
    USER_NAME=$(getent passwd "${PUID}" | cut -d: -f1)
  else
    USER_NAME="sigma_${PUID}"
    useradd -u "${PUID}" -g "${GROUP_NAME}" -d /app -s /usr/sbin/nologin "${USER_NAME}"
  fi
  chown -R "${PUID}:${PGID}" "${USERDATA_DIR}"
fi

exec "$@"
