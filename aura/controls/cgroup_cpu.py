"""cgroup_cpu.py
Implementation of safe transactional wrappers for cgroup v2 CPUWeight control.
This is a minimal implementation suitable for unit testing; actual production
code would interact with the Linux cgroup filesystem under /sys/fs/cgroup.
"""
import os
import json
from pathlib import Path

CGROUP_CPU_WEIGHT_PATH = "/sys/fs/cgroup/cpu.weight"

def _read_weight() -> int:
    """Read the current CPU weight from the cgroup filesystem.
    Returns 0 if the file is missing or unreadable.
    """
    try:
        with open(CGROUP_CPU_WEIGHT_PATH, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except Exception:
        return 0

def _write_weight(weight: int) -> bool:
    """Write a new CPU weight. Returns True on success.
    """
    try:
        with open(CGROUP_CPU_WEIGHT_PATH, "w", encoding="utf-8") as f:
            f.write(str(weight))
        return True
    except Exception:
        return False

def get_cpu_weight(slice_name: str) -> int:
    """Return the current CPU weight for a given slice.
    The *slice_name* argument is kept for API compatibility but is unused in
    this simplified implementation.
    """
    return _read_weight()

def set_cpu_weight(slice_name: str, weight: int) -> dict:
    """Set a new CPU weight transactionally.
    Returns a dict with the original weight and a status flag.
    """
    original = _read_weight()
    success = _write_weight(weight)
    return {"original_weight": original, "new_weight": weight, "status": "ok" if success else "failed"}

def restore_cpu_weight(slice_name: str, original_weight: int) -> dict:
    """Restore a previously saved CPU weight.
    Returns a dict indicating the result.
    """
    success = _write_weight(original_weight)
    return {"restored_weight": original_weight, "status": "ok" if success else "failed"}
