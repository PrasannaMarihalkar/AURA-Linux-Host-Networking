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
        v HTTP /work
[AURA HTTP Server (aura/server.py)]  <---+
        |                                 | Telemetry Snapshots
   (Port 8080)                            |
        |                           [Shridhar: Host Telemetry (aura/telemetry.py)]
        v
  POST /report (Ingress Schema)
        |
        v Enriched Ingestion Record
[Prasanna: Controller / Diagnosis (aura/controller.py)]  ---> controller_event.jsonl
        |
        v Safe Wrappers (Future Gates)
[Shravan: Resource Controls (aura/controls/)]
```

### Current Status: Gate A — Shared Contracts and Semantic Freeze
* **Gate A (Implemented in this branch):**
  * Repository foundation, branching policies, and pull request conventions.
  * Shared JSON Schema Draft 2020-12 interchange contracts (`contracts/*.schema.json`).
  * Two-stage client window ingestion specification (`client_window` ingress vs. `client_window_ingested`).
  * Normative rules for physical units, invariants, nearest-rank percentiles, tail reporting guardrails, and counter deltas (`contracts/CONTRACT_RULES.md`).
  * Conforming test fixtures and negative validation cases (`contracts/examples/`).
  * Contract and semantic test suite (`tests/test_contracts.py`) using `pytest` and `jsonschema`.
  * Continuous Integration workflow (`.github/workflows/ci.yml`).
* **Gate B (Planned next step — not implemented in Gate A):**
  * Baseline HTTP workload server (`aura/server.py` with `/health`, deterministic `/work`, and `/report`).
  * Rule-based heuristic diagnosis module (`aura/diagnosis.py`).
  * Observe-only / dry-run controller daemon (`aura/controller.py`).
  * Server-side monotonic timestamp stamping, boot ID continuity checks, and report persistence.

---

## 3. Team Roles and Module Ownership

| Team Member | GitHub Username | Role | Assigned Files / Responsibilities | Assigned Branch |
|---|---|---|---|---|
| **Prasanna Marihalkar** | `@PrasannaMarihalkar` | Project Lead & Controller Owner | Contracts (`contracts/`), `aura/server.py` [Gate B], `aura/controller.py` [Gate B], `aura/diagnosis.py` [Gate B] | `feat/prasanna-foundation` |
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
│       └── ci.yml                           # GitHub Actions CI matrix workflow
├── aura/
│   ├── __init__.py                          # Package initialization
│   ├── server.py                            # [Planned - Gate B] Workload HTTP server (/health, /work, /report)
│   ├── controller.py                        # [Planned - Gate B] Observe-only / dry-run controller loop
│   └── diagnosis.py                         # [Planned - Gate B] Candidate diagnosis engine
├── config/
│   └── thresholds.json                      # Default threshold configuration parameters
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
│   ├── research/
│   └── decisions/
├── experiments/                             # Workload and benchmarking automation scripts
├── analysis/                                # Aggregation and plotting scripts
├── tests/
│   ├── __init__.py
│   └── test_contracts.py                   # Pytest suite for contract schemas and semantic rules
├── requirements.txt                         # Runtime dependencies (jsonschema)
├── requirements-dev.txt                     # Development and testing dependencies (pytest)
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
python3 -m compileall -q aura analysis tests

# 3. Run contract and semantic test suite using pytest
python3 -m pytest -v tests/

# 4. Validate all contract schemas and examples individually
for f in contracts/*.schema.json contracts/examples/*.json; do
    python3 -m json.tool "$f" > /dev/null
done
```

---

## 6. Contribution Rules

Direct pushes to `main` are disabled. All contributions must follow the [AURA Development Workflow](CONTRIBUTING.md) and pass local compile, test, and schema verification gates before opening a Pull Request.
