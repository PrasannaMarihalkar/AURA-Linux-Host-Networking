"""End-to-end integration tests for AURA system runtime.

Verifies:
1. Runtime startup, /health and /work endpoints.
2. Real-time client window streaming to /report.
3. Telemetry sampling and controller event generation without manual mock injection.
4. Duplicate window rejection and idempotency.
5. Conflicting window rejection.
6. Graceful shutdown and persistence recovery on restart.
"""
from __future__ import annotations

import json
from pathlib import Path
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import pytest

from aura.runtime import AuraRuntime
from aura.client import run_benchmark

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "thresholds.json"


def load_example(name: str) -> dict:
    with (EXAMPLES_DIR / name).open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def mock_telemetry_sampler():
    """Provides a deterministic, schema-valid host snapshot sampler."""
    sample_template = load_example("host_snapshot.example.json")

    def _sampler(interface: str | None) -> dict:
        data = dict(sample_template)
        data["snapshot_timestamp_epoch_s"] = time.time()
        data["snapshot_monotonic_s"] = time.monotonic()
        data["host_boot_id"] = "test-runtime-boot-001"
        return data

    return _sampler


def test_runtime_end_to_end_streaming_and_evaluation(tmp_path: Path, mock_telemetry_sampler):
    """Test A, B, D: Start runtime, run benchmark with submit_reports=True, verify controller events."""
    results_dir = tmp_path / "results"
    test_boot_id = "test-runtime-boot-001"

    runtime = AuraRuntime(
        host="127.0.0.1",
        port=0,
        interface="eth0",
        config_path=CONFIG_PATH,
        results_dir=results_dir,
        contracts_dir=CONTRACTS_DIR,
        boot_id_provider=lambda: test_boot_id,
        telemetry_sampler=mock_telemetry_sampler,
        telemetry_interval_s=0.1,
    )

    try:
        runtime.start(background=True)
        server_port = runtime.server.http_server.server_address[1]
        base_url = f"http://127.0.0.1:{server_port}"

        # 1. Health check
        with urlopen(f"{base_url}/health") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["status"] == "ok"

        # 2. Run client benchmark streaming real-time windows
        records = run_benchmark(
            url=f"{base_url}/work?n=50",
            duration_s=2,
            workers=2,
            window_s=1,
            timeout_s=2.0,
            scenario="integration_test",
            run_id="test-run-stream",
            submit_reports=True,
        )

        assert len(records) >= 1
        assert all(r["metadata"]["report_submission"] == "accepted" for r in records)

        # Allow consumer worker to process queue
        time.sleep(0.5)

        # 3. Verify server persisted client windows
        client_log = results_dir / "client_window_ingested.jsonl"
        assert client_log.exists()
        client_lines = client_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(client_lines) == len(records)

        # 4. Verify telemetry persisted
        host_log = results_dir / "host_snapshot.jsonl"
        assert host_log.exists()
        assert len(host_log.read_text(encoding="utf-8").strip().splitlines()) >= 1

        # 5. Verify controller events generated
        controller_log = results_dir / "controller_event.jsonl"
        assert controller_log.exists()
        ctrl_lines = controller_log.read_text(encoding="utf-8").strip().splitlines()
        assert len(ctrl_lines) >= 1

        first_event = json.loads(ctrl_lines[0])
        assert first_event["server_boot_id"] == test_boot_id
        assert first_event["controller_mode"] == "observe_only"
        assert first_event["action"]["status"] in ("not_attempted", "dry_run")
        assert first_event["action"]["type"] == "no_op"

    finally:
        runtime.shutdown()


