"""AURA rule-based heuristic candidate diagnosis engine.

Evaluates evidence from client measurement windows and host telemetry snapshots.
Produces strictly non-causal candidate hypotheses conforming to controller_event.schema.json:
- cpu_contention_candidate
- rx_activity_skew_candidate
- interrupt_pressure_candidate
- uncertain
- normal

The engine is deterministic, side-effect-free, and performs no I/O, privileged operations,
or resource control modifications.
"""
from __future__ import annotations

import math
from typing import Any, Mapping


# Supported candidate diagnoses per contracts/controller_event.schema.json
DIAGNOSIS_LABELS = {
    "cpu_contention_candidate",
    "rx_activity_skew_candidate",
    "interrupt_pressure_candidate",
    "uncertain",
    "normal",
}


def compute_rx_skew(per_cpu_deltas: list[dict[str, Any]] | None) -> tuple[float | None, int | None, int]:
    """Calculate maximum skew ratio and busiest CPU ID from per-CPU counter deltas.

    Formula:
        skew_ratio = max_cpu_delta / sum_all_deltas (if sum > 0)

    Returns:
        (skew_ratio, busiest_cpu_id, total_events)
    """
    if not per_cpu_deltas:
        return None, None, 0

    total_events = 0
    max_delta = -1
    busiest_cpu: int | None = None

    for item in per_cpu_deltas:
        cpu_id = item.get("cpu_id")
        delta = item.get("delta")
        if delta is not None and isinstance(delta, int) and delta >= 0:
            total_events += delta
            if delta > max_delta:
                max_delta = delta
                busiest_cpu = cpu_id

    if total_events <= 0 or max_delta < 0:
        return None, busiest_cpu, total_events

    skew_ratio = float(max_delta) / float(total_events)
    return skew_ratio, busiest_cpu, total_events


