"""rpsctl.py — Read-only Receive Packet Steering (RPS) inspection utilities.

Inspects sysfs entries under /sys/class/net/<interface>/queues/rx-*/rps_cpus.
Reports exact hardware/virtual interface queue configuration, real CPU masks,
and availability status without placeholders.

SAFETY GUARANTEE:
RPS modification is strictly disabled in MVP-1 per AURA safety contracts.
All inspection functions are strictly read-only.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("aura.controls.rpsctl")

DEFAULT_SYS_NET_ROOT = Path("/sys/class/net")


def validate_interface_name(interface: str) -> None:
    """Validate network interface name format to prevent path traversal."""
    if not isinstance(interface, str) or not interface.strip():
        raise ValueError("interface must be a non-empty string")
    if "/" in interface or interface in {".", ".."}:
        raise ValueError(f"Invalid interface name '{interface}'")
    # Linux interface name length limit is IFNAMSIZ (16 chars including null)
    if len(interface) > 15:
        raise ValueError(f"Interface name '{interface}' exceeds 15 characters limit")


def inspect_rps(
    interface: str,
    sys_root: Path = DEFAULT_SYS_NET_ROOT,
) -> dict[str, Any]:
    """Inspect RPS configuration across all receive queues for an interface.

    Reads:
    - /sys/class/net/<interface>/queues/rx-<N>/rps_cpus
    - /sys/class/net/<interface>/queues/rx-<N>/rps_flow_cnt (if available)

    Returns:
        Structured dict containing per-queue CPU masks or explicit unsupported reason.
        Never returns placeholder strings.
    """
    validate_interface_name(interface)

    iface_dir = sys_root / interface
    if not iface_dir.exists():
        return {
            "interface": interface,
            "supported": False,
            "reason": f"Interface '{interface}' does not exist in sysfs",
            "queues": {},
        }

    queues_dir = iface_dir / "queues"
    if not queues_dir.exists():
        return {
            "interface": interface,
            "supported": False,
            "reason": f"Sysfs queues directory not found for interface '{interface}'",
            "queues": {},
        }

    # Discover rx-* directories
    try:
        rx_dirs = sorted(
            [d for d in queues_dir.iterdir() if d.is_dir() and d.name.startswith("rx-")],
            key=lambda p: int(p.name.split("-")[1]) if p.name.split("-")[1].isdigit() else 0,
        )
    except OSError as exc:
        return {
            "interface": interface,
            "supported": False,
            "reason": f"Failed to list queues directory: {exc}",
            "queues": {},
        }

    if not rx_dirs:
        return {
            "interface": interface,
            "supported": False,
            "reason": f"No receive queues (rx-*) found for interface '{interface}'",
            "queues": {},
        }

    queue_results: dict[str, dict[str, Any]] = {}
    any_rps_file_found = False

    for rx_dir in rx_dirs:
        q_name = rx_dir.name
        rps_cpus_file = rx_dir / "rps_cpus"
        rps_flow_file = rx_dir / "rps_flow_cnt"

        q_info: dict[str, Any] = {
            "rps_cpus": None,
            "rps_flow_cnt": None,
            "error": None,
        }

        if rps_cpus_file.exists():
            any_rps_file_found = True
            try:
                raw_mask = rps_cpus_file.read_text(encoding="utf-8").strip()
                # Validate hex mask format (e.g. 0, ff, 00000001, or comma-separated lists)
                clean_mask = raw_mask.replace(",", "")
                if all(c in "0123456789abcdefABCDEF" for c in clean_mask):
                    q_info["rps_cpus"] = raw_mask
                else:
                    q_info["error"] = f"Invalid hex mask: '{raw_mask}'"
            except OSError as exc:
                q_info["error"] = f"Cannot read rps_cpus: {exc}"
        else:
            q_info["error"] = "rps_cpus sysfs node not present"

        if rps_flow_file.exists():
            try:
                q_info["rps_flow_cnt"] = int(rps_flow_file.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                pass

        queue_results[q_name] = q_info

    if not any_rps_file_found:
        return {
            "interface": interface,
            "supported": False,
            "reason": "Kernel driver does not expose rps_cpus (RPS unsupported or disabled in kernel config)",
            "queues": queue_results,
        }

    all_zero = all(
        q.get("rps_cpus") is not None and set(q["rps_cpus"].replace(",", "")).issubset({"0"})
        for q in queue_results.values()
    )

    return {
        "interface": interface,
        "supported": True,
        "rx_queue_count": len(queue_results),
        "all_zero": all_zero,
        "queues": queue_results,
    }


def backup_rps_configuration(
    interface: str,
    sys_root: Path = DEFAULT_SYS_NET_ROOT,
) -> dict[str, Any]:
    """Capture a read-only snapshot of current RPS configuration.

    Returns the inspection dict with an added backup_timestamp_mono marker.
    """
    import time
    info = inspect_rps(interface, sys_root=sys_root)
    return {
        **info,
        "backup_timestamp_monotonic_s": time.monotonic(),
        "read_only": True,
    }


def apply_rps(
    interface: str,
    queue_masks: dict[str, str],
    sys_root: Path = DEFAULT_SYS_NET_ROOT,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Inhibited in MVP-1: active modification of RPS is forbidden by safety contracts."""
    if not dry_run:
        raise NotImplementedError(
            "Active RPS reconfiguration is prohibited in MVP-1 per AURA safety contracts. "
            "RPS inspection is strictly read-only."
        )

    validate_interface_name(interface)
    info = inspect_rps(interface, sys_root=sys_root)
    return {
        "interface": interface,
        "status": "dry_run",
        "reason": "Dry-run RPS reconfiguration evaluation (active modification disabled in MVP-1).",
        "current_configuration": info,
        "requested_configuration": queue_masks,
    }
