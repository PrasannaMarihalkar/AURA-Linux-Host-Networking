### Problem Addressed
<!-- Brief description of the bug, contract change, or module capability being introduced -->

### Files Changed
<!-- Exact list of files created or modified -->

### Paper / Architecture Requirement Supported
<!-- Which paper, design requirement, or gate does this PR implement? -->

### Exact Test Commands
```bash
# e.g., python3 -m compileall -q aura analysis tests
# e.g., python3 -m unittest discover -s tests -v
# e.g., python3 -m json.tool contracts/client_window.schema.json
```

### Actual Test Output
```text
<!-- Paste terminal test output demonstrating passes without errors -->
```

### Schema Validation
- [ ] Conforms to `contracts/client_window.schema.json`
- [ ] Conforms to `contracts/host_snapshot.schema.json`
- [ ] Conforms to `contracts/controller_event.schema.json`
- [ ] Not applicable to this PR

### Privileged Settings & Safety Checks
- [ ] Touches `/sys`, `/proc`, `systemd`, or `cgroup`
- [ ] Read-only inspection only (no writes)
- [ ] Safe `get -> backup -> set -> readback -> restore -> verify` implemented
- [ ] Reversibility and rollback tested under failure injection
- [ ] Not applicable (unprivileged user-space code)

### Known Limitations
<!-- Explicitly describe what is not yet supported, caveats, or virtualized environment constraints -->

### Evidence Path / Run ID
<!-- Reference to archived test output, e.g., results/raw/<run_id>.jsonl or results/env/<host>.txt -->
