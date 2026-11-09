# AURA Shared Contract Rules and Semantic Specifications

**Version:** 1.2  
**Scope:** Normative contract definitions for all AURA components:
- Benchmark Client (`aura/client.py` — Akash)
- Host Telemetry (`aura/telemetry.py` — Shridhar)
- Resource Controls (`aura/controls/` — Shravan)
- Diagnosis & Controller (`aura/diagnosis.py`, `aura/controller.py` — Prasanna)

---

## 1. Naming Conventions and Physical Units

1. **Format:** All JSON keys, payload fields, and log streams use lowercase `snake_case`.
2. **Standard Units:**
   - **Latencies:** Milliseconds (`*_ms`), e.g., `p50_ms`, `p95_ms`, `p99_ms`, `p999_ms`.
   - **Throughput:** Requests per second (`throughput_rps`).
   - **Stall & Pressure:** Percentage scale $0.0$ to $100.0$ (`*_pct`), e.g., `cpu_psi_some_avg10_pct`, `cpu_psi_some_avg60_pct`, `cpu_psi_some_avg300_pct`.
   - **Kernel Time Accumulators:** Microseconds (`*_us`), e.g., `cpu_psi_some_total_us`.
   - **Timestamps and Durations:** Seconds (`*_s`), e.g., `window_duration_s`, `interval_duration_s`, `report_age_s`.

---

## 2. Client Benchmark Contract Specifications

### 2.1 Request Categorization and Terminal Outcome Assignment
- **Terminal Outcome Rule:** A request is assigned to the window in which it reaches its terminal outcome (i.e. when HTTP 200 response headers are parsed, socket disconnects, or request timeout expires).
- **Total Requests (`requests`):** Exact count of all HTTP requests reaching terminal outcome in the window.
- **Successful Requests (`successes`):** Exact count of requests reaching terminal HTTP status `200 OK` within timeout.
- **Failed Requests (`errors`):** Total count of requests terminating in error (timeouts, TCP RST, connection refusals, non-200 HTTP response codes).
- **Identity Invariant:**
  $$\text{requests} = \text{successes} + \text{errors}$$

### 2.2 Sample Count & Throughput Calculation
- **`sample_count`:** Exact count of successful requests with valid monotonic latency measurements:
  $$\text{sample\_count} = \text{successes}$$
- **`throughput_rps`:** Rate of successfully completed requests over the strictly positive window duration:
  $$\text{throughput\_rps} = \frac{\text{successes}}{\text{window\_duration\_s}}$$
  (`window_duration_s` is mandatory and strictly positive: `window_duration_s > 0`).

### 2.3 Percentile Calculation & Reporting Guardrails
- **Timing Source:** Request latencies MUST be measured strictly using monotonic clocks (`time.monotonic()`).
- **Domain:** Latency percentiles represent successful-response latencies only. Failures are reported separately in `errors`.
- **Reporting Guardrails (Minimum Tail Counts):**
  Statistical reporting of extreme percentiles requires sufficient empirical events in the tail distribution:
  - **P99 Policy:**
    - At $P99$, an empirical sample of $10,000$ events yields an expected tail count of $10,000 \times 0.01 = 100$ tail observations.
    - If $\text{sample\_count} \ge 10,000 \implies \text{p99\_status} = \text{"minimum\_tail\_count\_met"}$.
    - If $100 \le \text{sample\_count} < 10,000 \implies \text{p99\_status} = \text{"exploratory"}$.
    - If $\text{sample\_count} < 100 \implies \text{p99\_status} = \text{"insufficient"}$.
  - **P99.9 Policy:**
    - At $P99.9$, an empirical sample of $100,000$ events yields an expected tail count of $100,000 \times 0.001 = 100$ tail observations.
    - If $\text{sample\_count} \ge 100,000 \implies \text{p999\_status} = \text{"minimum\_tail\_count\_met"}$.
    - If $1,000 \le \text{sample\_count} < 100,000 \implies \text{p999\_status} = \text{"exploratory"}$.
    - If $\text{sample\_count} < 1,000 \implies \text{p999\_status} = \text{"insufficient"}$.
- **Methodological Disclaimer:** Meeting the minimum sample count is a reporting policy guardrail; it does NOT guarantee statistical significance or replace repeated independent experiments and uncertainty intervals.

---

## 3. Host Telemetry Contract Specifications

