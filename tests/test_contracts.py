"""Comprehensive unit and semantic specification tests for AURA shared JSON contracts.

Tests cover Gate A semantic freeze specifications:
- JSON Schema Draft 2020-12 metaschema compliance for all schemas and fixtures.
- Strict rejection of JSON constants (NaN, Infinity, -Infinity).
- Client window invariants (requests = successes + errors, sample_count <= successes, throughput).
- Nearest-rank percentile calculation and sample ordering across boundary conditions.
- Reporting guardrails for tail percentiles (p99_status, p999_status).
- Two-stage ingestion: strict rejection of server fields on ingress, required fields on enriched.
- Monotonic clock freshness and server boot identity continuity verification.
- Host telemetry counter deltas, CPU PSI 'some' preservation, structured missing_fields.
- Operational action safety guardrails: strict prohibition of 'applied' in observe/dry-run modes.
"""
from __future__ import annotations

import json
import math
import random
import uuid
from pathlib import Path
import pytest
import jsonschema
from jsonschema import Draft202012Validator

from aura.contracts import (
    calculate_nearest_rank_percentile,
    evaluate_tail_statuses,
    validate_client_window_invariants,
    check_monotonic_freshness,
    stamp_ingress_payload,
    validate_action_guardrails,
)

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"

SCHEMA_FILES = [
    "client_window.schema.json",
    "client_window_ingested.schema.json",
    "host_snapshot.schema.json",
    "controller_event.schema.json",
    "action_record.schema.json",
]


def load_strict_json(path: Path) -> dict:
    """Load JSON file strictly rejecting NaN, Infinity, and -Infinity."""
    def _reject_constant(val: str) -> None:
        raise ValueError(f"JSON constant '{val}' is forbidden in AURA contracts")

    with path.open("r", encoding="utf-8") as f:
        return json.load(f, parse_constant=_reject_constant)


def load_schema(name: str) -> dict:
    return load_strict_json(CONTRACTS_DIR / name)


def load_example(name: str) -> dict:
    return load_strict_json(EXAMPLES_DIR / name)


# ============================================================================
# 1. Metaschema & Example Validation
# ============================================================================

@pytest.mark.parametrize("schema_name", SCHEMA_FILES)
def test_schema_syntax_and_metaschema(schema_name: str) -> None:
    """Verify each schema is syntactically valid JSON and complies with Draft 2020-12."""
    schema = load_schema(schema_name)
    Draft202012Validator.check_schema(schema)


def test_client_window_ingress_example_valid() -> None:
    """Verify client_window_ingress.example.json complies with client_window.schema.json."""
    schema = load_schema("client_window.schema.json")
    example = load_example("client_window_ingress.example.json")
    jsonschema.validate(instance=example, schema=schema, cls=Draft202012Validator)


def test_client_window_ingested_example_valid() -> None:
    """Verify client_window_ingested.example.json complies with client_window_ingested.schema.json."""
    schema = load_schema("client_window_ingested.schema.json")
    example = load_example("client_window_ingested.example.json")
    jsonschema.validate(instance=example, schema=schema, cls=Draft202012Validator)


def test_host_snapshot_example_valid() -> None:
    """Verify host_snapshot.example.json complies with host_snapshot.schema.json."""
    schema = load_schema("host_snapshot.schema.json")
    example = load_example("host_snapshot.example.json")
    jsonschema.validate(instance=example, schema=schema, cls=Draft202012Validator)


def test_controller_event_example_valid() -> None:
    """Verify controller_event.example.json complies with controller_event.schema.json."""
    schema = load_schema("controller_event.schema.json")
    example = load_example("controller_event.example.json")
    jsonschema.validate(instance=example, schema=schema, cls=Draft202012Validator)


def test_action_record_example_valid() -> None:
    """Verify action_record.example.json complies with action_record.schema.json."""
    schema = load_schema("action_record.schema.json")
    example = load_example("action_record.example.json")
    jsonschema.validate(instance=example, schema=schema, cls=Draft202012Validator)


# ============================================================================
# 2. Strict JSON Parsing Tests (Rejecting NaN / Infinity)
# ============================================================================

