"""AURA baseline HTTP server.

Implements Gate B endpoints:
- GET /health: lightweight liveness check returning {"status": "ok"}.
- GET /work?n=<iters>: deterministic CPU-bound computation workload.
- POST /report: 4-stage validated benchmark measurement window ingestion pipeline
  with server monotonic receipt timestamping, Linux boot identity continuity,
  and thread-safe JSONL persistence.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from pathlib import Path
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from typing import Any, Callable
from urllib.parse import urlparse, parse_qs

import jsonschema
from jsonschema import Draft202012Validator

from aura.contracts import validate_client_window_invariants

logger = logging.getLogger("aura.server")

# Default configuration parameters
DEFAULT_MAX_WORK_ITERS = 10_000_000
DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576  # 1 MB
DEFAULT_REPORT_FILE = "results/raw/client_window_ingested.jsonl"


def get_system_boot_id() -> str:
    """Read host Linux boot ID from /proc/sys/kernel/random/boot_id.

    Raises:
        RuntimeError: If /proc/sys/kernel/random/boot_id is inaccessible or empty.
    """
    proc_boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    if proc_boot_id_path.exists():
        try:
            boot_id = proc_boot_id_path.read_text(encoding="utf-8").strip()
            if boot_id:
                return boot_id
        except Exception as exc:
            raise RuntimeError(f"Failed to read Linux boot ID from {proc_boot_id_path}: {exc}") from exc

    raise RuntimeError("Linux host boot ID (/proc/sys/kernel/random/boot_id) is unavailable.")


def deterministic_cpu_work(iterations: int) -> dict[str, Any]:
    """Execute a deterministic, CPU-bound calculation without sleeping.

    Performs iterated SHA-256 rounds seeded by iteration count.
    Each iteration performs genuine mathematical/cryptographic operations.

    Args:
        iterations: Positive integer iteration count.

    Returns:
        dict containing iteration count, final hex digest checksum, and elapsed seconds.
    """
    start_mono = time.monotonic()
    state = hashlib.sha256(str(iterations).encode("utf-8")).digest()
    for _ in range(iterations):
        state = hashlib.sha256(state).digest()

    elapsed = time.monotonic() - start_mono
    return {
        "status": "completed",
        "iterations": iterations,
        "checksum": state.hex(),
        "elapsed_s": elapsed,
    }


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Threaded HTTP server handling concurrent requests without blocking."""
    daemon_threads = True
    allow_reuse_address = True


class AuraRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for AURA server endpoints."""

    server_version = "AURAServer/1.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Route internal server messages through standard logging."""
        logger.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def _send_json_response(self, status_code: int, data: dict[str, Any], close_connection: bool = False) -> None:
        """Serialize and send a JSON response with proper headers."""
        try:
            payload = json.dumps(data, allow_nan=False).encode("utf-8")
        except Exception:
            self.send_response(500)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", "25")
            self.end_headers()
            self.wfile.write(b'{"error": "server_error"}')
            return

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _send_error_json(self, status_code: int, error_code: str, message: str, close_connection: bool = False) -> None:
        """Send a structured JSON error response."""
        self._send_json_response(status_code, {
            "error": error_code,
            "message": message,
        }, close_connection=close_connection)

    def do_GET(self) -> None:
        """Handle GET endpoints: /health and /work."""
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._handle_health()
        elif path == "/work":
            self._handle_work(parsed.query)
        else:
            self._send_error_json(404, "not_found", f"Endpoint '{path}' not found.")

    def do_POST(self) -> None:
        """Handle POST endpoints: /report."""
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/report":
            self._handle_report()
        else:
            self._send_error_json(404, "not_found", f"Endpoint '{path}' not found.")

    def _handle_health(self) -> None:
        """Handle GET /health: fast liveness check."""
        self._send_json_response(200, {"status": "ok"})

    def _handle_work(self, query_string: str) -> None:
        """Handle GET /work?n=<iters>: deterministic CPU work."""
        params = parse_qs(query_string, keep_blank_values=True)
        if "n" not in params:
            self._send_error_json(400, "invalid_parameter", "Missing required query parameter 'n'.")
            return

        n_values = params["n"]
        if not n_values or len(n_values) != 1:
            self._send_error_json(400, "invalid_parameter", "Exactly one 'n' query parameter is required.")
            return

        n_str = n_values[0]
        try:
            iters = int(n_str)
        except (ValueError, TypeError):
            self._send_error_json(400, "invalid_parameter", f"Parameter 'n' must be an integer, got '{n_str}'.")
            return

        if iters < 0:
            self._send_error_json(400, "invalid_parameter", "Parameter 'n' cannot be negative.")
            return

        server = self.server  # type: ignore[attr-defined]
        max_iters = getattr(server, "max_work_iters", DEFAULT_MAX_WORK_ITERS)
        if iters > max_iters:
            self._send_error_json(
                400,
                "workload_too_large",
                f"Requested iterations {iters} exceeds server maximum {max_iters}."
            )
            return

        result = deterministic_cpu_work(iters)
        self._send_json_response(200, result)

    def _handle_report(self) -> None:
        """Handle POST /report: 4-stage validated benchmark window ingestion."""
        content_length_str = self.headers.get("Content-Length")
        if content_length_str is None or not content_length_str.strip():
            self._send_error_json(400, "missing_content_length", "Content-Length header is required.")
            return

        try:
            content_length = int(content_length_str.strip())
        except (ValueError, TypeError):
            self._send_error_json(400, "invalid_content_length", "Content-Length header must be a valid integer.")
            return

        if content_length < 0:
            self._send_error_json(400, "invalid_content_length", "Content-Length cannot be negative.")
            return

        server = self.server  # type: ignore[attr-defined]
        max_bytes = getattr(server, "max_body_bytes", DEFAULT_MAX_REQUEST_BODY_BYTES)
        if content_length > max_bytes:
            # Drain body if reasonable so the client can receive the HTTP 413 response cleanly
            try:
                drain_len = min(content_length, 1024 * 1024)
                self.rfile.read(drain_len)
            except Exception:
                pass
            self.close_connection = True
            self._send_error_json(
                413,
                "payload_too_large",
                f"Payload size {content_length} bytes exceeds maximum permitted {max_bytes} bytes.",
                close_connection=True,
            )
            return

        if content_length == 0:
            self._send_error_json(400, "empty_payload", "Request body cannot be empty.")
            return

        try:
            raw_body = self.rfile.read(content_length)
        except Exception as exc:
            self._send_error_json(400, "read_error", f"Failed to read request body: {exc}")
            return

        if len(raw_body) != content_length:
            self._send_error_json(
                400,
                "incomplete_read",
                f"Expected {content_length} bytes, but only received {len(raw_body)} bytes."
            )
            return

        try:
            body_text = raw_body.decode("utf-8")
        except UnicodeDecodeError:
            self._send_error_json(400, "invalid_encoding", "Request body must be valid UTF-8.")
            return

        # Reject JSON constants like NaN, Infinity, -Infinity
        def _reject_constant(val: str) -> None:
            raise ValueError(f"Forbidden constant '{val}' in JSON payload")

        try:
            ingress_payload = json.loads(body_text, parse_constant=_reject_constant)
        except Exception as exc:
            self._send_error_json(400, "invalid_json", f"JSON parsing failed: {exc}")
            return

        if not isinstance(ingress_payload, dict):
            self._send_error_json(400, "invalid_payload", "Root JSON payload must be an object.")
            return

        # Stage 1: Ingress Schema Validation
        ingress_validator = getattr(server, "ingress_validator", None)
        if ingress_validator is None:
            self._send_error_json(500, "server_configuration_error", "Ingress validator not initialized.")
            return

        errors = list(ingress_validator.iter_errors(ingress_payload))
        if errors:
            first_err = errors[0]
            self._send_error_json(
                400,
                "schema_validation_failed",
                f"Stage 1 Ingress Schema Violation: {first_err.message} at path '{list(first_err.absolute_path)}'"
            )
            return

        # Stage 2: Cross-field Arithmetic and Semantic Validation
        try:
            validate_client_window_invariants(
                requests=ingress_payload["requests"],
                successes=ingress_payload["successes"],
                errors=ingress_payload["errors"],
                sample_count=ingress_payload["sample_count"],
                window_duration_s=ingress_payload["window_duration_s"],
                throughput_rps=ingress_payload["throughput_rps"],
            )
        except (TypeError, ValueError) as exc:
            self._send_error_json(400, "semantic_invariant_violation", f"Stage 2 Semantic Invariant Violation: {exc}")
            return

        # Deduplication & Idempotency Check
        run_id = str(ingress_payload["run_id"])
        window_id = int(ingress_payload["window_id"])
        window_key = (run_id, window_id)

        dedup_cache = getattr(server, "ingestion_dedup_cache", None)
        dedup_lock = getattr(server, "dedup_lock", None)
        if dedup_cache is not None and dedup_lock is not None:
            with dedup_lock:
                existing_payload = dedup_cache.get(window_key)
                if existing_payload is not None:
                    # Compare content (excluding server receipt fields)
                    if existing_payload == ingress_payload:
                        # Idempotent response for identical duplicate
                        self._send_json_response(200, {
                            "status": "accepted_duplicate_idempotent",
                            "run_id": run_id,
                            "window_id": window_id,
                        })
                        return
                    else:
                        # Conflicting payload with existing identity
                        self._send_error_json(
                            409,
                            "conflict_duplicate_window",
                            f"Window {window_key} has already been ingested with conflicting payload."
                        )
                        return

        # Stage 3: Server-Owned Receipt Timestamping & Boot Identity
        time_provider: Callable[[], float] = getattr(server, "epoch_provider", time.time)
        mono_provider: Callable[[], float] = getattr(server, "monotonic_provider", time.monotonic)
        boot_id_provider: Callable[[], str] = getattr(server, "boot_id_provider", get_system_boot_id)

        try:
            server_epoch_s = float(time_provider())
            server_monotonic_s = float(mono_provider())
            server_boot_id = str(boot_id_provider())
        except Exception as exc:
            logger.error("Failed to acquire server receipt identity metadata: %s", exc)
            self._send_error_json(500, "receipt_metadata_error", f"Server metadata provider error: {exc}")
            return

        enriched_record = dict(ingress_payload)
        enriched_record["server_received_at_epoch_s"] = server_epoch_s
        enriched_record["server_received_at_monotonic_s"] = server_monotonic_s
        enriched_record["server_boot_id"] = server_boot_id

        # Stage 4: Enriched Schema Validation
        ingested_validator = getattr(server, "ingested_validator", None)
        if ingested_validator is None:
            self._send_error_json(500, "server_configuration_error", "Ingested validator not initialized.")
            return

        ingested_errors = list(ingested_validator.iter_errors(enriched_record))
        if ingested_errors:
            first_err = ingested_errors[0]
            self._send_error_json(
                500,
                "enriched_validation_failed",
                f"Stage 4 Enriched Schema Violation: {first_err.message}"
            )
            return

        # Persistence: thread-safe append to JSONL storage
        storage = getattr(server, "storage", None)
        if storage is not None:
            try:
                storage.persist_record(enriched_record)
            except Exception as exc:
                logger.error("Failed to persist enriched report: %s", exc)
                self._send_error_json(500, "storage_error", "Failed to persist enriched measurement record.")
                return

        # Record in dedup cache
        if dedup_cache is not None and dedup_lock is not None:
            with dedup_lock:
                dedup_cache[window_key] = dict(ingress_payload)

        # Notify optional runtime callback or handoff queue
        on_report_ingested = getattr(server, "on_report_ingested", None)
        if on_report_ingested is not None:
            try:
                on_report_ingested(enriched_record)
            except Exception as exc:
                logger.warning("on_report_ingested callback encountered error: %s", exc)

        self._send_json_response(200, {
            "status": "accepted",
            "run_id": enriched_record["run_id"],
            "window_id": enriched_record["window_id"],
            "server_received_at_monotonic_s": server_monotonic_s,
            "server_boot_id": server_boot_id,
        })


