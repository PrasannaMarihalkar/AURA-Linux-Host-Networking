# AURA Linux Host Telemetry Signals

## 1. Purpose

The AURA telemetry collector reads Linux kernel and network interface
statistics. It provides measurements that can help other AURA components
understand CPU pressure and network receive-path activity.

The collector is read-only and does not require root privileges for
normal operation.

## 2. Data Sources

| Signal | Linux source | Information |
|---|---|---|
| CPU PSI | `/proc/pressure/cpu` | System-level CPU pressure |
| NET_RX / NET_TX | `/proc/softirqs` | Per-CPU network softIRQ counters |
| Softnet | `/proc/net/softnet_stat` | Processed, dropped and time-squeeze counters |
| Interface counters | `/sys/class/net/<interface>/statistics/` | RX bytes, packets and drops |
| Driver | `/sys/class/net/<interface>/device/driver` | Network driver name |
| RX queues | `/sys/class/net/<interface>/queues/` | Receive queue count |
| RPS | `rps_cpus` under RX queue directories | Receive Packet Steering CPU masks |
| IRQ | `/proc/interrupts` | IRQ count and associated devices |
| Boot identity | `/proc/sys/kernel/random/boot_id` | Linux boot UUID |

## 3. CPU Pressure

The collector reads only the `some` line from `/proc/pressure/cpu`.

It reports:

- `cpu_psi_some_avg10_pct`
- `cpu_psi_some_avg60_pct`
- `cpu_psi_some_avg300_pct`
- `cpu_psi_some_total_us`

System-level CPU `full` pressure is intentionally excluded.

## 4. Network SoftIRQs

The collector reads `NET_RX` and `NET_TX` counters from `/proc/softirqs`.

The values are cumulative counters. The collector calculates differences
between successive samples and preserves the CPU ID for every result.

These counters are host-wide. They cannot identify an individual network
interface, HTTP request, socket or flow as the cause of the activity.

## 5. Softnet Statistics

The first three hexadecimal columns of `/proc/net/softnet_stat` represent:

1. Processed packets.
2. Dropped packets.
3. Time-squeeze events.

The collector maps the lines to CPU IDs using the system's possible CPU
list and considers online CPUs for the current sample.

## 6. Interface Statistics

The collector reads:

- RX bytes.
- RX packets.
- RX dropped packets.
- RX overrun errors (`rx_over_errors`).
- Driver name.
- RX queue count.
- RPS CPU masks.

The byte, packet and drop values are cumulative kernel counters. The
snapshot reports their interval differences.

## 7. Interrupt Attribution

The collector searches `/proc/interrupts` for IRQ lines associated with
the selected network interface.

If an IRQ line is shared by multiple devices, its total count cannot be
safely attributed to the network interface alone. In that case,
`network_irq_delta` is null and `missing_fields` records
`shared_irq_not_attributable`.

## 8. Counter Deltas

For a valid interval, the collector subtracts the previous counter from
the current counter.

Special cases:

- Missing current value: `metric_unavailable`.
- Missing baseline value: `baseline_missing`.
- Decreasing counter: `counter_reset_detected`.
- Disappeared CPU: `cpu_offline`.
- Newly appeared CPU: `baseline_missing_hotplug`.

Unavailable values are represented as null. The collector never
substitutes zero for an unavailable measurement.

## 9. Snapshot Timing and Baselines

The collector records:

- Wall-clock time using `time.time()`.
- Monotonic time using `time.monotonic()`.
- Host boot identity from the Linux boot UUID.

The first call takes an initial sample, waits one second, and takes a
second sample. This provides a real positive interval for the first
schema-valid snapshot.

Subsequent calls compare against the previous raw sample.

A baseline is discarded when the host boot identity or selected
interface changes.

The baseline is maintained in process memory. Restarting the collector
requires a new initial sampling interval.

## 10. Scope and Limitations

- NET_RX and NET_TX are host-wide per-CPU measurements.
- Softnet counters describe host-level network processing.
- Shared IRQ counts are not treated as network-only counts.
- The collector does not diagnose bottlenecks.
- The collector does not select or execute system tuning actions.
- The collector never writes to `/proc` or `/sys`.