def test_json_rejects_nan_and_infinity() -> None:
    """Ensure JSON parser strictly rejects NaN and Infinity constants."""
    def _reject_constant(val: str) -> None:
        raise ValueError(f"Forbidden constant: {val}")

    with pytest.raises(ValueError, match="Forbidden constant"):
        json.loads('{"p99_ms": NaN}', parse_constant=_reject_constant)

    with pytest.raises(ValueError, match="Forbidden constant"):
        json.loads('{"p99_ms": Infinity}', parse_constant=_reject_constant)

    with pytest.raises(ValueError, match="Forbidden constant"):
        json.loads('{"p99_ms": -Infinity}', parse_constant=_reject_constant)


# ============================================================================
# 3. Client Benchmark Semantic Invariants & Negative Cases
# ============================================================================

def test_requests_equals_successes_plus_errors_valid() -> None:
    """Verify requests = successes + errors holds for example."""
    example = load_example("client_window_ingress.example.json")
    validate_client_window_invariants(
        requests=example["requests"],
        successes=example["successes"],
        errors=example["errors"],
        sample_count=example["sample_count"],
        window_duration_s=example["window_duration_s"],
        throughput_rps=example["throughput_rps"],
    )


def test_requests_mismatch_negative_case() -> None:
    """Negative test: verify requests != successes + errors is rejected."""
    with pytest.raises(ValueError, match="Invariant violation: requests"):
        validate_client_window_invariants(
            requests=5000,
            successes=4900,
            errors=50,  # 4900 + 50 = 4950 != 5000
            sample_count=4900,
            window_duration_s=10.0,
            throughput_rps=490.0,
        )


def test_sample_count_cannot_exceed_successes() -> None:
    """Verify sample_count <= successes constraint."""
    # Valid: sample_count == successes
    validate_client_window_invariants(5000, 4998, 2, 4998, 10.0, 499.8)

    # Valid: partial sample where some successful requests had lost latency timers
    validate_client_window_invariants(5000, 4998, 2, 4950, 10.0, 499.8)

    # Negative test: sample_count > successes must raise ValueError
    with pytest.raises(ValueError, match="sample_count .* cannot exceed successes"):
        validate_client_window_invariants(5000, 4998, 2, 5001, 10.0, 499.8)


def test_throughput_calculation_with_tolerance() -> None:
    """Verify throughput_rps = successes / window_duration_s with documented float tolerance."""
    # Valid exact
    validate_client_window_invariants(1000, 1000, 0, 1000, 10.0, 100.0)

    # Valid within 1e-3 relative tolerance
    validate_client_window_invariants(1000, 1000, 0, 1000, 10.0, 100.05, tolerance=1e-3)

    # Negative test: throughput mismatch beyond tolerance
    with pytest.raises(ValueError, match="Throughput calculation mismatch"):
        validate_client_window_invariants(1000, 1000, 0, 1000, 10.0, 150.0, tolerance=1e-3)


@pytest.mark.parametrize("invalid_duration", [0.0, -1.0, -10.5, float("inf"), float("-inf"), float("nan")])
def test_invalid_duration_negative_cases(invalid_duration: float) -> None:
    """Negative test: verify non-positive or non-finite window durations are rejected."""
    with pytest.raises(ValueError, match="window_duration_s must be strictly positive and finite"):
        validate_client_window_invariants(
            requests=100,
            successes=100,
            errors=0,
            sample_count=100,
            window_duration_s=invalid_duration,
            throughput_rps=10.0,
        )


def test_client_window_invariants_rejects_non_integers_and_booleans() -> None:
    """Negative tests: verify boolean masquerading as int or float types are rejected for counts."""
    # Boolean requests
    with pytest.raises(TypeError, match="requests must be an integer"):
        validate_client_window_invariants(True, 1, 0, 1, 1.0, 1.0)

    # Boolean successes
    with pytest.raises(TypeError, match="successes must be an integer"):
        validate_client_window_invariants(1, True, 0, 1, 1.0, 1.0)

    # Boolean errors
    with pytest.raises(TypeError, match="errors must be an integer"):
        validate_client_window_invariants(1, 1, False, 1, 1.0, 1.0)

    # Boolean sample_count
    with pytest.raises(TypeError, match="sample_count must be an integer"):
        validate_client_window_invariants(10, 10, 0, True, 1.0, 10.0)

    # Float sample_count
    with pytest.raises(TypeError, match="sample_count must be an integer"):
        validate_client_window_invariants(10, 10, 0, 10.0, 1.0, 10.0)

    # Float requests
    with pytest.raises(TypeError, match="requests must be an integer"):
        validate_client_window_invariants(10.5, 10, 0, 10, 1.0, 10.0)


