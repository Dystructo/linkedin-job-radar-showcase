"""Pipeline steps shared by the CLI, the Claude routine (Mode A) and the Gemini fallback (Mode B).

Daily order:  prefilter -> judge (with one retry) -> finalize (validate, cap, tracker, digest)
              -> mark-seen
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Callable

from .common import (
    Paths, append_jsonl, ist_date, now_utc, read_jsonl, write_json, write_jsonl,
)
from .digest import render_digest
from .normalize import normalize_items
from .prefilter import Prefilter, PrefilterResult
from .seen import load_seen
from .tracker import push_rows, to_row
from .validate import apply_caps, validate_batch

log = logging.getLogger("jobradar")

# A judge takes a batch of inbox posts and the attempt number (1 or 2) and returns raw dicts.
JudgeFn = Callable[[list[dict], int], list[dict]]

# The fields a judge sees for each post (both Claude via `inbox` and Gemini).
JUDGE_FIELDS = ("id", "author_name", "author_headline", "author_type", "is_repost",
                "repost_author_name", "repost_author_headline", "mentioned_companies",
                "posted_at", "location_tag", "prefilter_flags", "text", "extras_text")


def judge_view(post: dict) -> dict:
    return {k: post.get(k) for k in JUDGE_FIELDS}


def _stamp() -> str:
    return now_utc().strftime("%Y-%m-%dT%H:%M:%SZ")


def run_prefilter(cfg: dict, paths: Paths, raw, now: datetime | None,
                  fixture: bool = False) -> tuple[PrefilterResult, dict]:
    posts, malformed = normalize_items(raw)
    result = Prefilter(cfg).run(posts, load_seen(paths), now=now)
    if malformed:
        result.counts["malformed"] = malformed
    paths.clear_scored()  # judgments from an older batch must not leak into this one
    write_jsonl(paths.inbox, result.kept)
    write_json(paths.pending_seen, result.ids_to_mark_seen())
    entry = {
        "run_at": _stamp(),
        "date": ist_date(),
        "stage": "prefilter",
        "fixture": fixture,
        "fetched": len(posts) + malformed,
        "kept": len(result.kept),
        "drops": dict(sorted(result.counts.items())),
        "kept_by_location": dict(sorted(result.location_counts.items())),
        "drop_details": result.dropped,
    }
    append_jsonl(paths.run_log, entry)
    return result, entry


def judge_with_retry(posts: list[dict], judge_fn: JudgeFn, cfg: dict
                     ) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Judge a batch; re-judge failed ids once. Returns (valid by id, errors by id)."""
    by_id = {p["id"]: p for p in posts}
    valid: dict[str, dict] = {}
    errors: dict[str, list[str]] = {}
    pending = list(posts)
    for attempt in (1, 2):
        if not pending:
            break
        try:
            raw = judge_fn(pending, attempt)
            crash = None
        except Exception as exc:  # a judge failure must never crash the run
            log.error("Judge attempt %d failed: %s", attempt, exc)
            raw, crash = [], f"judge error: {exc}"
        got, errors = validate_batch(raw, [p["id"] for p in pending], cfg)
        if crash:
            errors = {pid: [crash] for pid in errors}
        valid.update(got)
        pending = [by_id[pid] for pid in errors]
        if pending and attempt == 1:
            log.warning("Retrying %d posts with invalid judge output", len(pending))
    for pid, errs in errors.items():
        log.warning("Skipping %s after retry: %s", pid, "; ".join(errs))
    return valid, errors


def log_judge(paths: Paths, judge_name: str, valid: dict, errors: dict) -> None:
    append_jsonl(paths.run_log, {
        "run_at": _stamp(), "date": ist_date(), "stage": "judge", "judge": judge_name,
        "judged": len(valid), "failed": len(errors),
        "failures": [{"id": pid, "errors": errs} for pid, errs in errors.items()],
    })


def _record(judgment: dict, post: dict, today: str, keep_text_min: int) -> dict:
    """A judged post as stored in data/judged/. Post text is kept only for leads worth
    tracking (draft-outreach needs it); the rest stay small."""
    text = post.get("text", "") if judgment["relevance"] >= keep_text_min else ""
    return {
        **judgment,
        "post_text": text[:4000],
        "post_url": post.get("url", ""),
        "author": post.get("author_name", ""),
        "author_headline": post.get("author_headline", ""),
        "posted_at": post.get("posted_at", ""),
        "location_tag": post.get("location_tag", ""),
        "prefilter_flags": post.get("prefilter_flags", []),
        "source_query": post.get("source_query", ""),
        "judged_on": today,
    }


def day_counts(paths: Paths, today: str) -> dict:
    """Totals for one IST day across every run that day (re-runs included)."""
    drops: Counter = Counter()
    fetched, failed = 0, 0
    for entry in read_jsonl(paths.run_log, skip_bad=True):
        if entry.get("date") != today:
            continue
        if entry.get("stage") == "prefilter":
            fetched += entry.get("fetched", 0) - entry.get("drops", {}).get("duplicate_seen", 0)
            drops.update(entry.get("drops", {}))
        elif entry.get("stage") == "digest":
            failed += entry.get("judge_failed", 0)
    return {"fetched": fetched, "prefilter_drops": dict(drops), "judge_failed": failed}


def finalize(cfg: dict, paths: Paths, inbox: list[dict], raw_judgments: list[dict],
             tracker, today: str | None = None, note: str = "") -> dict:
    """Validate and cap judgments, merge into the day file, update tracker, write the digest."""
    today = today or ist_date()
    posts = {p["id"]: p for p in inbox}
    valid, errors = validate_batch(raw_judgments, list(posts), cfg)
    min_rel = int(cfg["tracker"]["min_relevance"])
    new_records = [_record(apply_caps(j, cfg), posts[pid], today, min_rel)
                   for pid, j in valid.items()]

    day_file = paths.judged / f"{today}.jsonl"
    day = {r["id"]: r for r in read_jsonl(day_file)}
    day.update({r["id"]: r for r in new_records})
    write_jsonl(day_file, day.values())

    rows = [to_row(r, today) for r in new_records if r["relevance"] >= min_rel]
    tracker_stats = push_rows(tracker, rows, cfg, paths)

    entry = {
        "run_at": _stamp(), "date": today, "stage": "digest",
        "judged": len(new_records), "judge_failed": len(errors),
        "judge_failures": [{"id": pid, "errors": errs} for pid, errs in errors.items()],
        "caps_applied": sum(1 for r in new_records if r["caps_applied"]),
        "tracker": tracker_stats,
    }
    append_jsonl(paths.run_log, entry)

    digest_path = paths.digests / f"{today}.md"
    digest_path.parent.mkdir(parents=True, exist_ok=True)
    digest_path.write_text(
        render_digest(today, list(day.values()), day_counts(paths, today), cfg, note=note),
        encoding="utf-8")
    entry["digest"] = str(digest_path)
    return entry
