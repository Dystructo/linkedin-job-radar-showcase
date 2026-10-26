"""data/seen.json: post id -> date first processed (IST). Pruned after the retention window."""
from __future__ import annotations

from datetime import date, timedelta

from .common import Paths, ist_date, read_json, write_json


def load_seen(paths: Paths) -> dict[str, str]:
    data = read_json(paths.seen, default={})
    return data if isinstance(data, dict) else {}


def mark_seen(paths: Paths, ids: list[str], retention_days: int, today: str | None = None) -> dict:
    """Add ids stamped with today, drop entries older than the retention window."""
    today = today or ist_date()
    seen = load_seen(paths)
    added = 0
    for pid in ids:
        if pid not in seen:
            seen[pid] = today
            added += 1
    cutoff = (date.fromisoformat(today) - timedelta(days=retention_days)).isoformat()
    before = len(seen)
    seen = {pid: day for pid, day in seen.items() if day >= cutoff}
    write_json(paths.seen, dict(sorted(seen.items())))
    return {"added": added, "pruned": before - len(seen), "total": len(seen)}
