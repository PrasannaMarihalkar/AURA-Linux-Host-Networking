# AURA Known Limitations and Operational Constraints

**Project:** AURA — Adaptive Root-Cause-Aware Control for Tail-Latency Optimization in Linux Host Networking  
**Document Type:** Technical Systems & Operational Constraint Disclosure  
**Target:** Engineering Maintainers, Research Evaluators, and System Operators  

---

## 1. Operating System and Virtualization Environment Limitations

### 1.1 Windows Subsystem for Linux (WSL2) Constraints
While the AURA codebase is architected with platform abstractions to run on standard Linux and CI environments, development or testing inside WSL2 entails specific virtualization nuances:
- **Synthetic Timers and Scheduler Jitter:** WSL2 runs on a lightweight Hyper-V utility VM. Microsecond-level monotonic clock readings (`time.monotonic()`) may exhibit synthetic virtualization jitter not present on bare-metal hardware.
- **Tickless Kernel / Idle States:** Host-level sleep transitions and Hyper-V CPU thread scheduling can introduce artificial latency spikes into benchmark measurements.
- **Restricted Cgroup Hierarchy:** Default WSL2 installations do not initialize systemd-managed cgroups by default. Running cgroup weight adjustments requires booting with `[boot]\nsystemd=true` in `/etc/wsl.conf` or executing on native Linux hosts.

### 1.2 Virtualized Network Drivers (veth and virtio-net)
- **Single Hardware Queue Constraints:** Virtual network interfaces (`veth`, synthetic hypervisor NICs) frequently expose only a single RX queue (`rx-0`). Multi-queue hardware features (e.g. 8 or 16 MSI-X queues common on Intel/Mellanox enterprise NICs) are unavailable in basic virtualized setups.
- **Synthetic Interrupt Counters:** Virtualized guest kernels often bundle network processing into timer-driven polling or single-vector virtual interrupts, dampening visible hardware interrupt rate differences across `/proc/interrupts`.
- **RPS Mask Verification:** In single-queue virtual interfaces, setting RPS masks across multiple CPU cores will distribute softirq processing across cores, but cannot resolve hardware interrupt head-of-line blocking.

---

## 2. Benchmark Load Model Constraints

### 2.1 Closed-Loop Fixed Concurrency Model
The built-in benchmark client (`aura/client.py`) utilizes a fixed worker thread pool ($C$ concurrent workers) operating in a closed loop:
- **Coordinated Omission Exposure:** In a closed-loop system, client threads do not issue requests on an independent Poisson schedule. If the server pauses or experiences severe tail latency, client workers block waiting for responses. During the blockage, no new requests are generated. Consequently, the measured latency distribution fails to record the delays experienced by requests that would have arrived during the stall interval.
- **Mitigation & Future Evolution:** While AURA collects high-resolution raw latency sidecars and measures accurate window durations, users conducting production-grade performance studies should complement the built-in client with open-loop load generators (e.g., `wrk2` or `vegeta`) that maintain an invariant arrival schedule regardless of server response times.

---

## 3. Subsystem and Control Boundaries

### 3.1 Gate B Observe-Only Controller Status
- In Gate B (current released milestone), the controller operates strictly in **observe-only mode**.
- Every diagnosis decision generates an action record with `action: no_op` and `execution_status: not_attempted` or `dry_run`.
- The controller **never modifies host system state autonomously**.
- The resource control modules (`aura/controls/cgroup_cpu.py` and `aura/controls/rpsctl.py`) are strictly decoupled and reserved for Gate C execution and administrative maintenance scripts (`scripts/setup_slices.sh`, `scripts/restore.sh`).

### 3.2 Read-Only RPS Subsystem Scope
- In MVP-1, RPS management in `aura/controls/rpsctl.py` is restricted to **inspection and configuration backup/restore**.
- Active programmatic actuation of `/sys/class/net/<interface>/queues/rx-*/rps_cpus` raises a documented `NotImplementedError` to safeguard host networking configurations during automated testing.
- Live RPS tuning is performed safely via the administrative helper scripts under superuser privileges.

---

## 4. Telemetry and Kernel Accounting Quirks

### 4.1 Linux CPU Pressure Stall Information (PSI)
- **System-Level CPU PSI `full` Undefined:** Under upstream Linux kernel semantics (`docs/accounting/psi.html`), `full` CPU pressure (where all runnable tasks are simultaneously stalled on CPU) is undefined at system level and reported as zero on newer kernels. AURA intentionally ignores `cpu_psi_full` for system diagnosis, relying exclusively on `cpu_psi_some`.
- **PSI Availability:** On older kernels (pre-4.20) or distributions built without `CONFIG_PSI=y`, `/proc/pressure/cpu` does not exist. AURA reports `cpu_psi_status: unavailable` and gracefully skips PSI-based diagnosis rules without crashing or fabricating zeros.

### 4.2 Shared Interrupts and Dynamic Topologies
- **Shared IRQ Vectors:** In cloud virtual machines, a single interrupt line may be shared between storage (NVMe/virtio-blk) and networking devices. AURA inspects interrupt line names and ignores shared or non-network interrupts to avoid attributing storage I/O bursts to network traffic.
- **CPU Hotplug:** Cloud hypervisors and power management governors may dynamically offline CPU cores. AURA's telemetry engine tracks dynamic CPU ID sets, reporting hotplug events cleanly without index errors or corrupted deltas.