def test_client_window_invariants_rejects_negative_counts() -> None:
    """Negative tests: verify negative values are rejected for all count parameters."""
    with pytest.raises(ValueError, match="requests cannot be negative"):
        validate_client_window_invariants(-1, 0, 0, 0, 1.0, 0.0)

    with pytest.raises(ValueError, match="successes cannot be negative"):
        validate_client_window_invariants(0, -1, 1, 0, 1.0, 0.0)

    with pytest.raises(ValueError, match="errors cannot be negative"):
        validate_client_window_invariants(0, 0, -1, 0, 1.0, 0.0)

    with pytest.raises(ValueError, match="sample_count cannot be negative"):
        validate_client_window_invariants(10, 10, 0, -1, 1.0, 10.0)


@pytest.mark.parametrize("bad_throughput", [float("nan"), float("inf"), float("-inf"), -5.0, True])
def test_client_window_invariants_rejects_invalid_throughput(bad_throughput) -> None:
    """Negative tests: verify non-finite, negative, or boolean throughput is rejected."""
    with pytest.raises(ValueError, match="throughput_rps must be a finite non-negative number"):
        validate_client_window_invariants(100, 100, 0, 100, 10.0, bad_throughput)


@pytest.mark.parametrize("bad_tolerance", [0.0, -1e-3, float("nan"), float("inf"), True])
def test_client_window_invariants_rejects_invalid_tolerance(bad_tolerance) -> None:
    """Negative tests: verify non-positive, non-finite, or boolean tolerance is rejected."""
    with pytest.raises(ValueError, match="tolerance must be a finite positive number"):
        validate_client_window_invariants(100, 100, 0, 100, 10.0, 10.0, tolerance=bad_tolerance)


# ============================================================================
# 4. Nearest-Rank Percentile & Tail Reporting Guardrail Tests
# ============================================================================

def test_nearest_rank_percentile_boundary_and_ordering() -> None:
    """Verify nearest-rank percentile across boundary cases and unsorted inputs."""
    # Boundary: N = 1
    assert calculate_nearest_rank_percentile([42.5], 0.50) == 42.5
    assert calculate_nearest_rank_percentile([42.5], 0.99) == 42.5

    # Known sequence: 100 elements (1.0 to 100.0)
    samples = [float(i) for i in range(1, 101)]
    assert calculate_nearest_rank_percentile(samples, 0.50) == 50.0
    assert calculate_nearest_rank_percentile(samples, 0.95) == 95.0
    assert calculate_nearest_rank_percentile(samples, 0.99) == 99.0
    assert calculate_nearest_rank_percentile(samples, 1.00) == 100.0

    # Unsorted input produces identical deterministic result
    shuffled = list(samples)
    random.seed(12345)
    random.shuffle(shuffled)
    assert calculate_nearest_rank_percentile(shuffled, 0.99) == 99.0

    # Non-integer rank: N = 10, P95: ceil(0.95 * 10) = ceil(9.5) = 10 -> 10th sample
    small_samples = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert calculate_nearest_rank_percentile(small_samples, 0.95) == 10.0

    # Negative cases: invalid percentiles
    with pytest.raises(ValueError, match="Percentile must be in"):
        calculate_nearest_rank_percentile(samples, 0.0)
    with pytest.raises(ValueError, match="Percentile must be in"):
        calculate_nearest_rank_percentile(samples, 1.05)
    with pytest.raises(ValueError, match="Cannot calculate percentile from an empty"):
        calculate_nearest_rank_percentile([], 0.50)


