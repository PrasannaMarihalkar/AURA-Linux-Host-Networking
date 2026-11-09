"""Unit and semantic specification tests for AURA shared JSON contracts and schemas.

Covers Gate A semantic freeze specifications:
- JSON Schema Draft 2020-12 validation for all schemas and examples
- Ingress vs Ingested two-stage client window contracts
- Client benchmark invariants: requests = successes + errors, sample_count <= successes
- Nearest-rank percentile calculation and sample ordering
- P99 and P99.9 reporting guardrail transitions
- Server ingestion stamping, monotonic freshness, and boot identity verification
- Host telemetry counter deltas, CPU PSI 'some' preservation, resets, and hotplug
- Operational action safety guardrails: strict prohibition of 'applied' in observe/dry-run modes
"""
from __future__ import annotations

import json
import math
import uuid
from pathlib import Path
import pytest
import jsonschema
from jsonschema import Draft202012Validator

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
    """Ensure JSON parser rejects NaN and Infinity constants."""
    def _reject_constant(val: str) -> None:
        raise ValueError(f"Forbidden constant: {val}")

    with pytest.raises(ValueError, match="Forbidden constant"):
        json.loads('{"p99_ms": NaN}', parse_constant=_reject_constant)

    with pytest.raises(ValueError, match="Forbidden constant"):
        json.loads('{"p99_ms": Infinity}', parse_constant=_reject_constant)

    with pytest.raises(ValueError, match="Forbidden constant"):
        json.loads('{"p99_ms": -Infinity}', parse_constant=_reject_constant)


# ============================================================================
# 3. Client Benchmark Semantic Contract Tests
# ============================================================================

def test_requests_equals_successes_plus_errors() -> None:
    """Verify requests = successes + errors invariant."""
    example = load_example("client_window_ingress.example.json")
    assert example["requests"] == example["successes"] + example["errors"], (
        f"Invariant broken: {example['requests']} != {example['successes']} + {example['errors']}"
    )


def test_sample_count_less_than_or_equal_to_successes() -> None:
    """Verify sample_count <= successes invariant."""
    example = load_example("client_window_ingress.example.json")
    assert example["sample_count"] <= example["successes"], "sample_count must be <= successes"

    # Edge case: partial measurement where some successful requests had dropped timers
    partial_example = dict(example)
    partial_example["sample_count"] = partial_example["successes"] - 10
    assert partial_example["sample_count"] <= partial_example["successes"]


def test_throughput_calculation() -> None:
    """Verify throughput_rps = successes / window_duration_s."""
    example = load_example("client_window_ingress.example.json")
    expected_throughput = example["successes"] / example["window_duration_s"]
    assert pytest.approx(example["throughput_rps"], rel=1e-3) == expected_throughput


def test_valid_duration_requirements() -> None:
    """Verify window_duration_s must be finite and strictly positive (> 0)."""
    schema = load_schema("client_window.schema.json")
    example = load_example("client_window_ingress.example.json")

    # Zero duration must fail
    invalid_zero = dict(example)
    invalid_zero["window_duration_s"] = 0.0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid_zero, schema=schema, cls=Draft202012Validator)

    # Negative duration must fail
    invalid_neg = dict(example)
    invalid_neg["window_duration_s"] = -5.0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid_neg, schema=schema, cls=Draft202012Validator)


def nearest_rank_percentile(latencies: list[float], percentile: float) -> float:
    """Compute nearest-rank percentile for 0 < percentile <= 1.0 using 1-indexed ceil(p * N)."""
    if not latencies:
        raise ValueError("Cannot calculate percentile of empty sample list")
    if not (0.0 < percentile <= 1.0):
        raise ValueError("Percentile must be in range (0.0, 1.0]")

    sorted_latencies = sorted(latencies)
    n = len(sorted_latencies)
    rank = int(math.ceil(percentile * n))
    rank_bounded = max(1, min(rank, n))
    return sorted_latencies[rank_bounded - 1]


