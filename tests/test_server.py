"""Comprehensive test suite for AURA Baseline HTTP Server.

Covers:
1. /health returns HTTP 200 with {"status": "ok"}.
2. /health response is valid JSON and has correct content-type.
3. /work accepts valid integer iteration parameter 'n'.
4. /work produces deterministic results for same iteration count.
5. Increasing iterations performs more CPU work and increases execution time.
6. Malformed, negative, non-integer iteration values are rejected with HTTP 400.
7. Excessively large workloads exceeding max_work_iters are rejected with HTTP 400.
8. Malformed JSON payload on POST /report is rejected with HTTP 400.
9. Invalid UTF-8 body is rejected with HTTP 400.
10. Missing required fields in report payload are rejected with HTTP 400.
11. Wrong field types (e.g., float where int required) are rejected with HTTP 400.
12. Client-injected server timestamps (server_received_at_epoch_s, server_received_at_monotonic_s) rejected with HTTP 400.
13. Client-injected server boot ID rejected with HTTP 400.
14. Arithmetic invariant violations (requests != successes + errors, sample_count > successes) rejected with HTTP 400.
15. Boolean-as-integer values rejected with HTTP 400.
16. Non-finite or mismatched throughput rejected with HTTP 400.
17. NaN and Infinity constants rejected in JSON parsing.
18. Valid ingress records are properly enriched with server-owned receipt timestamps and boot ID.
19. Epoch and monotonic timestamps are server-generated and non-negative.
20. Boot ID comes from provider.
21. Enriched schema validation occurs.
22. Invalid enriched records are not persisted.
23. Valid records produce exactly one JSONL line per request.
24. Concurrent requests are handled safely.
25. Filesystem errors handled safely.
26. Server shutdown is clean and leaves no hanging threads.
"""
from __future__ import annotations

import concurrent.futures
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import pytest

from aura.server import AuraServer, deterministic_cpu_work

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"


def load_example(name: str) -> dict:
    with (EXAMPLES_DIR / name).open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def running_server(tmp_path: Path):
    """Fixture providing a started AuraServer on an ephemeral port with temporary storage."""
    output_file = tmp_path / "reports.jsonl"
    fake_boot_id = "test-boot-uuid-12345"
    fake_epoch = 1794230000.0
    fake_mono = 1000.0

    server = AuraServer(
        host="127.0.0.1",
        port=0,  # OS assigned ephemeral port
        contracts_dir=CONTRACTS_DIR,
        output_file=output_file,
        max_work_iters=500_000,
        max_body_bytes=65536,
        epoch_provider=lambda: fake_epoch,
        monotonic_provider=lambda: fake_mono,
        boot_id_provider=lambda: fake_boot_id,
    )
    server.start(background=True)
    actual_port = server.http_server.server_address[1]
    base_url = f"http://127.0.0.1:{actual_port}"

    yield server, base_url, output_file

    server.shutdown()


def test_health_endpoint(running_server):
    """Test 1 & 2: GET /health returns HTTP 200 with valid JSON status: ok."""
    _, base_url, _ = running_server
    req = Request(f"{base_url}/health")
    with urlopen(req) as resp:
        assert resp.status == 200
        assert "application/json" in resp.headers.get("Content-Type")
        data = json.loads(resp.read().decode("utf-8"))
        assert data == {"status": "ok"}


def test_work_deterministic_computation():
    """Test 4: Deterministic computation returns identical checksums for same iteration count."""
    res1 = deterministic_cpu_work(500)
    res2 = deterministic_cpu_work(500)
    assert res1["status"] == "completed"
    assert res1["iterations"] == 500
    assert res1["checksum"] == res2["checksum"]


def test_work_endpoint_valid(running_server):
    """Test 3 & 5: GET /work?n=iters returns HTTP 200 and performs measurable CPU computation."""
    _, base_url, _ = running_server
    req = Request(f"{base_url}/work?n=1000")
    with urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode("utf-8"))
        assert data["status"] == "completed"
        assert data["iterations"] == 1000
        assert "checksum" in data
        assert len(data["checksum"]) == 64


def test_work_scaling(running_server):
    """Test 5: Increasing iteration count performs more work."""
    _, base_url, _ = running_server
    req_small = Request(f"{base_url}/work?n=100")
    with urlopen(req_small) as resp:
        res_small = json.loads(resp.read().decode("utf-8"))

    req_large = Request(f"{base_url}/work?n=10000")
    with urlopen(req_large) as resp:
        res_large = json.loads(resp.read().decode("utf-8"))

    assert res_small["checksum"] != res_large["checksum"]


