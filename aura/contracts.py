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
        ValueError: If latencies list is empty, contains non-finite or negative values,
                    or percentile is outside (0.0, 1.0].
    """
    if not latencies:
        raise ValueError("Cannot calculate percentile from an empty latency list")
    if (
        isinstance(percentile, bool)
        or not isinstance(percentile, (int, float))
        or not math.isfinite(percentile)
        or not (0.0 < percentile <= 1.0)
    ):
        raise ValueError(f"Percentile must be in (0.0, 1.0], got {percentile}")

    for val in latencies:
        if (
            isinstance(val, bool)
            or not isinstance(val, (int, float))
            or not math.isfinite(val)
            or val < 0.0
        ):
            raise ValueError(f"Latency values must be finite non-negative numbers, got {val}")

    sorted_latencies = sorted(latencies)
    n = len(sorted_latencies)
    rank = int(math.ceil(percentile * n))
    rank_bounded = max(1, min(rank, n))
    return float(sorted_latencies[rank_bounded - 1])


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

    Raises:
        TypeError: If sample_count is not an integer or is a boolean.
        ValueError: If sample_count is negative.
    """
    if isinstance(sample_count, bool) or not isinstance(sample_count, int):
        raise TypeError(f"sample_count must be an integer, got {type(sample_count).__name__}")
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
        1. requests, successes, errors, and sample_count must be integers (booleans rejected).
        2. requests == successes + errors
        3. sample_count <= successes
        4. window_duration_s > 0 and math.isfinite(window_duration_s)
        5. throughput_rps >= 0, finite, and abs(throughput_rps - (successes / window_duration_s)) <= tolerance
        6. tolerance > 0 and finite

    Raises:
        TypeError: If counts or types are invalid (including boolean masquerading as int).
        ValueError: If arithmetic invariants, non-finite values, or ranges are violated.
    """
    for name, val in (
        ("requests", requests),
        ("successes", successes),
        ("errors", errors),
        ("sample_count", sample_count),
    ):
        if isinstance(val, bool) or not isinstance(val, int):
            raise TypeError(f"{name} must be an integer, got {type(val).__name__}")
        if val < 0:
            raise ValueError(f"{name} cannot be negative, got {val}")

    if requests != successes + errors:
        raise ValueError(
            f"Invariant violation: requests ({requests}) != successes ({successes}) + errors ({errors})"
        )

    if sample_count > successes:
        raise ValueError(
            f"Invariant violation: sample_count ({sample_count}) cannot exceed successes ({successes})"
        )

    if (
        isinstance(window_duration_s, bool)
        or not isinstance(window_duration_s, (int, float))
        or not math.isfinite(window_duration_s)
        or window_duration_s <= 0.0
    ):
        raise ValueError(f"window_duration_s must be strictly positive and finite, got {window_duration_s}")

    if (
        isinstance(throughput_rps, bool)
        or not isinstance(throughput_rps, (int, float))
        or not math.isfinite(throughput_rps)
        or throughput_rps < 0.0
    ):
        raise ValueError(f"throughput_rps must be a finite non-negative number, got {throughput_rps}")

    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(tolerance)
        or tolerance <= 0.0
    ):
        raise ValueError(f"tolerance must be a finite positive number, got {tolerance}")

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

    Validates that max_report_age_s is a finite non-negative number.
    Safely rejects invalid timestamps and boot identities without crashing.

    Returns:
        tuple[bool, float | None, str]:
            (is_stale, report_age_s, reason_code)

    Raises:
        ValueError: If max_report_age_s is not a finite non-negative number.
    """
    if (
        isinstance(max_report_age_s, bool)
        or not isinstance(max_report_age_s, (int, float))
        or not math.isfinite(max_report_age_s)
        or max_report_age_s < 0.0
    ):
        raise ValueError(f"max_report_age_s must be a finite non-negative number, got {max_report_age_s}")

    # Safely reject missing, non-string, or blank boot identities
    if (
        not isinstance(current_server_boot_id, str)
        or not isinstance(report_server_boot_id, str)
        or not current_server_boot_id.strip()
        or not report_server_boot_id.strip()
    ):
        return True, None, "missing_boot_identity"

    if current_server_boot_id.strip() != report_server_boot_id.strip():
        return True, None, "server_boot_id_mismatch"

    # Safely reject boolean, non-numeric, or negative monotonic timestamps
    if (
        isinstance(server_monotonic_now, bool)
        or isinstance(report_monotonic_s, bool)
        or not isinstance(server_monotonic_now, (int, float))
        or not isinstance(report_monotonic_s, (int, float))
    ):
        return True, None, "invalid_monotonic_timestamp"

    if not math.isfinite(server_monotonic_now) or not math.isfinite(report_monotonic_s):
        return True, None, "non_finite_monotonic_timestamp"

    if server_monotonic_now < 0.0 or report_monotonic_s < 0.0:
        return True, None, "invalid_monotonic_timestamp"

    age_s = float(server_monotonic_now - report_monotonic_s)
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
    """Pure, side-effect-free contract transformation helper for payload enrichment.

    NOTE: This is NOT a production HTTP server implementation or endpoint handler.
    It performs purely memory-resident schema validation and metadata injection
    for testing and contract verification. In Gate B, the actual HTTP server
    (aura/server.py) will receive network requests on POST /report, generate
    active timestamps, and use this contract logic.

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
