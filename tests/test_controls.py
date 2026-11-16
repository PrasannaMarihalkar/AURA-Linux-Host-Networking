"""Tests for AURA resource controls subsystem.

Validates:
1. Whitelist enforcement for cgroup slices (only aura-app.slice and aura-bg.slice).
2. Weight boundary and type validation.
3. Readback verification and automatic rollback on verification failure.
4. Action lifecycle formatting conforming to action_record schema principles.
5. Real RPS sysfs queue inspection without placeholders.
6. Safety isolation: observe-only controller never invokes controls.
"""
from __future__ import annotations

from pathlib import Path
import pytest

from aura.controls.cgroup_cpu import (
    ALLOWED_SLICES,
    CALIBRATED_MAX_WEIGHT,
    CALIBRATED_MIN_WEIGHT,
    MAX_CPU_WEIGHT,
    MIN_CPU_WEIGHT,
    execute_weight_action_lifecycle,
    get_cpu_weight,
    resolve_slice_path,
    restore_cpu_weight,
    set_cpu_weight_transactional,
    validate_cpu_weight,
    validate_slice_name,
)
from aura.controls.rpsctl import (
    apply_rps,
    backup_rps_configuration,
    inspect_rps,
    validate_interface_name,
)


@pytest.fixture
def mock_cgroup_root(tmp_path: Path) -> Path:
    """Create a simulated cgroup v2 hierarchy with valid initial weights."""
    cgroup_root = tmp_path / "sys" / "fs" / "cgroup"
    for slice_name in ALLOWED_SLICES:
        slice_dir = cgroup_root / slice_name
        slice_dir.mkdir(parents=True, exist_ok=True)
        (slice_dir / "cpu.weight").write_text("100\n", encoding="utf-8")
    return cgroup_root


@pytest.fixture
def mock_sys_net_root(tmp_path: Path) -> Path:
    """Create a simulated sysfs net hierarchy with rx queues."""
    net_root = tmp_path / "sys" / "class" / "net"
    eth0_dir = net_root / "eth0" / "queues"
    eth0_dir.mkdir(parents=True, exist_ok=True)

    # Queue rx-0 with mask '01'
    q0 = eth0_dir / "rx-0"
    q0.mkdir()
    (q0 / "rps_cpus").write_text("01\n", encoding="utf-8")
    (q0 / "rps_flow_cnt").write_text("4096\n", encoding="utf-8")

    # Queue rx-1 with mask '02'
    q1 = eth0_dir / "rx-1"
    q1.mkdir()
    (q1 / "rps_cpus").write_text("02\n", encoding="utf-8")
    (q1 / "rps_flow_cnt").write_text("4096\n", encoding="utf-8")

    return net_root


# -----------------------------------------------------------------------------
# CPUWeight Tests
# -----------------------------------------------------------------------------

def test_slice_name_validation():
    """Verify strict whitelisting of authorized cgroup slices."""
    for valid_slice in ALLOWED_SLICES:
        validate_slice_name(valid_slice)

    for invalid_slice in ["system.slice", "user.slice", "root", "", "../etc", "aura-evil.slice"]:
        with pytest.raises(ValueError):
            validate_slice_name(invalid_slice)


def test_cpu_weight_validation():
    """Verify legal weight bounds and rejection of booleans and invalid values."""
    validate_cpu_weight(MIN_CPU_WEIGHT)
    validate_cpu_weight(MAX_CPU_WEIGHT)
    validate_cpu_weight(100)

    # Calibrated range
    validate_cpu_weight(100, enforce_calibrated_range=True)
    with pytest.raises(ValueError):
        validate_cpu_weight(10, enforce_calibrated_range=True)
    with pytest.raises(ValueError):
        validate_cpu_weight(800, enforce_calibrated_range=True)

    # Negative and out of bounds
    with pytest.raises(ValueError):
        validate_cpu_weight(0)
    with pytest.raises(ValueError):
        validate_cpu_weight(-50)
    with pytest.raises(ValueError):
        validate_cpu_weight(10001)

    # Type safety (reject booleans and floats)
    with pytest.raises(TypeError):
        validate_cpu_weight(True)
    with pytest.raises(TypeError):
        validate_cpu_weight(False)
    with pytest.raises(TypeError):
        validate_cpu_weight(100.5)  # type: ignore


