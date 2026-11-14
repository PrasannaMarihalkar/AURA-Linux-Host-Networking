"""AURA Observe-Only Controller.

Periodically consumes:
1. Validated, server-enriched client measurement windows.
2. Valid host telemetry snapshots.

Invokes the diagnosis engine (aura/diagnosis.py) and emits schema-compliant
controller_event.jsonl records adhering to contracts/controller_event.schema.json.

Safety Guarantee:
The controller operates in strict observe-only mode.
It never performs privileged actions, never applies cgroup or sysfs modifications,
and never emits an action status of 'applied'.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

import jsonschema
from jsonschema import Draft202012Validator

from aura.contracts import (
    check_monotonic_freshness,
    validate_action_guardrails,
)
from aura.diagnosis import evaluate_candidate_diagnosis
from aura.server import get_system_boot_id

logger = logging.getLogger("aura.controller")

DEFAULT_CONTROLLER_EVENT_LOG = "results/raw/controller_event.jsonl"
DEFAULT_THRESHOLDS_PATH = "config/thresholds.json"


class AuraController:
    """AURA Observe-Only Controller Daemon."""

    def __init__(
        self,
        contracts_dir: str | Path | None = None,
        thresholds_path: str | Path = DEFAULT_THRESHOLDS_PATH,
        event_log_path: str | Path = DEFAULT_CONTROLLER_EVENT_LOG,
        server_boot_id: str | None = None,
        boot_id_provider: Callable[[], str] | None = None,
        epoch_provider: Callable[[], float] | None = None,
        monotonic_provider: Callable[[], float] | None = None,
        controller_mode: str = "observe_only",
    ) -> None:
        if controller_mode not in ("observe_only", "dry_run"):
            raise ValueError(f"Gate B controller only supports 'observe_only' or 'dry_run', got '{controller_mode}'")

        self.controller_mode = controller_mode
        self.event_log_path = Path(event_log_path).resolve()
        self.event_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

        # Load thresholds
        self.thresholds_path = Path(thresholds_path).resolve()
        with self.thresholds_path.open("r", encoding="utf-8") as f:
            self.thresholds = json.load(f)

        # Contracts directory & Schema compilation
        if contracts_dir is None:
            contracts_dir = Path(__file__).resolve().parent.parent / "contracts"
        self.contracts_dir = Path(contracts_dir).resolve()

        event_schema_path = self.contracts_dir / "controller_event.schema.json"
        with event_schema_path.open("r", encoding="utf-8") as f:
            event_schema = json.load(f)
        Draft202012Validator.check_schema(event_schema)
        self.event_validator = Draft202012Validator(event_schema)

        # Providers
        self.boot_id_provider = boot_id_provider or get_system_boot_id
        self._explicit_boot_id = server_boot_id
        self.epoch_provider = epoch_provider or time.time
        self.monotonic_provider = monotonic_provider or time.monotonic

        # Latest states
        self._latest_client_window: dict[str, Any] | None = None
        self._latest_host_snapshot: dict[str, Any] | None = None
        self._running = False
        self._loop_thread: threading.Thread | None = None

    def get_server_boot_id(self) -> str:
        """Get active server boot identity."""
        if self._explicit_boot_id is not None:
            return self._explicit_boot_id
        return self.boot_id_provider()

    def update_client_window(self, window: dict[str, Any]) -> None:
        """Register the latest validated and server-enriched client measurement window."""
        with self._lock:
            self._latest_client_window = dict(window)

    def update_host_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Register the latest host telemetry snapshot."""
        with self._lock:
            self._latest_host_snapshot = dict(snapshot)

    def evaluate_cycle(self) -> dict[str, Any]:
        """Execute a single observation and decision cycle.

        Evaluates available evidence, checks freshness and boot identity continuity,
        produces an observe-only action recommendation (no_op), and appends to controller_event.jsonl.

        Returns:
            The generated schema-valid controller event record.
        """
        now_epoch = float(self.epoch_provider())
        now_mono = float(self.monotonic_provider())

        try:
            current_boot_id = self.get_server_boot_id()
        except Exception as exc:
            logger.error("Failed to acquire controller server boot ID: %s", exc)
            current_boot_id = "unknown-boot-id"

        with self._lock:
            client_window = dict(self._latest_client_window) if self._latest_client_window is not None else None
            host_snapshot = dict(self._latest_host_snapshot) if self._latest_host_snapshot is not None else None

        # Freshness checking
        max_age_s = float(self.thresholds.get("max_client_report_age_seconds", 15.0))
        is_stale = False
        report_age_s: float | None = None
        boot_id_mismatch = False

        if client_window is not None:
            report_mono = client_window.get("server_received_at_monotonic_s")
            report_boot_id = client_window.get("server_boot_id", "")
            if report_mono is not None:
                is_stale, report_age_s, reason = check_monotonic_freshness(
                    server_monotonic_now=now_mono,
                    report_monotonic_s=report_mono,
                    current_server_boot_id=current_boot_id,
                    report_server_boot_id=report_boot_id,
                    max_report_age_s=max_age_s,
                )
                if reason == "server_boot_id_mismatch":
                    boot_id_mismatch = True

        # Run diagnosis engine
        diag_result = evaluate_candidate_diagnosis(
            client_window=client_window,
            host_snapshot=host_snapshot,
            thresholds=self.thresholds,
            stale_telemetry=is_stale,
            boot_id_mismatch=boot_id_mismatch,
        )

        conflicting_evidence = diag_result["conflicting_evidence"]

        # Action formulation: Gate B is strictly observe-only
        # Action status MUST NOT be 'applied'
        action_type = "no_op"
        action_status = "dry_run" if self.controller_mode == "dry_run" else "not_attempted"

        diag_label = diag_result["label"]
        action_reason = (
            f"Candidate diagnosis '{diag_label}' evaluated under {self.controller_mode} mode; "
            "active system adjustments are inhibited."
        )

        # Baseline before/requested readback representation for observe-only logging
        cpu_weight_baseline = self.thresholds.get("cpu_weight_baseline", 100)
        cpu_weight_step = self.thresholds.get("cpu_weight_step", 200)

        before_state = {"aura-app.slice": cpu_weight_baseline, "aura-bg.slice": cpu_weight_baseline}
        requested_state = {"aura-app.slice": cpu_weight_step} if diag_label == "cpu_contention_candidate" else None
        after_state = before_state  # in observe-only, no change is made

        action_record = {
            "type": action_type,
            "status": action_status,
            "reason": action_reason,
            "before": before_state,
            "requested": requested_state,
            "after": after_state,
            "rollback_attempted": None,
            "rollback_verified": None,
            "error": None,
        }

        # Operational safety invariant verification
        validate_action_guardrails(
            controller_mode=self.controller_mode,
            action_status=action_status,
            before=before_state,
            requested=requested_state,
            after=after_state,
        )

        # Run ID and Window ID resolution
        run_id = client_window.get("run_id") if client_window else (host_snapshot.get("host_id", "unspecified-run") if host_snapshot else "no-run")
        window_id = client_window.get("window_id", 0) if client_window else 0

        # Optional summary telemetry fields
        p99_ms = client_window.get("p99_ms") if client_window else None
        throughput_rps = client_window.get("throughput_rps") if client_window else None
        cpu_psi_avg10 = host_snapshot.get("cpu_psi_some_avg10_pct") if host_snapshot else None

        event_record = {
            "schema_version": "1.2",
            "timestamp_epoch_s": now_epoch,
            "server_boot_id": current_boot_id,
            "run_id": str(run_id),
            "window_id": int(window_id),
            "report_age_s": report_age_s,
            "stale_telemetry": is_stale,
            "conflicting_evidence": conflicting_evidence,
            "controller_mode": self.controller_mode,
            "diagnosis": {
                "label": diag_result["label"],
                "confidence": diag_result["confidence"],
                "evidence": diag_result["evidence"],
                "limitations": diag_result["limitations"],
            },
            "action": action_record,
            "p99_ms": p99_ms,
            "throughput_rps": throughput_rps,
            "cpu_psi_some_avg10_pct": cpu_psi_avg10,
        }

        # Schema Validation
        self.event_validator.validate(event_record)

        # Persistence to controller_event.jsonl
        serialized = json.dumps(event_record, allow_nan=False) + "\n"
        with self._lock:
            with self.event_log_path.open("a", encoding="utf-8") as f:
                f.write(serialized)
                f.flush()

        return event_record

    def start_loop(self, interval_seconds: float | None = None) -> None:
        """Start periodic evaluation loop in a background thread."""
        if interval_seconds is None:
            interval_seconds = float(self.thresholds.get("controller_interval_seconds", 1.0))

        self._running = True

        def _loop() -> None:
            logger.info("AURA Controller loop started (interval=%.2fs)", interval_seconds)
            while self._running:
                try:
                    self.evaluate_cycle()
                except Exception as exc:
                    logger.error("Controller cycle encountered error: %s", exc, exc_info=True)
                time.sleep(interval_seconds)
            logger.info("AURA Controller loop finished.")

        self._loop_thread = threading.Thread(target=_loop, daemon=True)
        self._loop_thread.start()

    def stop_loop(self) -> None:
        """Stop periodic evaluation loop cleanly."""
        logger.info("Stopping AURA Controller loop...")
        self._running = False
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop_thread.join(timeout=3.0)
        logger.info("AURA Controller loop stopped.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    controller = AuraController()
    try:
        controller.start_loop()
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        controller.stop_loop()
