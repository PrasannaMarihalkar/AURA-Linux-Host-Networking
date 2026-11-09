# AURA: Adaptive Root-Cause-Aware Control for Tail-Latency Optimization in Linux Host Networking

[![Status](https://img.shields.io/badge/status-active%20development-blue.svg)](https://github.com/PrasannaMarihalkar/AURA-Linux-Host-Networking)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

AURA is an academic research capstone project investigating root-cause-aware host mitigation for request tail latency under CPU and network receive contention in Linux environments.

---

## 1. Research Question

Under a controlled Linux TCP service workload, can a lightweight host controller use application latency plus CPU/receive-path telemetry to choose between a bounded CPU-resource adjustment, a separately evaluated receive-flow configuration, or no action—and does the policy outperform default and best-fixed configurations without unacceptable throughput, background-work, or stability penalties?

---

## 2. Architecture & Data Flow

```text
[Akash: Remote Client]
        |
        v HTTP /work
[AURA HTTP Server]  <---+
        |                | Telemetry Snapshots
   (Port 8080)           |
        |          [Shridhar: Host Telemetry]
        v
  /report (JSONL)
        |
        v
[Prasanna: Controller / Diagnosis]  ---> controller_event.jsonl
        |
        v Safe Wrappers (Future Gates)
[Shravan: Resource Controls (cgroup / RPS)]
```

---

## 3. Team Roles and Ownership

| Team Member | GitHub Username | Role | Primary Module | Branch |
|---|---|---|---|---|
| **Prasanna Marihalkar** | `@PrasannaMarihalkar` | Project Lead & Controller Owner | `aura/server.py`, `aura/controller.py`, `contracts/` | `feat/prasanna-foundation` |
| **Akash Bagoji** | `@akashsb2005` | Workload & Benchmark Owner | `aura/client.py`, `analysis/` | `feat/akash-benchmark` |
| **Shridhar** | `@SHRI427161` | Telemetry Collector Owner | `aura/telemetry.py` | `feat/shridhar-telemetry` |
| **Shravan Kumar Patil** | `@shravanakumarpatil-123` | Resource Controls Owner | `aura/controls/` | `feat/shravan-controls` |

---

## 4. Repository Layout

```text
AURA-Linux-Host-Networking/
├── .github/              # Issue and PR templates, CI workflow definitions
├── aura/                 # Core Python source modules
│   ├── server.py         # Baseline workload HTTP server (/health, /work, /report)
│   ├── controller.py     # Observe-only and dry-run controller loop
│   └── diagnosis.py      # Rule-based candidate root-cause diagnosis
├── config/               # Threshold and configuration parameters
│   └── thresholds.json   # Latency and pressure thresholds
├── contracts/            # Frozen JSON schemas and contract rules
│   ├── client_window.schema.json           # Ingress client window schema
│   ├── client_window_ingested.schema.json  # Server-enriched window schema
│   ├── host_snapshot.schema.json          # Host telemetry snapshot schema
│   ├── controller_event.schema.json       # Controller event log schema
│   ├── action_record.schema.json          # Guarded mitigation action schema
│   ├── CONTRACT_RULES.md                  # Normative specifications
│   └── examples/                          # Valid JSON sample fixtures
├── docs/                 # Architectural and research documentation
│   ├── architecture/
│   ├── research/
│   └── decisions/
├── experiments/          # Workload and benchmarking automation scripts
├── analysis/             # Aggregation and plotting scripts
├── tests/                # Pytest suites and schema validation fixtures
└── results/              # Experimental evidence (raw, processed, plots)
```

---

## 5. Development Quickstart

AURA core runtime uses the Python standard library. Development and testing dependencies (`pytest`, `jsonschema`) are declared in `requirements-dev.txt`.

```bash
# 1. Install development dependencies
pip install -r requirements-dev.txt

# 2. Syntax check across all modules
python3 -m compileall -q aura analysis tests

# 3. Run unit and contract test suite using pytest
python3 -m pytest -v tests/

# 4. Validate all contract schemas and examples individually
for f in contracts/*.schema.json contracts/examples/*.json; do
    python3 -m json.tool "$f" > /dev/null
done
```

---

## 6. Contribution Rules

Direct pushes to `main` are disabled. All contributions must follow the [AURA Development Workflow](CONTRIBUTING.md) and pass local compile, test, and schema verification gates before opening a Pull Request.
