# AURA Development & Contribution Workflow

This document defines the engineering standards, branching strategy, testing requirements, and merge gates for the AURA research capstone.

---

## 1. Team Ownership & Assigned Branches

Every team member works on an assigned personal feature branch created from `main`:

| Member | GitHub Username | Role | Primary Branch | Files Owned |
|---|---|---|---|---|
| **Prasanna Marihalkar** | `@PrasannaMarihalkar` | Project Lead & Controller Owner | `feat/prasanna-foundation` | `contracts/`, `aura/server.py`, `aura/controller.py`, `aura/diagnosis.py` |
| **Akash Bagoji** | `@akashsb2005` | Workload & Benchmark Owner | `feat/akash-benchmark` | `aura/client.py`, `experiments/run_baseline.sh`, `analysis/` |
| **Shridhar** | `@SHRI427161` | Linux Telemetry Owner | `feat/shridhar-telemetry` | `aura/telemetry.py`, `tests/test_telemetry.py` |
| **Shravan Kumar Patil** | `@shravanakumarpatil-123` | Resource Controls & Safety Owner | `feat/shravan-controls` | `aura/controls/cgroup_cpu.py`, `aura/controls/rpsctl.py`, `scripts/` |

---

## 2. Branching & Git Rules

1. **`main` is protected:** Direct pushes to `main` are strictly prohibited. All code enters `main` via reviewed Pull Requests.
2. **Branch from latest `main`:** Before starting new work, pull the latest changes from `main`:
   ```bash
   git fetch origin
   git switch -c feat/<your-assigned-branch> origin/main
   ```
3. **No Cross-Branch Dependencies:** Never branch off another teammate's feature branch unless coordinated by Prasanna.
4. **Isolated Work:** Only edit the files you own. If a change to a shared file (such as `contracts/` or `thresholds.json`) is required, discuss and coordinate with the team lead first.
5. **Never Commit:**
   - Virtual environments (`.venv/`, `env/`)
   - Python bytecode (`__pycache__/`, `*.pyc`)
   - VM images, ISOs, OVAs, or raw disks
   - Personal credentials, tokens, or private SSH keys
   - Unanonymized large PCAP packet captures

---

## 3. Local Verification Before Opening a PR

Before submitting a PR, every member must run and pass the following merge gate locally:

```bash
# 1. Syntax and compilation check across all modules
python3 -m compileall -q aura analysis tests scripts

# 2. Run unit tests
python3 -m unittest discover -s tests -v

# 3. JSON schema syntax validation
python3 -m json.tool contracts/client_window.schema.json > /dev/null
python3 -m json.tool contracts/host_snapshot.schema.json > /dev/null
python3 -m json.tool contracts/controller_event.schema.json > /dev/null
```

---

## 4. Pull Request Requirements

Every PR must use the standard [Pull Request Template](.github/pull_request_template.md) and include:
- Clear problem statement and scope.
- Exact test commands executed.
- Actual console/terminal test output.
- Schema compliance check.
- Safety and reversibility verification (for any control code touching systemd or cgroups).
- Path to raw evidence logs or run manifest.

### Review & Merge Gate:
- At least one peer review is required before merging.
- Prasanna Marihalkar acts as final integration owner for merging PRs into `main`.
- Untested work or direct code pushes will be rejected.
