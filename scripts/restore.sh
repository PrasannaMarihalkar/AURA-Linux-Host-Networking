#!/usr/bin/env bash
# restore.sh — Reset AURA cgroup v2 slices to baseline weights.
#
# Usage:
#   sudo ./scripts/restore.sh
set -euo pipefail

CGROUP_ROOT="/sys/fs/cgroup"
APP_SLICE="${CGROUP_ROOT}/aura-app.slice"
BG_SLICE="${CGROUP_ROOT}/aura-bg.slice"
DEFAULT_WEIGHT=100

echo "[AURA] Restoring AURA cgroup slices to baseline..."

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: restore.sh must be executed as root (sudo)." >&2
    exit 1
fi

if [[ -f "${APP_SLICE}/cpu.weight" ]]; then
    echo "${DEFAULT_WEIGHT}" > "${APP_SLICE}/cpu.weight"
    CURR=$(cat "${APP_SLICE}/cpu.weight")
    echo "[AURA] aura-app.slice restored to ${CURR}."
fi

if [[ -f "${BG_SLICE}/cpu.weight" ]]; then
    echo "${DEFAULT_WEIGHT}" > "${BG_SLICE}/cpu.weight"
    CURR=$(cat "${BG_SLICE}/cpu.weight")
    echo "[AURA] aura-bg.slice restored to ${CURR}."
fi

echo "[AURA] Restoration complete."
