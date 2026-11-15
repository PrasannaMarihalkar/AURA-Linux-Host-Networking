"""AURA System Runtime Orchestrator.

Coordinates:
1. HTTP Server for workload generation (/work) and report ingestion (/report).
2. Telemetry Collector capturing host metrics at configured intervals.
3. Thread-safe queue delivering client measurement windows to the Observe-Only Controller.
4. Schema-valid persistence of client windows, host snapshots, and controller events.
5. Signal-safe, graceful start and shutdown.

Usage:
    python -m aura.runtime [--host 0.0.0.0] [--port 8080] [--interface eth0]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from aura.controller import AuraController
from aura.server import AuraServer, get_system_boot_id
from aura.telemetry import collect_snapshot

LOGGER = logging.getLogger("aura.runtime")

DEFAULT_CONFIG_PATH = Path("config/thresholds.json")
DEFAULT_RESULTS_DIR = Path("results/raw")


class AuraRuntime:
    """Integrated execution orchestrator for AURA research prototype."""

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        interface: str | None = None,
        config_path: str | Path = DEFAULT_CONFIG_PATH,
        results_dir: str | Path = DEFAULT_RESULTS_DIR,
        contracts_dir: str | Path | None = None,
        boot_id_provider: Callable[[], str] | None = None,
        epoch_provider: Callable[[], float] | None = None,
        monotonic_provider: Callable[[], float] | None = None,
        telemetry_sampler: Callable[[str | None], dict[str, Any]] | None = None,
        telemetry_interval_s: float = 1.0,
    ) -> None:
        self.host = host
        self.port = port
        self.interface = interface
        self.config_path = Path(config_path).resolve()
        self.results_dir = Path(results_dir).resolve()
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.telemetry_interval_s = max(0.1, float(telemetry_interval_s))

        # Providers
        self.boot_id_provider = boot_id_provider or get_system_boot_id
        self.epoch_provider = epoch_provider or time.time
        self.monotonic_provider = monotonic_provider or time.monotonic
        self.telemetry_sampler = telemetry_sampler or collect_snapshot

        # Establish server boot UUID
        try:
            self.boot_id = self.boot_id_provider()
        except Exception:
            self.boot_id = "aura-fallback-boot-id"

        # Paths
        self.client_log_path = self.results_dir / "client_window_ingested.jsonl"
        self.host_log_path = self.results_dir / "host_snapshot.jsonl"
        self.controller_log_path = self.results_dir / "controller_event.jsonl"

        # Inter-thread handoff queue for ingested client windows
        self.report_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1000)

        # Components
        self.server = AuraServer(
            host=self.host,
            port=self.port,
            contracts_dir=contracts_dir,
            output_file=self.client_log_path,
            boot_id_provider=lambda: self.boot_id,
            epoch_provider=self.epoch_provider,
            monotonic_provider=self.monotonic_provider,
            on_report_ingested=self._on_client_report_ingested,
        )

        self.controller = AuraController(
            contracts_dir=contracts_dir,
            thresholds_path=self.config_path,
            event_log_path=self.controller_log_path,
            server_boot_id=self.boot_id,
            boot_id_provider=lambda: self.boot_id,
            epoch_provider=self.epoch_provider,
            monotonic_provider=self.monotonic_provider,
            controller_mode="observe_only",
        )

        self._running = False
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.processed_count = 0

    def _on_client_report_ingested(self, record: dict[str, Any]) -> None:
        """Callback invoked by AuraServer upon successfully validating/persisting a client window."""
        try:
            self.report_queue.put_nowait(record)
        except queue.Full:
            LOGGER.error("Runtime report queue is full! Dropping report window %s", record.get("window_id"))

    def _telemetry_loop(self) -> None:
        """Periodic background thread collecting Linux host telemetry snapshots."""
        LOGGER.info("Telemetry sampling loop started (interface=%s, interval=%.2fs)", self.interface, self.telemetry_interval_s)
        while self._running:
            try:
                snapshot = self.telemetry_sampler(self.interface)
                if snapshot:
                    # Update controller's latest host telemetry
                    self.controller.update_host_snapshot(snapshot)
                    # Persist host snapshot to JSONL
                    serialized = json.dumps(snapshot, allow_nan=False) + "\n"
                    with self._lock:
                        with self.host_log_path.open("a", encoding="utf-8") as f:
                            f.write(serialized)
                            f.flush()
            except Exception as exc:
                LOGGER.warning("Telemetry collection error in loop: %s", exc)
            time.sleep(self.telemetry_interval_s)
        LOGGER.info("Telemetry sampling loop stopped.")

    def _controller_worker(self) -> None:
        """Worker consuming ingested client windows and running observe-only evaluations."""
        LOGGER.info("Controller evaluation worker started.")
        while self._running or not self.report_queue.empty():
            try:
                record = self.report_queue.get(timeout=0.2)
            except queue.Empty:
                continue

            try:
                # Update controller state with client window
                self.controller.update_client_window(record)
                # Run evaluate cycle
                event = self.controller.evaluate_cycle()
                if event:
                    with self._lock:
                        self.processed_count += 1
            except Exception as exc:
                LOGGER.error("Error evaluating controller cycle for report: %s", exc, exc_info=True)
            finally:
                self.report_queue.task_done()
        LOGGER.info("Controller evaluation worker finished.")

    def start(self, background: bool = False) -> None:
        """Start the complete AURA runtime."""
        self._running = True

        # 1. Start HTTP Server
        self.server.start(background=True)
        server_port = self.server.http_server.server_address[1]
        LOGGER.info("AURA Runtime started on http://%s:%d (boot_id=%s)", self.host, server_port, self.boot_id)

        # 2. Start Telemetry Thread
        t_telemetry = threading.Thread(target=self._telemetry_loop, name="aura-telemetry", daemon=True)
        t_telemetry.start()
        self._threads.append(t_telemetry)

        # 3. Start Controller Consumer Worker
        t_worker = threading.Thread(target=self._controller_worker, name="aura-controller-worker", daemon=True)
        t_worker.start()
        self._threads.append(t_worker)

        if not background:
            self._wait_for_shutdown()

    def _wait_for_shutdown(self) -> None:
        """Wait for termination signal (SIGINT/SIGTERM)."""
        stop_event = threading.Event()

        def _handler(sig, frame):
            LOGGER.info("Termination signal (%s) received.", sig)
            stop_event.set()

        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)

        while not stop_event.is_set():
            time.sleep(0.5)

        self.shutdown()

    def shutdown(self) -> None:
        """Gracefully stop all runtime components and flush pending queues."""
        LOGGER.info("Initiating graceful shutdown of AURA Runtime...")
        self._running = False

        # Stop HTTP server
        self.server.shutdown()

        # Wait for threads to finish
        for t in self._threads:
            if t.is_alive():
                t.join(timeout=2.0)

        LOGGER.info("AURA Runtime shutdown complete. Total windows processed: %d", self.processed_count)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AURA System Runtime Orchestrator")
    parser.add_argument("--host", default="0.0.0.0", help="HTTP server listen host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="HTTP server listen port (default: 8080)")
    parser.add_argument("--interface", default=None, help="Target network interface (e.g. eth0, enp0s3)")
    parser.add_argument("--config", default="config/thresholds.json", help="Path to thresholds.json")
    parser.add_argument("--results-dir", default="results/raw", help="Directory for output JSONL records")
    parser.add_argument("--telemetry-interval", type=float, default=1.0, help="Telemetry sample interval in seconds")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    )

    runtime = AuraRuntime(
        host=args.host,
        port=args.port,
        interface=args.interface,
        config_path=args.config,
        results_dir=args.results_dir,
        telemetry_interval_s=args.telemetry_interval,
    )

    try:
        runtime.start(background=False)
        return 0
    except KeyboardInterrupt:
        runtime.shutdown()
        return 0
    except Exception as exc:
        LOGGER.critical("Fatal runtime exception: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