def test_p99_and_p999_status_transitions() -> None:
    """Verify P99 and P99.9 reporting guardrail threshold transitions at every boundary."""
    # Boundary: sample_count < 100
    assert evaluate_tail_statuses(0) == ("insufficient", "insufficient")
    assert evaluate_tail_statuses(99) == ("insufficient", "insufficient")

    # Boundary: 100 <= sample_count < 1,000
    assert evaluate_tail_statuses(100) == ("exploratory", "insufficient")
    assert evaluate_tail_statuses(999) == ("exploratory", "insufficient")

    # Boundary: 1,000 <= sample_count < 10,000
    assert evaluate_tail_statuses(1000) == ("exploratory", "exploratory")
    assert evaluate_tail_statuses(4998) == ("exploratory", "exploratory")
    assert evaluate_tail_statuses(9999) == ("exploratory", "exploratory")

    # Boundary: 10,000 <= sample_count < 100,000
    assert evaluate_tail_statuses(10000) == ("minimum_tail_count_met", "exploratory")
    assert evaluate_tail_statuses(99999) == ("minimum_tail_count_met", "exploratory")

    # Boundary: sample_count >= 100,000
    assert evaluate_tail_statuses(100000) == ("minimum_tail_count_met", "minimum_tail_count_met")
    assert evaluate_tail_statuses(250000) == ("minimum_tail_count_met", "minimum_tail_count_met")


def test_sample_status_for_4998_example() -> None:
    """Verify the 4,998 successful sample example has 'exploratory' for both P99 and P99.9."""
    example = load_example("client_window_ingress.example.json")
    assert example["sample_count"] == 4998
    p99_status, p999_status = evaluate_tail_statuses(example["sample_count"])
    assert p99_status == "exploratory"
    assert p999_status == "exploratory"
    assert example["p99_status"] == p99_status
    assert example["p999_status"] == p999_status


def test_evaluate_tail_statuses_type_and_value_hardening() -> None:
    """Verify evaluate_tail_statuses rejects booleans, floats, strings, and negative values."""
    with pytest.raises(TypeError, match="sample_count must be an integer"):
        evaluate_tail_statuses(True)

    with pytest.raises(TypeError, match="sample_count must be an integer"):
        evaluate_tail_statuses(False)

    with pytest.raises(TypeError, match="sample_count must be an integer"):
        evaluate_tail_statuses(100.5)

    with pytest.raises(TypeError, match="sample_count must be an integer"):
        evaluate_tail_statuses("1000")

    with pytest.raises(ValueError, match="sample_count cannot be negative"):
        evaluate_tail_statuses(-1)


def test_nearest_rank_percentile_rejects_booleans_and_invalid_values() -> None:
    """Verify calculate_nearest_rank_percentile rejects booleans and non-finite values."""
    with pytest.raises(ValueError, match="Percentile must be in"):
        calculate_nearest_rank_percentile([10.0, 20.0], True)

    with pytest.raises(ValueError, match="Percentile must be in"):
        calculate_nearest_rank_percentile([10.0, 20.0], float("nan"))

    with pytest.raises(ValueError, match="Latency values must be finite non-negative"):
        calculate_nearest_rank_percentile([10.0, True], 0.99)

    with pytest.raises(ValueError, match="Latency values must be finite non-negative"):
        calculate_nearest_rank_percentile([10.0, -1.0], 0.99)


# ============================================================================
# 5. Two-Stage Ingestion Schema Rejection & Stamping Tests
# ============================================================================

def test_client_ingress_rejects_server_owned_fields() -> None:
    """Negative tests: verify ingress schema strictly rejects any server-owned receipt metadata."""
    ingress_schema = load_schema("client_window.schema.json")
    example = load_example("client_window_ingress.example.json")

    # Reject server_received_at_epoch_s
    bad_epoch = dict(example)
    bad_epoch["server_received_at_epoch_s"] = 1794230000.0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_epoch, schema=ingress_schema, cls=Draft202012Validator)

    # Reject server_received_at_monotonic_s
    bad_mono = dict(example)
    bad_mono["server_received_at_monotonic_s"] = 1420.0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_mono, schema=ingress_schema, cls=Draft202012Validator)

    # Reject server_boot_id
    bad_boot = dict(example)
    bad_boot["server_boot_id"] = str(uuid.uuid4())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_boot, schema=ingress_schema, cls=Draft202012Validator)

    # Reject all three
    bad_all = dict(example)
    bad_all["server_received_at_epoch_s"] = 1794230000.0
    bad_all["server_received_at_monotonic_s"] = 1420.0
    bad_all["server_boot_id"] = str(uuid.uuid4())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_all, schema=ingress_schema, cls=Draft202012Validator)


