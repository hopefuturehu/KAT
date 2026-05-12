"""Host hardware detection utilities."""

import os
import platform


def detect_hardware() -> dict:
    """Detect host hardware specifications."""
    spec = {
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python_version": platform.python_version(),
        "cpu_count_logical": os.cpu_count(),
        "cpu_count_physical": _physical_cores(),
        "total_ram_gb": _total_ram_gb(),
    }
    return spec


def _physical_cores() -> int | None:
    try:
        import psutil
        return psutil.cpu_count(logical=False)
    except ImportError:
        return None


def _total_ram_gb() -> float | None:
    try:
        import psutil
        return round(psutil.virtual_memory().total / (1024**3), 1)
    except ImportError:
        try:
            with open("/proc/meminfo") as f:
                for line in f:
                    if "MemTotal" in line:
                        kb = int(line.split()[1])
                        return round(kb / (1024**2), 1)
        except Exception:
            pass
    return None
