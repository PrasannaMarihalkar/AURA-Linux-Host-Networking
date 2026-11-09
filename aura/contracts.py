"""AURA pure contract validation helpers and semantic invariant checks.

This module provides side-effect-free contract utilities conforming to Gate A
semantic freeze specifications. It contains no network calls, no HTTP server,
no background threads, and no privileged system modifications.
"""
from __future__ import annotations

import math
from typing import Any
import jsonschema
from jsonschema import Draft202012Validator


def calculate_nearest_rank_percentile(latencies: list[float], percentile: float) -> float:
    """Calculate the nearest-rank percentile for successful request latencies.

    Formula:
        rank = ceil(percentile * N)
        rank_bounded = max(1, min(rank, N))
        percentile_value = sorted_latencies[rank_bounded - 1] (1-indexed)

    Args:
        latencies: List of positive latency measurements in milliseconds.
        percentile: Target percentile in the half-open interval (0.0, 1.0].

    Raises:
        ValueError: If latencies list is empty, contains non-finite values,
                    or percentile is outside (0.0, 1.0].
    """
    if not latencies:
        raise ValueError("Cannot calculate percentile from an empty latency list")
    if not (0.0 < percentile <= 1.0):
        raise ValueError(f"Percentile must be in (0.0, 1.0], got {percentile}")

    for val in latencies:
        if not math.isfinite(val) or val < 0.0:
            raise ValueError(f"Latency values must be finite non-negative numbers, got {val}")

    sorted_latencies = sorted(latencies)
    n = len(sorted_latencies)
    rank = int(math.ceil(percentile * n))
    rank_bounded = max(1, min(rank, n))
    return sorted_latencies[rank_bounded - 1]


def evaluate_tail_statuses(sample_count: int) -> tuple[str, str]:
    """Evaluate AURA reporting guardrail statuses for P99 and P99.9.

    Thresholds:
        P99:
            >= 10,000: 'minimum_tail_count_met'
            >= 100:    'exploratory'
            < 100:     'insufficient'
        P99.9:
            >= 100,000: 'minimum_tail_count_met'
            >= 1,000:   'exploratory'
            < 1,000:    'insufficient'

    Returns:
        tuple[str, str]: (p99_status, p999_status)
    """
    if sample_count < 0:
        raise ValueError(f"sample_count cannot be negative, got {sample_count}")

    if sample_count >= 10_000:
        p99_status = "minimum_tail_count_met"
    elif sample_count >= 100:
        p99_status = "exploratory"
    else:
        p99_status = "insufficient"

    if sample_count >= 100_000:
        p999_status = "minimum_tail_count_met"
    elif sample_count >= 1_000:
        p999_status = "exploratory"
    else:
        p999_status = "insufficient"

    return p99_status, p999_status


def validate_client_window_invariants(
    requests: int,
    successes: int,
    errors: int,
    sample_count: int,
    window_duration_s: float,
    throughput_rps: float,
    tolerance: float = 1e-3,
) -> None:
    """Validate arithmetic and semantic invariants of a client measurement window.

    Invariants:
        1. requests == successes + errors
        2. sample_count <= successes
        3. window_duration_s > 0 and math.isfinite(window_duration_s)
        4. abs(throughput_rps - (successes / window_duration_s)) <= tolerance

    Raises:
        ValueError: If any invariant is violated.
    """
    if not (isinstance(requests, int) and isinstance(successes, int) and isinstance(errors, int)):
        raise TypeError("requests, successes, and errors must be integers")
    if requests < 0 or successes < 0 or errors < 0 or sample_count < 0:
        raise ValueError("Counts cannot be negative")

    if requests != successes + errors:
        raise ValueError(
            f"Invariant violation: requests ({requests}) != successes ({successes}) + errors ({errors})"
        )

    if sample_count > successes:
        raise ValueError(
            f"Invariant violation: sample_count ({sample_count}) cannot exceed successes ({successes})"
        )

    if not math.isfinite(window_duration_s) or window_duration_s <= 0.0:
        raise ValueError(f"window_duration_s must be strictly positive and finite, got {window_duration_s}")

    expected_throughput = successes / window_duration_s
    if not math.isclose(throughput_rps, expected_throughput, rel_tol=tolerance, abs_tol=1e-6):
        raise ValueError(
            f"Throughput calculation mismatch: reported {throughput_rps}, expected {expected_throughput}"
        )


def check_monotonic_freshness(
    server_monotonic_now: float,
    report_monotonic_s: float,
    current_server_boot_id: str,
    report_server_boot_id: str,
    max_report_age_s: float = 15.0,
) -> tuple[bool, float | None, str]:
    """Evaluate whether an ingested telemetry report is fresh within the active server boot session.

    Returns:
        tuple[bool, float | None, str]:
            (is_stale, report_age_s, reason_code)
    """
    if not current_server_boot_id or not report_server_boot_id:
        return True, None, "missing_boot_identity"

    if current_server_boot_id != report_server_boot_id:
        return True, None, "server_boot_id_mismatch"

    if not math.isfinite(server_monotonic_now) or not math.isfinite(report_monotonic_s):
        return True, None, "non_finite_monotonic_timestamp"

    age_s = server_monotonic_now - report_monotonic_s
    if age_s < 0.0:
        return True, age_s, "future_monotonic_timestamp"

    if age_s > max_report_age_s:
        return True, age_s, "report_age_exceeded"

    return False, age_s, "fresh"


def stamp_ingress_payload(
    ingress_payload: dict[str, Any],
    server_epoch_s: float,
    server_monotonic_s: float,
    server_boot_id: str,
    ingress_schema: dict[str, Any],
    ingested_schema: dict[str, Any],
) -> dict[str, Any]:
    """Pure contract transformation: validate ingress payload and enrich with server receipt metadata.

    Raises:
        jsonschema.ValidationError: If ingress or enriched record fails schema validation.
    """
    Draft202012Validator.check_schema(ingress_schema)
    Draft202012Validator.check_schema(ingested_schema)

    # 1. Validate ingress structure (verifies client did not provide server fields)
    jsonschema.validate(instance=ingress_payload, schema=ingress_schema, cls=Draft202012Validator)

    # 2. Enrich with server-owned receipt attributes
    enriched = dict(ingress_payload)
    enriched["server_received_at_epoch_s"] = server_epoch_s
    enriched["server_received_at_monotonic_s"] = server_monotonic_s
    enriched["server_boot_id"] = server_boot_id

    # 3. Validate enriched record matches ingested schema
    jsonschema.validate(instance=enriched, schema=ingested_schema, cls=Draft202012Validator)
    return enriched


def validate_action_guardrails(controller_mode: str, action_status: str) -> None:
    """Validate operational safety constraint: 'applied' is forbidden in observe_only or dry_run.

    Raises:
        ValueError: If action_status == 'applied' under observe_only or dry_run modes.
    """
    if controller_mode in ("observe_only", "dry_run") and action_status == "applied":
        raise ValueError(
            f"Operational safety violation: action status 'applied' is strictly prohibited in '{controller_mode}' mode."
        )