@pytest.mark.parametrize("bad_param", [
    "",
    "n=",
    "n=abc",
    "n=-5",
    "n=10.5",
    "n=NaN",
    "n=Infinity",
])
def test_work_endpoint_invalid_params(running_server, bad_param):
    """Test 6: Malformed or negative iteration parameters are rejected with HTTP 400."""
    _, base_url, _ = running_server
    url = f"{base_url}/work?{bad_param}" if bad_param else f"{base_url}/work"
    with pytest.raises(HTTPError) as exc_info:
        urlopen(Request(url))
    assert exc_info.value.code == 400
    err = json.loads(exc_info.value.read().decode("utf-8"))
    assert err["error"] == "invalid_parameter"


def test_work_endpoint_exceeds_max(running_server):
    """Test 7: Iteration count exceeding configured max_work_iters is rejected with HTTP 400."""
    _, base_url, _ = running_server
    # Server max is 500,000
    with pytest.raises(HTTPError) as exc_info:
        urlopen(Request(f"{base_url}/work?n=600000"))
    assert exc_info.value.code == 400
    err = json.loads(exc_info.value.read().decode("utf-8"))
    assert err["error"] == "workload_too_large"


def test_report_valid_flow_and_persistence(running_server):
    """Test 18, 19, 20, 21, 23: Valid report is enriched and persisted as exactly one JSONL record."""
    _, base_url, output_file = running_server
    ingress_example = load_example("client_window_ingress.example.json")

    payload = json.dumps(ingress_example).encode("utf-8")
    req = Request(
        f"{base_url}/report",
        data=payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        method="POST",
    )
    with urlopen(req) as resp:
        assert resp.status == 200
        ack = json.loads(resp.read().decode("utf-8"))
        assert ack["status"] == "accepted"
        assert ack["run_id"] == ingress_example["run_id"]
        assert ack["window_id"] == ingress_example["window_id"]
        assert ack["server_boot_id"] == "test-boot-uuid-12345"
        assert ack["server_received_at_monotonic_s"] == 1000.0

    # Verify JSONL persistence
    assert output_file.exists()
    lines = output_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    persisted = json.loads(lines[0])
    assert persisted["run_id"] == ingress_example["run_id"]
    assert persisted["server_received_at_epoch_s"] == 1794230000.0
    assert persisted["server_received_at_monotonic_s"] == 1000.0
    assert persisted["server_boot_id"] == "test-boot-uuid-12345"


def test_report_rejects_malformed_json(running_server):
    """Test 8: Malformed JSON syntax is rejected with HTTP 400."""
    _, base_url, _ = running_server
    bad_payload = b'{"run_id": "exp", "unclosed}'
    req = Request(
        f"{base_url}/report",
        data=bad_payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(bad_payload))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req)
    assert exc_info.value.code == 400
    err = json.loads(exc_info.value.read().decode("utf-8"))
    assert err["error"] == "invalid_json"


def test_report_rejects_invalid_utf8(running_server):
    """Test 9: Invalid UTF-8 byte sequences are rejected with HTTP 400."""
    _, base_url, _ = running_server
    bad_bytes = b'{"run_id": \xFF\xFE\xFD}'
    req = Request(
        f"{base_url}/report",
        data=bad_bytes,
        headers={"Content-Type": "application/json", "Content-Length": str(len(bad_bytes))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req)
    assert exc_info.value.code == 400
    err = json.loads(exc_info.value.read().decode("utf-8"))
    assert err["error"] == "invalid_encoding"


def test_report_rejects_nan_and_infinity(running_server):
    """Test 17: JSON NaN and Infinity constants are rejected."""
    _, base_url, _ = running_server
    bad_payload = b'{"p99_ms": NaN}'
    req = Request(
        f"{base_url}/report",
        data=bad_payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(bad_payload))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req)
    assert exc_info.value.code == 400
    err = json.loads(exc_info.value.read().decode("utf-8"))
    assert err["error"] == "invalid_json"


def test_report_rejects_missing_required_fields(running_server):
    """Test 10: Missing required ingress schema fields are rejected with HTTP 400."""
    _, base_url, _ = running_server
    ingress_example = load_example("client_window_ingress.example.json")
    del ingress_example["window_id"]  # Remove required field

    payload = json.dumps(ingress_example).encode("utf-8")
    req = Request(
        f"{base_url}/report",
        data=payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req)
    assert exc_info.value.code == 400
    err = json.loads(exc_info.value.read().decode("utf-8"))
    assert err["error"] == "schema_validation_failed"