def test_server_ingested_schema_requires_server_fields() -> None:
    """Negative tests: verify server-enriched schema requires all three receipt fields."""
    ingested_schema = load_schema("client_window_ingested.schema.json")
    example = load_example("client_window_ingested.example.json")

    # Missing server_received_at_epoch_s must fail
    missing_epoch = dict(example)
    del missing_epoch["server_received_at_epoch_s"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=missing_epoch, schema=ingested_schema, cls=Draft202012Validator)

    # Missing server_received_at_monotonic_s must fail
    missing_mono = dict(example)
    del missing_mono["server_received_at_monotonic_s"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=missing_mono, schema=ingested_schema, cls=Draft202012Validator)

    # Missing server_boot_id must fail
    missing_boot = dict(example)
    del missing_boot["server_boot_id"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=missing_boot, schema=ingested_schema, cls=Draft202012Validator)


def test_stamp_ingress_payload_helper() -> None:
    """Verify pure stamp_ingress_payload helper transforms ingress to valid enriched payload."""
    ingress_schema = load_schema("client_window.schema.json")
    ingested_schema = load_schema("client_window_ingested.schema.json")
    ingress_record = load_example("client_window_ingress.example.json")

    server_epoch_now = 1794230050.123
    server_monotonic_now = 1450.456
    server_boot_id = str(uuid.uuid4())

    enriched = stamp_ingress_payload(
        ingress_payload=ingress_record,
        server_epoch_s=server_epoch_now,
        server_monotonic_s=server_monotonic_now,
        server_boot_id=server_boot_id,
        ingress_schema=ingress_schema,
        ingested_schema=ingested_schema,
    )
    assert enriched["run_id"] == ingress_record["run_id"]
    assert enriched["window_id"] == ingress_record["window_id"]
    assert enriched["server_received_at_epoch_s"] == server_epoch_now
    assert enriched["server_received_at_monotonic_s"] == server_monotonic_now
    assert enriched["server_boot_id"] == server_boot_id


# ============================================================================
# 6. Monotonic Freshness & Boot Identity Continuity Tests
# ============================================================================

def test_monotonic_freshness_boot_continuity() -> None:
    """Verify monotonic freshness checks enforce same server boot identity."""
    current_boot_id = "boot-session-1234"
    diff_boot_id = "boot-session-5678"

    # Valid: same boot id and age <= 15s -> fresh
    is_stale, age_s, reason = check_monotonic_freshness(
        server_monotonic_now=110.0,
        report_monotonic_s=100.0,
        current_server_boot_id=current_boot_id,
        report_server_boot_id=current_boot_id,
        max_report_age_s=15.0,
    )
    assert is_stale is False
    assert age_s == 10.0
    assert reason == "fresh"

    # Mismatch: different boot id -> immediately stale regardless of monotonic delta
    is_stale, age_s, reason = check_monotonic_freshness(
        server_monotonic_now=102.0,
        report_monotonic_s=100.0,
        current_server_boot_id=current_boot_id,
        report_server_boot_id=diff_boot_id,
        max_report_age_s=15.0,
    )
    assert is_stale is True
    assert age_s is None
    assert reason == "server_boot_id_mismatch"

    # Expired age: age > 15s -> stale
    is_stale, age_s, reason = check_monotonic_freshness(
        server_monotonic_now=130.0,
        report_monotonic_s=100.0,
        current_server_boot_id=current_boot_id,
        report_server_boot_id=current_boot_id,
        max_report_age_s=15.0,
    )
    assert is_stale is True
    assert age_s == 30.0
    assert reason == "report_age_exceeded"

    # Future monotonic timestamp -> stale and report_age_s is None
    is_stale, age_s, reason = check_monotonic_freshness(
        server_monotonic_now=90.0,
        report_monotonic_s=100.0,
        current_server_boot_id=current_boot_id,
        report_server_boot_id=current_boot_id,
        max_report_age_s=15.0,
    )
    assert is_stale is True
    assert age_s is None
    assert reason == "future_monotonic_timestamp"


@pytest.mark.parametrize("bad_max_age", [-1.0, -0.01, float("nan"), float("inf"), float("-inf"), True])
def test_check_monotonic_freshness_rejects_invalid_max_report_age(bad_max_age) -> None:
    """Verify check_monotonic_freshness raises ValueError for invalid max_report_age_s."""
    with pytest.raises(ValueError, match="max_report_age_s must be a finite non-negative number"):
        check_monotonic_freshness(
            server_monotonic_now=100.0,
            report_monotonic_s=90.0,
            current_server_boot_id="boot-1",
            report_server_boot_id="boot-1",
            max_report_age_s=bad_max_age,
        )