class JsonlRecordStorage:
    """Thread-safe persistent JSON Lines storage for validated records."""

    def __init__(self, file_path: str | Path) -> None:
        self.file_path = Path(file_path).resolve()
        self._lock = threading.Lock()
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def persist_record(self, record: dict[str, Any]) -> None:
        """Serialize and atomically append one record to the JSONL file."""
        serialized = json.dumps(record, allow_nan=False) + "\n"
        with self._lock:
            with self.file_path.open("a", encoding="utf-8") as f:
                f.write(serialized)
                f.flush()


class AuraServer:
    """Manager for the AURA HTTP server instance."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        contracts_dir: str | Path | None = None,
        output_file: str | Path = DEFAULT_REPORT_FILE,
        max_work_iters: int = DEFAULT_MAX_WORK_ITERS,
        max_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
        epoch_provider: Callable[[], float] | None = None,
        monotonic_provider: Callable[[], float] | None = None,
        boot_id_provider: Callable[[], str] | None = None,
        on_report_ingested: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.max_work_iters = max_work_iters
        self.max_body_bytes = max_body_bytes
        self.on_report_ingested = on_report_ingested
        self.output_file = Path(output_file).resolve()
        self.ingestion_dedup_cache: dict[tuple[str, int], dict[str, Any]] = {}
        self.dedup_lock = threading.Lock()
        self._load_existing_dedup_cache()

        # Resolve schemas directory
        if contracts_dir is None:
            contracts_dir = Path(__file__).resolve().parent.parent / "contracts"
        self.contracts_dir = Path(contracts_dir).resolve()

        # Pre-compile JSON Schema validators once for maximum performance
        ingress_schema_path = self.contracts_dir / "client_window.schema.json"
        ingested_schema_path = self.contracts_dir / "client_window_ingested.schema.json"

        with ingress_schema_path.open("r", encoding="utf-8") as f:
            ingress_schema = json.load(f)
        Draft202012Validator.check_schema(ingress_schema)
        self.ingress_validator = Draft202012Validator(ingress_schema)

        with ingested_schema_path.open("r", encoding="utf-8") as f:
            ingested_schema = json.load(f)
        Draft202012Validator.check_schema(ingested_schema)
        self.ingested_validator = Draft202012Validator(ingested_schema)

        # Storage
        self.storage = JsonlRecordStorage(output_file)

        # Metadata providers (injectable for testing)
        self.epoch_provider = epoch_provider or time.time
        self.monotonic_provider = monotonic_provider or time.monotonic
        self.boot_id_provider = boot_id_provider or get_system_boot_id

        # Server instance
        self.http_server = ThreadedHTTPServer((self.host, self.port), AuraRequestHandler)
        # Bind attributes to the server for access in request handlers
        self.http_server.max_work_iters = self.max_work_iters  # type: ignore[attr-defined]
        self.http_server.max_body_bytes = self.max_body_bytes  # type: ignore[attr-defined]
        self.http_server.ingress_validator = self.ingress_validator  # type: ignore[attr-defined]
        self.http_server.ingested_validator = self.ingested_validator  # type: ignore[attr-defined]
        self.http_server.storage = self.storage  # type: ignore[attr-defined]
        self.http_server.epoch_provider = self.epoch_provider  # type: ignore[attr-defined]
        self.http_server.monotonic_provider = self.monotonic_provider  # type: ignore[attr-defined]
        self.http_server.boot_id_provider = self.boot_id_provider  # type: ignore[attr-defined]
        self.http_server.on_report_ingested = self.on_report_ingested  # type: ignore[attr-defined]
        self.http_server.ingestion_dedup_cache = self.ingestion_dedup_cache  # type: ignore[attr-defined]
        self.http_server.dedup_lock = self.dedup_lock  # type: ignore[attr-defined]

        self._thread: threading.Thread | None = None

    def _load_existing_dedup_cache(self) -> None:
        """Populate ingestion dedup cache from pre-existing records in output_file."""
        if not self.output_file.exists():
            return
        try:
            with self.output_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                        run_id = rec.get("run_id")
                        window_id = rec.get("window_id")
                        if run_id is not None and window_id is not None:
                            clean_payload = {
                                k: v
                                for k, v in rec.items()
                                if k
                                not in {
                                    "server_received_at_epoch_s",
                                    "server_received_at_monotonic_s",
                                    "server_boot_id",
                                }
                            }
                            self.ingestion_dedup_cache[(str(run_id), int(window_id))] = clean_payload
                    except Exception:
                        continue
        except Exception as exc:
            logger.warning("Failed to recover dedup cache from %s: %s", self.output_file, exc)

    def start(self, background: bool = False) -> None:
        """Start the HTTP server."""
        actual_port = self.http_server.server_address[1]
        logger.info("AURA HTTP Server listening on %s:%d", self.host, actual_port)
        if background:
            self._thread = threading.Thread(target=self.http_server.serve_forever, daemon=True)
            self._thread.start()
        else:
            self.http_server.serve_forever()

    def shutdown(self) -> None:
        """Stop the HTTP server cleanly."""
        logger.info("Shutting down AURA HTTP server...")
        self.http_server.shutdown()
        self.http_server.server_close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("AURA HTTP server stopped.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    port = int(os.environ.get("AURA_PORT", "8080"))
    host = os.environ.get("AURA_HOST", "0.0.0.0")
    server = AuraServer(host=host, port=port)
    try:
        server.start()
    except KeyboardInterrupt:
        server.shutdown()
