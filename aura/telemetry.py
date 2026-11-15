"""Read-only Linux host telemetry collector for AURA."""

from __future__ import annotations

import math
import platform
import threading
import time
from pathlib import Path
from typing import Any


PROC_ROOT = Path("/proc")
SYS_CLASS_NET_ROOT = Path("/sys/class/net")
SYS_CPU_ROOT = Path("/sys/devices/system/cpu")

# The first call needs two samples to produce a valid positive interval.
INITIAL_SAMPLE_DELAY_S = 1.0

# Previous raw readings, maintained within this Python process protected by lock.
_BASELINE: dict[str, Any] | None = None
_BASELINE_LOCK = threading.Lock()


def _add_missing(
    missing_fields: list[dict[str, str]],
    metric: str,
    reason: str,
) -> None:
    """Add a missing metric only once for a given reason."""

    entry = {"metric": metric, "reason": reason}

    if entry not in missing_fields:
        missing_fields.append(entry)


def _parse_cpu_list(value: str) -> list[int]:
    """Parse Linux CPU lists such as 0-3,5,7-8."""

    result = []

    for part in value.strip().split(","):
        if not part:
            continue

        if "-" in part:
            start, end = part.split("-", 1)
            result.extend(range(int(start), int(end) + 1))
        else:
            result.append(int(part))

    return sorted(set(result))


def read_cpu_pressure() -> dict | None:
    """Read system-level CPU PSI 'some' metrics."""

    try:
        content = (PROC_ROOT / "pressure" / "cpu").read_text()
    except OSError:
        return None

    for line in content.splitlines():
        parts = line.split()

        if not parts or parts[0] != "some":
            continue

        result = {}

        try:
            for item in parts[1:]:
                key, value = item.split("=", 1)

                if key in ("avg10", "avg60", "avg300"):
                    number = float(value)

                    if not math.isfinite(number) or not 0 <= number <= 100:
                        return None

                    result[key] = number

                elif key == "total":
                    number = int(value)

                    if number < 0:
                        return None

                    result[key] = number

        except (ValueError, TypeError):
            return None

        required = {"avg10", "avg60", "avg300", "total"}

        if required.issubset(result):
            return result

    return None


def read_softirqs() -> dict | None:
    """Read host-wide NET_RX and NET_TX counters per CPU."""

    try:
        content = (PROC_ROOT / "softirqs").read_text()
    except OSError:
        return None

    lines = content.splitlines()

    if not lines:
        return None

    try:
        cpu_ids = [
            int(column.replace("CPU", ""))
            for column in lines[0].split()
        ]

        if not cpu_ids:
            return None

        result = {"NET_RX": {}, "NET_TX": {}}

        for line in lines[1:]:
            parts = line.split()

            if not parts:
                continue

            metric = parts[0].rstrip(":")

            if metric not in result:
                continue

            if len(parts) - 1 != len(cpu_ids):
                return None

            values = [int(value) for value in parts[1:]]

            if any(value < 0 for value in values):
                return None

            result[metric] = dict(zip(cpu_ids, values))

        if not result["NET_RX"] or not result["NET_TX"]:
            return None

        return result

    except (ValueError, IndexError):
        return None