def test_check_monotonic_freshness_safely_rejects_invalid_timestamps_and_boot_ids() -> None:
    """Verify check_monotonic_freshness safely handles invalid timestamps and boot IDs without crashing."""
    valid_boot = "boot-1234"

    # Missing or blank or non-string current_server_boot_id
    assert check_monotonic_freshness(100.0, 95.0, "", valid_boot) == (True, None, "missing_boot_identity")
    assert check_monotonic_freshness(100.0, 95.0, "   ", valid_boot) == (True, None, "missing_boot_identity")
    assert check_monotonic_freshness(100.0, 95.0, None, valid_boot) == (True, None, "missing_boot_identity")

    # Missing or blank or non-string report_server_boot_id
    assert check_monotonic_freshness(100.0, 95.0, valid_boot, "") == (True, None, "missing_boot_identity")
    assert check_monotonic_freshness(100.0, 95.0, valid_boot, "   ") == (True, None, "missing_boot_identity")
    assert check_monotonic_freshness(100.0, 95.0, valid_boot, None) == (True, None, "missing_boot_identity")

    # Boolean timestamps
    assert check_monotonic_freshness(True, 95.0, valid_boot, valid_boot) == (True, None, "invalid_monotonic_timestamp")
    assert check_monotonic_freshness(100.0, False, valid_boot, valid_boot) == (True, None, "invalid_monotonic_timestamp")

    # Negative timestamps
    assert check_monotonic_freshness(-10.0, 95.0, valid_boot, valid_boot) == (True, None, "invalid_monotonic_timestamp")
    assert check_monotonic_freshness(100.0, -5.0, valid_boot, valid_boot) == (True, None, "invalid_monotonic_timestamp")

    # Non-finite timestamps
    assert check_monotonic_freshness(float("nan"), 95.0, valid_boot, valid_boot) == (True, None, "non_finite_monotonic_timestamp")
    assert check_monotonic_freshness(100.0, float("inf"), valid_boot, valid_boot) == (True, None, "non_finite_monotonic_timestamp")


# ============================================================================
# 7. Host Telemetry Counter Handling (Reset, Hotplug, Missing Fields)
# ============================================================================

def test_counter_reset_and_structured_missing_fields() -> None:
    """Verify counter rollover/reset sets delta to null with reason and logs structured missing_fields."""
    schema = load_schema("host_snapshot.schema.json")
    snapshot = load_example("host_snapshot.example.json")

    # Simulate CPU 1 counter reset (e.g. counter overflow or driver reload)
    snapshot["net_rx_softirq_delta_per_cpu"] = [
        {"cpu_id": 0, "delta": 4820, "reason": None},
        {"cpu_id": 1, "delta": None, "reason": "counter_reset_detected"},
    ]
    snapshot["missing_fields"].append({
        "metric": "net_rx_softirq_delta_per_cpu[cpu_1]",
        "reason": "counter_reset_detected"
    })

    jsonschema.validate(instance=snapshot, schema=schema, cls=Draft202012Validator)


def test_cpu_hotplug_handling() -> None:
    """Verify CPU hotplug appearance or offline event is handled safely with structured reasons."""
    schema = load_schema("host_snapshot.schema.json")
    snapshot = load_example("host_snapshot.example.json")

    # Core 2 hotplugged online without baseline
    snapshot["net_rx_softirq_delta_per_cpu"] = [
        {"cpu_id": 0, "delta": 4820, "reason": None},
        {"cpu_id": 1, "delta": 115, "reason": None},
        {"cpu_id": 2, "delta": None, "reason": "baseline_missing_hotplug"},
    ]
    snapshot["missing_fields"].append({
        "metric": "net_rx_softirq_delta_per_cpu[cpu_2]",
        "reason": "baseline_missing_hotplug"
    })

    jsonschema.validate(instance=snapshot, schema=schema, cls=Draft202012Validator)