### 3.1 System-Level CPU PSI Semantics (Critical Correction)
- **Linux Kernel Documentation Standard:** Under official Linux PSI architecture (`/proc/pressure/cpu`), system-level CPU pressure ONLY exposes a `some` metric. A `full` CPU stall is undefined at the system level because CPUs are never completely idle/stalled while runnable work exists. On newer kernels, `full` is reported as zero solely for backward compatibility.
- **Specification:**
  - `cpu_psi_some_avg10_pct`, `cpu_psi_some_avg60_pct`, `cpu_psi_some_avg300_pct`, and `cpu_psi_some_total_us` are preserved.
  - `cpu_psi_full_*` metrics are removed from system-level diagnosis and contracts.
  - If CPU PSI is unsupported (e.g. kernel compiled without `CONFIG_PSI=y` or older VM), fields are set to `null` and added to `missing_fields`.

### 3.2 Monotonic Snapshot Timing & Host Boot Identity
- Host snapshots must record:
  - `snapshot_timestamp_epoch_s`: Wall-clock time (logging only).
  - `snapshot_monotonic_s`: Monotonic timestamp (`time.monotonic()`) for interval duration and delta verification.
  - `host_boot_id`: UUID string read from `/proc/sys/kernel/random/boot_id`.

### 3.3 Counter Delta Calculation, Hotplug, and Resets
- Every per-CPU delta (`net_rx_softirq_delta_per_cpu`, `net_tx_softirq_delta_per_cpu`, `softnet_*`) explicitly preserves `cpu_id`:
  ```json
  [
    {"cpu_id": 0, "delta": 412, "reason": null},
    {"cpu_id": 1, "delta": null, "reason": "counter_reset_detected"}
  ]
  ```
- **Baseline Requirement:** Deltas are computed ONLY when a valid baseline sample exists on the same `host_boot_id`.
- **Reset Detection:** If $\text{counter}(t_1) < \text{counter}(t_0)$, a rollover or driver reset occurred:
  - Set `delta = null`.
  - Set `reason = "counter_reset_detected"`.
  - Append `{"metric": "<field>", "reason": "counter_reset_detected"}` to `missing_fields`.
- **CPU Hotplug:** If a CPU core appears or disappears between intervals:
  - For disappearing core: record `null` delta with `reason = "cpu_offline"`.
  - For newly appeared core: record `null` delta with `reason = "baseline_missing_hotplug"`.
- **Structured `missing_fields`:** Every unavailable or reset metric is logged as `{"metric": "<name>", "reason": "<machine_readable_code>"}`. Zero is NEVER fabricated.

### 3.4 Scope Attribution Boundaries
- `/proc/softirqs` `NET_RX` is a host-wide per-CPU accumulator. It reflects host-wide softIRQ load across all interfaces and sockets, NOT per-flow or per-interface attribution.
- Telemetry modules must include these limits in `counter_scope_notes`.

---

## 4. Ingestion Pipeline and Clock Freshness Architecture

```text
[Akash Client]
      |
      |  1. Emits raw client_window (server timestamps omitted or null)
      v
[AURA HTTP Server (/report)]
      |
      |  2. Validates incoming payload against client_window.schema.json
      |  3. Assigns server_received_at_epoch_s (audit)
      |     Assigns server_received_at_monotonic_s (freshness)
      |     Assigns server_boot_id (/proc/sys/kernel/random/boot_id)
      v
[AURA Controller]
      |
      |  4. Ingests ONLY validated, stamped records
      |  5. Freshness check:
      |     assert report.server_boot_id == controller.server_boot_id
      |     report_age_s = server_monotonic_now - report.server_received_at_monotonic_s
      |     if report_age_s > max_age_s => stale_telemetry = true => no_op
```

- **Wall Clock Independence:** Client wall-clock time and server wall-clock time MUST NEVER be compared.
- **Boot Boundary:** If `server_boot_id` differs from the controller host's active boot ID, the report is marked invalid and stale.

---

## 5. Controller Diagnosis and Action Governance

1. **Candidate Labels Only:** `cpu_contention_candidate`, `rx_activity_skew_candidate`, `interrupt_pressure_candidate`, `uncertain`, `normal`.
2. **Inhibition on Stale or Conflicting Signals:**
   - If `stale_telemetry == true` or `conflicting_evidence == true` or sample count is `insufficient`, diagnosis MUST be `uncertain` and action MUST be `no_op`.
3. **Guardrails on `applied` Action Status:**
   - Setting `action.status = "applied"` is strictly prohibited when `controller_mode` is `observe_only` or `dry_run`.
   - Applying an action requires live readback verification matching `requested` and pre-verified rollback paths.