def test_runtime_duplicate_and_conflict_handling(tmp_path: Path, mock_telemetry_sampler):
    """Test duplicate window idempotency and conflicting payload rejection."""
    results_dir = tmp_path / "results"
    test_boot_id = "test-runtime-boot-001"

    runtime = AuraRuntime(
        host="127.0.0.1",
        port=0,
        interface="eth0",
        config_path=CONFIG_PATH,
        results_dir=results_dir,
        contracts_dir=CONTRACTS_DIR,
        boot_id_provider=lambda: test_boot_id,
        telemetry_sampler=mock_telemetry_sampler,
    )

    try:
        runtime.start(background=True)
        server_port = runtime.server.http_server.server_address[1]
        report_url = f"http://127.0.0.1:{server_port}/report"

        raw_ingress = load_example("client_window_ingress.example.json")
        body_bytes = json.dumps(raw_ingress).encode("utf-8")

        # First submission: 200 OK accepted
        req1 = Request(
            report_url,
            data=body_bytes,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body_bytes))},
            method="POST",
        )
        with urlopen(req1) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["status"] == "accepted"

        # Duplicate identical submission: 200 OK idempotent
        req2 = Request(
            report_url,
            data=body_bytes,
            headers={"Content-Type": "application/json", "Content-Length": str(len(body_bytes))},
            method="POST",
        )
        with urlopen(req2) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["status"] == "accepted_duplicate_idempotent"

        # Conflicting submission with same (run_id, window_id) but different requests: 409 Conflict
        conflicting_ingress = dict(raw_ingress)
        conflicting_ingress["requests"] = raw_ingress["requests"] + 10
        conflicting_ingress["successes"] = raw_ingress["successes"] + 10
        conflicting_ingress["sample_count"] = raw_ingress["sample_count"] + 10
        conflicting_ingress["throughput_rps"] = conflicting_ingress["successes"] / conflicting_ingress["window_duration_s"]
        conflict_bytes = json.dumps(conflicting_ingress).encode("utf-8")

        req3 = Request(
            report_url,
            data=conflict_bytes,
            headers={"Content-Type": "application/json", "Content-Length": str(len(conflict_bytes))},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc_info:
            urlopen(req3)
        assert exc_info.value.code == 409

    finally:
        runtime.shutdown()


def test_runtime_restart_and_recovery(tmp_path: Path, mock_telemetry_sampler):
    """Test F: Restarting runtime recovers processed state from existing JSONL without duplicates."""
    results_dir = tmp_path / "results"
    test_boot_id = "test-runtime-boot-001"

    # Runtime 1
    r1 = AuraRuntime(
        host="127.0.0.1",
        port=0,
        interface="eth0",
        config_path=CONFIG_PATH,
        results_dir=results_dir,
        contracts_dir=CONTRACTS_DIR,
        boot_id_provider=lambda: test_boot_id,
        telemetry_sampler=mock_telemetry_sampler,
    )
    r1.start(background=True)
    port1 = r1.server.http_server.server_address[1]

    raw_ingress = load_example("client_window_ingress.example.json")
    body_bytes = json.dumps(raw_ingress).encode("utf-8")
    req = Request(
        f"http://127.0.0.1:{port1}/report",
        data=body_bytes,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body_bytes))},
        method="POST",
    )
    with urlopen(req) as resp:
        assert resp.status == 200

    time.sleep(1.0)
    r1.shutdown()

    ctrl_log = results_dir / "controller_event.jsonl"
    events_r1 = ctrl_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(events_r1) == 1

    # Runtime 2 (Restart pointing to same directory)
    r2 = AuraRuntime(
        host="127.0.0.1",
        port=0,
        interface="eth0",
        config_path=CONFIG_PATH,
        results_dir=results_dir,
        contracts_dir=CONTRACTS_DIR,
        boot_id_provider=lambda: test_boot_id,
        telemetry_sampler=mock_telemetry_sampler,
    )
    r2.start(background=True)
    port2 = r2.server.http_server.server_address[1]

    # Verify server recovered the processed window key into dedup cache
    window_key = (str(raw_ingress["run_id"]), int(raw_ingress["window_id"]))
    assert window_key in r2.server.ingestion_dedup_cache

    # Attempting to post the identical window to restarted server is accepted idempotently without duplicate persistence
    req_dup = Request(
        f"http://127.0.0.1:{port2}/report",
        data=body_bytes,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body_bytes))},
        method="POST",
    )
    with urlopen(req_dup) as resp:
        assert resp.status == 200
        resp_data = json.loads(resp.read().decode())
        assert resp_data["status"] == "accepted_duplicate_idempotent"

    time.sleep(0.5)

    ingested_log = results_dir / "client_window_ingested.jsonl"
    ingested_lines = ingested_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(ingested_lines) == 1  # No duplicate record appended to ingested storage

    events_r2 = ctrl_log.read_text(encoding="utf-8").strip().splitlines()
    assert len(events_r2) == 1  # No duplicate event written

    r2.shutdown()


def test_runtime_queue_saturation_backpressure(tmp_path: Path, mock_telemetry_sampler):
    """Test G: Queue saturation enforces backpressure and rejects overflow with HTTP 503."""
    results_dir = tmp_path / "results_backpressure"
    test_boot_id = "test-runtime-boot-sat"

    # Configure a tiny queue with short timeout
    runtime = AuraRuntime(
        host="127.0.0.1",
        port=0,
        interface="eth0",
        config_path=CONFIG_PATH,
        results_dir=results_dir,
        contracts_dir=CONTRACTS_DIR,
        boot_id_provider=lambda: test_boot_id,
        telemetry_sampler=mock_telemetry_sampler,
        queue_capacity=1,
        queue_timeout_s=0.1,
    )
    # Pause the controller worker so the queue fills up immediately
    runtime.server.start(background=True)
    port = runtime.server.http_server.server_address[1]

    try:
        raw_ingress = load_example("client_window_ingress.example.json")

        # Fill the 1-slot queue
        p1 = dict(raw_ingress, window_id=1)
        b1 = json.dumps(p1).encode("utf-8")
        req1 = Request(
            f"http://127.0.0.1:{port}/report",
            data=b1,
            headers={"Content-Type": "application/json", "Content-Length": str(len(b1))},
            method="POST",
        )
        with urlopen(req1) as resp1:
            assert resp1.status == 200

        # Second item should trigger backpressure HTTP 503 because queue is saturated
        p2 = dict(raw_ingress, window_id=2)
        b2 = json.dumps(p2).encode("utf-8")
        req2 = Request(
            f"http://127.0.0.1:{port}/report",
            data=b2,
            headers={"Content-Type": "application/json", "Content-Length": str(len(b2))},
            method="POST",
        )
        with pytest.raises(HTTPError) as exc_info:
            urlopen(req2)
        assert exc_info.value.code == 503
        data = json.loads(exc_info.value.read().decode("utf-8"))
        assert data["error"] == "runtime_backpressure"

    finally:
        runtime.shutdown()

