"""OS / kernel / BIOS / NUMA performance tuning knowledge entries.

Target platforms: openEuler 22.03 SP3+, 24.03 LTS; RHEL/CentOS 8+; Ubuntu 22.04+.
These recommendations focus on stability and repeatability for benchmark scenarios.
"""

from src.knowledge.models import KnowledgeEntry

OS_KNOWLEDGE: list[KnowledgeEntry] = [
    # ── NUMA binding ────────────────────────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="numactl",
        title="NUMA Binding for Database Workloads",
        category="best_practice",
        content=(
            "Bind the database process to a single NUMA node to avoid cross-node "
            "memory access latency (60-120 ns vs 100-300 ns remote). Use: "
            "`numactl --cpunodebind=0 --membind=0 <redis-server>` "
            "For multi-instance deployments, pin each instance to a different "
            "NUMA node. Monitor with `numastat -p <pid>`. "
            "On openEuler, also set `kernel.numa_balancing=0` to prevent the "
            "kernel from migrating pages between nodes during benchmarks."
        ),
        source="Intel Optimization Manual; openEuler Performance Tuning Guide",
        confidence=0.95,
    ),
    KnowledgeEntry(
        system="linux",
        parameter_name="numa_balancing",
        title="Disable NUMA Auto-Balancing During Benchmarks",
        category="tuning_guide",
        content=(
            "`echo 0 > /proc/sys/kernel/numa_balancing` or sysctl "
            "`kernel.numa_balancing=0`. Auto-balancing causes unpredictable "
            "page migrations that introduce 5-15% variance in benchmark results. "
            "Re-enable after benchmarking if running production mixed workloads."
        ),
        source="openEuler Tuning Guide 22.03",
        confidence=0.90,
    ),

    # ── CPU governor & power management ──────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="cpu_governor",
        title="Set CPU Governor to Performance",
        category="best_practice",
        content=(
            "Set scaling governor to `performance` for all cores: "
            "`cpupower frequency-set -g performance` or "
            "`echo performance > /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor`. "
            "Also disable intel_pstate powersave on Intel: "
            "kernel boot param `intel_pstate=disable`. "
            "On openEuler ARM (Kunpeng), use `cppc_cpufreq performance`. "
            "This eliminates CPU frequency scaling jitter (0.5-2 ms latency spikes)."
        ),
        source="Red Hat Performance Tuning Guide",
        confidence=0.95,
    ),
    KnowledgeEntry(
        system="linux",
        parameter_name="cpu_idle_states",
        title="Limit Deep C-States for Low-Latency Workloads",
        category="tuning_guide",
        content=(
            "Deep C-states (C6+) add 50-200 us wakeup latency. Limit to C1: "
            "`cpupower idle-set -d 2` or kernel param `processor.max_cstate=1 "
            "intel_idle.max_cstate=0`. "
            "For latency-sensitive Redis/MySQL workloads targeting p99 < 1ms, "
            "this is essential. Trade-off: +5-15 W power consumption per socket."
        ),
        source="Intel SDM Vol.3; openEuler Latency Tuning",
        confidence=0.90,
    ),

    # ── BIOS settings ────────────────────────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="bios_hyperthreading",
        title="BIOS: Hyper-Threading Strategy",
        category="best_practice",
        content=(
            "For CPU-bound database workloads (Redis single-threaded event loop): "
            "DISABLE Hyper-Threading in BIOS. HT siblings share L1/L2 cache and "
            "execution units, causing 10-25% throughput variance under load. "
            "For multi-threaded workloads (MySQL with high innodb_read_io_threads): "
            "ENABLE HT but use `taskset` to pin worker threads to physical cores first. "
            "Check: `lscpu | grep 'Thread(s) per core'`"
        ),
        source="Intel Optimization Reference Manual",
        confidence=0.90,
    ),
    KnowledgeEntry(
        system="linux",
        parameter_name="bios_turbo_boost",
        title="BIOS: Turbo Boost — Determinism vs Peak Throughput",
        category="tuning_guide",
        content=(
            "Turbo Boost increases peak throughput but introduces frequency "
            "fluctuations. For stable benchmark results: DISABLE Turbo Boost "
            "in BIOS. This gives deterministic per-core frequency. "
            "For max throughput: ENABLE but be aware of ±5-8% run-to-run variance. "
            "On openEuler, also check `turbostat` output for actual frequency."
        ),
        source="Intel Turbo Boost Technology Guide",
        confidence=0.90,
    ),
    KnowledgeEntry(
        system="linux",
        parameter_name="bios_power_profile",
        title="BIOS: Power Profile = Maximum Performance",
        category="best_practice",
        content=(
            "Set BIOS power profile to 'Performance' or 'Custom' with: "
            "- C-states: C1 only (or disabled for latency-critical) "
            "- Package C-state: No limit "
            "- Energy Performance Bias: 0 (max performance) "
            "- DRAM latency: minimize (disable DRAM power saving) "
            "On openEuler / Kunpeng: BIOS → Performance → set 'Power Policy' to "
            "'Performance' and disable 'Dynamic Power Savings'."
        ),
        source="openEuler 22.03 SP3 BIOS Tuning Guide",
        confidence=0.85,
    ),

    # ── Kernel parameters ────────────────────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="vm_dirty_ratio",
        title="Kernel Dirty Page Ratio for Database Servers",
        category="tuning_guide",
        content=(
            "Reduce `vm.dirty_ratio` from default 20 to 5-10 for database servers "
            "with lots of RAM. At 20% of 128 GB (= 25.6 GB dirty), sync writes "
            "cause multi-second stalls. Set via sysctl: "
            "`vm.dirty_ratio=5` and `vm.dirty_background_ratio=3`. "
            "Also set `vm.dirty_expire_centisecs=1000` (10s) to flush sooner. "
            "For Redis with AOF: use lower values (3/1) since AOF buffers separately."
        ),
        source="Linux Kernel Documentation; Redis Administration Guide",
        confidence=0.90,
    ),
    KnowledgeEntry(
        system="linux",
        parameter_name="vm_swappiness",
        title="Disable or Minimize Swap for Database Workloads",
        category="best_practice",
        content=(
            "Set `vm.swappiness=1` (not 0). On modern kernels (4.0+), swappiness=1 "
            "still allows emergency swapping under extreme memory pressure but "
            "strongly avoids it. swappiness=0 was redefined in kernel 5.x to mean "
            "'never swap unless OOM'. For pure in-memory databases (Redis), also "
            "ensure `maxmemory` is set below available RAM to prevent OOM killer."
        ),
        source="Linux Kernel Documentation sysctl/vm.txt",
        confidence=0.95,
    ),
    KnowledgeEntry(
        system="linux",
        parameter_name="net_core_somaxconn",
        title="TCP Backlog for High Connection Rates",
        category="tuning_guide",
        content=(
            "Increase `net.core.somaxconn` to 4096 or 65535 to match Redis/mysql "
            "tcp-backlog and accept queue. Also tune: "
            "`net.ipv4.tcp_max_syn_backlog=8192` "
            "`net.core.netdev_max_backlog=10000` "
            "`net.ipv4.tcp_fastopen=3` (enable TFO for both client and server). "
            "These prevent connection drops under >10K connections/sec."
        ),
        source="Redis Performance Documentation",
        confidence=0.90,
    ),
    KnowledgeEntry(
        system="linux",
        parameter_name="kernel_sched_migration",
        title="Reduce Scheduler Migration for Pinned Workloads",
        category="tuning_guide",
        content=(
            "When using CPU pinning (taskset/numactl), reduce scheduler migration "
            "cost: `kernel.sched_migration_cost_ns=5000000` (5ms, default 0.5ms). "
            "`kernel.sched_min_granularity_ns=10000000` (10ms) for throughput. "
            "`kernel.sched_wakeup_granularity_ns=15000000` (15ms). "
            "These reduce involuntary context switches for pinned database threads. "
            "On openEuler with Kunpeng: also tune `kernel.sched_domain.cpu0.domain0`."
        ),
        source="Red Hat Performance Tuning; openEuler Scheduler Guide",
        confidence=0.85,
    ),

    # ── Huge Pages ───────────────────────────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="huge_pages",
        title="Transparent Huge Pages: Disable for Databases, Use Explicit Huge Pages",
        category="best_practice",
        content=(
            "THP (Transparent Huge Pages) causes unpredictable compaction stalls "
            "(10-100 ms latency spikes). DISABLE: "
            "`echo never > /sys/kernel/mm/transparent_hugepage/enabled`. "
            "For Redis: set `THP=never` in init script or systemd service. "
            "For MySQL with large buffer pools (32GB+): use explicit 2MB or 1GB "
            "huge pages via `hugetlbfs` + `nr_hugepages=N`. This gives the benefit "
            "of huge pages (reduced TLB misses) without THP compaction stalls. "
            "On openEuler: `echo 1024 > /proc/sys/vm/nr_hugepages`"
        ),
        source="Redis Latency Monitoring; MySQL InnoDB Documentation",
        confidence=0.95,
    ),

    # ── IRQ affinity ─────────────────────────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="irq_affinity",
        title="IRQ Affinity: Isolate Network IRQs from Database Cores",
        category="tuning_guide",
        content=(
            "Pin network card IRQs to CPU cores NOT used by the database process. "
            "For a 16-core machine running Redis on cores 0-3: "
            "`echo 00f0 > /proc/irq/<irq_num>/smp_affinity` (IRQs to cores 4-7). "
            "Use `irqbalance --oneshot` or disable irqbalance entirely and set "
            "affinity manually for reproducible results. "
            "Check with: `cat /proc/interrupts | grep eth0`"
        ),
        source="Intel DPDK Performance Guide",
        confidence=0.85,
    ),

    # ── Filesystem / I/O ─────────────────────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="io_scheduler",
        title="I/O Scheduler: none/noop for NVMe, mq-deadline for SATA/SAS",
        category="best_practice",
        content=(
            "NVMe drives: `echo none > /sys/block/nvme0n1/queue/scheduler`. "
            "The NVMe driver has internal queuing; kernel scheduling adds overhead. "
            "SATA/SAS SSDs: `mq-deadline` or `kyber`. "
            "Also tune: `/sys/block/<dev>/queue/nr_requests=1024` (reduce from "
            "default 256 for high-IOPS SSDs) and `read_ahead_kb=128` for database "
            "random I/O workloads. On openEuler, NVMe multi-queue: set "
            "`nvme_core.default_ps_max_latency_us=0` kernel param for lowest latency."
        ),
        source="Linux Block Layer Documentation",
        confidence=0.90,
    ),

    # ── Memory allocation ─────────────────────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="vm_overcommit",
        title="Memory Overcommit: Disable for Redis, Enable for MySQL",
        category="tuning_guide",
        content=(
            "Redis: `vm.overcommit_memory=1` (always overcommit) — Redis uses "
            "fork() for RDB snapshots and needs address space without allocation. "
            "MySQL with InnoDB: `vm.overcommit_memory=0` (heuristic) or `2` "
            "(strict, with ratio). Set `vm.overcommit_ratio=80` to limit overcommit. "
            "On openEuler 24.03 LTS, the default overcommit is 0 (heuristic) "
            "which is safe for most database workloads."
        ),
        source="Redis Administration; openEuler 24.03 Release Notes",
        confidence=0.90,
    ),

    # ── openEuler specific ───────────────────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="openeuler_kunpeng",
        title="openEuler + Kunpeng 920 Specific Optimizations",
        category="tuning_guide",
        content=(
            "Kunpeng 920 (ARM64) specific tuning on openEuler 22.03 SP3+: "
            "- CPU: `cppc_cpufreq performance` governor via `cpupower` "
            "- Prefetch: enable hardware prefetch in BIOS (important for ARM) "
            "- SVE: disable unless actively used (`kernel.sve_default_vector_length=0`) "
            "- NUMA: Kunpeng 920 has 2-4 sockets, each with 2 NUMA nodes. Pin "
            "  database processes to local socket+node for best latency. "
            "- DIE: Kunpeng 920 v2 uses DIE interconnect; ensure BIOS interleave "
            "  is set to 'NUMA' not 'Disabled' for proper multi-socket operation. "
            "- `numactl -H` to see node distances; cross-die latency is ~2.5x local."
        ),
        source="openEuler 22.03 SP3 Kunpeng Tuning Guide; Huawei Kunpeng 920 Datasheet",
        confidence=0.85,
    ),

    # ── Benchmark environment checklist ───────────────────────────────────
    KnowledgeEntry(
        system="linux",
        parameter_name="benchmark_checklist",
        title="Pre-Benchmark Environment Stability Checklist",
        category="best_practice",
        content=(
            "Before running any database benchmark, verify: "
            "1. CPU governor = performance (cpupower frequency-info) "
            "2. THP = never (cat /sys/kernel/mm/transparent_hugepage/enabled) "
            "3. NUMA balancing = 0 (cat /proc/sys/kernel/numa_balancing) "
            "4. Swap disabled or swappiness=1 (sysctl vm.swappiness) "
            "5. irqbalance stopped (systemctl stop irqbalance) "
            "6. No cron jobs / background processes (systemctl list-timers) "
            "7. Filesystem mounted with noatime (mount | grep noatime) "
            "8. ulimit -n 65535 (file descriptors) "
            "9. Network: disable LRO/GRO if measuring small-packet latency "
            "10. Take a 5-minute idle baseline (sar -u 1 300) to verify "
            "    system is quiesced before starting."
        ),
        source="Compiled from multiple performance engineering guides",
        confidence=0.95,
    ),
]
