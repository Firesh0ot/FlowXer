from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any


def _cgroup_bytes(path: Path) -> int | None:
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def collect_resources(mixer) -> dict[str, Any]:
    mem_total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    try:
        import resource as res

        usage = res.getrusage(res.RUSAGE_SELF)
        rss = int(usage.ru_maxrss * 1024)
    except Exception:
        rss = 0

    limit = _cgroup_bytes(Path("/sys/fs/cgroup/memory.max"))
    current = _cgroup_bytes(Path("/sys/fs/cgroup/memory.current"))
    if limit in {None, 0, 2**64 - 1, 2**63 - 1}:
        limit = mem_total
    if current is None:
        current = rss

    cpu_count = os.cpu_count() or 1
    load1, load5, load15 = os.getloadavg()
    cpu_percent = min(100.0, round((load1 / cpu_count) * 100.0, 1))

    issues: list[dict[str, str]] = []
    if mixer.error:
        issues.append({"level": "error", "message": mixer.error})
    if mixer.backend == "simulate":
        issues.append(
            {
                "level": "info",
                "message": "GStreamer/MXL plugins not on-air — WebRTC monitors are generated previews",
            }
        )
    domain = mixer.settings.mxl_domain
    if not (domain / "domain_def.json").exists():
        issues.append({"level": "warning", "message": f"MXL domain missing at {domain}"})
    if mixer.state.value == "error":
        issues.append({"level": "error", "message": "Mixer is in error state"})
    for keyer in getattr(mixer, "keyers", []):
        if keyer.enabled and not keyer.url:
            issues.append({"level": "warning", "message": f"{keyer.label} has no HTML URL"})

    level = "ok"
    if any(item["level"] == "error" for item in issues):
        level = "error"
    elif any(item["level"] == "warning" for item in issues):
        level = "warning"

    return {
        "cpu_percent": cpu_percent,
        "cpu_count": cpu_count,
        "load": {"m1": load1, "m5": load5, "m15": load15},
        "memory_bytes": int(current),
        "memory_limit_bytes": int(limit),
        "memory_percent": round((current / limit) * 100.0, 1) if limit else 0.0,
        "uptime_s": round(time.time() - mixer.started_at, 1),
        "pid": os.getpid(),
        "status": level,
        "issues": issues,
    }
