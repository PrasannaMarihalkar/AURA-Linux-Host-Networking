"""Tests for AURA benchmark workload generation and client window semantics."""
from __future__ import annotations

import json
import math
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError

import pytest
from jsonschema import Draft202012Validator

from aura.client import (
    _build_window_records,
    _ingress_validator,
    _normalise_target,
    nearest_rank_percentile,
    run_benchmark,
    tail_statuses,
    write_csv,
    write_jsonl,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((ROOT / "contracts" / "client_window.schema.json").read_text())


class MockHandler(BaseHTTPRequestHandler):
    report_records = []
    request_count = 0
    active_requests = 0
    max_active_requests = 0
    active_lock = threading.Lock()
    delay_s = 0.0
    status_code = 200

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path.startswith("/health"):
            self.send_response(200)
            self.end_headers()
            return
        if self.path.startswith("/work"):
            with type(self).active_lock:
                type(self).request_count += 1
                type(self).active_requests += 1
                type(self).max_active_requests = max(
                    type(self).max_active_requests, type(self).active_requests
                )
            try:
                if type(self).delay_s:
                    time.sleep(type(self).delay_s)
                self.send_response(type(self).status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                try:
                    self.wfile.write(b"{}")
                except OSError:
                    pass
            finally:
                with type(self).active_lock:
                    type(self).active_requests -= 1
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path != "/report":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        type(self).report_records.append(json.loads(raw))
        body = b'{"status":"ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture

def mock_server():
    MockHandler.report_records = []
    MockHandler.request_count = 0
    MockHandler.active_requests = 0
    MockHandler.max_active_requests = 0
    MockHandler.delay_s = 0.0
    MockHandler.status_code = 200
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_nearest_rank_percentile_uses_one_indexed_ceil_rank():
    values = [9.0, 1.0, 5.0, 3.0, 7.0]
    assert nearest_rank_percentile(values, 0.50) == 5.0
    assert nearest_rank_percentile(values, 0.95) == 9.0
    assert nearest_rank_percentile(values, 0.99) == 9.0
    assert nearest_rank_percentile(values, 0.999) == 9.0
    assert nearest_rank_percentile([], 0.99) is None


@pytest.mark.parametrize("p", [0, -0.1, 1.1, float("nan"), float("inf"), True])
def test_nearest_rank_rejects_invalid_percentile(p):
    with pytest.raises((TypeError, ValueError)):
        nearest_rank_percentile([1.0], p)


def test_nearest_rank_rejects_non_finite_or_negative_samples():
    for values in ([-1.0], [float("nan")], [float("inf")]):
        with pytest.raises(ValueError):
            nearest_rank_percentile(values, 0.99)


def test_tail_status_guardrail_boundaries():
    assert tail_statuses(0) == ("insufficient", "insufficient")
    assert tail_statuses(99) == ("insufficient", "insufficient")
    assert tail_statuses(100) == ("exploratory", "insufficient")
    assert tail_statuses(999) == ("exploratory", "insufficient")
    assert tail_statuses(1_000) == ("exploratory", "exploratory")
    assert tail_statuses(9_999) == ("exploratory", "exploratory")
    assert tail_statuses(10_000) == ("minimum_tail_count_met", "exploratory")
    assert tail_statuses(99_999) == ("minimum_tail_count_met", "exploratory")
    assert tail_statuses(100_000) == ("minimum_tail_count_met", "minimum_tail_count_met")


def test_target_url_normalisation():
    work, report = _normalise_target("http://127.0.0.1:8080", 1234)
    assert work == "http://127.0.0.1:8080/work?n=1234"
    assert report == "http://127.0.0.1:8080/report"
    work2, report2 = _normalise_target("http://127.0.0.1:8080/work?n=77", 1234)
    assert work2 == "http://127.0.0.1:8080/work?n=77"
    assert report2 == report


@pytest.mark.parametrize("url", ["", "ftp://localhost/work", "localhost/work", "http://localhost/bad"])
def test_target_url_rejects_unsupported_url(url):
    with pytest.raises(ValueError):
        _normalise_target(url, 100)


def test_window_assignment_uses_terminal_completion_time_and_drain_window():
    start = 100.0
    outcomes = [
        {"completed_at": 100.25, "success": True, "latency_ms": 2.0},
        {"completed_at": 101.25, "success": False, "latency_ms": None},
        {"completed_at": 102.20, "success": True, "latency_ms": 4.0},
    ]
    records = _build_window_records(
        outcomes,
        start_monotonic_s=start,
        generation_deadline_s=102.0,
        start_epoch_s=1_700_000_000.0,
        duration_s=2,
        window_s=1,
        workers=2,
        run_id="unit-test",
        scenario="baseline",
    )
    assert len(records) == 3
    assert [(r["requests"], r["successes"], r["errors"]) for r in records] == [
        (1, 1, 0), (1, 0, 1), (1, 1, 0)
    ]
    assert records[2]["window_duration_s"] == pytest.approx(0.2)
    assert records[0]["p99_status"] == "insufficient"


def test_run_benchmark_generates_schema_valid_records(mock_server):
    records = run_benchmark(
        mock_server + "/work?n=100",
        duration_s=1,
        workers=2,
        window_s=1,
        timeout_s=0.5,
        scenario="test",
        run_id="test-run-success",
    )
    assert records
    validator = Draft202012Validator(SCHEMA)
    for record in records:
        validator.validate(record)
        assert record["requests"] == record["successes"] + record["errors"]
        assert record["sample_count"] <= record["successes"]
        assert record["throughput_rps"] == pytest.approx(
            record["successes"] / record["window_duration_s"]
        )
        assert not ({"server_received_at_epoch_s", "server_received_at_monotonic_s", "server_boot_id"} & record.keys())
    assert sum(r["successes"] for r in records) > 0
    assert records[0]["metadata"]["work_iters"] == 100


def test_run_benchmark_uses_fixed_worker_concurrency_and_posts_ingress_records(mock_server):
    MockHandler.delay_s = 0.02
    records = run_benchmark(
        mock_server,
        duration_s=1,
        workers=3,
        window_s=1,
        timeout_s=0.5,
        run_id="test-report-run",
        submit_reports=True,
    )
    assert records
    assert MockHandler.report_records
    assert len(MockHandler.report_records) == len(records)
    assert MockHandler.max_active_requests >= 2
    for submitted in MockHandler.report_records:
        Draft202012Validator(SCHEMA).validate(submitted)
        assert "server_boot_id" not in submitted
        assert "server_received_at_monotonic_s" not in submitted


def test_connection_refusal_counts_errors_without_crashing():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    records = run_benchmark(
        f"http://127.0.0.1:{port}/work",
        duration_s=1,
        workers=2,
        window_s=1,
        timeout_s=0.15,
        run_id="connection-refused",
    )
    assert records
    assert sum(record["errors"] for record in records) > 0
    assert sum(record["successes"] for record in records) == 0
    assert all(record["requests"] == record["errors"] for record in records)


def test_socket_timeout_counts_as_error(mock_server):
    MockHandler.delay_s = 0.2
    records = run_benchmark(
        mock_server,
        duration_s=1,
        workers=1,
        window_s=1,
        timeout_s=0.05,
        run_id="socket-timeout",
    )
    assert sum(record["errors"] for record in records) > 0
    assert sum(record["successes"] for record in records) == 0


def test_non_200_http_status_counts_as_error(mock_server):
    MockHandler.status_code = 503
    records = run_benchmark(
        mock_server,
        duration_s=1,
        workers=1,
        window_s=1,
        timeout_s=0.5,
        run_id="http-503",
    )
    assert sum(record["errors"] for record in records) > 0
    assert sum(record["successes"] for record in records) == 0


def test_jsonl_and_csv_outputs_are_created_and_reopenable(mock_server, tmp_path):
    records = run_benchmark(mock_server, 1, 2, 1, timeout_s=0.5, run_id="file-output")
    jsonl = write_jsonl(records, tmp_path / "client.jsonl")
    csv_path = write_csv(records, tmp_path / "client.csv")
    loaded = [json.loads(line) for line in jsonl.read_text().splitlines()]
    assert len(loaded) == len(records)
    with csv_path.open(newline="", encoding="utf-8") as handle:
        rows = list(__import__("csv").DictReader(handle))
    assert len(rows) == len(records)
    assert "metadata" in rows[0]


def test_ingress_validator_rejects_server_owned_fields():
    validator = _ingress_validator()
    bad = {
        "schema_version": "1.2", "run_id": "x", "window_id": 1, "scenario": "baseline",
        "client_timestamp_epoch_s": 1.0, "window_duration_s": 1.0,
        "requests": 1, "successes": 1, "errors": 0, "sample_count": 1,
        "throughput_rps": 1.0, "p50_ms": 1.0, "p95_ms": 1.0, "p99_ms": 1.0,
        "p999_ms": 1.0, "p99_status": "insufficient", "p999_status": "insufficient",
        "workers": 1, "server_boot_id": "not-client-owned",
    }
    with pytest.raises(Exception):
        validator.validate(bad)


def test_invalid_run_parameters_are_rejected(mock_server):
    with pytest.raises(ValueError):
        run_benchmark(mock_server, 0, 1, 1)
    with pytest.raises(ValueError):
        run_benchmark(mock_server, 1, 0, 1)
    with pytest.raises(ValueError):
        run_benchmark(mock_server, 1, 1, 0)


def test_redirect_is_counted_as_non_200_error(mock_server):
    MockHandler.status_code = 302
    records = run_benchmark(
        mock_server,
        duration_s=1,
        workers=1,
        window_s=1,
        timeout_s=0.5,
        run_id="http-302",
    )
    assert sum(record["errors"] for record in records) > 0
    assert sum(record["successes"] for record in records) == 0


def test_run_benchmark_writes_raw_successful_latency_sidecar(mock_server, tmp_path):
    samples_path = tmp_path / "latencies.csv"
    records = run_benchmark(
        mock_server,
        duration_s=1,
        workers=2,
        window_s=1,
        timeout_s=0.5,
        run_id="raw-latencies",
        latency_samples_path=samples_path,
    )
    import csv
    with samples_path.open(newline="", encoding="utf-8") as handle:
        samples = list(csv.DictReader(handle))
    successes = sum(record["successes"] for record in records)
    assert len(samples) == successes
    assert all(float(sample["latency_ms"]) >= 0 for sample in samples)
    assert all(sample["run_id"] == "raw-latencies" for sample in samples)


def test_run_benchmark_duration_not_divisible_by_window_size(mock_server):
    """Test that non-divisible durations produce accurate partial final window duration."""
    records = run_benchmark(
        mock_server,
        duration_s=3,
        workers=2,
        window_s=2,
        timeout_s=1.0,
        run_id="partial-window-run",
    )
    assert len(records) >= 2
    # Window 1: full 2s window
    w1 = records[0]
    assert math.isclose(w1["window_duration_s"], 2.0, rel_tol=1e-2)
    # Window 2: partial window (~1s duration, strictly less than configured 2s)
    w2 = records[1]
    assert w2["window_duration_s"] < 1.8
    assert math.isclose(
        w2["throughput_rps"],
        w2["successes"] / w2["window_duration_s"],
        rel_tol=1e-6,
    )


def test_build_window_records_with_empty_and_boundary_outcomes():
    """Test _build_window_records correctly formats empty and boundary outcomes."""
    from aura.client import _build_window_records

    # Outcomes only in window 0; window 1 is completely empty
    outcomes = [
        {"completed_at": 100.5, "latency_ms": 12.0, "success": True},
        {"completed_at": 101.0, "latency_ms": 15.0, "success": True},
    ]
    records = _build_window_records(
        outcomes,
        start_monotonic_s=100.0,
        generation_deadline_s=104.0,  # 4s total -> 2 windows of 2s
        start_epoch_s=1700000000.0,
        duration_s=4,
        window_s=2,
        workers=1,
        run_id="empty-window-test",
        scenario="unit_test",
    )
    assert len(records) == 2
    # Window 1 has outcomes
    assert records[0]["requests"] == 2
    assert records[0]["successes"] == 2
    assert records[0]["sample_count"] == 2
    # Window 2 is empty but schema-valid
    assert records[1]["requests"] == 0
    assert records[1]["successes"] == 0
    assert records[1]["errors"] == 0
    assert records[1]["sample_count"] == 0
    assert records[1]["throughput_rps"] == 0.0
    assert records[1]["p50_ms"] is None
    assert records[1]["p99_status"] == "insufficient"


def test_async_delivery_retries_and_graceful_drain():
    """Test asynchronous report delivery queue retries on transient errors and drains cleanly."""
    attempts = 0
    lock = threading.Lock()

    class FlakyReportHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')

        def do_POST(self):
            nonlocal attempts
            with lock:
                attempts += 1
                curr = attempts
            # Fail on first attempt, succeed on retry
            if curr == 1:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b'{"error":"temporary_unavailable"}')
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"accepted"}')

        def log_message(self, *args, **kwargs):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), FlakyReportHandler)
    server_port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        records = run_benchmark(
            url=f"http://127.0.0.1:{server_port}/work",
            duration_s=1,
            workers=1,
            window_s=1,
            timeout_s=1.0,
            submit_reports=True,
            run_id="flaky-delivery-test",
        )
        assert len(records) >= 1
        # Due to retry, the submission should have succeeded
        assert records[0]["metadata"]["report_submission"] == "accepted"
        assert attempts >= 2
    finally:
        server.shutdown()
        server.server_close()


