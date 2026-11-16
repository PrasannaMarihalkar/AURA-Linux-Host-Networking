"""AURA Linux Resource Controls Subsystem.

Provides transactional, reversible resource controls for isolated experimentation:
- cgroup_cpu: Transactional cgroup v2 CPUWeight wrappers with verification and rollback.
- rpsctl: Read-only Receive Packet Steering (RPS) queue inspection.

SAFETY GUARANTEE:
These controls are strictly isolated from the observe-only Gate B controller.
Privileged execution never occurs automatically.
"""
from __future__ import annotations

from aura.controls.cgroup_cpu import (
    ALLOWED_SLICES,
    DEFAULT_CGROUP_ROOT,
    MAX_CPU_WEIGHT,
    MIN_CPU_WEIGHT,
    execute_weight_action_lifecycle,
    get_cpu_weight,
    restore_cpu_weight,
    set_cpu_weight_transactional,
)
from aura.controls.rpsctl import backup_rps_configuration, inspect_rps

__all__ = [
    "ALLOWED_SLICES",
    "DEFAULT_CGROUP_ROOT",
    "MIN_CPU_WEIGHT",
    "MAX_CPU_WEIGHT",
    "get_cpu_weight",
    "set_cpu_weight_transactional",
    "restore_cpu_weight",
    "execute_weight_action_lifecycle",
    "inspect_rps",
    "backup_rps_configuration",
]