def read_softnet_stat() -> dict | None:
    """Read per-CPU processed, dropped and time-squeeze counters.

    Linux exposes softnet statistics in /proc/net/softnet_stat.
    Depending on the kernel version and configuration, lines may correspond
    to all possible CPUs, or exclusively online CPUs. Additionally, Linux 5.x+
    may append an explicit hex CPU core ID in column 11 (0-indexed).
    This function detects the kernel format, validates unique CPU IDs,
    and returns a mapping of online CPU IDs to their softnet metrics.
    """

    try:
        content = (PROC_ROOT / "net" / "softnet_stat").read_text()
    except OSError:
        return None

    lines = [line.strip() for line in content.splitlines() if line.strip()]

    if not lines:
        return None

    try:
        possible_file = SYS_CPU_ROOT / "possible"
        if possible_file.exists():
            possible_cpus = _parse_cpu_list(possible_file.read_text())
        else:
            possible_cpus = list(range(len(lines)))

        online_file = SYS_CPU_ROOT / "online"
        if online_file.exists():
            online_cpus = set(_parse_cpu_list(online_file.read_text()))
        else:
            online_cpus = set(possible_cpus)

        result = {}

        # Check if lines have an explicit CPU ID in column 11 (12+ fields)
        first_row_fields = lines[0].split()
        has_explicit_cpu_id = False
        if len(first_row_fields) >= 12:
            try:
                cpu_candidate = int(first_row_fields[11], 16)
                if cpu_candidate in possible_cpus:
                    has_explicit_cpu_id = True
            except ValueError:
                has_explicit_cpu_id = False

        if has_explicit_cpu_id:
            for line in lines:
                values = line.split()
                if len(values) < 12:
                    return None
                cpu_id = int(values[11], 16)
                if cpu_id not in possible_cpus or cpu_id in result:
                    return None
                processed = int(values[0], 16)
                dropped = int(values[1], 16)
                time_squeeze = int(values[2], 16)
                if min(processed, dropped, time_squeeze) < 0:
                    return None
                if cpu_id in online_cpus:
                    result[cpu_id] = {
                        "processed": processed,
                        "dropped": dropped,
                        "time_squeeze": time_squeeze,
                    }
            return result

        # Fallback: if lines count matches online_cpus count, map to sorted online_cpus
        if len(lines) == len(online_cpus):
            cpu_ids_to_map = sorted(online_cpus)
        elif len(lines) == len(possible_cpus):
            cpu_ids_to_map = possible_cpus
        else:
            return None

        for cpu_id, line in zip(cpu_ids_to_map, lines):
            values = line.split()
            if len(values) < 3:
                return None
            processed = int(values[0], 16)
            dropped = int(values[1], 16)
            time_squeeze = int(values[2], 16)
            if min(processed, dropped, time_squeeze) < 0:
                return None
            if cpu_id in online_cpus:
                result[cpu_id] = {
                    "processed": processed,
                    "dropped": dropped,
                    "time_squeeze": time_squeeze,
                }

        return result

    except (OSError, ValueError):
        return None


def read_interface_stats(interface: str | None) -> dict | None:
    """Read interface counters, driver, RX queues and RPS masks."""

    if not interface or "/" in interface or interface in (".", ".."):
        return None

    base = SYS_CLASS_NET_ROOT / interface

    if not base.is_dir():
        return None

    def read_counter(name: str) -> int | None:
        try:
            value = int(
                (base / "statistics" / name).read_text().strip()
            )
            return value if value >= 0 else None
        except (OSError, ValueError):
            return None

    try:
        driver_path = (base / "device" / "driver").resolve(strict=True)
        driver = driver_path.name
    except OSError:
        driver = None

    try:
        queues = sorted(
            path for path in (base / "queues").glob("rx-*")
            if path.is_dir()
        )
        rx_queue_count = len(queues)
    except OSError:
        queues = []
        rx_queue_count = None

    rps_masks = {}

    for queue in queues:
        try:
            rps_masks[queue.name] = (
                queue / "rps_cpus"
            ).read_text().strip()
        except OSError:
            rps_masks[queue.name] = None

    return {
        "rx_bytes": read_counter("rx_bytes"),
        "rx_packets": read_counter("rx_packets"),
        "rx_dropped": read_counter("rx_dropped"),
        "rx_over_errors": read_counter("rx_over_errors"),
        "driver": driver,
        "rx_queue_count": rx_queue_count,
        "rps_masks_by_rx_queue": rps_masks,
    }


