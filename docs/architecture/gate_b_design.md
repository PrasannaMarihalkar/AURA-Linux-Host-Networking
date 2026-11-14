# AURA Gate B Architecture and Design Specification

## Overview
This document specifies the architecture, data flow, component interfaces, and safety guarantees of **AURA Gate B — Baseline HTTP Server and Observe-Only Controller**.

---

## 1. Component Architecture & End-to-End Data Pipeline

```text
[Akash: Remote Benchmark Client (aura/client.py)]
        |
        | 1. HTTP Workload (GET /work?n=<iters>)
        | 2. Raw Measurement Window (POST /report)
        v
[AURA HTTP Server (aura/server.py)]
        |
        +---> Stage 1: Ingress Schema Validation (contracts/client_window.schema.json)
        |     - Validates payload structure, types, and ranges.
        |     - Strictly rejects client-injected server fields (server_received_at_*, server_boot_id).
        |
        +---> Stage 2: Semantic & Invariant Validation (aura/contracts.py)
        |     - requests == successes + errors
        |     - sample_count <= successes
        |     - Strictly positive finite window_duration_s
        |     - throughput_rps matches successes / window_duration_s
        |     - Strict integer counts (rejecting boolean values)
        |
        +---> Stage 3: Server-Owned Receipt Timestamping & Identity
        |     - server_received_at_epoch_s = time.time()
        |     - server_received_at_monotonic_s = time.monotonic()
        |     - server_boot_id = /proc/sys/kernel/random/boot_id
        |
        +---> Stage 4: Enriched Schema Validation (contracts/client_window_ingested.schema.json)
        |
        v Atomic JSONL Append
[Persistence: results/raw/client_window_ingested.jsonl]
        |
        | (Enriched Window)
        v
[AURA Controller (aura/controller.py)] <--- [Shridhar: Host Telemetry (aura/telemetry.py)]
        |                                   (Host Snapshot: CPU PSI, NET_RX, Softnet)
        |
        +---> Freshness & Continuity Check (check_monotonic_freshness)
        |     - server_monotonic_now - report_monotonic_s <= max_report_age_s
        |     - current_server_boot_id == report_server_boot_id
        |
        +---> Candidate Diagnosis Engine (aura/diagnosis.py)
        |     - cpu_contention_candidate: P99 degraded + CPU PSI elevated
        |     - rx_activity_skew_candidate: NET_RX concentrated on single core (>=75%) or softnet drops
        |     - interrupt_pressure_candidate: NAPI budget time-squeeze events elevated
        |     - normal: all measured signals within calibrated healthy thresholds
        |     - uncertain: missing, stale, or conflicting evidence
        |
        +---> Observe-Only Decision & Operational Guardrails (validate_action_guardrails)
        |     - Controller mode strictly set to 'observe_only'
        |     - Action type set to 'no_op'
        |     - Action status set to 'not_attempted' or 'dry_run'
        |     - 'applied' status is forbidden and unachievable in Gate B
        |
        v Atomic JSONL Append
[Persistence: results/raw/controller_event.jsonl]
```

---

## 2. Server Endpoints & Ingestion Pipeline

### 2.1 GET /health
* **Status:** 200 OK
* **Response:** `{"status": "ok"}`
* **Headers:** `Content-Type: application/json; charset=utf-8`

### 2.2 GET /work?n=<iters>
* **Query Parameter:** `n` (positive integer, up to configured `max_work_iters`, default `10,000,000`).
* **Computation:** Iterative SHA-256 state updates seeded by `n`. Genuinely consumes CPU cycles without artificial sleeping.
* **Response:**
  ```json
  {
    "status": "completed",
    "iterations": 1000,
    "checksum": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "elapsed_s": 0.00123
  }
  ```

### 2.3 POST /report
* **Input:** Raw JSON matching `contracts/client_window.schema.json`.
* **Output:**
  ```json
  {
    "status": "accepted",
    "run_id": "exp-contention-001",
    "window_id": 1,
    "server_received_at_monotonic_s": 1245.678,
    "server_boot_id": "b7a8d50f-e25c-4861-9c12-32b78912e541"
  }
  ```

---

## 3. Observe-Only Controller Safety Guarantees

1. **Strictly Non-Privileged:** The controller performs zero cgroup adjustments, zero sysfs writes, zero `nice`/`renice` operations, and zero network configuration changes.
2. **Schema Invariant:** `contracts/controller_event.schema.json` and `contracts/action_record.schema.json` programmatically forbid `action.status = applied` when `controller_mode == "observe_only"`.
3. **Conservative Fail-Safe:** Any missing data, stale snapshot, boot ID discontinuity, or conflicting signal forces a safe `uncertain` diagnosis with `no_op` action.