def evaluate_candidate_diagnosis(
    client_window: Mapping[str, Any] | None,
    host_snapshot: Mapping[str, Any] | None,
    thresholds: Mapping[str, Any],
    stale_telemetry: bool = False,
    boot_id_mismatch: bool = False,
) -> dict[str, Any]:
    """Evaluate candidate root-cause diagnosis based on client window and host telemetry.

    Returns a dictionary conforming to the diagnosis sub-object of controller_event.schema.json:
    {
        "label": str,
        "confidence": float | None,
        "evidence": list[str],
        "limitations": list[str],
        "conflicting_evidence": bool
    }
    """
    evidence: list[str] = []
    limitations: list[str] = [
        "Rule-based heuristic candidate diagnosis; strictly non-causal without controlled perturbation.",
        "Observe-only evaluation; active mitigations disabled.",
    ]
    conflicting_evidence = False

    # 1. Check for missing or corrupted input structures
    if client_window is None and host_snapshot is None:
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": ["Missing both client measurement window and host telemetry snapshot."],
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    if client_window is None:
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": ["Missing client measurement window required for latency baseline."],
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    if host_snapshot is None:
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": ["Missing host telemetry snapshot required for root-cause attribution."],
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # 2. Check for stale telemetry or boot ID discontinuity
    if boot_id_mismatch:
        evidence.append("Host/Server boot identity mismatch detected; monotonic clocks discontinuous.")
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    if stale_telemetry:
        evidence.append("Telemetry snapshot exceeded maximum allowable age threshold (stale).")
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # 3. Extract metrics and thresholds
    p99_target_ms = thresholds.get("p99_target_ms")
    cpu_psi_thresh = thresholds.get("cpu_psi_some_avg10_threshold_pct")
    skew_thresh = thresholds.get("rx_activity_skew_candidate_threshold", 0.75)
    min_rx_events = thresholds.get("min_net_rx_events_per_window", 200)
    min_dropped_thresh = thresholds.get("min_softnet_dropped_threshold", 1)
    min_squeeze_thresh = thresholds.get("min_softnet_squeeze_threshold", 1)

    p99_ms = client_window.get("p99_ms")
    p99_status = client_window.get("p99_status", "insufficient")
    sample_count = client_window.get("sample_count", 0)

    # 4. Enforce P99 sample sufficiency safeguard
    # When p99_status == "insufficient", diagnosis must be "uncertain", with no candidate
    # root-cause classification and a no_op action. Do not allow elevated CPU PSI, RX skew,
    # softnet drops, or time-squeeze counts to bypass the insufficient-tail safeguard.
    if p99_status == "insufficient":
        limitations.append(
            f"P99 latency sample count ({sample_count}) is insufficient for statistical confidence; "
            "root-cause candidate classification inhibited."
        )
        evidence.append("P99 latency sample count is insufficient; evaluating diagnosis as uncertain.")
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # For exploratory P99 evidence, document guardrail and do not assign misleading high confidence
    if p99_status == "exploratory":
        limitations.append(
            f"P99 latency sample count ({sample_count}) meets exploratory criteria (<10,000 samples); "
            "tail confidence is attenuated."
        )

    # Always include baseline scope attribution limitation
    limitations.append(
        "Host-wide /proc/softirqs NET_RX is not per-flow attributed; reflects aggregate host receive processing."
    )

    # 5. Extract host metrics
    cpu_psi_avg10 = host_snapshot.get("cpu_psi_some_avg10_pct")
    net_rx_deltas = host_snapshot.get("net_rx_softirq_delta_per_cpu")
    softnet_dropped = host_snapshot.get("softnet_dropped_delta_per_cpu")
    softnet_squeeze = host_snapshot.get("softnet_time_squeeze_delta_per_cpu")
    missing_fields = host_snapshot.get("missing_fields") or []

    # Check for structured missing fields or null essential telemetry
    # Telemetry is incomplete if essential signals are null or explicitly recorded in missing_fields
    has_missing_essential_telemetry = False
    missing_reasons: list[str] = []

    if missing_fields:
        for mf in missing_fields:
            if isinstance(mf, dict):
                metric_name = mf.get("metric", "unknown")
                reason_code = mf.get("reason", "unspecified")
                missing_reasons.append(f"{metric_name} ({reason_code})")
        if missing_reasons:
            has_missing_essential_telemetry = True
            evidence.append(f"Host telemetry reports missing/reset fields: {', '.join(missing_reasons)}.")

    # Check per-CPU deltas for null/reset reasons when present
    if net_rx_deltas is None and "net_rx_softirq_delta_per_cpu" in host_snapshot:
        has_missing_essential_telemetry = True
        missing_reasons.append("net_rx_softirq_delta_per_cpu (is_null)")
    elif isinstance(net_rx_deltas, list):
        for item in net_rx_deltas:
            if isinstance(item, dict) and item.get("delta") is None:
                has_missing_essential_telemetry = True
                reason = item.get("reason") or "null_delta"
                cpu_id = item.get("cpu_id", "?")
                missing_reasons.append(f"net_rx_softirq_delta_per_cpu[cpu_{cpu_id}] ({reason})")

    for delta_list, name in (
        (softnet_dropped, "softnet_dropped_delta_per_cpu"),
        (softnet_squeeze, "softnet_time_squeeze_delta_per_cpu"),
    ):
        if delta_list is None and name in host_snapshot:
            has_missing_essential_telemetry = True
            missing_reasons.append(f"{name} (is_null)")
        elif isinstance(delta_list, list):
            for item in delta_list:
                if isinstance(item, dict) and item.get("delta") is None:
                    has_missing_essential_telemetry = True
                    reason = item.get("reason") or "null_delta"
                    cpu_id = item.get("cpu_id", "?")
                    missing_reasons.append(f"{name}[cpu_{cpu_id}] ({reason})")

    if cpu_psi_avg10 is None and "cpu_psi_some_avg10_pct" in host_snapshot:
        has_missing_essential_telemetry = True
        missing_reasons.append("cpu_psi_some_avg10_pct (is_null)")

    if missing_reasons and not evidence:
        evidence.append(f"Host telemetry contains missing or partial signals: {', '.join(missing_reasons)}.")
    elif missing_reasons and not any("missing" in e for e in evidence):
        evidence.append(f"Host telemetry contains missing or partial signals: {', '.join(missing_reasons)}.")

    # Evaluate latency degradation signal
    latency_degraded = False
    if p99_ms is not None and p99_target_ms is not None:
        if p99_ms > p99_target_ms:
            latency_degraded = True
            evidence.append(f"Client P99 latency ({p99_ms:.2f}ms) exceeded target ({p99_target_ms:.2f}ms).")
        else:
            evidence.append(f"Client P99 latency ({p99_ms:.2f}ms) within target ({p99_target_ms:.2f}ms).")
    else:
        # If latency metric is missing, we cannot assess degradation
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": ["Client P99 latency metric is missing; cannot evaluate degradation."],
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # Evaluate CPU PSI signal
    cpu_psi_elevated = False
    if cpu_psi_avg10 is not None and cpu_psi_thresh is not None:
        if cpu_psi_avg10 >= cpu_psi_thresh:
            cpu_psi_elevated = True
            evidence.append(f"CPU PSI some avg10 ({cpu_psi_avg10:.2f}%) exceeded threshold ({cpu_psi_thresh:.2f}%).")
        else:
            evidence.append(f"CPU PSI some avg10 ({cpu_psi_avg10:.2f}%) within threshold ({cpu_psi_thresh:.2f}%).")

    # Evaluate Softnet dropped / squeeze
    softnet_drops_total = 0
    if softnet_dropped:
        for item in softnet_dropped:
            d = item.get("delta")
            if d is not None and isinstance(d, int) and d > 0:
                softnet_drops_total += d

    softnet_squeeze_total = 0
    if softnet_squeeze:
        for item in softnet_squeeze:
            d = item.get("delta")
            if d is not None and isinstance(d, int) and d > 0:
                softnet_squeeze_total += d

    # Evaluate NET_RX skew
    skew_ratio, busiest_cpu, total_rx_events = compute_rx_skew(net_rx_deltas)
    rx_skew_detected = False
    if skew_ratio is not None and total_rx_events >= min_rx_events:
        if skew_ratio >= skew_thresh:
            rx_skew_detected = True
            evidence.append(
                f"NET_RX softIRQ skew detected: CPU {busiest_cpu} handled {skew_ratio * 100:.1f}% "
                f"of {total_rx_events} events (threshold {skew_thresh * 100:.1f}%)."
            )

    has_softnet_pressure = False
    if softnet_drops_total >= min_dropped_thresh:
        has_softnet_pressure = True
        evidence.append(f"Softnet backlog frame drops detected ({softnet_drops_total} drops).")
    if softnet_squeeze_total >= min_squeeze_thresh:
        has_softnet_pressure = True
        evidence.append(f"NAPI budget time-squeeze events detected ({softnet_squeeze_total} squeezes).")

    # 6. Check for conflicting evidence
    # Example: CPU PSI is high, but latency is normal; or latency is degraded but all host metrics are completely nominal.
    if cpu_psi_elevated and (p99_target_ms is not None and not latency_degraded):
        conflicting_evidence = True
        evidence.append("Conflicting evidence: CPU PSI is elevated while client P99 latency remains within target.")

    # 7. Determine Candidate Hypothesis
    if conflicting_evidence:
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": True,
        }

    # Strong CPU contention: Latency elevated + CPU PSI elevated
    if cpu_psi_elevated and latency_degraded:
        confidence = 0.85 if p99_status == "minimum_tail_count_met" else 0.50
        return {
            "label": "cpu_contention_candidate",
            "confidence": confidence,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # Softnet drop or high RX skew
    if rx_skew_detected or (softnet_drops_total >= min_dropped_thresh and latency_degraded):
        confidence = 0.80 if p99_status == "minimum_tail_count_met" else 0.50
        return {
            "label": "rx_activity_skew_candidate",
            "confidence": confidence,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    if softnet_squeeze_total >= min_squeeze_thresh and latency_degraded:
        confidence = 0.75 if p99_status == "minimum_tail_count_met" else 0.45
        return {
            "label": "interrupt_pressure_candidate",
            "confidence": confidence,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # If latency is degraded but no host signals explain it (e.g., network path delay, client bottleneck)
    if latency_degraded:
        evidence.append("Tail latency elevated without corroborating host CPU PSI or softnet congestion signals.")
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # If any essential telemetry is missing or partial, we MUST NOT declare 'normal'
    if has_missing_essential_telemetry:
        evidence.append("Required telemetry signals are unavailable, reset, or partial; cannot confirm normal state.")
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # Normal: When thresholds are calibrated, no essential telemetry is missing,
    # and evidence shows healthy latency AND no host pressure (no PSI elevation, no RX skew, no softnet drops, no softnet squeeze).
    if (
        p99_target_ms is not None
        and not latency_degraded
        and not cpu_psi_elevated
        and not rx_skew_detected
        and not has_softnet_pressure
    ):
        confidence = 0.90 if p99_status == "minimum_tail_count_met" else 0.60
        return {
            "label": "normal",
            "confidence": confidence,
            "evidence": evidence if evidence else ["All measured metrics within healthy thresholds."],
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # If latency is degraded but no host signals explain it (e.g., network path delay, client bottleneck)
    if latency_degraded:
        evidence.append("Tail latency elevated without corroborating host CPU PSI or softnet congestion signals.")
        return {
            "label": "uncertain",
            "confidence": None,
            "evidence": evidence,
            "limitations": limitations,
            "conflicting_evidence": False,
        }

    # Default conservative outcome
    return {
        "label": "uncertain",
        "confidence": None,
        "evidence": evidence if evidence else ["Insufficient baseline telemetry to declare normal state."],
        "limitations": limitations,
        "conflicting_evidence": False,
    }
