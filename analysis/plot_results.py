"""Plot measured AURA window latency, throughput and error rate.

The plots use observed per-window records directly. No smoothing, interpolation,
synthetic observations or pooling of per-window quantiles is performed.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def load_records(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            item = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if not isinstance(item, dict):
                raise ValueError(f"Expected JSON object at line {number}")
            records.append(item)
    if not records:
        raise ValueError(f"No records found in {path}")
    return records


def load_latency_samples(path: str | Path) -> dict[str, list[float]]:
    """Load raw successful request latencies from the sidecar CSV."""
    grouped: dict[str, list[float]] = defaultdict(list)
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        for number, row in enumerate(csv.DictReader(handle), start=2):
            try:
                value = float(row["latency_ms"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Invalid latency sample at {path}:{number}") from exc
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Latency must be finite and non-negative at {path}:{number}")
            grouped[str(row.get("run_id", "unknown"))].append(value)
    return grouped


def plot_records(
    records: list[dict[str, Any]],
    output_dir: str | Path,
    latency_samples: dict[str, list[float]] | None = None,
) -> list[Path]:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("Plotting requires matplotlib. Install with: pip install -r requirements-analysis.txt") from exc

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in records:
        groups[str(item.get("run_id", "unknown"))].append(item)
    for items in groups.values():
        items.sort(key=lambda item: int(item.get("window_id", 0)))

    # 1. Latency percentiles across actual windows.
    fig, ax = plt.subplots(figsize=(10, 5.5))
    for run_id, items in sorted(groups.items()):
        xs = [int(item["window_id"]) for item in items]
        for field, label in (("p50_ms", "P50"), ("p95_ms", "P95"), ("p99_ms", "P99"), ("p999_ms", "P99.9")):
            ys = [item.get(field) for item in items]
            if any(value is not None for value in ys):
                ax.plot(xs, ys, marker="o", linewidth=1.2, label=f"{run_id} — {label}")
    ax.set_title("Observed latency percentiles by measurement window")
    ax.set_xlabel("Window ID")
    ax.set_ylabel("Latency (ms)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize="small", ncol=2)
    fig.tight_layout()
    latency_png = output / "latency_percentiles_by_window.png"
    latency_pdf = output / "latency_percentiles_by_window.pdf"
    fig.savefig(latency_png, dpi=160)
    fig.savefig(latency_pdf)
    plt.close(fig)

    # 2. Successful request throughput by actual measurement window.
    fig, ax = plt.subplots(figsize=(10, 5.5))
    for run_id, items in sorted(groups.items()):
        xs = [int(item["window_id"]) for item in items]
        ys = [float(item.get("throughput_rps", 0.0)) for item in items]
        ax.plot(xs, ys, marker="o", linewidth=1.2, label=run_id)
    ax.set_title("Observed successful throughput by measurement window")
    ax.set_xlabel("Window ID")
    ax.set_ylabel("Successful requests per second")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize="small")
    fig.tight_layout()
    throughput_png = output / "throughput_by_window.png"
    throughput_pdf = output / "throughput_by_window.pdf"
    fig.savefig(throughput_png, dpi=160)
    fig.savefig(throughput_pdf)
    plt.close(fig)

    # 3. Per-window terminal request error rate.
    fig, ax = plt.subplots(figsize=(10, 5.5))
    for run_id, items in sorted(groups.items()):
        xs = [int(item["window_id"]) for item in items]
        rates = []
        for item in items:
            total = int(item.get("requests", 0))
            errors = int(item.get("errors", 0))
            rates.append(100.0 * errors / total if total else 0.0)
        ax.plot(xs, rates, marker="o", linewidth=1.2, label=run_id)
    ax.set_title("Observed request error rate by measurement window")
    ax.set_xlabel("Window ID")
    ax.set_ylabel("Errors (%)")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize="small")
    fig.tight_layout()
    error_png = output / "error_rate_by_window.png"
    error_pdf = output / "error_rate_by_window.pdf"
    fig.savefig(error_png, dpi=160)
    fig.savefig(error_pdf)
    plt.close(fig)

    generated = [latency_png, latency_pdf, throughput_png, throughput_pdf, error_png, error_pdf]

    # True latency distribution plots require the raw successful observations.
    # Window quantiles alone cannot reconstruct the original sample distribution.
    if latency_samples:
        non_empty = {run: sorted(values) for run, values in latency_samples.items() if values}
        if non_empty:
            fig, ax = plt.subplots(figsize=(10, 5.5))
            for run_id, values in sorted(non_empty.items()):
                bins = max(10, min(100, int(math.sqrt(len(values))) or 10))
                ax.hist(values, bins=bins, alpha=0.45, label=f"{run_id} (n={len(values)})")
            ax.set_title("Empirical successful-request latency distribution")
            ax.set_xlabel("Latency (ms)")
            ax.set_ylabel("Observed requests")
            ax.grid(True, axis="y", alpha=0.3)
            ax.legend(fontsize="small")
            fig.tight_layout()
            histogram_png = output / "latency_distribution_histogram.png"
            histogram_pdf = output / "latency_distribution_histogram.pdf"
            fig.savefig(histogram_png, dpi=160)
            fig.savefig(histogram_pdf)
            plt.close(fig)

            fig, ax = plt.subplots(figsize=(10, 5.5))
            for run_id, values in sorted(non_empty.items()):
                ys = [(index + 1) / len(values) for index in range(len(values))]
                ax.step(values, ys, where="post", label=f"{run_id} (n={len(values)})")
            ax.set_title("Empirical CDF of successful-request latency")
            ax.set_xlabel("Latency (ms)")
            ax.set_ylabel("Cumulative probability")
            ax.set_ylim(0, 1.01)
            ax.grid(True, alpha=0.3)
            ax.legend(fontsize="small")
            fig.tight_layout()
            cdf_png = output / "latency_distribution_ecdf.png"
            cdf_pdf = output / "latency_distribution_ecdf.pdf"
            fig.savefig(cdf_png, dpi=160)
            fig.savefig(cdf_pdf)
            plt.close(fig)
            generated.extend([histogram_png, histogram_pdf, cdf_png, cdf_pdf])
    return generated


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="results/raw/client_window.jsonl")
    parser.add_argument("--output-dir", default="results/plots")
    parser.add_argument("--latency-samples", default=None, help="Raw successful-latency CSV sidecar for histogram and ECDF plots")
    args = parser.parse_args()
    raw_samples = load_latency_samples(args.latency_samples) if args.latency_samples else None
    generated = plot_records(load_records(args.input), args.output_dir, raw_samples)
    for path in generated:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
