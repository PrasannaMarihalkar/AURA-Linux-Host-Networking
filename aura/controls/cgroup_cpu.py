"""cgroup_cpu.py — Safe, transactional cgroup v2 CPUWeight control wrappers.

Designed for isolated Gate C mitigation experiments.
Enforces:
1. Strict slice whitelisting: only 'aura-app.slice' and 'aura-bg.slice' are permitted.
2. Weight range validation: [1, 10000], calibrated experimental range [50, 500].
3. Pre-flight read: captures existing weight as verified baseline; never fabricates zero.
4. Verified write: writes requested value and reads back from sysfs to confirm kernel state.
5. Automatic rollback: if readback verification fails, restores original baseline immediately.
6. Full action lifecycle tracking: get -> backup -> set -> readback verify -> restore -> readback verify.

SAFETY GUARANTEE:
Privileged execution never occurs automatically. The observe-only Gate B controller
never triggers active control adjustments.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("aura.controls.cgroup_cpu")

ALLOWED_SLICES = frozenset({"aura-app.slice", "aura-bg.slice"})
DEFAULT_CGROUP_ROOT = Path("/sys/fs/cgroup")

# Linux cgroup v2 cpu.weight specification boundaries
MIN_CPU_WEIGHT = 1
MAX_CPU_WEIGHT = 10000

# AURA calibrated experimental bounds
CALIBRATED_MIN_WEIGHT = 50
CALIBRATED_MAX_WEIGHT = 500
DEFAULT_BASELINE_WEIGHT = 100


def validate_slice_name(slice_name: str) -> None:
    """Validate that slice_name is strictly an allowed AURA cgroup slice."""
    if not isinstance(slice_name, str) or not slice_name.strip():
        raise ValueError("slice_name must be a non-empty string")
    if slice_name not in ALLOWED_SLICES:
        raise ValueError(
            f"Unauthorized cgroup slice '{slice_name}'. Modifications are strictly "
            f"restricted to: {sorted(ALLOWED_SLICES)}"
        )


def validate_cpu_weight(weight: int, enforce_calibrated_range: bool = False) -> None:
    """Validate that weight is an integer within legal cgroup v2 boundaries."""
    if isinstance(weight, bool) or not isinstance(weight, int):
        raise TypeError(f"weight must be an integer, got {type(weight).__name__}")
    if weight < MIN_CPU_WEIGHT or weight > MAX_CPU_WEIGHT:
        raise ValueError(
            f"cgroup v2 cpu.weight must be between {MIN_CPU_WEIGHT} and {MAX_CPU_WEIGHT}, got {weight}"
        )
    if enforce_calibrated_range:
        if weight < CALIBRATED_MIN_WEIGHT or weight > CALIBRATED_MAX_WEIGHT:
            raise ValueError(
                f"weight {weight} exceeds calibrated AURA experimental range "
                f"[{CALIBRATED_MIN_WEIGHT}, {CALIBRATED_MAX_WEIGHT}]"
            )


def resolve_slice_path(slice_name: str, cgroup_root: Path = DEFAULT_CGROUP_ROOT) -> Path:
    """Resolve and validate the cgroup slice directory path."""
    validate_slice_name(slice_name)
    slice_dir = cgroup_root / slice_name
    return slice_dir


def get_cpu_weight(slice_name: str, cgroup_root: Path = DEFAULT_CGROUP_ROOT) -> int:
    """Read current cpu.weight for a slice.
    
    Never returns 0 for missing or unreadable files; raises FileNotFoundError or PermissionError.
    """
    slice_dir = resolve_slice_path(slice_name, cgroup_root)
    weight_file = slice_dir / "cpu.weight"

    if not weight_file.exists():
        raise FileNotFoundError(f"cgroup cpu.weight file not found at {weight_file}")

    try:
        content = weight_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PermissionError(f"Cannot read cgroup weight at {weight_file}: {exc}") from exc

    try:
        weight = int(content)
    except ValueError as exc:
        raise ValueError(f"Corrupted cpu.weight content at {weight_file}: '{content}'") from exc

    validate_cpu_weight(weight)
    return weight


def set_cpu_weight_transactional(
    slice_name: str,
    weight: int,
    cgroup_root: Path = DEFAULT_CGROUP_ROOT,
    enforce_calibrated_range: bool = False,
) -> dict[str, Any]:
    """Transactionally adjust cpu.weight with readback verification and rollback on failure.

    Lifecycle:
    1. Read and backup current weight (before).
    2. Write requested weight.
    3. Read back to verify kernel state.
    4. If verification fails, roll back to backup and verify restoration.

    Returns:
        Structured action lifecycle record.
    """
    validate_slice_name(slice_name)
    validate_cpu_weight(weight, enforce_calibrated_range=enforce_calibrated_range)

    # 1. Read existing weight (backup)
    original_weight = get_cpu_weight(slice_name, cgroup_root)

    # If already set to requested weight, return early
    if original_weight == weight:
        return {
            "type": "cpu_weight",
            "status": "applied",
            "reason": f"Slice {slice_name} is already configured to requested weight {weight}.",
            "before": {"slice": slice_name, "weight": original_weight},
            "requested": {"slice": slice_name, "weight": weight},
            "after": {"slice": slice_name, "weight": original_weight},
            "rollback_attempted": False,
            "rollback_verified": None,
            "error": None,
        }

    weight_file = resolve_slice_path(slice_name, cgroup_root) / "cpu.weight"

    # 2. Write new weight
    try:
        weight_file.write_text(str(weight), encoding="utf-8")
    except OSError as exc:
        LOGGER.error("Failed to write requested weight %d to %s: %s", weight, weight_file, exc)
        return {
            "type": "cpu_weight",
            "status": "failed",
            "reason": f"Write failed: {exc}",
            "before": {"slice": slice_name, "weight": original_weight},
            "requested": {"slice": slice_name, "weight": weight},
            "after": {"slice": slice_name, "weight": original_weight},
            "rollback_attempted": False,
            "rollback_verified": None,
            "error": str(exc),
        }

    # 3. Readback verify
    try:
        verified_weight = get_cpu_weight(slice_name, cgroup_root)
    except Exception as exc:
        LOGGER.error("Readback verification read error on %s: %s", slice_name, exc)
        verified_weight = -1

    if verified_weight == weight:
        return {
            "type": "cpu_weight",
            "status": "applied",
            "reason": f"Adjusted CPUWeight on {slice_name} from {original_weight} to {weight} with verified readback.",
            "before": {"slice": slice_name, "weight": original_weight},
            "requested": {"slice": slice_name, "weight": weight},
            "after": {"slice": slice_name, "weight": verified_weight},
            "rollback_attempted": False,
            "rollback_verified": None,
            "error": None,
        }

    # 4. Readback failed -> Automatic rollback
    LOGGER.warning(
        "Readback verification mismatch on %s: expected %d, observed %d. Triggering rollback to %d.",
        slice_name,
        weight,
        verified_weight,
        original_weight,
    )
    rollback_result = restore_cpu_weight(slice_name, original_weight, cgroup_root)
    rollback_verified = rollback_result["status"] == "applied"

    return {
        "type": "cpu_weight",
        "status": "rolled_back",
        "reason": f"Readback mismatch (expected {weight}, observed {verified_weight}); rolled back to {original_weight}.",
        "before": {"slice": slice_name, "weight": original_weight},
        "requested": {"slice": slice_name, "weight": weight},
        "after": {"slice": slice_name, "weight": rollback_result["after"]["weight"]},
        "rollback_attempted": True,
        "rollback_verified": rollback_verified,
        "error": f"Readback verification failed: expected {weight}, got {verified_weight}",
    }


def restore_cpu_weight(
    slice_name: str,
    original_weight: int,
    cgroup_root: Path = DEFAULT_CGROUP_ROOT,
) -> dict[str, Any]:
    """Restore previously saved CPU weight and verify restoration."""
    validate_slice_name(slice_name)
    validate_cpu_weight(original_weight)

    weight_file = resolve_slice_path(slice_name, cgroup_root) / "cpu.weight"
    try:
        weight_file.write_text(str(original_weight), encoding="utf-8")
        current = get_cpu_weight(slice_name, cgroup_root)
        if current == original_weight:
            return {
                "type": "cpu_weight",
                "status": "applied",
                "reason": f"Restored CPUWeight on {slice_name} to baseline {original_weight}.",
                "before": None,
                "requested": {"slice": slice_name, "weight": original_weight},
                "after": {"slice": slice_name, "weight": current},
                "rollback_attempted": False,
                "rollback_verified": True,
                "error": None,
            }
        else:
            return {
                "type": "cpu_weight",
                "status": "failed",
                "reason": f"Restoration readback mismatch: expected {original_weight}, got {current}.",
                "before": None,
                "requested": {"slice": slice_name, "weight": original_weight},
                "after": {"slice": slice_name, "weight": current},
                "rollback_attempted": False,
                "rollback_verified": False,
                "error": "Restoration readback mismatch",
            }
    except Exception as exc:
        return {
            "type": "cpu_weight",
            "status": "failed",
            "reason": f"Failed to restore weight: {exc}",
            "before": None,
            "requested": {"slice": slice_name, "weight": original_weight},
            "after": {"slice": slice_name, "weight": -1},
            "rollback_attempted": False,
            "rollback_verified": False,
            "error": str(exc),
        }


def execute_weight_action_lifecycle(
    slice_name: str,
    target_weight: int,
    cgroup_root: Path = DEFAULT_CGROUP_ROOT,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Execute complete get -> backup -> set -> readback verify -> restore -> readback verify lifecycle.

    Used for isolated empirical perturbation verification on test nodes.
    """
    validate_slice_name(slice_name)
    validate_cpu_weight(target_weight)

    if dry_run:
        original = get_cpu_weight(slice_name, cgroup_root)
        return {
            "type": "cpu_weight",
            "status": "dry_run",
            "reason": f"Dry-run evaluation of CPUWeight adjustment {original} -> {target_weight} on {slice_name}.",
            "before": {"slice": slice_name, "weight": original},
            "requested": {"slice": slice_name, "weight": target_weight},
            "after": {"slice": slice_name, "weight": original},
            "rollback_attempted": None,
            "rollback_verified": None,
            "error": None,
        }

    # Phase 1: Apply
    apply_record = set_cpu_weight_transactional(slice_name, target_weight, cgroup_root)
    if apply_record["status"] != "applied":
        return apply_record

    # Phase 2: Restore back to baseline
    original_weight = apply_record["before"]["weight"]
    restore_record = restore_cpu_weight(slice_name, original_weight, cgroup_root)

    return {
        "apply_phase": apply_record,
        "restore_phase": restore_record,
        "lifecycle_complete": restore_record["status"] == "applied",
    }
