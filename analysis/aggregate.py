"""Aggregate AURA window records without fabricating run-level percentiles.

Window percentiles are summaries, not raw samples. They cannot be combined
into a mathematically valid run-wide P99/P99.9. This script therefore reports
throughput and counts over the run, plus explicitly named distributions of
per-window percentile estimates (median/max across windows).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


NUMERIC_WINDOW_FIELDS = ("p50_ms", "p95_ms", "p99_ms", "p999_ms")


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                item = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"Invalid strict JSON at {path}:{line_number}: {exc}") from exc
            if not isinstance(item, dict):
                raise ValueError(f"Expected JSON object at {path}:{line_number}")
            records.append(item)
    if not records:
        raise ValueError(f"No benchmark window records found in {path}")
    return records


def _finite_values(records: Iterable[dict[str, Any]], field: str) -> list[float]:
    values: list[float] = []
    for record in records:
        value = record.get(field)
        if value is None:
            continue
        number = float(value)
        if math.isfinite(number):
            values.append(number)
    return values


def aggregate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        requests = int(record.get("requests", 0))
        successes = int(record.get("successes", 0))
        errors = int(record.get("errors", 0))
        samples = int(record.get("sample_count", 0))
        duration = float(record.get("window_duration_s", 0.0))
        throughput = float(record.get("throughput_rps", 0.0))
        if requests != successes + errors:
            raise ValueError(f"Window {record.get('window_id')}: requests != successes + errors")
        if samples > successes:
            raise ValueError(f"Window {record.get('window_id')}: sample_count exceeds successes")
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError(f"Window {record.get('window_id')}: invalid window duration")
        if not math.isfinite(throughput) or not math.isclose(throughput, successes / duration, rel_tol=1e-6, abs_tol=1e-9):
            raise ValueError(f"Window {record.get('window_id')}: throughput does not match successes / duration")
        groups[(str(record.get("run_id", "unknown")), str(record.get("scenario", "unknown")))].append(record)

    summaries: list[dict[str, Any]] = []
    for (run_id, scenario), windows in sorted(groups.items()):
        windows.sort(key=lambda item: int(item.get("window_id", 0)))
        requests = sum(int(item.get("requests", 0)) for item in windows)
        successes = sum(int(item.get("successes", 0)) for item in windows)
        errors = sum(int(item.get("errors", 0)) for item in windows)
        samples = sum(int(item.get("sample_count", 0)) for item in windows)
        elapsed = sum(float(item.get("window_duration_s", 0.0)) for item in windows)
        if requests != successes + errors:
            raise ValueError(f"Run {run_id}: requests != successes + errors")
        if elapsed <= 0 or not math.isfinite(elapsed):
            raise ValueError(f"Run {run_id}: total window duration must be finite and positive")
        row: dict[str, Any] = {
            "run_id": run_id,
            "scenario": scenario,
            "windows": len(windows),
            "requests": requests,
            "successes": successes,
            "errors": errors,
            "sample_count": samples,
            "window_duration_s": elapsed,
            "throughput_rps": successes / elapsed,
            "error_rate_pct": (100.0 * errors / requests) if requests else 0.0,
            "latency_windows": 0,
            "p99_minimum_tail_count_windows": sum(
                item.get("p99_status") == "minimum_tail_count_met" for item in windows
            ),
            "p999_minimum_tail_count_windows": sum(
                item.get("p999_status") == "minimum_tail_count_met" for item in windows
            ),
        }
        for field in NUMERIC_WINDOW_FIELDS:
            values = _finite_values(windows, field)
            row[f"median_window_{field}"] = statistics.median(values) if values else None
            row[f"max_window_{field}"] = max(values) if values else None
            if field == "p99_ms":
                row["latency_windows"] = len(values)
        summaries.append(row)
    return summaries


def write_summary_csv(rows: list[dict[str, Any]], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else []
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="results/raw/client_window.jsonl")
    parser.add_argument("--output", default="results/processed/run_summary.csv")
    args = parser.parse_args()
    records = load_jsonl(args.input)
    rows = aggregate_records(records)
    path = write_summary_csv(rows, args.output)
    print(f"Aggregated {len(records)} windows into {len(rows)} run summaries: {path}")
    print("Note: percentile columns are medians/maxima of window percentiles, not pooled run-wide percentiles.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
