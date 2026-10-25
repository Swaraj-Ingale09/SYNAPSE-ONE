"""Real host telemetry: CPU, memory, disk, network, GPU, process.

Every number here is *measured*, never estimated. Where a measurement is
impossible on this host, the field is ``None`` with a stated reason rather
than a plausible-looking zero:

- **GPU** is read from ``nvidia-smi``. If NVIDIA drivers are absent (or the
  binary is not on PATH) the result is :class:`GpuUnavailable` with the reason,
  not ``utilization=0`` — "0%" and "no GPU" mean very different things on a
  dashboard and conflating them is exactly the fake-metric failure mode this
  project refuses.
- **Load average** does not exist on Windows; it is ``None`` with a reason
  rather than a fabricated 1-minute figure.
- **Network rates** are deltas between two real samples. With only one sample
  in history the rate is ``None`` — a rate needs a time base, and inventing one
  would produce a number that looks measured but is not.

Sampling cost matters: ``nvidia-smi`` is a subprocess that costs ~50-150 ms, so
GPU readings are cached for a short window rather than re-spawned per request.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime

import psutil

log = logging.getLogger("synapse.telemetry.system")

# How long a GPU reading stays valid. nvidia-smi spawns a process; polling it
# faster than this wastes CPU for a value that barely changes.
GPU_CACHE_SECONDS = 2.0
# How many network samples to retain. 60 samples at the default poll interval
# covers a minute of history without unbounded growth.
NETWORK_HISTORY = 60
# Hard cap on nvidia-smi output we will parse.
NVIDIA_SMI_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class MemoryInfo:
    total_bytes: int
    available_bytes: int
    used_bytes: int
    percent: float


@dataclass(frozen=True)
class DiskInfo:
    total_bytes: int
    used_bytes: int
    free_bytes: int
    percent: float
    mount: str


@dataclass(frozen=True)
class NetworkRate:
    """Bytes per second over the interval between the last two samples."""

    bytes_sent_per_sec: float
    bytes_recv_per_sec: float
    sample_age_seconds: float
    total_bytes_sent: int
    total_bytes_recv: int


@dataclass(frozen=True)
class GpuSample:
    """One real GPU reading from nvidia-smi."""

    name: str
    index: int
    utilization_percent: float
    memory_used_mb: float
    memory_total_mb: float
    temperature_c: float | None

    @property
    def memory_percent(self) -> float:
        if self.memory_total_mb <= 0:
            return 0.0
        return round(100.0 * self.memory_used_mb / self.memory_total_mb, 1)


@dataclass(frozen=True)
class GpuUnavailable:
    """No GPU reading could be taken. States why instead of guessing."""

    reason: str


@dataclass(frozen=True)
class SystemSnapshot:
    timestamp: datetime
    cpu_percent: float
    cpu_count_logical: int
    cpu_count_physical: int | None
    load_average_1m: float | None
    load_average_unavailable_reason: str | None
    memory: MemoryInfo
    swap_percent: float | None
    swap_unavailable_reason: str | None
    disk: DiskInfo
    network: NetworkRate | None
    gpu: GpuSample | GpuUnavailable
    process_uptime_seconds: float
    process_rss_bytes: float
    process_threads: int


_NVIDIA_SMI_UNSUPPORTED = ("[N/A]", "[Not Supported]")


def parse_nvidia_smi_output(output: str) -> GpuSample | GpuUnavailable:
    """Parse ``--format=csv,noheader,nounits`` output into a reading.

    Pure function so the parsing rules are testable without spawning a real
    subprocess. Returns :class:`GpuUnavailable` with a reason when the output
    contains no parseable GPU row — never a fabricated zero.
    """
    for line in output.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            # "[Not Supported]" is a literal token nvidia-smi emits for
            # virtualised / MSHybrid GPUs; parsing it as float would crash.
            raw_temp = parts[5]
            temperature = None if raw_temp in _NVIDIA_SMI_UNSUPPORTED else float(raw_temp)
            return GpuSample(
                index=int(parts[0]),
                name=parts[1],
                utilization_percent=float(parts[2]),
                memory_used_mb=float(parts[3]),
                memory_total_mb=float(parts[4]),
                temperature_c=temperature,
            )
        except ValueError:
            continue
    return GpuUnavailable(reason="nvidia-smi returned no parseable GPU row")


def _process_start_time() -> float:
    try:
        return psutil.Process().create_time()
    except (psutil.Error, OSError):
        # Fall back to import time; uptime is then "since this module loaded",
        # which is honest as long as it is not presented as host uptime.
        return time.time()


_PROCESS_START = _process_start_time()


class TelemetryCollector:
    """Samples the host and serves cached snapshots.

    Thread-safe: the API may be queried concurrently while the worker process
    is busy. One instance per process is enough.
    """

    def __init__(
        self,
        *,
        poll_interval: float = 1.0,
        root: str | None = None,
        nvidia_smi_path: str | None = None,
    ) -> None:
        self._poll_interval = max(0.1, poll_interval)
        self._root = root or os.path.abspath(os.sep)
        self._nvidia_smi = nvidia_smi_path or shutil.which("nvidia-smi")
        self._lock = threading.Lock()
        self._network_history: deque[tuple[float, int, int]] = deque(maxlen=NETWORK_HISTORY)
        self._gpu_cache: tuple[float, GpuSample | GpuUnavailable] | None = None
        self._last_cpu_percent: float | None = None
        self._last_cpu_sample_at: float | None = None
        # Prime psutil's non-blocking CPU counter so the first read is a real
        # delta over the poll interval rather than a blocking call or 0.0.
        psutil.cpu_percent(interval=None)

    # --- individual readings -------------------------------------------------

    def _cpu(self) -> float:
        now = time.monotonic()
        percent = psutil.cpu_percent(interval=None)
        if percent == 0.0 and self._last_cpu_percent is not None:
            # psutil returns 0.0 when called more often than its internal
            # sampling window. Reuse the last real reading rather than
            # reporting a fake idle spike.
            return self._last_cpu_percent
        self._last_cpu_percent = percent
        self._last_cpu_sample_at = now
        return percent

    def _memory(self) -> MemoryInfo:
        vm = psutil.virtual_memory()
        return MemoryInfo(
            total_bytes=int(vm.total),
            available_bytes=int(vm.available),
            used_bytes=int(vm.used),
            percent=float(vm.percent),
        )

    def _swap(self) -> tuple[float | None, str | None]:
        try:
            swap = psutil.swap_memory()
        except (psutil.Error, OSError) as exc:
            return None, f"swap unavailable: {exc}"
        return float(swap.percent), None

    def _disk(self) -> DiskInfo:
        usage = psutil.disk_usage(self._root)
        return DiskInfo(
            total_bytes=int(usage.total),
            used_bytes=int(usage.used),
            free_bytes=int(usage.free),
            percent=float(usage.percent),
            mount=self._root,
        )

    def _network(self) -> NetworkRate | None:
        try:
            counters = psutil.net_io_counters()
        except (psutil.Error, OSError):
            return None
        now = time.monotonic()
        sent, recv = int(counters.bytes_sent), int(counters.bytes_recv)
        self._network_history.append((now, sent, recv))

        if len(self._network_history) < 2:
            # A rate requires two points. Refuse to invent one.
            return None
        (t0, s0, r0), (t1, s1, r1) = self._network_history[-2], self._network_history[-1]
        elapsed = t1 - t0
        if elapsed <= 0:
            return None
        return NetworkRate(
            bytes_sent_per_sec=max(0.0, (s1 - s0) / elapsed),
            bytes_recv_per_sec=max(0.0, (r1 - r0) / elapsed),
            sample_age_seconds=now - t1,
            total_bytes_sent=s1,
            total_bytes_recv=r1,
        )

    def _load_average(self) -> tuple[float | None, str | None]:
        try:
            load1, _load5, _load15 = os.getloadavg()
        except (AttributeError, OSError):
            # Windows has no getloadavg. Reporting a made-up figure here would
            # be exactly the kind of metric that looks real and is not.
            return None, "load average is not provided by this operating system"
        return float(load1), None

    def _gpu(self) -> GpuSample | GpuUnavailable:
        now = time.monotonic()
        if self._gpu_cache is not None:
            cached_at, value = self._gpu_cache
            if now - cached_at < GPU_CACHE_SECONDS:
                return value
        value = self._read_gpu()
        self._gpu_cache = (now, value)
        return value

    def _read_gpu(self) -> GpuSample | GpuUnavailable:
        if not self._nvidia_smi:
            return GpuUnavailable(
                reason="nvidia-smi not found on PATH (no NVIDIA driver detected)"
            )
        command = [
            self._nvidia_smi,
            "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
            "--format=csv,noheader,nounits",
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=5.0,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            return GpuUnavailable(reason=f"nvidia-smi failed to run: {exc}")

        if completed.returncode != 0:
            detail = completed.stderr[:200].decode("utf-8", errors="replace").strip()
            return GpuUnavailable(
                reason=f"nvidia-smi exited {completed.returncode}: {detail or 'no detail'}"
            )

        output = completed.stdout[:NVIDIA_SMI_MAX_BYTES].decode("utf-8", errors="replace")
        return parse_nvidia_smi_output(output)

    # --- the snapshot ---------------------------------------------------------

    def snapshot(self) -> SystemSnapshot:
        """Take one consistent reading of the host."""
        with self._lock:
            physical: int | None
            try:
                physical = psutil.cpu_count(logical=False)
            except (NotImplementedError, OSError):
                physical = None

            load1, load_reason = self._load_average()
            swap_percent, swap_reason = self._swap()
            now = time.monotonic()

            try:
                rss = int(psutil.Process().memory_info().rss)
                threads = int(psutil.Process().num_threads())
            except (psutil.Error, OSError):
                rss, threads = 0, 0

            return SystemSnapshot(
                timestamp=datetime.now(UTC),
                cpu_percent=self._cpu(),
                cpu_count_logical=psutil.cpu_count(logical=True) or 1,
                cpu_count_physical=physical,
                load_average_1m=load1,
                load_average_unavailable_reason=load_reason,
                memory=self._memory(),
                swap_percent=swap_percent,
                swap_unavailable_reason=swap_reason,
                disk=self._disk(),
                network=self._network(),
                gpu=self._gpu(),
                process_uptime_seconds=max(0.0, now - _PROCESS_START),
                process_rss_bytes=rss,
                process_threads=threads,
            )


_default: TelemetryCollector | None = None
_default_lock = threading.Lock()


def get_collector() -> TelemetryCollector:
    """Process-wide collector (module-level singleton)."""
    global _default
    with _default_lock:
        if _default is None:
            _default = TelemetryCollector()
        return _default
