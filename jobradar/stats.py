"""Numbers for tuning (used by the tune-radar skill): what each query and filter rule did."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta

from .common import Paths, ist_date, read_jsonl


def collect_stats(paths: Paths, days: int, top_min: int) -> dict:
    since = (date.fromisoformat(ist_date()) - timedelta(days=days - 1)).isoformat()
    drops: Counter = Counter()
    kept_by_location: Counter = Counter()
    per_day: dict[str, dict] = defaultdict(lambda: {"fetched": 0, "kept": 0, "judged": 0,
                                                    "top_picks": 0, "judge_failed": 0})
    usage_usd = 0.0
    for e in read_jsonl(paths.run_log, skip_bad=True):
        day = e.get("date", "")
        if day < since:
            continue
        stage = e.get("stage")
        if stage == "fetch":
            usage_usd += e.get("usage_usd") or 0
        elif stage == "prefilter":
            per_day[day]["fetched"] += e.get("fetched", 0)
            per_day[day]["kept"] += e.get("kept", 0)
            drops.update(e.get("drops", {}))
            kept_by_location.update(e.get("kept_by_location", {}))
        elif stage == "digest":
            per_day[day]["judge_failed"] += e.get("judge_failed", 0)

    queries: dict[str, dict] = defaultdict(lambda: {"judged": 0, "top_picks": 0, "scores": []})
    categories: Counter = Counter()
    caps: Counter = Counter()
    for f in sorted(paths.judged.glob("*.jsonl")) if paths.judged.exists() else []:
        if f.stem < since:
            continue
        for r in read_jsonl(f, skip_bad=True):
            per_day[f.stem]["judged"] += 1
            q = queries[r.get("source_query") or "(unknown query)"]
            q["judged"] += 1
            q["scores"].append(r["relevance"])
            if r["relevance"] >= top_min:
                per_day[f.stem]["top_picks"] += 1
                q["top_picks"] += 1
            categories[r["role_category"]] += 1
            caps.update(r.get("caps_applied", []))

    return {
        "window": f"{since} to {ist_date()}",
        "apify_usd": round(usage_usd, 4),
        "per_day": dict(sorted(per_day.items())),
        "prefilter_drops": dict(drops.most_common()),
        "kept_by_location": dict(kept_by_location.most_common()),
        "queries": {k: {"judged": v["judged"], "top_picks": v["top_picks"],
                        "avg_score": round(sum(v["scores"]) / len(v["scores"]), 1)}
                    for k, v in sorted(queries.items(), key=lambda kv: -kv[1]["top_picks"])},
        "role_categories": dict(categories.most_common()),
        "caps_applied": dict(caps.most_common()),
    }