def test_get_cpu_weight_success(mock_cgroup_root: Path):
    """Test reading existing CPU weight."""
    weight = get_cpu_weight("aura-app.slice", cgroup_root=mock_cgroup_root)
    assert weight == 100


def test_get_cpu_weight_missing_file_raises_error(mock_cgroup_root: Path):
    """Missing or unreadable file must raise FileNotFoundError, never fabricate zero."""
    (mock_cgroup_root / "aura-app.slice" / "cpu.weight").unlink()
    with pytest.raises(FileNotFoundError):
        get_cpu_weight("aura-app.slice", cgroup_root=mock_cgroup_root)


def test_get_cpu_weight_corrupted_file_raises_error(mock_cgroup_root: Path):
    """Corrupted non-integer file content raises ValueError."""
    (mock_cgroup_root / "aura-app.slice" / "cpu.weight").write_text("corrupted_text\n")
    with pytest.raises(ValueError):
        get_cpu_weight("aura-app.slice", cgroup_root=mock_cgroup_root)


def test_set_cpu_weight_transactional_success(mock_cgroup_root: Path):
    """Test successful adjustment with verified readback."""
    record = set_cpu_weight_transactional("aura-app.slice", 200, cgroup_root=mock_cgroup_root)
    assert record["status"] == "applied"
    assert record["before"]["weight"] == 100
    assert record["requested"]["weight"] == 200
    assert record["after"]["weight"] == 200
    assert record["rollback_attempted"] is False

    # Check file
    assert get_cpu_weight("aura-app.slice", cgroup_root=mock_cgroup_root) == 200


def test_set_cpu_weight_unchanged_idempotent(mock_cgroup_root: Path):
    """Requesting the current weight returns applied early without writing."""
    record = set_cpu_weight_transactional("aura-app.slice", 100, cgroup_root=mock_cgroup_root)
    assert record["status"] == "applied"
    assert record["before"]["weight"] == 100
    assert record["after"]["weight"] == 100


def test_set_cpu_weight_readback_failure_triggers_automatic_rollback(mock_cgroup_root: Path, monkeypatch):
    """If readback verification detects mismatch, automatic rollback restores original baseline."""
    # Simulate a buggy filesystem where get_cpu_weight reads unexpected value after write
    original_get = get_cpu_weight

    read_count = 0
    def _mock_flaky_get(slice_name: str, cgroup_root: Path = mock_cgroup_root) -> int:
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return 100  # Initial read (before)
        if read_count == 2:
            return 999  # Verification readback mismatch (simulated kernel failure)
        return original_get(slice_name, cgroup_root)

    monkeypatch.setattr("aura.controls.cgroup_cpu.get_cpu_weight", _mock_flaky_get)

    record = set_cpu_weight_transactional("aura-app.slice", 250, cgroup_root=mock_cgroup_root)
    assert record["status"] == "rolled_back"
    assert record["rollback_attempted"] is True
    assert record["rollback_verified"] is True
    assert "Readback verification failed" in record["error"]

    # Verify that the file was restored to original 100
    assert original_get("aura-app.slice", cgroup_root=mock_cgroup_root) == 100


def test_restore_cpu_weight(mock_cgroup_root: Path):
    """Test explicit restoration of original CPU weight."""
    (mock_cgroup_root / "aura-app.slice" / "cpu.weight").write_text("300\n")
    rec = restore_cpu_weight("aura-app.slice", 100, cgroup_root=mock_cgroup_root)
    assert rec["status"] == "applied"
    assert rec["after"]["weight"] == 100
    assert get_cpu_weight("aura-app.slice", cgroup_root=mock_cgroup_root) == 100


def test_execute_weight_action_lifecycle(mock_cgroup_root: Path):
    """Test full get -> backup -> set -> readback verify -> restore -> readback verify sequence."""
    lifecycle = execute_weight_action_lifecycle("aura-app.slice", 200, cgroup_root=mock_cgroup_root)
    assert lifecycle["lifecycle_complete"] is True
    assert lifecycle["apply_phase"]["status"] == "applied"
    assert lifecycle["restore_phase"]["status"] == "applied"
    # End state restored to baseline 100
    assert get_cpu_weight("aura-app.slice", cgroup_root=mock_cgroup_root) == 100