def test_unstructured_missing_fields_negative_case() -> None:
    """Negative test: verify plain string in missing_fields fails schema validation."""
    schema = load_schema("host_snapshot.schema.json")
    snapshot = load_example("host_snapshot.example.json")

    # Plain string instead of structured {"metric": ..., "reason": ...}
    snapshot["missing_fields"] = ["cpu_psi_some_avg10_pct"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=snapshot, schema=schema, cls=Draft202012Validator)


def test_cpu_psi_full_excluded_from_system_telemetry() -> None:
    """Verify system-level CPU PSI full metrics are removed from host_snapshot schema."""
    schema = load_schema("host_snapshot.schema.json")
    assert "cpu_psi_full_avg10_pct" not in schema["properties"], "System-level CPU PSI full must be excluded"
    assert "cpu_psi_full_total_us" not in schema["properties"], "System-level CPU PSI full must be excluded"
    assert "cpu_psi_some_avg10_pct" in schema["properties"]
    assert "cpu_psi_some_avg60_pct" in schema["properties"]
    assert "cpu_psi_some_avg300_pct" in schema["properties"]
    assert "cpu_psi_some_total_us" in schema["properties"]


# ============================================================================
# 8. Controller Safety & Action Guardrails
# ============================================================================

def test_rejection_of_applied_action_in_observe_only_mode() -> None:
    """Verify action status 'applied' is strictly rejected when controller_mode is observe_only."""
    schema = load_schema("action_record.schema.json")
    record = load_example("action_record.example.json")

    record["controller_mode"] = "observe_only"
    record["action"]["status"] = "applied"

    # Schema must reject via allOf conditional
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=record, schema=schema, cls=Draft202012Validator)

    # Pure validation helper must reject
    with pytest.raises(ValueError, match="Operational safety violation"):
        validate_action_guardrails("observe_only", "applied")


def test_rejection_of_applied_action_in_dry_run_mode() -> None:
    """Verify action status 'applied' is strictly rejected when controller_mode is dry_run."""
    schema = load_schema("action_record.schema.json")
    record = load_example("action_record.example.json")

    record["controller_mode"] = "dry_run"
    record["action"]["status"] = "applied"

    # Schema must reject via allOf conditional
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=record, schema=schema, cls=Draft202012Validator)

    # Pure validation helper must reject
    with pytest.raises(ValueError, match="Operational safety violation"):
        validate_action_guardrails("dry_run", "applied")


def test_valid_action_record_under_apply_mode() -> None:
    """Verify action status 'applied' is permitted only under 'apply' mode with valid readback evidence."""
    schema = load_schema("action_record.schema.json")
    record = load_example("action_record.example.json")

    record["controller_mode"] = "apply"
    record["action"]["status"] = "applied"
    record["action"]["before"] = {"aura-app.slice": 100}
    record["action"]["requested"] = {"aura-app.slice": 200}
    record["action"]["after"] = {"aura-app.slice": 200}
    record["action"]["rollback_attempted"] = False
    record["action"]["rollback_verified"] = False

    # Valid under apply mode
    jsonschema.validate(instance=record, schema=schema, cls=Draft202012Validator)
    validate_action_guardrails(
        controller_mode="apply",
        action_status="applied",
        before=record["action"]["before"],
        requested=record["action"]["requested"],
        after=record["action"]["after"],
    )


@pytest.mark.parametrize("schema_name,example_name", [
    ("action_record.schema.json", "action_record.example.json"),
    ("controller_event.schema.json", "controller_event.example.json"),
])
def test_applied_action_schema_rejects_null_evidence(schema_name: str, example_name: str) -> None:
    """Schema test: verify action status 'applied' strictly rejects null before, requested, or after."""
    schema = load_schema(schema_name)
    record = load_example(example_name)
    record["controller_mode"] = "apply"
    record["action"]["status"] = "applied"
    record["action"]["before"] = {"aura-app.slice": 100}
    record["action"]["requested"] = {"aura-app.slice": 200}
    record["action"]["after"] = {"aura-app.slice": 200}

    # Null before must fail
    bad_before = dict(record)
    bad_before["action"] = dict(record["action"])
    bad_before["action"]["before"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_before, schema=schema, cls=Draft202012Validator)

    # Null requested must fail
    bad_requested = dict(record)
    bad_requested["action"] = dict(record["action"])
    bad_requested["action"]["requested"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_requested, schema=schema, cls=Draft202012Validator)

    # Null after must fail
    bad_after = dict(record)
    bad_after["action"] = dict(record["action"])
    bad_after["action"]["after"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=bad_after, schema=schema, cls=Draft202012Validator)