def read_network_irq(interface: str | None) -> dict | None:
    """Find IRQ lines associated with a network interface.

    Supports exact interface matches and multi-queue vector labels
    such as enp1s0-rx-0, enp1s0-tx-1, or enp1s0-0.
    Accumulates counts across all dedicated queue IRQ lines. If any matching
    IRQ line is shared with non-interface devices, marks shared=True.
    """

    if not interface:
        return None

    try:
        content = (PROC_ROOT / "interrupts").read_text()
    except OSError:
        return None

    matched_irqs: list[str] = []
    total_count = 0
    sources: list[str] = []
    any_shared = False

    def _matches_interface(lbl: str) -> bool:
        if lbl == interface:
            return True
        if lbl.startswith(f"{interface}-"):
            suffix = lbl[len(interface) + 1:]
            # Must be a queue or vector identifier (e.g. rx-0, tx-0, 0, fp-0)
            if any(suffix.startswith(prefix) for prefix in ("rx", "tx", "fp", "queue")) or suffix.isdigit():
                return True
        return False

    for line in content.splitlines():
        if ":" not in line:
            continue

        irq, details = line.split(":", 1)
        parts = details.split()

        if not parts:
            continue

        try:
            cpu_count = 0
            for value in parts:
                if value.isdigit():
                    cpu_count += 1
                else:
                    break

            if cpu_count == 0:
                continue

            counts = [int(value) for value in parts[:cpu_count]]
            source = " ".join(parts[cpu_count:])

            labels = [
                part.strip().split()[-1]
                for part in source.split(",")
                if part.strip()
            ]

            interface_matches = [lbl for lbl in labels if _matches_interface(lbl)]
            if not interface_matches:
                continue

            matched_irqs.append(irq.strip())
            total_count += sum(counts)
            sources.append(source)
            if len(labels) > len(interface_matches):
                any_shared = True

        except (ValueError, IndexError):
            continue

    if not matched_irqs:
        return None

    return {
        "irq": ",".join(matched_irqs),
        "count": total_count,
        "source": "; ".join(sources),
        "shared": any_shared,
    }


def read_host_metadata() -> dict:
    """Read host identity and snapshot timing metadata."""

    try:
        boot_id = (
            PROC_ROOT / "sys" / "kernel" / "random" / "boot_id"
        ).read_text().strip()
    except OSError:
        boot_id = None

    return {
        "snapshot_timestamp_epoch_s": time.time(),
        "snapshot_monotonic_s": time.monotonic(),
        "host_boot_id": boot_id,
        "host_id": platform.node(),
        "kernel_release": platform.release(),
    }


def _capture_raw(interface: str | None) -> dict[str, Any]:
    """Capture one set of raw readings."""

    metadata = read_host_metadata()

    if not metadata["host_boot_id"]:
        raise RuntimeError(
            "Cannot collect a schema-valid snapshot: host boot ID unavailable"
        )

    return {
        **metadata,
        "interface": interface,
        "cpu_psi": read_cpu_pressure(),
        "softirqs": read_softirqs(),
        "softnet": read_softnet_stat(),
        "interface_stats": read_interface_stats(interface),
        "network_irq": read_network_irq(interface),
    }


def _counter_delta(
    current: int | None,
    previous: int | None,
) -> tuple[int | None, str | None]:
    """Calculate a counter delta and return a reason if unavailable."""

    if current is None:
        return None, "metric_unavailable"

    if previous is None:
        return None, "baseline_missing"

    if current < previous:
        return None, "counter_reset_detected"

    return current - previous, None


def _scalar_delta(
    current: int | None,
    previous: int | None,
    metric: str,
    missing_fields: list[dict[str, str]],
) -> int | None:
    """Calculate a scalar counter delta and record missing values."""

    delta, reason = _counter_delta(current, previous)

    if reason:
        _add_missing(missing_fields, metric, reason)

    return delta


