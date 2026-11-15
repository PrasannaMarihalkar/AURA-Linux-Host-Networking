# AURA — Complete Engineering Review and System Audit

**Date:** 2026-11-15  
**Auditor:** Principal Linux Systems & Reliability Engineer  
**Scope:** Full codebase audit covering frozen Gate A contracts, Gate B Baseline HTTP Server & Controller, Shridhar's Telemetry Collector (PR #8), Akash's Benchmark Client (PR #9), Analysis Pipeline, and End-to-End System Integration.

---

## 1. System Implementation Status

| Component | Module | GitHub Status | Operational State | Audit Assessment |
| :--- | :--- | :--- | :--- | :--- |
| **Contracts (Gate A)** | `aura/contracts.py`, `contracts/` | Merged (`main`) | Frozen (v1.2) | **Robust**. Full verification of arithmetic invariants, sample count sufficiency, and monotonic freshness. |
| **Server (Gate B)** | `aura/server.py` | Merged (`main`) | Operational | **Hardened**. 4-stage ingestion pipeline with Content-Length bounds and receipt stamping. Lacks server-side `(run_id, window_id)` deduplication and direct runtime handoff to controller. |
| **Controller (Gate B)** | `aura/controller.py` | Merged (`main`) | Observe-Only | **Partially Integrated**. Operates via periodic pull or explicit method injection. Re-evaluates identical window repeatedly if not updated. Observe-only invariants hold (`status != 'applied'`). |
| **Diagnosis Engine** | `aura/diagnosis.py` | Merged (`main`) | Heuristic | **Needs Refinement**. Conflates softnet backlog frame drops with NET_RX skew (`rx_activity_skew_candidate`). Missing essential vs optional telemetry distinction. |
| **Host Telemetry** | `aura/telemetry.py` | Merged (`main`, PR #8) | Read-only | **Defects Present**. (1) softnet stat parser assumes `len(lines) == len(possible_cpus)`, fails on non-contiguous or sparse online CPUs. (2) `read_network_irq()` only inspects first line match, misses multi-queue IRQ vectors. (3) Global `_BASELINE` lacks thread locks. (4) CPU PSI parser rejects valid zero-stalls if malformed. |
| **Benchmark Client** | `aura/client.py` | Submitted (PR #9) | Working Draft | **Defects Present**. (1) `submit_reports=True` postpones all report POSTs until *after* the entire benchmark ends, violating real-time freshness. (2) Terminal outcome definition captures elapsed time before body read completes, but contract requires full response consumption. (3) Lacks streaming window coordinator and thread-safe real-time window dispatch. |
| **Analysis & Plotting** | `analysis/` | Submitted (PR #9) | Working Draft | **Sound Design**. Appropriately separates per-window quantiles from raw sample distribution, but requires raw sidecar file. |
| **Runtime Orchestrator** | None | Not Implemented | Missing | **Critical Gap**. No `aura/runtime.py` to coordinate server, telemetry sampler, report queue, controller daemon, and graceful shutdown. |

---

## 2. Confirmed Defects and Technical Vulnerabilities

### Critical Findings

1. **[CRIT-01] Delayed Report Submission Breaks Real-Time Freshness (`aura/client.py`)**
   - *Impact:* In `run_benchmark()`, when `submit_reports=True`, window records are accumulated in memory and only submitted via `_post_window()` in a loop after all worker threads finish.
   - *Consequence:* In a 60-second run with 5-second windows, window 1 is submitted 60 seconds after closing. The server stamps receipt at $t=60\text{s}$, masking client-side generation delays or, conversely, triggering `stale_telemetry` in downstream controllers. Real-time controller feedback is completely impossible.
   - *Remedy:* Implement active window streaming with a thread-safe window-coordinator that closes, validates, and dispatches completed windows via worker threads or queue while workload generation continues.

2. **[CRIT-02] Broken Linux softnet CPU Identity Mapping (`aura/telemetry.py`)**
   - *Impact:* `_read_softnet()` zips `possible_cpus` directly with `lines` of `/proc/net/softnet_stat`. If `len(possible_cpus) != len(lines)`, it aborts with `None`.
   - *Consequence:* In Linux kernels where CPU cores are hotplugged, non-contiguous (e.g. CPU 0 and CPU 2 online, CPU 1 offline), or where the kernel reports online CPUs with an explicit hex CPU ID in field 11 (Linux 5.x/6.x), this parser fails or misattributes softnet drops to the wrong CPU ID.
   - *Remedy:* Implement format-aware parsing: inspect online CPUs, verify explicit trailing CPU ID column if present, and handle non-contiguous online cores cleanly without crashing or misaligning.

3. **[CRIT-03] Disconnected System Runtime (No Orchestrator)**
   - *Impact:* The server, telemetry sampler, and controller exist as separate unlinked classes. There is no orchestrator process linking server ingestion to the controller and feeding active host telemetry into evaluation cycles.
   - *Consequence:* An operator cannot launch AURA with `python -m aura.runtime`. Tests only verify components via synthetic manual mock injection.
   - *Remedy:* Build `aura/runtime.py` with thread-safe queue handoff between HTTP server ingestion and controller evaluation, background telemetry collection, idempotency deduplication, and graceful signal handling.

---

### High Severity Findings

4. **[HIGH-01] Latency Measured Before Complete Body Consumption (`aura/client.py`)**
   - *Impact:* `_attempt_request()` records `terminal = time.monotonic()` as soon as `response.getcode()` returns headers, then calls `response.read()` in a try/except block that discards socket errors.
   - *Consequence:* Truncated responses, connection dropouts during body transfer, or stalled chunks are falsely counted as successes with abnormally low latency, invalidating tail latency measurements.
   - *Remedy:* Measure latency up to the completion of `response.read()`. If `read()` fails or is truncated, record an error and exclude from the successful latency distribution.

5. **[HIGH-02] Duplicate Decision Formulation on Static Controller State (`aura/controller.py`)**
   - *Impact:* `AuraController.evaluate_cycle()` processes whatever is stored in `_latest_client_window`. If no new report arrives before the next evaluation tick, the controller evaluates the exact same window again, emitting duplicate controller events with identical `(run_id, window_id)`.
   - *Consequence:* Pollutes event logs, triggers duplicate downstream actions/alerts, and skews timeline analysis.
   - *Remedy:* Maintain a processed window tracking set or consume from a FIFO queue, ensuring each `(run_id, window_id)` is evaluated exactly once.

6. **[HIGH-03] Multi-Queue Network IRQ Under-Attribution (`aura/telemetry.py`)**
   - *Impact:* `read_network_irq()` returns upon encountering the *first* line in `/proc/interrupts` matching the interface label.
   - *Consequence:* On modern multi-queue NICs with IRQ vectors like `eth0-rx-0`, `eth0-rx-1`, `eth0-tx-0`, only the first queue vector is counted. The remaining queue interrupts are omitted, reporting a fraction of true network interrupt load.
   - *Remedy:* Accumulate counts across all matching queue vectors (`<iface>`, `<iface>-rx-*`, `<iface>-tx-*`, `<iface>-*`) and track the set of IRQ numbers.

7. **[HIGH-04] Diagnosis Conflates Softnet Backlog Drops with RX Activity Skew (`aura/diagnosis.py`)**
   - *Impact:* `evaluate_candidate_diagnosis()` labels an event `rx_activity_skew_candidate` if `softnet_drops_total >= min_dropped_thresh` even when NET_RX softIRQ skew ratio is completely balanced.
   - *Consequence:* Misdiagnoses general host/queue backlog saturation as per-core IRQ affinity skew.
   - *Remedy:* Require true NET_RX softIRQ imbalance (`skew_ratio >= threshold`) for `rx_activity_skew_candidate`. If softnet drops exist without skew, classify as receive-path pressure under `uncertain` or documented pressure limitation, never fabricating skew attribution.

---

### Medium Severity Findings

8. **[MED-01] Server Ingestion Deduplication and Idempotency Missing (`aura/server.py`)**
   - *Impact:* If a client re-submits a report due to network retry, `POST /report` appends a duplicate enriched record to `client_window_ingested.jsonl`.
   - *Remedy:* Enforce memory/cache deduplication on `(run_id, window_id)`. Idempotent identical submissions return HTTP 200 without appending duplicates; conflicting payloads with existing ID return HTTP 409 Conflict.

9. **[MED-02] Essential vs. Optional Telemetry Ambiguity (`aura/diagnosis.py`)**
   - *Impact:* If optional metadata like `interface_driver` or `rps_masks_by_rx_queue` is null (common on virtual interfaces), diagnosis marks evidence as missing and refuses `normal` classification.
   - *Remedy:* Explicitly partition telemetry requirements: essential signals (P99, CPU PSI, softIRQs, softnet) gate candidate diagnoses and healthy confirmation; optional hardware descriptors do not invalidate healthy state.

10. **[MED-03] Telemetry `_BASELINE` Global Concurrency Vulnerability (`aura/telemetry.py`)**
    - *Impact:* `_BASELINE` is a module-level global dictionary modified in `collect_snapshot()` without synchronization locks.
    - *Remedy:* Protect baseline state with a `threading.Lock` or encapsulate in a stateful `TelemetryCollector` class.

---

## 3. Architecture Alignment Review

- **Gate A Contracts:** All frozen JSON schemas (Draft 2020-12) and `CONTRACT_RULES.md` are 100% compliant.
- **Clock Domain Boundaries:** Client monotonic clock is correctly treated as local to the benchmark run; server receipt monotonic time is stamped by `aura/server.py`.
- **Observe-Only Safety:** Controller adheres strictly to `no_op` actions with `status="not_attempted"` and `dry_run=True`. No privileged writes to `/proc`, `/sys`, or `cgroup` occur.

---

## 4. Remediation Plan

1. **Phase Two:** Harden `aura/client.py` with full response-body consumption and thread-safe real-time window streaming during execution.
2. **Phase Three:** Refactor `aura/telemetry.py` with format-aware softnet CPU parsing, multi-queue IRQ aggregation, and thread-safe collector encapsulation.
3. **Phase Four:** Refine `aura/diagnosis.py` to decouple softnet backlog drops from RX skew and classify essential vs optional telemetry.
4. **Phase Five:** Implement `aura/runtime.py` linking server, telemetry, queue handoff, and controller deduplication.
5. **Phase Six:** Add comprehensive integration tests covering end-to-end streaming, restarts, multi-queue IRQs, and recovery.
