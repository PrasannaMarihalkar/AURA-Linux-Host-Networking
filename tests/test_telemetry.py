"""Tests for the AURA Linux telemetry collector."""

import json
from pathlib import Path

import pytest
from jsonschema import validate

from aura import telemetry


def test_counter_delta_normal():
    assert telemetry._counter_delta(150, 100) == (50, None)


def test_counter_delta_reset():
    assert telemetry._counter_delta(50, 100) == (
        None,
        "counter_reset_detected",
    )


def test_counter_delta_missing_baseline():
    assert telemetry._counter_delta(50, None) == (
        None,
        "baseline_missing",
    )


def test_per_cpu_normal_delta():
    missing = []

    result = telemetry._per_cpu_deltas(
        {0: 150, 1: 300},
        {0: 100, 1: 250},
        "test_metric",
        missing,
    )

    assert result == [
        {"cpu_id": 0, "delta": 50, "reason": None},
        {"cpu_id": 1, "delta": 50, "reason": None},
    ]
    assert missing == []


def test_per_cpu_counter_reset():
    missing = []

    result = telemetry._per_cpu_deltas(
        {0: 50},
        {0: 100},
        "test_metric",
        missing,
    )

    assert result == [
        {
            "cpu_id": 0,
            "delta": None,
            "reason": "counter_reset_detected",
        }
    ]

    assert {
        "metric": "test_metric",
        "reason": "counter_reset_detected",
    } in missing


def test_cpu_hotplug():
    missing = []

    result = telemetry._per_cpu_deltas(
        {0: 200, 2: 50},
        {0: 100, 1: 150},
        "test_metric",
        missing,
    )

    assert result == [
        {"cpu_id": 0, "delta": 100, "reason": None},
        {"cpu_id": 1, "delta": None, "reason": "cpu_offline"},
        {
            "cpu_id": 2,
            "delta": None,
            "reason": "baseline_missing_hotplug",
        },
    ]


def test_cpu_list_parser():
    assert telemetry._parse_cpu_list("0-3,5,7-8") == [
        0, 1, 2, 3, 5, 7, 8
    ]


def test_cpu_pressure_parser(tmp_path, monkeypatch):
    pressure_dir = tmp_path / "pressure"
    pressure_dir.mkdir()

    (pressure_dir / "cpu").write_text(
        "some avg10=12.50 avg60=10.25 avg300=8.75 total=123456\n"
        "full avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
    )

    monkeypatch.setattr(telemetry, "PROC_ROOT", tmp_path)

    result = telemetry.read_cpu_pressure()

    assert result == {
        "avg10": 12.50,
        "avg60": 10.25,
        "avg300": 8.75,
        "total": 123456,
    }


def test_softirq_parser(tmp_path, monkeypatch):
    (tmp_path / "softirqs").write_text(
        "                    CPU0       CPU1\n"
        "NET_TX:                10          20\n"
        "NET_RX:               100         200\n"
    )

    monkeypatch.setattr(telemetry, "PROC_ROOT", tmp_path)

    assert telemetry.read_softirqs() == {
        "NET_RX": {0: 100, 1: 200},
        "NET_TX": {0: 10, 1: 20},
    }


def test_softnet_parser(tmp_path, monkeypatch):
    proc_net = tmp_path / "net"
    proc_net.mkdir()

    proc_net.joinpath("softnet_stat").write_text(
        "00000010 00000002 00000003\n"
        "00000020 00000004 00000005\n"
    )

    cpu_root = tmp_path / "cpu"
    cpu_root.mkdir()
    cpu_root.joinpath("possible").write_text("0-1\n")
    cpu_root.joinpath("online").write_text("0-1\n")

    monkeypatch.setattr(telemetry, "PROC_ROOT", tmp_path)
    monkeypatch.setattr(telemetry, "SYS_CPU_ROOT", cpu_root)

    assert telemetry.read_softnet_stat() == {
        0: {
            "processed": 16,
            "dropped": 2,
            "time_squeeze": 3,
        },
        1: {
            "processed": 32,
            "dropped": 4,
            "time_squeeze": 5,
        },
    }


