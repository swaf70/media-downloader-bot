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


def details() -> str:
    """تفصيل مختصر لمصدر الذاكرة: anon/file/slab + أكبر العمليات."""
    parts: list[str] = []
    try:
        stat = Path("/sys/fs/cgroup/memory.stat").read_text(encoding="utf-8")
        wanted = {"anon", "file", "slab", "sock", "stack"}
        for line in stat.splitlines():
            key, value = line.split(" ", 1)
            if key in wanted:
                parts.append(f"{key}={int(value) / 1048576:.0f}MB")
    except Exception:  # noqa: BLE001
        pass

    procs: list[tuple[float, str, str]] = []
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                text = (entry / "status").read_text(encoding="utf-8")
            except Exception:  # noqa: BLE001
                continue
            name = ""
            rss = 0.0
            for line in text.splitlines():
                if line.startswith("Name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("VmRSS:"):
                    rss = float(line.split()[1]) / 1024.0
            if rss > 5:
                procs.append((rss, entry.name, name))
    except Exception:  # noqa: BLE001
        pass
    procs.sort(reverse=True)
    parts.append("procs=" + ",".join(f"{n}:{p}:{r:.0f}MB" for r, p, n in procs[:6]))
    return " ".join(parts)