def test_applied_action_semantic_validation_rejects_mismatched_after_state() -> None:
    """Semantic test: verify validate_action_guardrails rejects unverified or mismatched after state."""
    before = {"aura-app.slice": 100}
    requested = {"aura-app.slice": 200}

    # Missing evidence
    with pytest.raises(ValueError, match="Applied action evidence violation"):
        validate_action_guardrails("apply", "applied", before=None, requested=requested, after=requested)

    with pytest.raises(ValueError, match="Applied action evidence violation"):
        validate_action_guardrails("apply", "applied", before=before, requested=None, after=requested)

    with pytest.raises(ValueError, match="Applied action evidence violation"):
        validate_action_guardrails("apply", "applied", before=before, requested=requested, after=None)

    # Empty requested
    with pytest.raises(ValueError, match="requested configuration cannot be empty"):
        validate_action_guardrails("apply", "applied", before=before, requested={}, after={})

    # Mismatched value in after state (system setting failed to update)
    mismatched_after = {"aura-app.slice": 100}
    with pytest.raises(ValueError, match="Applied action verification mismatch"):
        validate_action_guardrails("apply", "applied", before=before, requested=requested, after=mismatched_after)

    # Missing key in after state
    missing_key_after = {"other.slice": 200}
    with pytest.raises(ValueError, match="Applied action verification mismatch"):
        validate_action_guardrails("apply", "applied", before=before, requested=requested, after=missing_key_after)

    # Valid matching after state passes
    matching_after = {"aura-app.slice": 200, "aura-bg.slice": 100}
    validate_action_guardrails("apply", "applied", before=before, requested=requested, after=matching_after)


@pytest.mark.parametrize("delta_field", [
    "net_rx_softirq_delta_per_cpu",
    "net_tx_softirq_delta_per_cpu",
    "softnet_processed_delta_per_cpu",
    "softnet_dropped_delta_per_cpu",
    "softnet_time_squeeze_delta_per_cpu",
])
def test_per_cpu_delta_reason_contract_rules(delta_field: str) -> None:
    """Verify per-CPU delta schemas strictly enforce reason invariants across all softnet and IRQ fields:
    - delta=null requires non-empty reason string.
    - numeric delta requires reason=null.
    """
    schema = load_schema("host_snapshot.schema.json")
    base_snapshot = load_example("host_snapshot.example.json")

    # 1. Valid numeric delta with reason=null must pass
    valid_numeric = dict(base_snapshot)
    valid_numeric[delta_field] = [{"cpu_id": 0, "delta": 100, "reason": None}]
    jsonschema.validate(instance=valid_numeric, schema=schema, cls=Draft202012Validator)

    # 2. Valid null delta with non-empty reason must pass
    valid_null = dict(base_snapshot)
    valid_null[delta_field] = [{"cpu_id": 0, "delta": None, "reason": "counter_reset_detected"}]
    jsonschema.validate(instance=valid_null, schema=schema, cls=Draft202012Validator)

    # 3. Negative: Null delta with missing reason must fail
    missing_reason = dict(base_snapshot)
    missing_reason[delta_field] = [{"cpu_id": 0, "delta": None}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=missing_reason, schema=schema, cls=Draft202012Validator)

    # 4. Negative: Null delta with reason=null must fail
    null_reason_for_null_delta = dict(base_snapshot)
    null_reason_for_null_delta[delta_field] = [{"cpu_id": 0, "delta": None, "reason": None}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=null_reason_for_null_delta, schema=schema, cls=Draft202012Validator)

    # 5. Negative: Numeric delta with a non-null reason must fail
    non_null_reason_for_numeric = dict(base_snapshot)
    non_null_reason_for_numeric[delta_field] = [{"cpu_id": 0, "delta": 100, "reason": "unexpected"}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=non_null_reason_for_numeric, schema=schema, cls=Draft202012Validator)

    # 6. Negative: Null delta with empty string reason must fail
    empty_reason = dict(base_snapshot)
    empty_reason[delta_field] = [{"cpu_id": 0, "delta": None, "reason": ""}]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=empty_reason, schema=schema, cls=Draft202012Validator)
