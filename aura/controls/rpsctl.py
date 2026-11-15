"""rpsctl.py
Read‑only RPS (receive packet steering) inspection utilities.
This simplified implementation does not modify the system; it merely reads
available sysfs entries if they exist.
"""
import os
from pathlib import Path

def _rps_path(interface: str) -> Path:
    return Path(f"/sys/class/net/{interface}/queues")

def inspect_rps(interface: str) -> dict:
    """Return a dictionary describing the RPS configuration for *interface*.
    If the sysfs entries are missing, returns ``{"unsupported": True}``.
    """
    base = _rps_path(interface)
    if not base.exists():
        return {"unsupported": True, "reason": "sysfs entries not found"}
    # In a real implementation we would read files like rps_cpus, rps_flow_cnt.
    # Here we return a placeholder.
    return {"interface": interface, "rps": "placeholder"}

def backup_rps(interface: str) -> dict:
    """Create a backup of the current RPS settings (read‑only).
    Returns a dict with the captured state or an ``unsupported`` flag.
    """
    info = inspect_rps(interface)
    if info.get("unsupported"):
        return info
    # Placeholder backup
    return {"interface": interface, "backup": "placeholder"}
