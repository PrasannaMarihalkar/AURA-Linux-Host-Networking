# AURA Benchmark Client (Issue #2)

## Purpose and measurement model

`aura/client.py` generates a fixed-concurrency, closed-loop HTTP workload against the AURA server's `GET /work?n=<iters>` endpoint. Each worker performs one request at a time. The configured worker count is the maximum number of concurrent in-flight requests; the client does not claim to provide an open-loop request-arrival rate.

Request durations use `time.monotonic()` only. The terminal outcome is timestamped when an HTTP response status is available (or when a connection failure/timeout is raised). The result is placed in the measurement window containing that terminal timestamp. Requests still in flight when request generation stops are allowed to complete or time out and their terminal outcomes are retained; this may create a final drain window. `client_timestamp_epoch_s` is metadata only and is never used for duration calculation or freshness.

Only HTTP 200 responses contribute latency samples and percentile calculations. HTTP errors, timeouts, connection refusals, resets and other transport failures increment `errors`. The client enforces `requests = successes + errors`, `sample_count <= successes`, and `throughput_rps = successes / window_duration_s`.

## Percentile definitions

Percentiles use nearest rank: `sorted_samples[ceil(p * N) - 1]` (1-indexed and bounded). The Issue #1 / Gate A guardrails are implemented exactly:

- P99: `minimum_tail_count_met` at 10,000 samples; `exploratory` from 100 to 9,999; otherwise `insufficient`.
- P99.9: `minimum_tail_count_met` at 100,000 samples; `exploratory` from 1,000 to 99,999; otherwise `insufficient`.

A guardrail is an engineering reporting threshold, not a claim of statistical significance. Use repeated independent trials for research conclusions.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
pip install -r requirements-analysis.txt   # only needed for plots
```

The client validates every ingress record against `contracts/client_window.schema.json` before writing it. It never emits server receipt timestamps or `server_boot_id`.

## Start the target server

After Gate B PR #7 is merged, start the server on the machine that hosts AURA:

```bash
python3 -m aura.server
```

The default server URL is `http://127.0.0.1:8080/work`. For a remote server, pass its reachable address, for example `http://192.168.1.50:8080/work`.

## Run a baseline

```bash
./experiments/run_baseline.sh http://127.0.0.1:8080/work 60 4 10 1000
```

This runs for 60 seconds, uses four concurrent workers, creates 10-second windows, and asks the server to perform 1,000 SHA-256 iterations per request. Outputs are unique UTC-named files under `results/raw/`, including a separate raw successful-latency CSV sidecar.

## Run a named workload

```bash
./experiments/run_workload.sh http://127.0.0.1:8080/work 120 8 10 cpu_stress 1000
```

The script only generates client load. Do not describe a client-side scenario label as proof that host CPU or NIC contention occurred; corroborate that using host telemetry.

## Submit windows to the AURA server

Client measurement files are written locally regardless of report-submission status. To additionally POST each ingress window to the server's `/report` endpoint:

```bash
AURA_SUBMIT_REPORTS=1 ./experiments/run_baseline.sh http://127.0.0.1:8080/work 60 4 10 1000
```

Or use the CLI option `--submit-reports`. Submission happens after measurement and is not included in workload latency/throughput. A failed report submission is recorded in the window's `metadata` and does not rewrite the measured counters.

## Aggregate and plot

```bash
python -m analysis.aggregate --input results/raw/baseline_<timestamp>.jsonl
python -m analysis.plot_results \
  --input results/raw/baseline_<timestamp>.jsonl \
  --latency-samples results/raw/baseline_<timestamp>_latencies.csv
```

Plots show observed per-window P50/P95/P99/P99.9, successful throughput and error rate. When the raw latency sidecar is provided, the tool also generates an empirical histogram and empirical CDF from actual successful request samples. It does not smooth data or synthesize missing observations.

**Important aggregation limitation:** Per-window percentile summaries cannot be pooled to compute a mathematically valid run-wide P99/P99.9. For a true run-level empirical distribution, use the raw successful-latency sidecar CSV. `aggregate.py` reports median/max *window percentile estimates* and total run throughput/counts; it does not label these as a pooled percentile.

## Tests

```bash
python -m pytest -v tests/test_client.py
python -m pytest -v tests/
```

The client tests use local mock HTTP servers, synthetic latency vectors for percentile correctness, schema checks, connection failures and timeouts. They do not require Internet access or a physical NIC.