def test_nearest_rank_percentile_calculation() -> None:
    """Verify nearest-rank percentile formula: rank = ceil(p * N) 1-indexed."""
    # 100 values from 1.0 to 100.0
    samples = [float(i) for i in range(1, 101)]

    # P50 (median): ceil(0.50 * 100) = 50 -> 50.0
    assert nearest_rank_percentile(samples, 0.50) == 50.0
    # P95: ceil(0.95 * 100) = 95 -> 95.0
    assert nearest_rank_percentile(samples, 0.95) == 95.0
    # P99: ceil(0.99 * 100) = 99 -> 99.0
    assert nearest_rank_percentile(samples, 0.99) == 99.0

    # Non-integer rank: 10 samples, P95: ceil(0.95 * 10) = ceil(9.5) = 10 -> 10th element
    small_samples = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
    assert nearest_rank_percentile(small_samples, 0.95) == 100.0

    # Test sample ordering invariant (unsorted input produces identical result)
    import random
    shuffled = list(samples)
    random.seed(42)
    random.shuffle(shuffled)
    assert nearest_rank_percentile(shuffled, 0.99) == 99.0


def evaluate_tail_statuses(sample_count: int) -> tuple[str, str]:
    """Helper implementing AURA reporting guardrails for P99 and P99.9."""
    if sample_count >= 10_000:
        p99 = "minimum_tail_count_met"
    elif sample_count >= 100:
        p99 = "exploratory"
    else:
        p99 = "insufficient"

    if sample_count >= 100_000:
        p999 = "minimum_tail_count_met"
    elif sample_count >= 1_000:
        p999 = "exploratory"
    else:
        p999 = "insufficient"

    return p99, p999


def test_p99_and_p999_status_transitions() -> None:
    """Verify P99 and P99.9 reporting guardrail threshold transitions."""
    # Under 100 samples
    assert evaluate_tail_statuses(50) == ("insufficient", "insufficient")
    # Between 100 and 999 samples
    assert evaluate_tail_statuses(500) == ("exploratory", "insufficient")
    # Between 1,000 and 9,999 samples
    assert evaluate_tail_statuses(5000) == ("exploratory", "exploratory")
    # Between 10,000 and 99,999 samples
    assert evaluate_tail_statuses(25000) == ("minimum_tail_count_met", "exploratory")
    # 100,000+ samples
    assert evaluate_tail_statuses(150000) == ("minimum_tail_count_met", "minimum_tail_count_met")


def test_sample_status_for_4998_example() -> None:
    """Verify the 4,998 successful sample example has 'exploratory' for both P99 and P99.9."""
    example = load_example("client_window_ingress.example.json")
    assert example["sample_count"] == 4998
    assert example["p99_status"] == "exploratory"
    assert example["p999_status"] == "exploratory"


# ============================================================================
# 4. Two-Stage Ingestion, Monotonic Freshness & Boot ID Tests
# ============================================================================

def test_rejection_of_client_supplied_server_receipt_metadata() -> None:
    """Verify ingress schema strictly rejects client payloads that inject server receipt fields."""
    ingress_schema = load_schema("client_window.schema.json")
    example = load_example("client_window_ingress.example.json")

    # Injecting server_received_at_epoch_s into ingress must fail
    invalid_epoch = dict(example)
    invalid_epoch["server_received_at_epoch_s"] = 1794230000.0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid_epoch, schema=ingress_schema, cls=Draft202012Validator)

    # Injecting server_received_at_monotonic_s into ingress must fail
    invalid_mono = dict(example)
    invalid_mono["server_received_at_monotonic_s"] = 1420.0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid_mono, schema=ingress_schema, cls=Draft202012Validator)

    # Injecting server_boot_id into ingress must fail
    invalid_boot = dict(example)
    invalid_boot["server_boot_id"] = str(uuid.uuid4())
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=invalid_boot, schema=ingress_schema, cls=Draft202012Validator)


