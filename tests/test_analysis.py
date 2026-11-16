"""Tests for benchmark aggregation and evidence-preserving plotting inputs."""
from __future__ import annotations

import json
from pathlib import Path
import pytest

from analysis.aggregate import aggregate_records, load_jsonl, write_summary_csv
from analysis.plot_results import load_latency_samples, plot_records


def sample_record(window_id: int, *, p99: float, throughput: float, requests: int = 10):
    return {
        "schema_version": "1.2",
        "run_id": "analysis-run",
        "window_id": window_id,
        "scenario": "baseline",
        "client_timestamp_epoch_s": 1_700_000_000.0 + window_id,
        "window_duration_s": 1.0,
        "requests": requests,
        "successes": requests - 1,
        "errors": 1,
        "sample_count": requests - 1,
        "throughput_rps": throughput,
        "p50_ms": 1.0,
        "p95_ms": 2.0,
        "p99_ms": p99,
        "p999_ms": p99 + 1,
        "p99_status": "insufficient",
        "p999_status": "insufficient",
        "workers": 2,
        "metadata": {},
    }


def test_aggregate_reports_counts_throughput_and_window_percentile_summaries():
    rows = aggregate_records([
        sample_record(1, p99=5.0, throughput=9.0),
        sample_record(2, p99=9.0, throughput=9.0),
    ])
    assert len(rows) == 1
    row = rows[0]
    assert row["requests"] == 20
    assert row["successes"] == 18
    assert row["errors"] == 2
    assert row["throughput_rps"] == 9.0
    assert row["median_window_p99_ms"] == 7.0
    assert row["max_window_p99_ms"] == 9.0
    assert "run_p99_ms" not in row  # never invent a pooled percentile


def test_aggregate_rejects_inconsistent_request_invariant():
    record = sample_record(1, p99=5.0, throughput=9.0)
    record["requests"] = 99
    try:
        aggregate_records([record])
    except ValueError as exc:
        assert "requests != successes + errors" in str(exc)
    else:
        raise AssertionError("inconsistent request count was accepted")


def test_aggregate_jsonl_and_csv_output(tmp_path: Path):
    source = tmp_path / "windows.jsonl"
    source.write_text(json.dumps(sample_record(1, p99=5.0, throughput=9.0)) + "\n")
    records = load_jsonl(source)
    rows = aggregate_records(records)
    output = write_summary_csv(rows, tmp_path / "summary.csv")
    assert output.exists()
    assert "median_window_p99_ms" in output.read_text()


def test_plotter_creates_actual_data_figures_and_raw_distribution(tmp_path: Path):
    pytest.importorskip("matplotlib")
    samples = tmp_path / "latencies.csv"
    samples.write_text(
        "run_id,scenario,window_id,latency_ms\n"
        "analysis-run,baseline,1,1.0\n"
        "analysis-run,baseline,1,2.0\n"
        "analysis-run,baseline,2,5.0\n"
    )
    generated = plot_records(
        [
            sample_record(1, p99=5.0, throughput=9.0),
            sample_record(2, p99=9.0, throughput=9.0),
        ],
        tmp_path,
        load_latency_samples(samples),
    )
    assert len(generated) == 10
    assert all(path.is_file() and path.stat().st_size > 0 for path in generated)
