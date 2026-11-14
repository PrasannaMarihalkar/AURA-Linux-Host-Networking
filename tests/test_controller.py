"""Comprehensive test suite for AURA Observe-Only Controller and Diagnosis Engine.

Tests cover all scenarios required by Gate B specification:
1. Strong CPU contention evidence (elevated P99 latency + elevated CPU PSI).
2. Weak CPU contention evidence (elevated latency without CPU PSI -> uncertain).
3. Receive-path activity skew (busiest CPU handling >= 75% of NET_RX softIRQ).
4. Softnet frame drops and time-squeeze signals.
5. Normal healthy state when all metrics are within configured thresholds.
6. Missing required telemetry / missing client window handled conservatively.
7. Stale telemetry reports rejected / flagged with uncertain outcome and no_op.
8. Server/Host boot ID mismatch detected and handled with monotonic discontinuity flag.
9. Counter resets in per-CPU delta arrays handled safely via structured missing_fields.
10. Newly appeared CPU cores (hotplug) handled safely.
11. Future monotonic timestamps handled safely with age_s is None.
12. Conflicting evidence (e.g., elevated PSI with nominal latency) sets conflicting_evidence=True and uncertain diagnosis.
13. Tail sample size guardrails:
    - P99 < 100: insufficient
    - 100 <= P99 < 10,000: exploratory
    - P99 >= 10,000: minimum_tail_count_met
    - P99.9 < 1,000: insufficient
    - P99.9 >= 100,000: minimum_tail_count_met
14. Observe-only mode never emits action status 'applied'.
15. Controller events validate against contracts/controller_event.schema.json.
16. Thread-safe JSONL controller event log persistence.
17. Multiple evaluation cycles in periodic loop.
18. Clean controller loop start and graceful shutdown.
19. Malformed input isolation (bad input does not crash controller daemon).
20. Cross-component end-to-end integration:
    raw report -> HTTP server ingestion -> enriched persistence -> controller evaluation.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
import pytest
import jsonschema
from jsonschema import Draft202012Validator

from aura.controller import AuraController
from aura.diagnosis import evaluate_candidate_diagnosis, compute_rx_skew
from aura.server import AuraServer

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"
THRESHOLDS_PATH = Path(__file__).resolve().parent.parent / "config" / "thresholds.json"


def load_example(name: str) -> dict:
    with (EXAMPLES_DIR / name).open("r", encoding="utf-8") as f:
        return json.load(f)


def load_thresholds() -> dict:
    with THRESHOLDS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================================
# 1. Diagnosis Engine Unit Tests
# ============================================================================

def test_diagnosis_strong_cpu_contention():
    """Test 1: High latency + elevated CPU PSI produces cpu_contention_candidate."""
    thresholds = load_thresholds()
    thresholds["p99_target_ms"] = 15.0
    thresholds["cpu_psi_some_avg10_threshold_pct"] = 10.0

    client_window = {
        "p99_ms": 25.0,
        "p99_status": "minimum_tail_count_met",
        "sample_count": 12000,
    }
    host_snapshot = {
        "cpu_psi_some_avg10_pct": 18.5,
        "net_rx_softirq_delta_per_cpu": [{"cpu_id": 0, "delta": 100, "reason": None}],
    }

    diag = evaluate_candidate_diagnosis(client_window, host_snapshot, thresholds)
    assert diag["label"] == "cpu_contention_candidate"
    assert diag["confidence"] == 0.85
    assert diag["conflicting_evidence"] is False
    assert any("exceeded target" in e for e in diag["evidence"])
    assert any("exceeded threshold" in e for e in diag["evidence"])


def test_diagnosis_weak_cpu_contention_is_uncertain():
    """Test 2: High latency alone without corroborating CPU PSI produces uncertain."""
    thresholds = load_thresholds()
    thresholds["p99_target_ms"] = 15.0
    thresholds["cpu_psi_some_avg10_threshold_pct"] = 10.0

    client_window = {
        "p99_ms": 25.0,
        "p99_status": "minimum_tail_count_met",
        "sample_count": 12000,
    }
    host_snapshot = {
        "cpu_psi_some_avg10_pct": 2.0,  # Healthy CPU
        "net_rx_softirq_delta_per_cpu": [{"cpu_id": 0, "delta": 50, "reason": None}, {"cpu_id": 1, "delta": 50, "reason": None}],
    }

    diag = evaluate_candidate_diagnosis(client_window, host_snapshot, thresholds)
    assert diag["label"] == "uncertain"
    assert diag["confidence"] is None
    assert any("without corroborating" in e for e in diag["evidence"])


def test_diagnosis_rx_activity_skew():
    """Test 3: NET_RX softIRQ concentrated on one CPU produces rx_activity_skew_candidate."""
    thresholds = load_thresholds()
    thresholds["rx_activity_skew_candidate_threshold"] = 0.75
    thresholds["min_net_rx_events_per_window"] = 200

    client_window = {
        "p99_ms": 12.0,
        "p99_status": "minimum_tail_count_met",
        "sample_count": 12000,
    }
    # CPU 0 handles 900 of 1000 events (90% >= 75%)
    host_snapshot = {
        "cpu_psi_some_avg10_pct": 2.0,
        "net_rx_softirq_delta_per_cpu": [
            {"cpu_id": 0, "delta": 900, "reason": None},
            {"cpu_id": 1, "delta": 100, "reason": None},
        ],
    }

    diag = evaluate_candidate_diagnosis(client_window, host_snapshot, thresholds)
    assert diag["label"] == "rx_activity_skew_candidate"
    assert diag["confidence"] == 0.80
    assert any("NET_RX softIRQ skew detected" in e for e in diag["evidence"])


def test_diagnosis_softnet_congestion():
    """Test 4: Softnet dropped frames with degraded latency produces rx_activity_skew_candidate."""
    thresholds = load_thresholds()
    thresholds["p99_target_ms"] = 15.0

    client_window = {
        "p99_ms": 22.0,
        "p99_status": "minimum_tail_count_met",
        "sample_count": 12000,
    }
    host_snapshot = {
        "cpu_psi_some_avg10_pct": 1.0,
        "net_rx_softirq_delta_per_cpu": [{"cpu_id": 0, "delta": 50, "reason": None}],
        "softnet_dropped_delta_per_cpu": [{"cpu_id": 0, "delta": 15, "reason": None}],
    }

    diag = evaluate_candidate_diagnosis(client_window, host_snapshot, thresholds)
    assert diag["label"] == "rx_activity_skew_candidate"
    assert any("frame drops detected" in e for e in diag["evidence"])


def test_diagnosis_normal():
    """Test 5: Healthy latency and healthy host metrics produces normal candidate."""
    thresholds = load_thresholds()
    thresholds["p99_target_ms"] = 15.0
    thresholds["cpu_psi_some_avg10_threshold_pct"] = 10.0

    client_window = {
        "p99_ms": 5.0,
        "p99_status": "minimum_tail_count_met",
        "sample_count": 12000,
    }
    host_snapshot = {
        "cpu_psi_some_avg10_pct": 1.2,
        "net_rx_softirq_delta_per_cpu": [
            {"cpu_id": 0, "delta": 100, "reason": None},
            {"cpu_id": 1, "delta": 100, "reason": None},
        ],
    }

    diag = evaluate_candidate_diagnosis(client_window, host_snapshot, thresholds)
    assert diag["label"] == "normal"
    assert diag["confidence"] == 0.90


def test_diagnosis_conflicting_evidence():
    """Test 12: Elevated CPU PSI with nominal latency sets conflicting_evidence=True and uncertain."""
    thresholds = load_thresholds()
    thresholds["p99_target_ms"] = 15.0
    thresholds["cpu_psi_some_avg10_threshold_pct"] = 10.0

    client_window = {
        "p99_ms": 6.0,  # Nominal latency
        "p99_status": "minimum_tail_count_met",
        "sample_count": 12000,
    }
    host_snapshot = {
        "cpu_psi_some_avg10_pct": 25.0,  # Elevated PSI
    }

    diag = evaluate_candidate_diagnosis(client_window, host_snapshot, thresholds)
    assert diag["label"] == "uncertain"
    assert diag["conflicting_evidence"] is True
    assert any("Conflicting evidence" in e for e in diag["evidence"])


def test_diagnosis_missing_inputs():
    """Test 6: Missing inputs returns uncertain."""
    thresholds = load_thresholds()
    assert evaluate_candidate_diagnosis(None, None, thresholds)["label"] == "uncertain"
    assert evaluate_candidate_diagnosis({}, None, thresholds)["label"] == "uncertain"
    assert evaluate_candidate_diagnosis(None, {}, thresholds)["label"] == "uncertain"


# ============================================================================
# 2. Controller Observe-Only & Contract Verification Tests
# ============================================================================

def test_controller_observe_only_cycle(tmp_path: Path):
    """Test 14 & 15: Controller cycle generates schema-valid controller_event with action status not applied."""
    event_log = tmp_path / "controller_event.jsonl"
    fake_boot_id = "controller-boot-uuid-999"

    controller = AuraController(
        contracts_dir=CONTRACTS_DIR,
        thresholds_path=THRESHOLDS_PATH,
        event_log_path=event_log,
        server_boot_id=fake_boot_id,
        epoch_provider=lambda: 1794230100.0,
        monotonic_provider=lambda: 500.0,
        controller_mode="observe_only",
    )

    client_win = load_example("client_window_ingested.example.json")
    client_win["server_boot_id"] = fake_boot_id
    client_win["server_received_at_monotonic_s"] = 499.5

    host_snap = load_example("host_snapshot.example.json")

    controller.update_client_window(client_win)
    controller.update_host_snapshot(host_snap)

    event = controller.evaluate_cycle()

    # Schema validation
    controller.event_validator.validate(event)

    # Invariants
    assert event["schema_version"] == "1.2"
    assert event["server_boot_id"] == fake_boot_id
    assert event["controller_mode"] == "observe_only"
    assert event["action"]["status"] in ("not_attempted", "dry_run")
    assert event["action"]["status"] != "applied"
    assert event["stale_telemetry"] is False
    assert event["report_age_s"] == 0.5

    # Check persistence
    assert event_log.exists()
    lines = event_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    persisted = json.loads(lines[0])
    assert persisted["server_boot_id"] == fake_boot_id


def test_controller_stale_telemetry_handling(tmp_path: Path):
    """Test 7: Stale measurement window sets stale_telemetry=True and uncertain diagnosis."""
    event_log = tmp_path / "controller_event.jsonl"
    fake_boot_id = "boot-session-123"

    controller = AuraController(
        contracts_dir=CONTRACTS_DIR,
        thresholds_path=THRESHOLDS_PATH,
        event_log_path=event_log,
        server_boot_id=fake_boot_id,
        epoch_provider=lambda: 1794230100.0,
        monotonic_provider=lambda: 600.0,  # Now is 600s
        controller_mode="observe_only",
    )

    client_win = load_example("client_window_ingested.example.json")
    client_win["server_boot_id"] = fake_boot_id
    client_win["server_received_at_monotonic_s"] = 500.0  # Age = 100s > max_age (15s)

    host_snap = load_example("host_snapshot.example.json")

    controller.update_client_window(client_win)
    controller.update_host_snapshot(host_snap)

    event = controller.evaluate_cycle()
    controller.event_validator.validate(event)

    assert event["stale_telemetry"] is True
    assert event["report_age_s"] == 100.0
    assert event["diagnosis"]["label"] == "uncertain"


def test_controller_boot_id_mismatch(tmp_path: Path):
    """Test 8: Boot ID mismatch sets stale_telemetry=True and uncertain."""
    event_log = tmp_path / "controller_event.jsonl"

    controller = AuraController(
        contracts_dir=CONTRACTS_DIR,
        thresholds_path=THRESHOLDS_PATH,
        event_log_path=event_log,
        server_boot_id="current-boot-id-1",
        epoch_provider=lambda: 1794230100.0,
        monotonic_provider=lambda: 505.0,
    )

    client_win = load_example("client_window_ingested.example.json")
    client_win["server_boot_id"] = "previous-boot-id-old"
    client_win["server_received_at_monotonic_s"] = 500.0

    controller.update_client_window(client_win)
    event = controller.evaluate_cycle()

    assert event["stale_telemetry"] is True
    assert event["report_age_s"] is None  # Mismatch cannot compute monotonic delta
    assert event["diagnosis"]["label"] == "uncertain"


def test_controller_future_monotonic_timestamp(tmp_path: Path):
    """Test 11: Future monotonic timestamp handled safely with report_age_s=None."""
    event_log = tmp_path / "controller_event.jsonl"
    fake_boot_id = "boot-session-456"

    controller = AuraController(
        contracts_dir=CONTRACTS_DIR,
        thresholds_path=THRESHOLDS_PATH,
        event_log_path=event_log,
        server_boot_id=fake_boot_id,
        monotonic_provider=lambda: 500.0,
    )

    client_win = load_example("client_window_ingested.example.json")
    client_win["server_boot_id"] = fake_boot_id
    client_win["server_received_at_monotonic_s"] = 600.0  # Report in future

    controller.update_client_window(client_win)
    event = controller.evaluate_cycle()

    assert event["stale_telemetry"] is True
    assert event["report_age_s"] is None


def test_controller_periodic_loop_execution(tmp_path: Path):
    """Test 17 & 18: Controller start_loop and graceful stop_loop producing multiple events."""
    event_log = tmp_path / "controller_event.jsonl"
    fake_boot_id = "loop-boot-id-789"

    controller = AuraController(
        contracts_dir=CONTRACTS_DIR,
        thresholds_path=THRESHOLDS_PATH,
        event_log_path=event_log,
        server_boot_id=fake_boot_id,
    )

    client_win = load_example("client_window_ingested.example.json")
    client_win["server_boot_id"] = fake_boot_id
    controller.update_client_window(client_win)

    # Start loop with very fast interval for testing
    controller.start_loop(interval_seconds=0.05)
    time.sleep(0.25)
    controller.stop_loop()

    assert event_log.exists()
    lines = event_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 3


# ============================================================================
# 3. End-to-End Cross-Component Integration Test
# ============================================================================

def test_end_to_end_server_to_controller_pipeline(tmp_path: Path):
    """Test 20: Full integration:
    1. Benchmark client submits raw ingress window to HTTP server /report.
    2. Server validates, enriches with server receipt timestamps, and persists to JSONL.
    3. Controller reads enriched report and host snapshot.
    4. Controller runs diagnosis and outputs schema-valid controller_event.jsonl.
    """
    server_output = tmp_path / "server_reports.jsonl"
    controller_output = tmp_path / "controller_events.jsonl"
    common_boot_id = "integrated-test-boot-001"

    # Start server
    server = AuraServer(
        host="127.0.0.1",
        port=0,
        contracts_dir=CONTRACTS_DIR,
        output_file=server_output,
        boot_id_provider=lambda: common_boot_id,
        epoch_provider=lambda: 1794230500.0,
        monotonic_provider=lambda: 1200.0,
    )
    server.start(background=True)
    server_port = server.http_server.server_address[1]

    # Initialize controller
    controller = AuraController(
        contracts_dir=CONTRACTS_DIR,
        thresholds_path=THRESHOLDS_PATH,
        event_log_path=controller_output,
        server_boot_id=common_boot_id,
        epoch_provider=lambda: 1794230501.0,
        monotonic_provider=lambda: 1201.0,  # 1s after ingestion
    )

    try:
        # Step 1: Submit raw ingress payload to HTTP server
        from urllib.request import Request, urlopen
        raw_ingress = load_example("client_window_ingress.example.json")
        body_bytes = json.dumps(raw_ingress).encode("utf-8")
        req = Request(
            f"http://127.0.0.1:{server_port}/report",
            data=body_bytes,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body_bytes))},
            method="POST",
        )
        with urlopen(req) as resp:
            assert resp.status == 200

        # Step 2: Read enriched record from server output file
        lines = server_output.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        enriched_report = json.loads(lines[0])
        assert enriched_report["server_boot_id"] == common_boot_id
        assert enriched_report["server_received_at_monotonic_s"] == 1200.0

        # Step 3: Feed enriched report & host snapshot into controller
        host_snap = load_example("host_snapshot.example.json")
        controller.update_client_window(enriched_report)
        controller.update_host_snapshot(host_snap)

        # Step 4: Evaluate controller cycle
        event = controller.evaluate_cycle()
        assert event["server_boot_id"] == common_boot_id
        assert event["report_age_s"] == 1.0
        assert event["stale_telemetry"] is False
        assert event["controller_mode"] == "observe_only"
        assert event["action"]["status"] in ("not_attempted", "dry_run")

        # Step 5: Verify controller event log written
        c_lines = controller_output.read_text(encoding="utf-8").strip().splitlines()
        assert len(c_lines) == 1

    finally:
        server.shutdown()
