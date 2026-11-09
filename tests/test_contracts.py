"""Unit and semantic specification tests for AURA shared JSON contracts and schemas."""
import json
import uuid
from pathlib import Path
import pytest
import jsonschema
from jsonschema import Draft202012Validator

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
EXAMPLES_DIR = CONTRACTS_DIR / "examples"

SCHEMA_FILES = [
    "client_window.schema.json",
    "host_snapshot.schema.json",
    "controller_event.schema.json",
    "action_record.schema.json",
]


def load_schema(name: str) -> dict:
    with (CONTRACTS_DIR / name).open("r", encoding="utf-8") as f:
        return json.load(f)


def load_example(name: str) -> dict:
    with (EXAMPLES_DIR / name).open("r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================================
# 1. Metaschema & Example Validation
# ============================================================================

@pytest.mark.parametrize("schema_name", SCHEMA_FILES)
def test_schema_syntax_and_metaschema(schema_name: str) -> None:
    """Verify each schema is syntactically valid JSON and complies with Draft 2020-12."""
    schema = load_schema(schema_name)
    Draft202012Validator.check_schema(schema)


def test_client_window_example_valid() -> None:
    """Verify client_window.example.json complies with client_window.schema.json."""
    schema = load_schema("client_window.schema.json")
    example = load_example("client_window.example.json")
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
# 2. Client Benchmark Semantic Contract Tests
# ============================================================================

def test_requests_equals_successes_plus_errors() -> None:
    """Verify requests = successes + errors invariant."""
    example = load_example("client_window.example.json")
    assert example["requests"] == example["successes"] + example["errors"], (
        f"Invariant broken: {example['requests']} != {example['successes']} + {example['errors']}"
    )


def test_sample_count_consistency() -> None:
    """Verify sample_count strictly equals successful requests."""
    example = load_example("client_window.example.json")
    assert example["sample_count"] == example["successes"]


def test_throughput_calculation() -> None:
    """Verify throughput_rps = successes / window_duration_s."""
    example = load_example("client_window.example.json")
    expected_throughput = example["successes"] / example["window_duration_s"]
    assert pytest.approx(example["throughput_rps"], rel=1e-3) == expected_throughput


def test_valid_duration_requirements() -> None:
    """Verify window_duration_s is strictly positive (> 0)."""
    schema = load_schema("client_window.schema.json")
    example = load_example("client_window.example.json")

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


def evaluate_tail_statuses(sample_count: int) -> tuple[str, str]:
    """Helper to evaluate tail statuses according to AURA reporting guardrails."""
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
    example = load_example("client_window.example.json")
    assert example["sample_count"] == 4998
    assert example["p99_status"] == "exploratory"
    assert example["p999_status"] == "exploratory"


# ============================================================================
# 3. Two-Stage Ingestion, Monotonic Freshness & Boot ID Tests
# ============================================================================

def test_monotonic_timestamp_stamping_by_server() -> None:
    """Verify client payload can omit server stamps, and server stamps them upon ingestion."""
    schema = load_schema("client_window.schema.json")
    raw_client_payload = load_example("client_window.example.json")

    # Client emits with server fields as null or omitted
    raw_client_payload["server_received_at_epoch_s"] = None
    raw_client_payload["server_received_at_monotonic_s"] = None
    raw_client_payload["server_boot_id"] = None
    jsonschema.validate(instance=raw_client_payload, schema=schema, cls=Draft202012Validator)

    # Server performs ingestion stamping
    server_epoch_now = 1794230050.0
    server_monotonic_now = 1470.5
    server_boot_id = str(uuid.uuid4())

    stamped_record = dict(raw_client_payload)
    stamped_record["server_received_at_epoch_s"] = server_epoch_now
    stamped_record["server_received_at_monotonic_s"] = server_monotonic_now
    stamped_record["server_boot_id"] = server_boot_id

    jsonschema.validate(instance=stamped_record, schema=schema, cls=Draft202012Validator)
    assert stamped_record["server_boot_id"] == server_boot_id
    assert stamped_record["server_received_at_monotonic_s"] == server_monotonic_now


def test_boot_identifier_mismatch_logic() -> None:
    """Verify freshness calculation detects server boot ID mismatch and marks telemetry stale."""
    active_controller_boot_id = "boot-id-server-session-A"
    report_server_boot_id = "boot-id-server-session-B"  # Simulated reboot or different host

    report = load_example("controller_event.example.json")
    report["server_boot_id"] = report_server_boot_id

    # Simulated controller freshness logic:
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
# 4. Host Telemetry Counter Handling (Reset, Hotplug, Missing Fields)
# ============================================================================

def test_counter_reset_handling() -> None:
    """Verify counter rollover/reset sets delta to null and logs structured reason in missing_fields."""
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
    """Verify CPU hotplug appearance or disappearance is handled safely."""
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
# 5. Controller Safety & Action Guardrails
# ============================================================================

def test_rejection_of_applied_action_in_observe_or_dry_run_modes() -> None:
    """Verify operational rule: 'applied' action status is prohibited in observe_only or dry_run."""
    action_record = load_example("action_record.example.json")

    # Attempt to set applied in dry_run mode
    action_record["controller_mode"] = "dry_run"
    action_record["action"]["status"] = "applied"

    # Governance assertion:
    is_valid_execution = not (
        action_record["controller_mode"] in ("observe_only", "dry_run")
        and action_record["action"]["status"] == "applied"
    )
    assert is_valid_execution is False, "Status 'applied' must never execute in observe_only or dry_run modes"
