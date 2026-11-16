"""AURA HTTP workload client and windowed latency collector.

The client measures each request with ``time.monotonic()`` and assigns its
terminal outcome to the measurement window in which the outcome occurs.
Only HTTP 200 responses contribute latency samples. Failed requests are
counted separately and never enter latency percentiles.

The emitted records are client-ingress records (schema v1.2): this module
never adds server receipt timestamps or a server boot ID.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import queue
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import jsonschema
from jsonschema import Draft202012Validator

LOGGER = logging.getLogger("aura.client")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "contracts" / "client_window.schema.json"
CSV_FIELDS = (
    "schema_version", "run_id", "window_id", "scenario",
    "client_timestamp_epoch_s", "window_duration_s", "requests",
    "successes", "errors", "sample_count", "throughput_rps",
    "p50_ms", "p95_ms", "p99_ms", "p999_ms", "p99_status",
    "p999_status", "workers", "metadata",
)
class _NoRedirectHandler(HTTPRedirectHandler):
    """Treat redirects as non-200 terminal HTTP outcomes; do not hide them."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


_OPENER_LOCAL = threading.local()


def _http_opener():
    """Return a per-thread urllib opener so worker requests share no handler state."""
    opener = getattr(_OPENER_LOCAL, "opener", None)
    if opener is None:
        opener = build_opener(_NoRedirectHandler())
        _OPENER_LOCAL.opener = opener
    return opener


SERVER_OWNED_FIELDS = {
    "server_received_at_epoch_s",
    "server_received_at_monotonic_s",
    "server_boot_id",
}


@lru_cache(maxsize=1)
def _ingress_validator() -> Draft202012Validator:
    """Load and compile the normative client-ingress schema once."""
    if not SCHEMA_PATH.is_file():
        raise FileNotFoundError(
            f"AURA client ingress schema was not found at {SCHEMA_PATH}. "
            "Run the client from an AURA repository checkout."
        )
    with SCHEMA_PATH.open("r", encoding="utf-8") as handle:
        schema = json.load(handle, parse_constant=_reject_json_constant)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-standard JSON numeric constant is forbidden: {value}")


def nearest_rank_percentile(values: Sequence[float], percentile: float) -> float | None:
    """Return nearest-rank percentile (1-indexed ceil(p*N)); None for no samples."""
    if isinstance(percentile, bool) or not isinstance(percentile, (int, float)):
        raise TypeError("percentile must be a number in (0, 1]")
    p = float(percentile)
    if not math.isfinite(p) or not 0.0 < p <= 1.0:
        raise ValueError("percentile must be finite and in (0, 1]")
    if not values:
        return None

    clean: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("latency samples must be numeric and not boolean")
        number = float(value)
        if not math.isfinite(number) or number < 0:
            raise ValueError("latency samples must be finite and non-negative")
        clean.append(number)
    clean.sort()
    rank = max(1, min(math.ceil(p * len(clean)), len(clean)))
    return clean[rank - 1]


def tail_statuses(sample_count: int) -> tuple[str, str]:
    """Return contract v1.2 P99 and P99.9 reporting sufficiency statuses."""
    if isinstance(sample_count, bool) or not isinstance(sample_count, int) or sample_count < 0:
        raise ValueError("sample_count must be a non-negative integer")
    if sample_count >= 10_000:
        p99_status = "minimum_tail_count_met"
    elif sample_count >= 100:
        p99_status = "exploratory"
    else:
        p99_status = "insufficient"

    if sample_count >= 100_000:
        p999_status = "minimum_tail_count_met"
    elif sample_count >= 1_000:
        p999_status = "exploratory"
    else:
        p999_status = "insufficient"
    return p99_status, p999_status