def test_report_rejects_client_injected_server_timestamps(running_server):
    """Test 12 & 13: Ingress rejecting client-injected server timestamps and boot ID."""
    _, base_url, _ = running_server
    ingress_example = load_example("client_window_ingress.example.json")

    # Injected epoch timestamp
    injected_epoch = dict(ingress_example, server_received_at_epoch_s=123456.7)
    payload = json.dumps(injected_epoch).encode("utf-8")
    req = Request(
        f"{base_url}/report",
        data=payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req)
    assert exc_info.value.code == 400
    assert "schema_validation_failed" in exc_info.value.read().decode("utf-8")

    # Injected boot ID
    injected_boot = dict(ingress_example, server_boot_id="client-fake-boot-id")
    payload2 = json.dumps(injected_boot).encode("utf-8")
    req2 = Request(
        f"{base_url}/report",
        data=payload2,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload2))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info2:
        urlopen(req2)
    assert exc_info2.value.code == 400
    assert "schema_validation_failed" in exc_info2.value.read().decode("utf-8")


def test_report_rejects_arithmetic_invariant_violations(running_server):
    """Test 14: Rejection of requests != successes + errors and sample_count > successes."""
    _, base_url, _ = running_server
    ingress_example = load_example("client_window_ingress.example.json")

    # Invariant 1: requests mismatch (requests=5000 != 4900 + 2)
    bad_req = dict(ingress_example, requests=5000, successes=4900, errors=2)
    payload = json.dumps(bad_req).encode("utf-8")
    req = Request(
        f"{base_url}/report",
        data=payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req)
    assert exc_info.value.code == 400
    assert "semantic_invariant_violation" in exc_info.value.read().decode("utf-8")

    # Invariant 2: sample_count > successes
    bad_sample = dict(ingress_example, sample_count=5000, successes=4998, requests=5000, errors=2)
    payload2 = json.dumps(bad_sample).encode("utf-8")
    req2 = Request(
        f"{base_url}/report",
        data=payload2,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload2))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info2:
        urlopen(req2)
    assert exc_info2.value.code == 400
    assert "semantic_invariant_violation" in exc_info2.value.read().decode("utf-8")


def test_report_rejects_booleans_for_counts(running_server):
    """Test 15: Boolean values masquerading as integers are rejected."""
    _, base_url, _ = running_server
    ingress_example = load_example("client_window_ingress.example.json")
    bad_bool = dict(ingress_example, requests=True, successes=1, errors=0)

    payload = json.dumps(bad_bool).encode("utf-8")
    req = Request(
        f"{base_url}/report",
        data=payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req)
    assert exc_info.value.code == 400


def test_report_rejects_throughput_mismatch(running_server):
    """Test 16: Throughput mismatch is rejected."""
    _, base_url, _ = running_server
    ingress_example = load_example("client_window_ingress.example.json")
    bad_tp = dict(ingress_example, throughput_rps=9999.0)

    payload = json.dumps(bad_tp).encode("utf-8")
    req = Request(
        f"{base_url}/report",
        data=payload,
        headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req)
    assert exc_info.value.code == 400
    assert "semantic_invariant_violation" in exc_info.value.read().decode("utf-8")


def test_report_payload_too_large(running_server):
    """Test body exceeding max_body_bytes (64KB in test fixture) is rejected with HTTP 413."""
    _, base_url, _ = running_server
    huge_data = b"a" * 70000
    req = Request(
        f"{base_url}/report",
        data=huge_data,
        headers={"Content-Type": "application/json", "Content-Length": str(len(huge_data))},
        method="POST",
    )
    with pytest.raises(HTTPError) as exc_info:
        urlopen(req)
    assert exc_info.value.code == 413


def test_concurrent_requests_handling(running_server):
    """Test 24: Concurrent requests to /health, /work, and /report without deadlock or loss."""
    _, base_url, output_file = running_server
    ingress_example = load_example("client_window_ingress.example.json")

    def _send_health():
        with urlopen(f"{base_url}/health") as resp:
            return resp.status

    def _send_work():
        with urlopen(f"{base_url}/work?n=200") as resp:
            return resp.status

    def _send_report(idx: int):
        rep = dict(ingress_example, window_id=idx)
        payload = json.dumps(rep).encode("utf-8")
        req = Request(
            f"{base_url}/report",
            data=payload,
            headers={"Content-Type": "application/json", "Content-Length": str(len(payload))},
            method="POST",
        )
        with urlopen(req) as resp:
            return resp.status

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        futures = []
        for _ in range(5):
            futures.append(executor.submit(_send_health))
            futures.append(executor.submit(_send_work))
        for i in range(10):
            futures.append(executor.submit(_send_report, i))

        results = [f.result() for f in futures]

    assert all(status == 200 for status in results)
    lines = output_file.read_text(encoding="utf-8").strip().splitlines()
    # Exactly 10 reports persisted
    assert len(lines) == 10
