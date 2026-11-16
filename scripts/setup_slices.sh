#!/usr/bin/env bash
# setup_slices.sh — Setup isolated cgroup v2 slices for AURA research experiments.
#
# Pre-requisites:
# 1. Linux host with cgroup v2 mounted at /sys/fs/cgroup
# 2. Root privileges
# 3. CPU controller enabled in cgroup subtree
#
# Usage:
#   sudo ./scripts/setup_slices.sh
set -euo pipefail

CGROUP_ROOT="/sys/fs/cgroup"
APP_SLICE="${CGROUP_ROOT}/aura-app.slice"
BG_SLICE="${CGROUP_ROOT}/aura-bg.slice"
DEFAULT_WEIGHT=100

echo "[AURA] Checking pre-conditions for cgroup v2 setup..."

if [[ "${EUID}" -ne 0 ]]; then
    echo "ERROR: setup_slices.sh must be executed as root (sudo)." >&2
    exit 1
fi

if [[ ! -f "${CGROUP_ROOT}/cgroup.controllers" ]]; then
    echo "ERROR: cgroup v2 filesystem not detected at ${CGROUP_ROOT}." >&2
    echo "Ensure Linux systemd is booted with systemd.unified_cgroup_hierarchy=1" >&2
    exit 1
fi

# Check if CPU controller is available
if ! grep -q "cpu" "${CGROUP_ROOT}/cgroup.controllers"; then
    echo "ERROR: 'cpu' controller is not enabled in ${CGROUP_ROOT}/cgroup.controllers." >&2
    exit 1
fi

# Enable cpu controller in root subtree if not already present
if ! grep -q "cpu" "${CGROUP_ROOT}/cgroup.subtree_control"; then
    echo "+cpu" > "${CGROUP_ROOT}/cgroup.subtree_control"
fi

echo "[AURA] Creating designated AURA research slices..."

mkdir -p "${APP_SLICE}"
mkdir -p "${BG_SLICE}"

echo "[AURA] Initializing default CPUWeight (${DEFAULT_WEIGHT})..."
echo "${DEFAULT_WEIGHT}" > "${APP_SLICE}/cpu.weight"
echo "${DEFAULT_WEIGHT}" > "${BG_SLICE}/cpu.weight"

# Readback verification
APP_W=$(cat "${APP_SLICE}/cpu.weight")
BG_W=$(cat "${BG_SLICE}/cpu.weight")

if [[ "${APP_W}" -eq "${DEFAULT_WEIGHT}" && "${BG_W}" -eq "${DEFAULT_WEIGHT}" ]]; then
    echo "[AURA] Verification SUCCESS: aura-app.slice (${APP_W}) and aura-bg.slice (${BG_W}) initialized."
else
    echo "ERROR: Readback verification failed!" >&2
    exit 1
fi
