"""قراءة استهلاك الذاكرة داخل حاوية Linux (للتشخيص)."""

from __future__ import annotations

import ctypes
import os
import sysconfig
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


def evict_file_cache() -> float:
    """تفريغ جزء من صفحات الملفات القابلة للاستعادة قبل الترميز.

    يعيد مقدار الذاكرة (MB) الذي انخفض بعد المحاولة. يفشل بصمت إن منعت
    الحاوية ذلك؛ لا يؤثر على صحة المعالجة.
    """
    before = memory_mb()[1]
    try:
        Path("/proc/sys/vm/drop_caches").write_text("3\n", encoding="ascii")
    except Exception:  # noqa: BLE001
        pass

    try:
        libc = ctypes.CDLL(None, use_errno=True)
        posix_fadvise = libc.posix_fadvise
        posix_fadvise.argtypes = [
            ctypes.c_int,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.c_int,
        ]
        posix_fadvise.restype = ctypes.c_int
        dontneed = 4  # POSIX_FADV_DONTNEED
        roots = [
            Path(sysconfig.get_paths().get("purelib", "")),
            Path("/usr/local/lib/python3.12/site-packages"),
        ]
        seen: set[Path] = set()
        for root in roots:
            if not root or root in seen or not root.is_dir():
                continue
            seen.add(root)
            for item in root.rglob("*"):
                try:
                    if not item.is_file():
                        continue
                    fd = os.open(item, os.O_RDONLY)
                except Exception:  # noqa: BLE001
                    continue
                try:
                    posix_fadvise(fd, 0, 0, dontneed)
                except Exception:  # noqa: BLE001
                    pass
                finally:
                    os.close(fd)
    except Exception:  # noqa: BLE001
        pass
    return max(0.0, before - memory_mb()[1])


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