@pytest.mark.skipif(
    not Path("/proc").exists(),
    reason="Linux proc filesystem required",
)
def test_real_snapshot_matches_schema():
    schema_path = Path("contracts/host_snapshot.schema.json")
    schema = json.loads(schema_path.read_text())

    telemetry._BASELINE = None

    snapshot = telemetry.collect_snapshot("lo")

    validate(instance=snapshot, schema=schema)

    assert snapshot["interval_duration_s"] > 0
    assert snapshot["schema_version"] == "1.2"
    assert snapshot["host_boot_id"]


@pytest.mark.skipif(
    not Path("/proc").exists(),
    reason="Linux proc filesystem required",
)
def test_successive_snapshots_match_schema():
    schema = json.loads(
        Path("contracts/host_snapshot.schema.json").read_text()
    )

    telemetry._BASELINE = None

    first = telemetry.collect_snapshot("lo")
    second = telemetry.collect_snapshot("lo")

    validate(instance=first, schema=schema)
    validate(instance=second, schema=schema)

    assert first["interval_duration_s"] > 0
    assert second["interval_duration_s"] > 0
    assert (
        first["host_boot_id"] == second["host_boot_id"]
    )


def make_raw_sample(
    boot_id="boot-123",
    interface="enp0s3",
    monotonic=10.0,
    rx_bytes=1000,
    irq_shared=False,
):
    """Create a raw sample for collector unit tests."""

    return {
        "snapshot_timestamp_epoch_s": 1000.0 + monotonic,
        "snapshot_monotonic_s": monotonic,
        "host_boot_id": boot_id,
        "host_id": "test-host",
        "kernel_release": "test-kernel",
        "interface": interface,
        "cpu_psi": {
            "avg10": 1.0,
            "avg60": 2.0,
            "avg300": 3.0,
            "total": 100,
        },
        "softirqs": {
            "NET_RX": {0: 100, 1: 200},
            "NET_TX": {0: 10, 1: 20},
        },
        "softnet": {
            0: {
                "processed": 100,
                "dropped": 1,
                "time_squeeze": 2,
            },
            1: {
                "processed": 200,
                "dropped": 2,
                "time_squeeze": 3,
            },
        },
        "interface_stats": {
            "rx_bytes": rx_bytes,
            "rx_packets": 100,
            "rx_dropped": 0,
            "rx_over_errors": 0,
            "driver": "e1000",
            "rx_queue_count": 1,
            "rps_masks_by_rx_queue": {"rx-0": "0"},
        },
        "network_irq": {
            "irq": "19",
            "count": 500,
            "source": "ehci_hcd:usb2, enp0s3"
            if irq_shared else "enp0s3",
            "shared": irq_shared,
        },
    }


def test_shared_irq_is_not_reported_as_network_delta():
    current = make_raw_sample(monotonic=20.0, irq_shared=True)
    previous = make_raw_sample(monotonic=10.0, irq_shared=True)

    snapshot = telemetry._build_snapshot(current, previous)

    assert snapshot["network_irq_delta"] is None

    assert {
        "metric": "network_irq_delta",
        "reason": "shared_irq_not_attributable",
    } in snapshot["missing_fields"]


def test_interface_counter_reset_is_reported():
    current = make_raw_sample(
        monotonic=20.0,
        rx_bytes=100,
    )
    previous = make_raw_sample(
        monotonic=10.0,
        rx_bytes=1000,
    )

    snapshot = telemetry._build_snapshot(current, previous)

    assert snapshot["interface_rx_bytes_delta"] is None

    assert {
        "metric": "interface_rx_bytes_delta",
        "reason": "counter_reset_detected",
    } in snapshot["missing_fields"]