def _positive_int(name: str, value: Any, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _non_negative_int(name: str, value: Any, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return value


def _positive_finite(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return number


def _normalise_target(url: str, work_iters: int) -> tuple[str, str]:
    """Resolve a server base URL or /work URL to concrete /work and /report URLs."""
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty HTTP(S) URL")
    parsed = urlsplit(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("url must include an http:// or https:// scheme and host")
    if parsed.username or parsed.password:
        raise ValueError("credentials must not be embedded in the benchmark URL")

    path = parsed.path.rstrip("/")
    if path.endswith("/work"):
        prefix = path[:-len("/work")]
    elif path in {"", "/"}:
        prefix = ""
    else:
        raise ValueError("url path must be empty, '/', or end in '/work'")

    supplied_query = parse_qs(parsed.query, keep_blank_values=True)
    query = {"n": supplied_query.get("n", [str(work_iters)])}
    if len(query["n"]) != 1:
        raise ValueError("the /work URL must contain at most one 'n' parameter")
    try:
        n = int(query["n"][0])
    except (TypeError, ValueError) as exc:
        raise ValueError("the /work 'n' parameter must be a non-negative integer") from exc
    if n < 0:
        raise ValueError("the /work 'n' parameter must be a non-negative integer")

    work_path = f"{prefix}/work" or "/work"
    report_path = f"{prefix}/report" or "/report"
    work_url = urlunsplit((parsed.scheme, parsed.netloc, work_path, urlencode(query, doseq=True), ""))
    report_url = urlunsplit((parsed.scheme, parsed.netloc, report_path, "", ""))
    return work_url, report_url


def _attempt_request(
    work_url: str,
    timeout_s: float,
    start_deadline_s: float | None = None,
) -> tuple[bool, float, float, str | None] | None:
    """Perform one request and return (success, terminal_mono, latency_ms, error_kind).

    For HTTP responses, terminal time is captured strictly after the full response
    body is completely consumed. If reading the body raises an exception or stalls,
    the request terminates as an error. For connection failures and timeouts,
    terminal time is captured when the exception terminates the request.
    """
    started = time.monotonic()
    if start_deadline_s is not None and started >= start_deadline_s:
        return None
    request = Request(
        work_url,
        headers={"Accept": "application/json", "User-Agent": "AURA-Benchmark-Client/1.0", "Connection": "close"},
        method="GET",
    )
    try:
        with _http_opener().open(request, timeout=timeout_s) as response:
            status = response.getcode()
            try:
                body = response.read()
            except (OSError, TimeoutError, socket.timeout) as exc:
                terminal = time.monotonic()
                reason = getattr(exc, "reason", exc)
                return False, terminal, max(0.0, (terminal - started) * 1000.0), f"body_read_error_{type(reason).__name__}"

            terminal = time.monotonic()
            elapsed_ms = max(0.0, (terminal - started) * 1000.0)

            if status == 200:
                if not body:
                    return False, terminal, elapsed_ms, "empty_response_body"
                return True, terminal, elapsed_ms, None

            return False, terminal, elapsed_ms, f"http_{status}"
    except HTTPError as exc:
        terminal = time.monotonic()
        error_kind = f"http_{exc.code}"
        try:
            exc.read()
        except (OSError, TimeoutError, socket.timeout):
            pass
        exc.close()
        return False, terminal, max(0.0, (terminal - started) * 1000.0), error_kind
    except (URLError, TimeoutError, socket.timeout, OSError) as exc:
        terminal = time.monotonic()
        reason = getattr(exc, "reason", exc)
        return False, terminal, max(0.0, (terminal - started) * 1000.0), type(reason).__name__


def _build_window_records(
    outcomes: Sequence[Mapping[str, Any]],
    *,
    start_monotonic_s: float,
    generation_deadline_s: float,
    start_epoch_s: float,
    duration_s: int,
    window_s: int,
    workers: int,
    run_id: str,
    scenario: str,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Bucket terminal outcomes by completion time and build schema records."""
    latest_terminal = max(
        (float(outcome["completed_at"]) for outcome in outcomes),
        default=generation_deadline_s,
    )
    effective_end = max(generation_deadline_s, latest_terminal)
    effective_elapsed = max(float(duration_s), effective_end - start_monotonic_s)
    # ceil() ensures a completion after the nominal run deadline is not lost.
    number_of_windows = max(1, math.ceil(effective_elapsed / window_s - 1e-12))
    buckets: list[list[Mapping[str, Any]]] = [[] for _ in range(number_of_windows)]

    for outcome in outcomes:
        elapsed = max(0.0, float(outcome["completed_at"]) - start_monotonic_s)
        index = min(int(elapsed // window_s), number_of_windows - 1)
        buckets[index].append(outcome)

    base_metadata: dict[str, Any] = {
        "client_hostname": socket.gethostname(),
        "load_model": "closed_loop_fixed_concurrency",
        "timing_source": "time.monotonic",
        "client_wall_clock_is_metadata_only": True,
    }
    if metadata:
        base_metadata.update(dict(metadata))

    records: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        offset = index * window_s
        actual_duration = min(float(window_s), effective_elapsed - offset)
        # Floating-point edge cases must never produce a zero-duration contract window.
        if actual_duration <= 0:
            continue
        latencies = [float(item["latency_ms"]) for item in bucket if item["success"]]
        successes = len(latencies)
        errors = len(bucket) - successes
        requests = successes + errors
        sample_count = len(latencies)
        p99_status, p999_status = tail_statuses(sample_count)
        window_end_offset = offset + actual_duration
        record_metadata = dict(base_metadata)
        record_metadata.update({
            "window_start_offset_s": offset,
            "window_end_offset_s": window_end_offset,
            "configured_window_s": window_s,
            "generation_duration_s": duration_s,
            "terminal_outcomes_in_window": requests,
        })
        record = {
            "schema_version": "1.2",
            "run_id": run_id,
            "window_id": index + 1,
            "scenario": scenario,
            # Wall-clock time is metadata only. Measurement/bucketing uses monotonic time.
            "client_timestamp_epoch_s": float(start_epoch_s + window_end_offset),
            "window_duration_s": float(actual_duration),
            "requests": requests,
            "successes": successes,
            "errors": errors,
            "sample_count": sample_count,
            "throughput_rps": float(successes / actual_duration),
            "p50_ms": nearest_rank_percentile(latencies, 0.50),
            "p95_ms": nearest_rank_percentile(latencies, 0.95),
            "p99_ms": nearest_rank_percentile(latencies, 0.99),
            "p999_ms": nearest_rank_percentile(latencies, 0.999),
            "p99_status": p99_status,
            "p999_status": p999_status,
            "workers": workers,
            "metadata": record_metadata,
        }
        if SERVER_OWNED_FIELDS.intersection(record):
            raise AssertionError("client must not populate server-owned ingress fields")
        _ingress_validator().validate(record)
        _validate_semantics(record)
        # Ensure nested metadata cannot smuggle NaN/Infinity into output files.
        json.dumps(record, allow_nan=False)
        records.append(record)
    return records


def _validate_semantics(record: Mapping[str, Any]) -> None:
    """Check cross-field arithmetic invariants not expressible in JSON Schema."""
    requests = record["requests"]
    successes = record["successes"]
    errors = record["errors"]
    sample_count = record["sample_count"]
    duration = record["window_duration_s"]
    throughput = record["throughput_rps"]
    if requests != successes + errors:
        raise ValueError("requests must equal successes + errors")
    if sample_count > successes:
        raise ValueError("sample_count must be <= successes")
    if not math.isfinite(float(duration)) or float(duration) <= 0:
        raise ValueError("window_duration_s must be finite and positive")
    expected = successes / float(duration)
    if not math.isclose(float(throughput), expected, rel_tol=1e-6, abs_tol=1e-9):
        raise ValueError("throughput_rps must equal successes / window_duration_s")


def _post_window(report_url: str, record: Mapping[str, Any], timeout_s: float) -> tuple[bool, str | None]:
    """Optionally submit one client ingress record to the server's /report endpoint."""
    payload = json.dumps(record, allow_nan=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        report_url,
        data=payload,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with _http_opener().open(request, timeout=timeout_s) as response:
            if response.getcode() != 200:
                return False, f"HTTP {response.getcode()}"
            response.read()
        return True, None
    except (HTTPError, URLError, TimeoutError, socket.timeout, OSError, ValueError) as exc:
        return False, f"{type(exc).__name__}: {exc}"


def write_latency_samples(
    outcomes: Sequence[Mapping[str, Any]],
    path: str | Path,
    *,
    start_monotonic_s: float,
    generation_deadline_s: float,
    duration_s: int,
    window_s: int,
    run_id: str,
    scenario: str,
) -> Path:
    """Write successful raw latency observations for real distribution plots.

    This file is deliberately separate from the client-window contract: the
    contract stores window summaries, while this sidecar preserves the actual
    successful samples without trying to reconstruct a distribution from
    P50/P95/P99/P99.9 summaries.
    """
    latest_terminal = max(
        (float(item["completed_at"]) for item in outcomes),
        default=generation_deadline_s,
    )
    effective_elapsed = max(float(duration_s), latest_terminal - start_monotonic_s)
    window_count = max(1, math.ceil(effective_elapsed / window_s - 1e-12))
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("run_id", "scenario", "window_id", "latency_ms"))
        writer.writeheader()
        for item in outcomes:
            if not item.get("success"):
                continue
            offset = max(0.0, float(item["completed_at"]) - start_monotonic_s)
            window_index = min(int(offset // window_s), window_count - 1)
            writer.writerow({
                "run_id": run_id,
                "scenario": scenario,
                "window_id": window_index + 1,
                "latency_ms": float(item["latency_ms"]),
            })
    return output


def run_benchmark(
    url: str,
    duration_s: int,
    workers: int,
    window_s: int,
    *,
    timeout_s: float = 2.0,
    work_iters: int = 1_000,
    scenario: str = "baseline",
    run_id: str | None = None,
    submit_reports: bool = False,
    report_url: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    latency_samples_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Run a fixed-concurrency HTTP workload and return client ingress windows.

    Args:
        url: Server base URL or explicit ``/work`` URL. If no ``n`` query
            parameter is supplied, ``work_iters`` is added.
        duration_s: Positive integer duration for launching new requests.
        workers: Number of concurrent worker threads.
        window_s: Positive integer measurement-window size in seconds.
        timeout_s: Per-request socket timeout in seconds.
        work_iters: Iteration count passed to the server's deterministic /work endpoint.
        scenario: Scenario label recorded in each client window.
        run_id: Optional run identifier; defaults to a generated UUID.
        submit_reports: If true, POST each window to ``/report`` after measurement.
            Submission happens after timing and is not included in workload metrics.
        report_url: Optional explicit report endpoint; otherwise derived from ``url``.
        metadata: Additional JSON-serializable run metadata.

    Returns:
        Ordered list of schema-v1.2 ingress records. Failed requests are counted
        as errors; only HTTP 200 latencies contribute to percentiles.
    """
    duration_s = _positive_int("duration_s", duration_s, maximum=86_400)
    workers = _positive_int("workers", workers, maximum=512)
    window_s = _positive_int("window_s", window_s, maximum=86_400)
    timeout_s = _positive_finite("timeout_s", timeout_s)
    work_iters = _non_negative_int("work_iters", work_iters, maximum=10_000_000)
    if not isinstance(scenario, str) or not scenario.strip():
        raise ValueError("scenario must be a non-empty string")
    if run_id is None:
        run_id = f"aura-{uuid.uuid4()}"
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be a non-empty string")

    work_url, derived_report_url = _normalise_target(url, work_iters)
    actual_work_iters = int(parse_qs(urlsplit(work_url).query)["n"][0])
    final_report_url = report_url or derived_report_url
    start_monotonic_s = time.monotonic()
    generation_deadline_s = start_monotonic_s + duration_s
    start_epoch_s = time.time()
    outcomes: list[dict[str, Any]] = []
    outcome_lock = threading.Lock()
    records: list[dict[str, Any]] = []
    records_lock = threading.Lock()
    closed_windows: set[int] = set()
    stop_event = threading.Event()

    base_metadata: dict[str, Any] = {
        "client_hostname": socket.gethostname(),
        "load_model": "closed_loop_fixed_concurrency",
        "load_model_note": "Closed-loop benchmark subject to coordinated omission during server latency spikes",
        "timing_source": "time.monotonic",
        "client_wall_clock_is_metadata_only": True,
        "target_url": work_url,
        "work_iters": actual_work_iters,
        "request_timeout_s": timeout_s,
        **(dict(metadata) if metadata else {}),
    }

    delivery_queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=1000)
    delivery_worker_thread: threading.Thread | None = None

    def delivery_worker_loop() -> None:
        """Asynchronously deliver completed window records to /report with bounded retries."""
        while True:
            item = delivery_queue.get()
            if item is None:
                delivery_queue.task_done()
                break
            record = item
            max_retries = 2
            success = False
            last_err = None
            for attempt in range(max_retries + 1):
                ok, err = _post_window(final_report_url, record, timeout_s)
                if ok:
                    success = True
                    break
                last_err = err
                if attempt < max_retries:
                    time.sleep(0.05 * (2 ** attempt))
            if success:
                record["metadata"]["report_submission"] = "accepted"
                LOGGER.info("window %s accepted by server /report", record["window_id"])
            else:
                record["metadata"]["report_submission"] = "failed"
                record["metadata"]["report_submission_error"] = last_err
                LOGGER.warning("window %s report submission failed after %d retries: %s", record["window_id"], max_retries, last_err)
            delivery_queue.task_done()

    if submit_reports:
        delivery_worker_thread = threading.Thread(target=delivery_worker_loop, name="aura-delivery", daemon=True)
        delivery_worker_thread.start()

    def _finalize_window(
        window_idx: int,
        window_outcomes: list[dict[str, Any]],
        actual_duration: float | None = None,
    ) -> dict[str, Any]:
        offset = window_idx * window_s
        if actual_duration is None:
            actual_duration = float(window_s)
        actual_duration = max(1e-6, float(actual_duration))
        latencies = [float(item["latency_ms"]) for item in window_outcomes if item["success"]]
        successes = len(latencies)
        errors = len(window_outcomes) - successes
        requests = successes + errors
        sample_count = len(latencies)
        p99_status, p999_status = tail_statuses(sample_count)
        window_end_offset = offset + actual_duration

        rec_meta = dict(base_metadata)
        rec_meta.update({
            "window_start_offset_s": offset,
            "window_end_offset_s": window_end_offset,
            "configured_window_s": window_s,
            "generation_duration_s": duration_s,
            "actual_window_duration_s": actual_duration,
            "terminal_outcomes_in_window": requests,
        })
        record = {
            "schema_version": "1.2",
            "run_id": run_id,
            "window_id": window_idx + 1,
            "scenario": scenario.strip(),
            "client_timestamp_epoch_s": float(start_epoch_s + window_end_offset),
            "window_duration_s": float(actual_duration),
            "requests": requests,
            "successes": successes,
            "errors": errors,
            "sample_count": sample_count,
            "throughput_rps": float(successes / actual_duration),
            "p50_ms": nearest_rank_percentile(latencies, 0.50),
            "p95_ms": nearest_rank_percentile(latencies, 0.95),
            "p99_ms": nearest_rank_percentile(latencies, 0.99),
            "p999_ms": nearest_rank_percentile(latencies, 0.999),
            "p99_status": p99_status,
            "p999_status": p999_status,
            "workers": workers,
            "metadata": rec_meta,
        }
        _ingress_validator().validate(record)
        _validate_semantics(record)
        json.dumps(record, allow_nan=False)

        if submit_reports:
            record["metadata"]["report_submission"] = "enqueued"
            try:
                delivery_queue.put_nowait(record)
            except queue.Full:
                record["metadata"]["report_submission"] = "dropped_queue_full"
                LOGGER.error("client report delivery queue is full; dropping report for window %s", record["window_id"])

        return record

    def coordinator_loop() -> None:
        """Periodically check and finalize elapsed windows while workers run."""
        while not stop_event.is_set():
            time.sleep(min(0.2, window_s / 5.0))
            now_mono = time.monotonic()
            elapsed = now_mono - start_monotonic_s
            current_window_idx = int(elapsed // window_s)

            for w_idx in range(current_window_idx):
                if w_idx not in closed_windows:
                    w_start = start_monotonic_s + w_idx * window_s
                    w_end = w_start + window_s
                    with outcome_lock:
                        w_outcomes = [
                            o for o in outcomes
                            if w_start <= float(o["completed_at"]) < w_end
                        ]
                    record = _finalize_window(w_idx, w_outcomes, actual_duration=float(window_s))
                    with records_lock:
                        records.append(record)
                        closed_windows.add(w_idx)

    def worker_loop() -> None:
        while time.monotonic() < generation_deadline_s:
            result = _attempt_request(work_url, timeout_s, generation_deadline_s)
            if result is None:
                return
            success, completed_at, latency_ms, error_kind = result
            outcome = {
                "success": success,
                "completed_at": completed_at,
                "latency_ms": latency_ms if success else None,
                "error_kind": error_kind,
            }
            with outcome_lock:
                outcomes.append(outcome)
            if not success and error_kind not in {"http_400", "http_404", "http_500"}:
                time.sleep(0.005)

    coordinator_thread = threading.Thread(target=coordinator_loop, name="aura-coord", daemon=True)
    coordinator_thread.start()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="aura-bench") as executor:
        futures = [executor.submit(worker_loop) for _ in range(workers)]
        for future in futures:
            future.result()

    stop_event.set()
    coordinator_thread.join(timeout=2.0)

    # Finalize any remaining windows after all workers complete
    latest_terminal = max(
        (float(o["completed_at"]) for o in outcomes),
        default=generation_deadline_s,
    )
    effective_end = max(generation_deadline_s, latest_terminal)
    effective_elapsed = max(float(duration_s), effective_end - start_monotonic_s)
    total_windows = max(1, math.ceil(effective_elapsed / window_s - 1e-12))

    for w_idx in range(total_windows):
        if w_idx not in closed_windows:
            offset = w_idx * window_s
            actual_dur = min(float(window_s), max(1e-6, effective_elapsed - offset))
            w_start = start_monotonic_s + offset
            w_end = w_start + actual_dur
            with outcome_lock:
                w_outcomes = [
                    o for o in outcomes
                    if (w_start <= float(o["completed_at"]) < w_end) or (w_idx == total_windows - 1 and float(o["completed_at"]) >= w_start)
                ]
            record = _finalize_window(w_idx, w_outcomes, actual_duration=actual_dur)
            with records_lock:
                records.append(record)
                closed_windows.add(w_idx)

    # Wait for all enqueued reports to be delivered
    if submit_reports and delivery_worker_thread is not None:
        delivery_queue.put(None)
        delivery_worker_thread.join(timeout=5.0)

    records.sort(key=lambda r: int(r["window_id"]))

    if latency_samples_path is not None:
        write_latency_samples(
            outcomes,
            latency_samples_path,
            start_monotonic_s=start_monotonic_s,
            generation_deadline_s=generation_deadline_s,
            duration_s=duration_s,
            window_s=window_s,
            run_id=run_id,
            scenario=scenario.strip(),
        )

    return records


def write_jsonl(records: Iterable[Mapping[str, Any]], path: str | Path) -> Path:
    """Write one strict JSON ingress record per line."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            _ingress_validator().validate(dict(record))
            _validate_semantics(record)
            handle.write(json.dumps(record, allow_nan=False, separators=(",", ":")) + "\n")
    return output


def write_csv(records: Sequence[Mapping[str, Any]], path: str | Path) -> Path:
    """Write window records to a tidy CSV; metadata is JSON-encoded in one column."""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for record in records:
            _ingress_validator().validate(dict(record))
            _validate_semantics(record)
            row = dict(record)
            row["metadata"] = json.dumps(row.get("metadata", {}), allow_nan=False, sort_keys=True)
            writer.writerow(row)
    return output


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the AURA HTTP workload benchmark.")
    parser.add_argument("--url", default=os.environ.get("AURA_URL", "http://127.0.0.1:8080/work"), help="Server base URL or /work URL")
    parser.add_argument("--duration", type=int, default=int(os.environ.get("AURA_DURATION_S", "60")), help="Request-generation duration in seconds")
    parser.add_argument("--workers", type=int, default=int(os.environ.get("AURA_WORKERS", "4")), help="Concurrent worker threads")
    parser.add_argument("--window", type=int, default=int(os.environ.get("AURA_WINDOW_S", "10")), help="Measurement window size in seconds")
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("AURA_TIMEOUT_S", "2")), help="Per-request timeout in seconds")
    parser.add_argument("--work-iters", type=int, default=int(os.environ.get("AURA_WORK_ITERS", "1000")), help="Iterations for server /work")
    parser.add_argument("--scenario", default=os.environ.get("AURA_SCENARIO", "baseline"), help="Scenario label")
    parser.add_argument("--run-id", default=None, help="Optional run identifier")
    parser.add_argument("--jsonl", default="results/raw/client_window.jsonl", help="JSONL output path")
    parser.add_argument("--csv", default="results/raw/client_windows.csv", help="CSV output path")
    parser.add_argument("--latency-samples", default="results/raw/latency_samples.csv", help="Raw successful latency sample sidecar CSV")
    parser.add_argument("--submit-reports", action="store_true", help="POST completed ingress windows to server /report")
    parser.add_argument("--report-url", default=None, help="Override derived /report URL")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress summary")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        run_id = args.run_id or f"aura-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        records = run_benchmark(
            args.url,
            args.duration,
            args.workers,
            args.window,
            timeout_s=args.timeout,
            work_iters=args.work_iters,
            scenario=args.scenario,
            run_id=run_id,
            submit_reports=args.submit_reports,
            report_url=args.report_url,
            metadata={"client_cli": "aura.client"},
            latency_samples_path=args.latency_samples,
        )
        jsonl_path = write_jsonl(records, args.jsonl)
        csv_path = write_csv(records, args.csv)
        requests = sum(record["requests"] for record in records)
        successes = sum(record["successes"] for record in records)
        errors = sum(record["errors"] for record in records)
        duration = sum(record["window_duration_s"] for record in records)
        throughput = successes / duration if duration > 0 else 0.0
        if not args.quiet:
            print(f"run_id: {run_id}")
            print(f"scenario: {args.scenario}")
            print(f"windows: {len(records)}")
            print(f"requests: {requests} (successes={successes}, errors={errors})")
            print(f"throughput_rps: {throughput:.6f}")
            print(f"jsonl: {jsonl_path}")
            print(f"csv: {csv_path}")
            print(f"latency_samples: {args.latency_samples}")
        return 0
    except (ValueError, OSError, jsonschema.ValidationError) as exc:
        LOGGER.error("benchmark failed: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