def _per_cpu_deltas(
    current: dict | None,
    previous: dict | None,
    metric: str,
    missing_fields: list[dict[str, str]],
) -> list[dict] | None:
    """Calculate per-CPU deltas with reset and hotplug handling."""

    if current is None:
        _add_missing(missing_fields, metric, "metric_unavailable")
        return None

    if previous is None:
        _add_missing(missing_fields, metric, "baseline_missing")
        return None

    result = []

    for cpu_id in sorted(set(current) | set(previous)):
        if cpu_id not in current:
            delta = None
            reason = "cpu_offline"

        elif cpu_id not in previous:
            delta = None
            reason = "baseline_missing_hotplug"

        else:
            delta, reason = _counter_delta(
                current[cpu_id],
                previous[cpu_id],
            )

        result.append({
            "cpu_id": cpu_id,
            "delta": delta,
            "reason": reason,
        })

        if reason:
            _add_missing(missing_fields, metric, reason)

    return result


def _softnet_metric(
    values: dict | None,
    key: str,
) -> dict | None:
    """Extract one softnet counter for each CPU."""

    if values is None:
        return None

    return {
        cpu_id: cpu_values[key]
        for cpu_id, cpu_values in values.items()
    }


def _build_snapshot(
    current: dict[str, Any],
    previous: dict[str, Any],
) -> dict[str, Any]:
    """Build the schema-compatible snapshot from two raw samples."""

    missing_fields: list[dict[str, str]] = []

    interval = (
        current["snapshot_monotonic_s"]
        - previous["snapshot_monotonic_s"]
    )

    if interval <= 0:
        raise RuntimeError("Snapshot interval must be positive")

    snapshot = {
        "schema_version": "1.2",
        "snapshot_timestamp_epoch_s": current["snapshot_timestamp_epoch_s"],
        "snapshot_monotonic_s": current["snapshot_monotonic_s"],
        "host_boot_id": current["host_boot_id"],
        "interval_duration_s": interval,
        "host_id": current["host_id"],
        "kernel_release": current["kernel_release"],
        "interface": current["interface"],
    }

    # CPU PSI
    psi = current["cpu_psi"]

    psi_fields = {
        "cpu_psi_some_avg10_pct": "avg10",
        "cpu_psi_some_avg60_pct": "avg60",
        "cpu_psi_some_avg300_pct": "avg300",
        "cpu_psi_some_total_us": "total",
    }

    for output_name, source_name in psi_fields.items():
        value = psi.get(source_name) if psi else None
        snapshot[output_name] = value

        if value is None:
            _add_missing(
                missing_fields,
                output_name,
                "metric_unavailable",
            )

    # NET_RX and NET_TX softIRQ deltas
    current_softirqs = current["softirqs"]
    previous_softirqs = previous["softirqs"]

    for source_name, output_name in (
        ("NET_RX", "net_rx_softirq_delta_per_cpu"),
        ("NET_TX", "net_tx_softirq_delta_per_cpu"),
    ):
        current_values = (
            current_softirqs.get(source_name)
            if current_softirqs else None
        )
        previous_values = (
            previous_softirqs.get(source_name)
            if previous_softirqs else None
        )

        snapshot[output_name] = _per_cpu_deltas(
            current_values,
            previous_values,
            output_name,
            missing_fields,
        )

    # Softnet per-CPU deltas
    softnet_fields = {
        "processed": "softnet_processed_delta_per_cpu",
        "dropped": "softnet_dropped_delta_per_cpu",
        "time_squeeze": "softnet_time_squeeze_delta_per_cpu",
    }

    for source_name, output_name in softnet_fields.items():
        snapshot[output_name] = _per_cpu_deltas(
            _softnet_metric(current["softnet"], source_name),
            _softnet_metric(previous["softnet"], source_name),
            output_name,
            missing_fields,
        )

    # Interface statistics
    current_interface = current["interface_stats"]
    previous_interface = previous["interface_stats"]

    interface_fields = {
        "rx_bytes": "interface_rx_bytes_delta",
        "rx_packets": "interface_rx_packets_delta",
        "rx_dropped": "interface_rx_dropped_delta",
        "rx_over_errors": "interface_rx_over_errors_delta",
    }

    for source_name, output_name in interface_fields.items():
        current_value = (
            current_interface.get(source_name)
            if current_interface else None
        )
        previous_value = (
            previous_interface.get(source_name)
            if previous_interface else None
        )

        snapshot[output_name] = _scalar_delta(
            current_value,
            previous_value,
            output_name,
            missing_fields,
        )

    snapshot["interface_driver"] = (
        current_interface.get("driver")
        if current_interface else None
    )

    snapshot["rx_queue_count"] = (
        current_interface.get("rx_queue_count")
        if current_interface else None
    )

    snapshot["rps_masks_by_rx_queue"] = (
        current_interface.get("rps_masks_by_rx_queue")
        if current_interface else None
    )

    for field in ("interface_driver", "rx_queue_count",
                  "rps_masks_by_rx_queue"):
        if snapshot[field] is None:
            _add_missing(missing_fields, field, "metric_unavailable")

    # Network IRQ: shared IRQ totals cannot be attributed to the NIC alone.
    current_irq = current["network_irq"]
    previous_irq = previous["network_irq"]

    snapshot["network_irq_source"] = (
        current_irq["source"] if current_irq else None
    )

    if current_irq is None:
        snapshot["network_irq_delta"] = None
        _add_missing(
            missing_fields,
            "network_irq_delta",
            "metric_unavailable",
        )
        _add_missing(
            missing_fields,
            "network_irq_source",
            "metric_unavailable",
        )

    elif current_irq["shared"]:
        snapshot["network_irq_delta"] = None
        _add_missing(
            missing_fields,
            "network_irq_delta",
            "shared_irq_not_attributable",
        )

    elif previous_irq is None or previous_irq["shared"]:
        snapshot["network_irq_delta"] = None
        _add_missing(
            missing_fields,
            "network_irq_delta",
            "baseline_missing_or_shared",
        )

    elif current_irq["irq"] != previous_irq["irq"]:
        snapshot["network_irq_delta"] = None
        _add_missing(
            missing_fields,
            "network_irq_delta",
            "irq_source_changed",
        )

    else:
        snapshot["network_irq_delta"] = _scalar_delta(
            current_irq["count"],
            previous_irq["count"],
            "network_irq_delta",
            missing_fields,
        )

    snapshot["counter_scope_notes"] = {
        "net_rx_softirq": (
            "Host-wide per-CPU NET_RX activity; not attributable "
            "to an individual interface, flow, or socket."
        ),
        "net_tx_softirq": (
            "Host-wide per-CPU NET_TX activity; not attributable "
            "to an individual interface, flow, or socket."
        ),
        "softnet": (
            "Per-CPU Linux network backlog counters; they are "
            "host-level receive-path indicators."
        ),
        "network_irq": (
            "Shared IRQ counts are not reported as NIC-specific "
            "deltas because other devices may share the IRQ line."
        ),
    }

    snapshot["missing_fields"] = missing_fields

    return snapshot


def collect_snapshot(interface: str | None) -> dict:
    """
    Collect a schema-compatible Linux telemetry snapshot.

    The first call takes two samples separated by
    INITIAL_SAMPLE_DELAY_S to establish a real baseline interval.

    Later calls compare against the previous raw sample.
    Baselines are discarded when the host boot ID or interface changes.
    """

    global _BASELINE

    with _BASELINE_LOCK:
        first_sample = _capture_raw(interface)

        needs_new_baseline = (
            _BASELINE is None
            or _BASELINE["host_boot_id"] != first_sample["host_boot_id"]
            or _BASELINE["interface"] != interface
        )

        if needs_new_baseline:
            previous = first_sample

            # Retry if the machine reboots while establishing the baseline.
            for _ in range(3):
                time.sleep(INITIAL_SAMPLE_DELAY_S)
                current = _capture_raw(interface)

                if current["host_boot_id"] == previous["host_boot_id"]:
                    break

                previous = current
            else:
                raise RuntimeError(
                    "Host boot identity changed repeatedly during sampling"
                )

        else:
            previous = _BASELINE
            current = first_sample

        snapshot = _build_snapshot(current, previous)

        # Keep the raw current counters for the next collection call.
        _BASELINE = current

        return snapshot