def test_execute_weight_action_lifecycle_dry_run(mock_cgroup_root: Path):
    """Test dry_run lifecycle produces dry_run action without modifying filesystem."""
    lifecycle = execute_weight_action_lifecycle("aura-app.slice", 200, cgroup_root=mock_cgroup_root, dry_run=True)
    assert lifecycle["status"] == "dry_run"
    assert lifecycle["before"]["weight"] == 100
    assert lifecycle["requested"]["weight"] == 200
    assert get_cpu_weight("aura-app.slice", cgroup_root=mock_cgroup_root) == 100


# -----------------------------------------------------------------------------
# RPS Inspection Tests
# -----------------------------------------------------------------------------

def test_interface_name_validation():
    """Verify interface name safety checks."""
    validate_interface_name("eth0")
    validate_interface_name("enp0s3")
    validate_interface_name("lo")

    with pytest.raises(ValueError):
        validate_interface_name("")
    with pytest.raises(ValueError):
        validate_interface_name("../etc")
    with pytest.raises(ValueError):
        validate_interface_name("eth0/evil")
    with pytest.raises(ValueError):
        validate_interface_name("this_interface_name_is_way_too_long")


def test_inspect_rps_success(mock_sys_net_root: Path):
    """Verify real RPS inspection returns actual queue masks without placeholders."""
    info = inspect_rps("eth0", sys_root=mock_sys_net_root)
    assert info["supported"] is True
    assert info["rx_queue_count"] == 2
    assert info["all_zero"] is False
    assert info["queues"]["rx-0"]["rps_cpus"] == "01"
    assert info["queues"]["rx-0"]["rps_flow_cnt"] == 4096
    assert info["queues"]["rx-1"]["rps_cpus"] == "02"
    assert info["queues"]["rx-1"]["rps_flow_cnt"] == 4096
    # Assert NO placeholder string anywhere in the result
    assert "placeholder" not in str(info)


def test_inspect_rps_all_zero(mock_sys_net_root: Path):
    """Verify all_zero flag when all queues have zero mask."""
    (mock_sys_net_root / "eth0" / "queues" / "rx-0" / "rps_cpus").write_text("0\n")
    (mock_sys_net_root / "eth0" / "queues" / "rx-1" / "rps_cpus").write_text("00000000\n")
    info = inspect_rps("eth0", sys_root=mock_sys_net_root)
    assert info["supported"] is True
    assert info["all_zero"] is True


def test_inspect_rps_missing_interface(mock_sys_net_root: Path):
    """Verify non-existent interface returns supported=False with structured reason."""
    info = inspect_rps("eth99", sys_root=mock_sys_net_root)
    assert info["supported"] is False
    assert "does not exist" in info["reason"]


def test_backup_rps_configuration(mock_sys_net_root: Path):
    """Verify read-only backup snapshot."""
    backup = backup_rps_configuration("eth0", sys_root=mock_sys_net_root)
    assert backup["read_only"] is True
    assert "backup_timestamp_monotonic_s" in backup
    assert backup["supported"] is True


def test_apply_rps_prohibited_in_mvp1(mock_sys_net_root: Path):
    """Verify active RPS modification raises NotImplementedError in MVP-1."""
    with pytest.raises(NotImplementedError) as exc_info:
        apply_rps("eth0", {"rx-0": "03"}, sys_root=mock_sys_net_root, dry_run=False)
    assert "prohibited in MVP-1" in str(exc_info.value)


# -----------------------------------------------------------------------------
# Safety Isolation Invariant Test
# -----------------------------------------------------------------------------

def test_controller_never_imports_or_calls_controls():
    """Verify that aura.controller and aura.diagnosis have no dependencies on aura.controls."""
    import inspect
    import aura.controller
    import aura.diagnosis

    controller_src = inspect.getsource(aura.controller)
    diagnosis_src = inspect.getsource(aura.diagnosis)

    assert "aura.controls" not in controller_src
    assert "set_cpu_weight" not in controller_src
    assert "aura.controls" not in diagnosis_src
    assert "set_cpu_weight" not in diagnosis_src