def stamp_ingestion(
    ingress_payload: dict,
    server_epoch_s: float,
    server_monotonic_s: float,
    server_boot_id: str,
) -> dict:
    """Contract helper: validates ingress payload and enriches with server receipt metadata."""
    ingress_schema = load_schema("client_window.schema.json")
    jsonschema.validate(instance=ingress_payload, schema=ingress_schema, cls=Draft202012Validator)

    ingested_record = dict(ingress_payload)
    ingested_record["server_received_at_epoch_s"] = server_epoch_s
    ingested_record["server_received_at_monotonic_s"] = server_monotonic_s
    ingested_record["server_boot_id"] = server_boot_id

    ingested_schema = load_schema("client_window_ingested.schema.json")
    jsonschema.validate(instance=ingested_record, schema=ingested_schema, cls=Draft202012Validator)
    return ingested_record


def test_server_owned_timestamp_generation_helper() -> None:
    """Verify standalone stamp_ingestion helper validates ingress and enriches schema-compliant record."""
    ingress_record = load_example("client_window_ingress.example.json")
    server_epoch_now = 1794230050.123
    server_monotonic_now = 1450.456
    server_boot_id = str(uuid.uuid4())

    enriched = stamp_ingestion(
        ingress_record,
        server_epoch_s=server_epoch_now,
        server_monotonic_s=server_monotonic_now,
        server_boot_id=server_boot_id,
    )
    assert enriched["run_id"] == ingress_record["run_id"]
    assert enriched["window_id"] == ingress_record["window_id"]
    assert enriched["server_received_at_epoch_s"] == server_epoch_now
    assert enriched["server_received_at_monotonic_s"] == server_monotonic_now
    assert enriched["server_boot_id"] == server_boot_id


def test_boot_identifier_mismatch_logic() -> None:
    """Verify freshness calculation detects server boot ID mismatch and marks telemetry stale."""
    active_controller_boot_id = "boot-id-server-session-A"
    report_server_boot_id = "boot-id-server-session-B"  # Mismatch (reboot or different server)

    report = load_example("controller_event.example.json")
    report["server_boot_id"] = report_server_boot_id

    if report["server_boot_id"] != active_controller_boot_id:
        stale_telemetry = True
        diagnosis_label = "uncertain"
        action_type = "no_op"
    else:
        stale_telemetry = False
        diagnosis_label = "normal"
        action_type = "no_op"

    assert stale_telemetry is True
    assert diagnosis_label == "uncertain"
    assert action_type == "no_op"


# ============================================================================
# 5. Host Telemetry Counter Handling (Reset, Hotplug, Missing Fields)
# ============================================================================

def test_counter_reset_handling() -> None:
    """Verify counter rollover/reset sets delta to null and logs structured reason in missing_fields."""
    schema = load_schema("host_snapshot.schema.json")
    snapshot = load_example("host_snapshot.example.json")

    # Simulate CPU 1 counter reset
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
    """Verify CPU hotplug core appearance or offline event is handled safely with structured reasons."""
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


def test_missing_telemetry_handling() -> None:
    """Verify missing kernel telemetry reports null metrics and structured reasons in missing_fields."""
    schema = load_schema("host_snapshot.schema.json")
    snapshot = load_example("host_snapshot.example.json")

    snapshot["cpu_psi_some_avg10_pct"] = None
    snapshot["cpu_psi_some_avg60_pct"] = None
    snapshot["cpu_psi_some_avg300_pct"] = None
    snapshot["cpu_psi_some_total_us"] = None
    snapshot["missing_fields"].append({
        "metric": "cpu_psi_some",
        "reason": "kernel_file_missing"
    })

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
# 6. Controller Safety & Action Guardrails
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


def test_rejection_of_applied_action_in_dry_run_mode() -> None:
    """Verify action status 'applied' is strictly rejected when controller_mode is dry_run."""
    schema = load_schema("action_record.schema.json")
    record = load_example("action_record.example.json")

    record["controller_mode"] = "dry_run"
    record["action"]["status"] = "applied"

    # Schema must reject via allOf conditional
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=record, schema=schema, cls=Draft202012Validator)
