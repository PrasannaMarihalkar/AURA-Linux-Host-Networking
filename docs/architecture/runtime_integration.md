# AURA Runtime Integration and Lifecycle Architecture

## 1. Overview

The `AuraRuntime` orchestrator (`aura/runtime.py`) provides a unified, production-grade daemon coordinating all three core subsystems of the AURA Linux host networking research platform:
1. **Workload & Ingestion HTTP Server (`aura/server.py`)**: Exposes `/health`, deterministic computational endpoint `/work?n=<iters>`, and `/report` for streaming client measurement windows with 4-stage validation and idempotent deduplication.
2. **Host Telemetry Collector (`aura/telemetry.py`)**: Periodic non-invasive background sampling loop reading `/proc/pressure/cpu`, `/proc/net/softnet_stat`, `/proc/interrupts`, and `/proc/softirqs` without root privileges or host system mutation.
3. **Observe-Only Diagnosis & Controller Engine (`aura/controller.py` & `aura/diagnosis.py`)**: Event-driven evaluation consuming ingested measurement windows and host telemetry snapshots, checking monotonic freshness and boot identity continuity, and recording structured diagnostic hypotheses to `controller_event.jsonl`.

```text
+-----------------------------------------------------------------------------------+
|                                  AuraRuntime                                      |
|                                                                                   |
|  +--------------------+        +---------------------+        +----------------+  |
|  |     AuraServer     |        |   Telemetry Loop    |        | AuraController |  |
|  |                    |        |                     |        |                |  |
|  |  GET /health       |        | Periodic Sampling   |        | Observe-Only   |  |
|  |  GET /work         |        | (/proc non-invasive)|        | Diagnosis      |  |
|  |  POST /report      |        |   (0.1s - 1.0s)     |        | Guardrails     |  |
|  +---------+----------+        +----------+----------+        +--------+-------+  |
|            |                              |                            ^          |
|            | Validated Window             | Host Snapshot              |          |
|            v                              v                            |          |
|    +---------------+              +---------------+                    |          |
|    | Dedup & Cache |              | host_snapshot |--------------------+          |
|    +-------+-------+              |    .jsonl     |      Host Context             |
|            |                              +---------------+                       |
|            v                                                                      |
|    +---------------+                                                              |
|    | Report Queue  |--------------------------------------------------------------+
|    +-------+-------+                       Client Window Handoff                  |
|            |                                                                      |
|            v                                                                      |
|    +---------------+                                                              |
|    | client_window |                                                              |
|    | _ingested.    |                                                              |
|    | jsonl         |                                                              |
|    +---------------+                                                              |
+-----------------------------------------------------------------------------------+
```

---

## 2. Ingestion & Streaming Flow

### Real-Time Streaming Window Coordinator
Rather than accumulating client windows in memory and flushing them at benchmark completion, the benchmark client (`aura/client.py`) executes a dedicated background `coordinator_thread`. As each time window (default 1.0s) closes, the coordinator:
- Computes sample statistics and nearest-rank percentiles.
- Formulates schema-valid `client_window` ingress payload.
- Asynchronously posts to `/report` via HTTP POST.
- The server validates the payload, stamps server receipt timestamps (`server_received_at_epoch_s`, `server_received_at_monotonic_s`, `server_boot_id`), persists to `client_window_ingested.jsonl`, and enqueues the window for controller evaluation without blocking HTTP worker threads.

### Deduplication and Idempotent Replay Handling
In distributed and benchmark scenarios, network retries or replay attacks can resubmit identical measurement windows:
- The server maintains a bounded deduplication cache `ingestion_dedup_cache` keyed by `(run_id, window_id)`.
- **Identical Replay:** When an exact matching ingress payload is re-submitted, the server responds with HTTP 200 `{"status": "accepted_duplicate_idempotent"}`. No duplicate record is written to storage, and no duplicate evaluation is triggered.
- **Conflicting Replay:** When a payload with identical `(run_id, window_id)` but differing fields arrives, the server rejects it with HTTP 409 `conflict_duplicate_window`.
- **Restart Recovery:** Upon initialization or process restart, `AuraServer` pre-populates its cache from pre-existing records in `client_window_ingested.jsonl`, maintaining idempotency across process lifetimes.

---

## 3. Host Telemetry Collector Resilience

The telemetry engine (`aura/telemetry.py`) inspects Linux host state without write permissions or host reconfiguration:
1. **CPU Pressure (`/proc/pressure/cpu`):** Reads `some` pressure indicators (`avg10`, `avg60`, `avg300`, `total`). CPU `full` pressure is recognized as undefined at system level and safely ignored per Linux kernel documentation.
2. **Softnet Statistics (`/proc/net/softnet_stat`):** Formats vary across Linux kernel versions (5.x/6.x expose online CPUs or 12-column formats with explicit CPU ID; older kernels expose all possible CPUs). The parser dynamically detects column structures and maps entries safely without crash or misalignment.
3. **Multi-Queue Network IRQs (`/proc/interrupts`):** Multi-queue NICs allocate separate vectors per queue (`eth0-rx-0`, `eth0-TxRx-1`, `eth0-fp-2`). The collector accumulates all vectors associated with the target network interface rather than halting on the first prefix match.
4. **Counter Deltas & Rollover:** Monotonic counter deltas for softnet drops, squeeze, and interface packets explicitly guard against negative values or integer wrapping by flagging `"counter_reset"`.

---

## 4. Diagnosis Engine & Strict Observe-Only Guardrails

The diagnosis engine (`aura/diagnosis.py`) evaluates candidate root causes:
- **`cpu_contention`:** Corroborated when client P99 is elevated AND host CPU PSI exceeds calibrated thresholds (`cpu_psi_some_avg10_pct > 10.0`).
- **`rx_activity_skew`:** Evaluates Gini coefficient and max-to-median ratios of `net_rx_softirq_delta_per_cpu` across active CPUs. Softnet backlog drops are strictly decoupled from softIRQ execution skew.
- **`softnet_congestion`:** Triggered when cumulative softnet `dropped` or `time_squeeze` counts increase during the window.
- **`uncertain`:** Mandatory fallback when P99 sample sufficiency is not met (`p99_status == "insufficient"`), when conflicting signals occur, or when telemetry freshness is invalid.

### Operational Safety Guardrails
In Gate B, the controller is strictly observe-only:
- `action.type` is always `no_op`.
- `action.status` is `not_attempted` (or `dry_run`). It is NEVER `applied`.
- `before`, `requested`, and `after` readbacks remain `None`.
- Under no circumstances does the system write to `/proc/sys`, `/sys/class/net`, or cgroup controls.