def test_missing_cpu_pressure_is_reported():
    current = make_raw_sample(monotonic=20.0)
    previous = make_raw_sample(monotonic=10.0)

    current["cpu_psi"] = None

    snapshot = telemetry._build_snapshot(current, previous)

    assert snapshot["cpu_psi_some_avg10_pct"] is None

    assert {
        "metric": "cpu_psi_some_avg10_pct",
        "reason": "metric_unavailable",
    } in snapshot["missing_fields"]


def test_host_reboot_replaces_baseline(monkeypatch):
    old_baseline = make_raw_sample(
        boot_id="old-boot",
        monotonic=100.0,
    )

    first_new_boot_sample = make_raw_sample(
        boot_id="new-boot",
        monotonic=10.0,
    )

    second_new_boot_sample = make_raw_sample(
        boot_id="new-boot",
        monotonic=11.0,
        rx_bytes=1100,
    )

    telemetry._BASELINE = old_baseline

    samples = iter([
        first_new_boot_sample,
        second_new_boot_sample,
    ])

    monkeypatch.setattr(
        telemetry,
        "_capture_raw",
        lambda interface: next(samples),
    )
    monkeypatch.setattr(
        telemetry.time,
        "sleep",
        lambda seconds: None,
    )

    snapshot = telemetry.collect_snapshot("enp0s3")

    assert snapshot["host_boot_id"] == "new-boot"
    assert snapshot["interval_duration_s"] == 1.0
    assert telemetry._BASELINE["host_boot_id"] == "new-boot"


def test_interface_change_establishes_new_baseline(monkeypatch):
    telemetry._BASELINE = make_raw_sample(
        interface="eth0",
        monotonic=100.0,
    )

    first = make_raw_sample(
        interface="enp0s3",
        monotonic=10.0,
    )

    second = make_raw_sample(
        interface="enp0s3",
        monotonic=11.0,
        rx_bytes=1200,
    )

    samples = iter([first, second])

    monkeypatch.setattr(
        telemetry,
        "_capture_raw",
        lambda interface: next(samples),
    )
    monkeypatch.setattr(
        telemetry.time,
        "sleep",
        lambda seconds: None,
    )

    snapshot = telemetry.collect_snapshot("enp0s3")

    assert snapshot["interface"] == "enp0s3"
    assert snapshot["interval_duration_s"] == 1.0
    assert telemetry._BASELINE["interface"] == "enp0s3"


def test_unavailable_softirq_is_not_fabricated_as_zero():
    current = make_raw_sample(monotonic=20.0)
    previous = make_raw_sample(monotonic=10.0)

    current["softirqs"] = None

    snapshot = telemetry._build_snapshot(current, previous)

    assert snapshot["net_rx_softirq_delta_per_cpu"] is None

    assert {
        "metric": "net_rx_softirq_delta_per_cpu",
        "reason": "metric_unavailable",
    } in snapshot["missing_fields"]


def test_cpu_pressure_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "PROC_ROOT", tmp_path)

    assert telemetry.read_cpu_pressure() is None


def test_softirqs_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "PROC_ROOT", tmp_path)

    assert telemetry.read_softirqs() is None


def test_softnet_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "PROC_ROOT", tmp_path)

    assert telemetry.read_softnet_stat() is None


def test_interrupts_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(telemetry, "PROC_ROOT", tmp_path)

    assert telemetry.read_network_irq("enp0s3") is None


def test_nonexistent_interface():
    assert telemetry.read_interface_stats(
        "aura_nonexistent_interface"
    ) is None


def test_rx_overrun_delta():
    current = make_raw_sample(monotonic=20.0)
    previous = make_raw_sample(monotonic=10.0)

    current["interface_stats"]["rx_over_errors"] = 15
    previous["interface_stats"]["rx_over_errors"] = 5

    snapshot = telemetry._build_snapshot(current, previous)

    assert snapshot["interface_rx_over_errors_delta"] == 10
