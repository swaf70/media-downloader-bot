"""قراءة استهلاك الذاكرة داخل حاوية Linux (للتشخيص)."""

from __future__ import annotations

from pathlib import Path


def memory_mb() -> tuple[float, float]:
    """يعيد (ذاكرة عملية Python، إجمالي ذاكرة الحاوية) بالميجابايت."""
    rss = 0.0
    try:
        for line in Path("/proc/self/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                rss = float(line.split()[1]) / 1024.0
                break
    except Exception:  # noqa: BLE001
        pass

    total = 0.0
    for candidate in (
        "/sys/fs/cgroup/memory.current",
        "/sys/fs/cgroup/memory/memory.usage_in_bytes",
    ):
        try:
            total = int(Path(candidate).read_text(encoding="utf-8").strip()) / 1048576.0
            break
        except Exception:  # noqa: BLE001
            continue
    return rss, total
