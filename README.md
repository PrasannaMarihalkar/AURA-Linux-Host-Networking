# AURA: Adaptive Root-Cause-Aware Control for Tail-Latency Optimization in Linux Host Networking

[![Status](https://img.shields.io/badge/status-active%20development-blue.svg)](https://github.com/PrasannaMarihalkar/AURA-Linux-Host-Networking)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

AURA is an academic research capstone project investigating root-cause-aware host mitigation for request tail latency under CPU and network receive contention in Linux environments.

---

## 1. Research Question

Under a controlled Linux TCP service workload, can a lightweight host controller use application latency plus CPU/receive-path telemetry to choose between a bounded CPU-resource adjustment, a separately evaluated receive-flow configuration, or no action—and does the policy outperform default and best-fixed configurations without unacceptable throughput, background-work, or stability penalties?

---

## 2. Project Architecture & Milestone Roadmap

```text
[Akash: Remote Client (aura/client.py)]
        |
        | HTTP requests (GET /work?n=<iters>)
        v
[AURA HTTP Server (aura/server.py)]  <---+
        |                                 | Host Telemetry Snapshots
   (Port 8080)                            |
        |                           [Shridhar: Host Telemetry (aura/telemetry.py)]
        v
  POST /report (4-Stage Validated Ingestion Pipeline)
        |
        v Enriched JSONL Measurement Storage (server_received_at_*, server_boot_id)
[Prasanna: Controller / Diagnosis (aura/controller.py)]  ---> controller_event.jsonl
        |
        v Safe Wrappers (Future Phase 5 / Gate C)
[Shravan: Resource Controls (aura/controls/)]
```

### Current Status: Master Integration & Verification (Gates A, B, and C Foundation)
* **Gate A (Completed & Merged into `main`):**
  * Repository foundation, branching policies, and pull request conventions.
  * Shared JSON Schema Draft 2020-12 interchange contracts (`contracts/*.schema.json`).
  * Two-stage client window ingestion specification (`client_window` ingress vs. `client_window_ingested`).
  * Normative rules for physical units, invariants, nearest-rank percentiles, tail reporting guardrails, and counter deltas (`contracts/CONTRACT_RULES.md`).
  * Conforming test fixtures and negative validation cases (`contracts/examples/`).
  * Contract and semantic test suite (`tests/test_contracts.py`).
  * Continuous Integration matrix workflow (`.github/workflows/ci.yml`).
* **Gate B (Completed & Merged into `main`):**
  * Baseline HTTP workload server (`aura/server.py` with `/health`, deterministic `/work?n=<iters>`, and 4-stage validated `/report`).
  * Rule-based heuristic diagnosis engine (`aura/diagnosis.py`) covering CPU contention, receive activity skew, softnet congestion, and conflicting evidence.
  * Observe-only / dry-run controller daemon (`aura/controller.py`) with periodic evaluation loop, monotonic freshness checking, boot identity continuity, and schema-valid `controller_event.jsonl` emission.
  * Server-side monotonic timestamp stamping (`time.monotonic()`), Linux boot identity continuity (`/proc/sys/kernel/random/boot_id`), and thread-safe JSONL persistence.
  * Calibrated configuration and runtime thresholds (`config/thresholds.json`).
* **Master Integration & Hardening (Current `feat/prasanna-master-integration`):**
  * Reconciled and audited all team contributions (Akash, Shridhar, Prasanna, Shravan).
  * Benchmark client: accurate partial window duration accounting, asynchronous HTTP delivery queue with exponential backoff and graceful drain, and explicit coordinated omission documentation.
  * HTTP server: race-safe in-flight deduplication synchronization, backpressure propagation (HTTP 503 `runtime_backpressure`), and non-blocking socket drain preventing TCP reset aborts.
  * Reversible resource controls (`aura/controls/cgroup_cpu.py` & `aura/controls/rpsctl.py`): strict slice whitelisting (`aura-app.slice`, `aura-bg.slice`), weight range validation, pre-flight reading, readback verification, transactional rollback on mismatch, RPS sysfs multi-queue inspection, and setup/restore helper scripts (`scripts/`).
  * Strict architectural isolation: verified that `aura.controller` never imports or invokes controls in observe-only mode.
  * Comprehensive review and research documentation (`docs/reviews/`, `docs/research/`).

---

## 3. Team Roles and Module Ownership

| Team Member | GitHub Username | Role | Assigned Files / Responsibilities | Assigned Branch |
|---|---|---|---|---|
| **Prasanna Marihalkar** | `@PrasannaMarihalkar` | Project Lead & Controller Owner | Contracts (`contracts/`), `aura/server.py`, `aura/controller.py`, `aura/diagnosis.py` | `feat/prasanna-gate-b` |
| **Akash Bagoji** | `@akashsb2005` | Workload & Benchmark Owner | Client runner (`aura/client.py`), benchmarks (`experiments/`), analysis (`analysis/`) | `feat/akash-benchmark` |
| **Shridhar** | `@SHRI427161` | Telemetry Collector Owner | Host telemetry collector (`aura/telemetry.py`), tests (`tests/test_telemetry.py`) | `feat/shridhar-telemetry` |
| **Shravan Kumar Patil** | `@shravanakumarpatil-123` | Resource Controls Owner | Linux resource controls (`aura/controls/`), safety scripts (`scripts/`) | `feat/shravan-controls` |

---

## 4. Repository Layout

```text
AURA-Linux-Host-Networking/
├── .github/
│   ├── pull_request_template.md             # Standard PR template
│   ├── ISSUE_TEMPLATE/                      # Bug, feature, and paper review templates
│   └── workflows/
│       └── ci.yml                           # GitHub Actions CI matrix workflow (Python 3.10–3.13)
├── aura/
│   ├── __init__.py                          # Package initialization
│   ├── contracts.py                         # Pure contract validation and invariant helpers
│   ├── server.py                            # Baseline workload HTTP server (/health, /work, /report)
│   ├── controller.py                        # Observe-only controller daemon & periodic evaluation loop
│   ├── diagnosis.py                         # Rule-based candidate root-cause diagnosis engine
│   ├── telemetry.py                         # Host telemetry sampling (/proc/pressure, softnet, IRQ, softirq)
│   ├── client.py                            # Multi-threaded benchmark client with streaming window coordinator
│   ├── runtime.py                           # Integrated runtime daemon coordinating server, telemetry, controller
│   └── controls/                            # Resource controls (Gate C and manual actuation)
│       ├── __init__.py
│       ├── cgroup_cpu.py                    # Cgroup v2 cpu.weight control with rollback verification
│       └── rpsctl.py                        # RPS sysfs multi-queue inspection and backup
├── config/
│   └── thresholds.json                      # Baseline threshold and timing configuration parameters
├── contracts/
│   ├── client_window.schema.json            # Ingress measurement window schema (client -> server)
│   ├── client_window_ingested.schema.json   # Server-enriched window schema (server -> controller)
│   ├── host_snapshot.schema.json           # Host telemetry snapshot schema
│   ├── controller_event.schema.json        # Controller evaluation event schema
│   ├── action_record.schema.json           # Guarded mitigation action lifecycle schema
│   ├── CONTRACT_RULES.md                   # Normative contract semantics and formulas
│   └── examples/                           # Conforming sample JSON records
│       ├── client_window_ingress.example.json
│       ├── client_window_ingested.example.json
│       ├── host_snapshot.example.json
│       ├── controller_event.example.json
│       └── action_record.example.json
├── docs/                                    # Architectural and research documentation
│   ├── architecture/
│   │   ├── gate_b_design.md                 # Gate B design and implementation specification
│   │   ├── telemetry_signals.md             # Telemetry collection specification
│   │   └── runtime_integration.md           # Integrated runtime architecture and lifecycle
│   ├── research/
│   │   └── EXPERIMENT_METHODOLOGY.md        # Scientific experiment methodology & tail-latency rigor
│   ├── reviews/
│   │   ├── AURA_FINAL_ENGINEERING_AUDIT.md  # Comprehensive final integration engineering audit
│   │   ├── AURA_INTEGRATION_MATRIX.md       # Subsystem boundaries, schema contracts, and tests
│   │   ├── KNOWN_LIMITATIONS.md             # OS virtualization quirks, load models, and constraints
│   │   └── AURA_ENGINEERING_AUDIT.md        # Pre-integration engineering audit
│   └── benchmark_client.md                  # Benchmark client usage and configuration
├── experiments/                             # Workload and benchmarking automation scripts
│   ├── run_baseline.sh                      # Zero-load baseline run automation
│   └── run_workload.sh                      # Under-load benchmark run automation
├── scripts/                                 # Host configuration and administration scripts
│   ├── setup_slices.sh                      # Systemd slice initialization script
│   └── restore.sh                           # Clean cgroup and RPS restoration script
├── analysis/                                # Aggregation and plotting scripts
│   ├── aggregate.py                         # Metrics aggregator and summary generator
│   └── plot_results.py                      # Latency distribution and timeline plotter
├── tests/
│   ├── __init__.py
│   ├── test_contracts.py                   # Pytest suite for contract schemas and semantic rules (48 tests)
│   ├── test_server.py                      # Pytest suite for HTTP server endpoints and ingestion (28 tests)
│   ├── test_controller.py                  # Pytest suite for diagnosis engine & controller loop (39 tests)
│   ├── test_telemetry.py                   # Pytest suite for Linux host telemetry collection (26 tests)
│   ├── test_client.py                      # Pytest suite for benchmark client and coordinator (28 tests)
│   ├── test_controls.py                    # Pytest suite for cgroup CPU and RPS controls (18 tests)
│   ├── test_analysis.py                    # Pytest suite for analysis aggregators and plotters (4 tests)
│   └── test_runtime.py                     # Pytest suite for end-to-end runtime integration (4 tests)
├── requirements.txt                         # Runtime dependencies (jsonschema)
├── requirements-dev.txt                     # Development and testing dependencies (pytest, matplotlib)
├── CONTRIBUTING.md                          # Contribution guidelines and merge gates
└── README.md                                # Project overview and documentation
```

---

## 5. Development Quickstart

AURA uses Python 3 (supported versions: 3.10 through 3.13).

```bash
# 1. Install runtime and development dependencies
pip install -r requirements-dev.txt

# 2. Syntax compilation check across modules
python3 -m compileall -q aura analysis tests scripts

# 3. Run complete test suite using pytest (188 tests: 186 passed, 2 environment skipped)
python3 -m pytest -v tests/

# 4. Validate all contract schemas, fixtures, and configuration files
for f in contracts/*.schema.json contracts/examples/*.json config/*.json; do
    python3 -m json.tool "$f" > /dev/null
done

# 5. Start the complete integrated AURA runtime daemon
python3 -m aura.runtime --host 127.0.0.1 --port 8080 --interface eth0 --results-dir results

# 6. Execute benchmark experiment workloads
./experiments/run_baseline.sh
./experiments/run_workload.sh
```

---

## 6. HTTP Server Endpoints

The baseline HTTP server runs on port 8080 by default (`AURA_PORT` and `AURA_HOST` configurable via environment variables):

* **`GET /health`**: Fast liveness probe returning HTTP 200 `{"status": "ok"}`.
* **`GET /work?n=<iters>`**: Deterministic CPU-bound computation workload using iterated SHA-256 rounds. Returns iteration count, execution time, and cryptographic checksum. Rejects non-integers, negative counts, and requests exceeding `max_work_iters`.
* **`POST /report`**: 4-stage validated benchmark window ingestion:
  1. **Stage 1 (Ingress Schema):** Validates raw payload against `contracts/client_window.schema.json`. Rejects malformed JSON, NaN/Infinity, and client-injected server timestamps.
  2. **Stage 2 (Semantic & Invariant Validation):** Pure arithmetic verification via `aura.contracts:validate_client_window_invariants` (`requests == successes + errors`, `sample_count <= successes`, strictly positive duration, throughput consistency, strict integer types).
  3. **Stage 3 (Server Enrichment):** Stamped with server receipt timestamps (`server_received_at_epoch_s`, `server_received_at_monotonic_s`) and host Linux boot identity (`server_boot_id`).
  4. **Stage 4 (Enriched Schema):** Validates enriched payload against `contracts/client_window_ingested.schema.json` before atomic append to `results/raw/client_window_ingested.jsonl`.

---

## 7. Observe-Only Controller Safety Guarantees

In accordance with Gate B specifications, the controller operates in strict **`observe_only`** mode:
* **No Active Mitigations:** System settings (`cgroups`, `CPUWeight`, `RPS`, `IRQ` affinities) are never modified.
* **Guaranteed Action Status:** Emitted action status is strictly `not_attempted` or `dry_run`. Emitting `status = applied` is strictly prohibited by schema and contract invariants.
* **Conservative Fallback:** If telemetry is stale, boot IDs mismatch, timestamps are invalid, or evidence is conflicting, the controller conservatively outputs candidate diagnosis `uncertain` and action `no_op`.

---

## 8. Contribution Rules

Direct pushes to `main` are disabled. All contributions must follow the [AURA Development Workflow](CONTRIBUTING.md) and pass local compile, test, and schema verification gates before opening a Pull Request.
