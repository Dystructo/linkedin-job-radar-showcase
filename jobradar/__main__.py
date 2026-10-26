"""Command line entry point: python -m jobradar <command> [--fixture].

Mode A (Claude routine):  fetch -> prefilter -> Claude writes data/scored_parts/*.jsonl -> validate
                          -> Claude re-judges ids in judge_retry.json once -> digest -> mark-seen
Mode B / fixtures:        run   (does every step itself)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .common import (
    fixture_posts_path, get_paths, ist_date, load_config, load_dotenv, now_utc, read_json,
    read_jsonl, repo_path, setup_logging, write_json, write_jsonl,
)
from .fetch import FetchError, build_actor_input, fetch_posts
from .judge_gemini import GeminiError, GeminiJudge
from .judge_stub import StubJudge
from .pipeline import finalize, judge_view, judge_with_retry, log_judge, run_prefilter
from .seen import mark_seen
from .stats import collect_stats
from .tracker import TrackerError, get_tracker
from .validate import validate_batch

log = logging.getLogger("jobradar")



def _fixture_now(raw) -> datetime:
    """Fixture posts have fixed dates; pretend "now" is just after the newest one."""
    from .normalize import normalize_items
    posts, _ = normalize_items(raw)
    newest = max((p["posted_ts"] for p in posts), default=0)
    return datetime.fromtimestamp(newest / 1000, tz=timezone.utc) + timedelta(hours=1)


def _load_raw(args, cfg, paths):
    if getattr(args, "input", None):
        source = Path(args.input)
    elif args.fixture:
        source = fixture_posts_path(cfg)
    else:
        source = paths.raw_latest
    raw = read_json(source)
    if raw is None:
        log.error("No input at %s. Run 'fetch' first, or use --fixture.", source)
    return raw


def _report_prefilter(entry: dict, inbox: Path) -> None:
    log.info("Prefilter: fetched %d, kept %d -> %s", entry["fetched"], entry["kept"], inbox)
    for reason, count in entry["drops"].items():
        log.info("  dropped %-20s %d", reason, count)
    for tag, count in entry["kept_by_location"].items():
        log.info("  kept    %-20s %d", tag, count)


def _report_digest(entry: dict) -> None:
    t = entry["tracker"]
    log.info("Judged %d (failed %d, capped %d). Tracker [%s]: +%d new, %d already there%s",
             entry["judged"], entry["judge_failed"], entry["caps_applied"], t["backend"],
             t["added"], t["skipped"], f", {t['unsent']} saved for retry" if t["unsent"] else "")
    log.info("Digest -> %s", entry["digest"])


def cmd_fetch(args, cfg) -> int:
    paths = get_paths(cfg, fixture=args.fixture, data_dir=args.data_dir)
    if args.fixture:
        log.info("--fixture: no fetch needed; prefilter reads %s", fixture_posts_path(cfg))
        return 0
    if args.dry_run:
        queries = cfg["queries"][: args.queries or None]
        per_query = args.max_posts or cfg["apify"]["max_posts_per_query"]
        log.info("Would run %s with:\n%s", cfg["apify"]["actor"],
                 json.dumps(build_actor_input(queries, cfg, per_query), indent=2, ensure_ascii=False))
        return 0
    try:
        fetch_posts(cfg, paths, max_posts=args.max_posts, n_queries=args.queries)
    except FetchError as exc:
        log.error("%s", exc)
        return 1
    return 0


def cmd_prefilter(args, cfg) -> int:
    paths = get_paths(cfg, fixture=args.fixture, data_dir=args.data_dir)
    raw = _load_raw(args, cfg, paths)
    if raw is None:
        return 1
    now = _fixture_now(raw) if args.fixture else now_utc()
    _, entry = run_prefilter(cfg, paths, raw, now, fixture=args.fixture)
    _report_prefilter(entry, paths.inbox)
    return 0


def _utf8_stdout() -> None:
    """Windows consoles default to cp1252, which cannot print emoji or the rupee sign."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


def _print_json_lines(rows) -> None:
    _utf8_stdout()
    for row in rows:
        print(json.dumps(row, ensure_ascii=False))


def cmd_inbox(args, cfg) -> int:
    """Print the posts to judge, trimmed to the fields the rubric uses."""
    paths = get_paths(cfg, fixture=args.fixture, data_dir=args.data_dir)
    inbox = read_jsonl(paths.inbox)
    batch = inbox[args.start: args.start + args.count if args.count else None]
    log.info("Inbox: %d posts total, showing %d starting at %d", len(inbox), len(batch), args.start)
    _print_json_lines(judge_view(p) for p in batch)
    return 0


def cmd_tracker(args, cfg) -> int:
    paths = get_paths(cfg, fixture=args.fixture, data_dir=args.data_dir)
    tracker = get_tracker(cfg, paths, args.fixture)
    try:
        rows = tracker.read()
    except TrackerError as exc:
        log.error("%s", exc)
        return 1
    if args.marked:
        rows = [r for r in rows if str(r.get("status", "")).strip() or str(r.get("notes", "")).strip()]
    log.info("%s: %d rows", tracker.name, len(rows))
    _print_json_lines(rows)
    return 0


def cmd_stats(args, cfg) -> int:
    paths = get_paths(cfg, fixture=args.fixture, data_dir=args.data_dir)
    _utf8_stdout()
    print(json.dumps(collect_stats(paths, args.days, int(cfg["digest"]["top_pick_min"])),
                     indent=2, ensure_ascii=False))
    return 0


def cmd_validate(args, cfg) -> int:
    """Check data/scored.jsonl (written by Claude) and list ids that need one re-judge."""
    paths = get_paths(cfg, fixture=args.fixture, data_dir=args.data_dir)
    inbox = read_jsonl(paths.inbox)
    valid, errors = validate_batch(paths.read_scored(), [p["id"] for p in inbox], cfg)
    log.info("Valid judgments: %d of %d", len(valid), len(inbox))
    if errors:
        write_json(paths.judge_retry, [{"id": pid, "errors": e} for pid, e in errors.items()])
        for pid, errs in errors.items():
            log.warning("  %s: %s", pid, "; ".join(errs))
        log.warning("Re-judge these %d ids once (append fixed lines to scored.jsonl): %s",
                    len(errors), paths.judge_retry)
    elif paths.judge_retry.exists():
        paths.judge_retry.unlink()
    return 0


def cmd_digest(args, cfg) -> int:
    paths = get_paths(cfg, fixture=args.fixture, data_dir=args.data_dir)
    inbox = read_jsonl(paths.inbox)
    scored = paths.read_scored()
    entry = finalize(cfg, paths, inbox, scored, get_tracker(cfg, paths, args.fixture),
                     note=args.note or "")
    _report_digest(entry)
    return 0


def cmd_mark_seen(args, cfg) -> int:
    paths = get_paths(cfg, fixture=args.fixture, data_dir=args.data_dir)
    ids = read_json(paths.pending_seen)
    if ids is None:
        log.info("Nothing to mark: %s does not exist.", paths.pending_seen)
        return 0
    stats = mark_seen(paths, ids, int(cfg["prefilter"]["seen_retention_days"]))
    paths.pending_seen.unlink()
    log.info("seen.json: +%d new, %d pruned, %d total", stats["added"], stats["pruned"], stats["total"])
    return 0


def _gemini_judge(cfg):
    return GeminiJudge(cfg, os.environ.get("GEMINI_API_KEY"))


def cmd_judge_gemini(args, cfg) -> int:
    """Mode B judge step on its own: inbox.jsonl -> scored.jsonl."""
    paths = get_paths(cfg, fixture=args.fixture, data_dir=args.data_dir)
    inbox = read_jsonl(paths.inbox)
    if not inbox:
        log.info("Inbox is empty; nothing to judge.")
        return 0
    try:
        judge = _gemini_judge(cfg)
    except GeminiError as exc:
        log.error("%s", exc)
        return 1
    valid, errors = judge_with_retry(inbox, judge, cfg)
    log_judge(paths, judge.name, valid, errors)
    paths.clear_scored()
    write_jsonl(paths.scored, valid.values())
    log.info("Gemini judged %d of %d posts -> %s", len(valid), len(inbox), paths.scored)
    return 0


def cmd_run_real(args, cfg) -> int:
    """Full Mode B pipeline: fetch -> prefilter -> Gemini judge -> finalize -> mark-seen.
    Always leaves a digest for today, with a note when something failed."""
    paths = get_paths(cfg, data_dir=args.data_dir)
    tracker = get_tracker(cfg, paths, fixture=False)
    try:
        fetched = fetch_posts(cfg, paths)
    except FetchError as exc:
        log.error("%s", exc)
        _report_digest(finalize(cfg, paths, [], [], tracker, note=f"Fetch failed: {exc}"))
        return 1
    note = ("Monthly Apify budget reached, no fetch today."
            if fetched.get("status") == "SKIPPED_BUDGET" else "")

    result, entry = run_prefilter(cfg, paths, read_json(paths.raw_latest, default=[]), now_utc())
    _report_prefilter(entry, paths.inbox)
    if not result.kept:
        _report_digest(finalize(cfg, paths, [], [], tracker, note=note))
        return cmd_mark_seen(args, cfg)

    try:
        judge = _gemini_judge(cfg)
    except GeminiError as exc:
        # Leave the posts unseen so a later run can judge them.
        log.error("%s", exc)
        _report_digest(finalize(cfg, paths, [], [], tracker,
                                note=f"Judge unavailable, {len(result.kept)} posts not judged: {exc}"))
        return 1
    valid, errors = judge_with_retry(result.kept, judge, cfg)
    log_judge(paths, judge.name, valid, errors)
    write_jsonl(paths.scored, valid.values())
    _report_digest(finalize(cfg, paths, result.kept, list(valid.values()), tracker, note=note))
    return cmd_mark_seen(args, cfg)


def cmd_run(args, cfg) -> int:
    if not args.fixture:
        return cmd_run_real(args, cfg)
    paths = get_paths(cfg, fixture=True, data_dir=args.data_dir)
    if args.fresh and paths.data.exists():
        shutil.rmtree(paths.data)
        log.info("Cleared %s", paths.data)
    raw = _load_raw(args, cfg, paths)
    if raw is None:
        return 1

    result, entry = run_prefilter(cfg, paths, raw, _fixture_now(raw), fixture=True)
    _report_prefilter(entry, paths.inbox)

    valid, errors = judge_with_retry(result.kept, StubJudge(repo_path(cfg, "fixture_judgments")), cfg)
    log_judge(paths, "stub", valid, errors)
    write_jsonl(paths.scored, valid.values())

    digest_entry = finalize(cfg, paths, result.kept, list(valid.values()),
                            get_tracker(cfg, paths, fixture=True))
    _report_digest(digest_entry)
    return cmd_mark_seen(args, cfg)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fixture", action="store_true",
                        help="use tests/fixtures data and .fixture_data/, no network calls")
    common.add_argument("--config", help="path to config.yaml (default: repo root)")
    common.add_argument("--data-dir", help="override the data directory")
    common.add_argument("-v", "--verbose", action="store_true")

    parser = argparse.ArgumentParser(prog="python -m jobradar", description="LinkedIn Job Radar")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("fetch", parents=[common], help="run the Apify actor, save data/raw/latest.json")
    p.add_argument("--max-posts", type=int, help="posts per query (default: config)")
    p.add_argument("--queries", type=int, help="only the first N queries (for testing)")
    p.add_argument("--dry-run", action="store_true", help="print the actor input, call nothing")
    p = sub.add_parser("prefilter", parents=[common], help="dedupe, drop junk/scams, write inbox.jsonl")
    p.add_argument("--input", help="raw posts JSON (default: data/raw/latest.json)")
    p = sub.add_parser("inbox", parents=[common], help="print posts to judge (compact JSON lines)")
    p.add_argument("--start", type=int, default=0, help="first post index (0-based)")
    p.add_argument("--count", type=int, default=0, help="how many posts (0 = all)")
    sub.add_parser("validate", parents=[common],
                   help="check scored.jsonl against the schema; list ids to re-judge")
    p = sub.add_parser("digest", parents=[common],
                       help="validate + cap judgments, update tracker, write today's digest")
    p.add_argument("--note", help="warning line shown at the top of the digest")
    p = sub.add_parser("tracker", parents=[common], help="print tracker rows (Sheet or CSV)")
    p.add_argument("--marked", action="store_true", help="only rows with a status or notes")
    p = sub.add_parser("stats", parents=[common], help="per-query and per-filter numbers")
    p.add_argument("--days", type=int, default=14)
    sub.add_parser("mark-seen", parents=[common], help="add the last batch's ids to seen.json")
    p = sub.add_parser("run", parents=[common],
                       help="full pipeline: Mode B with Gemini, or --fixture with a stub judge")
    p.add_argument("--input", help="raw posts JSON instead of the fixture file")
    p.add_argument("--fresh", action="store_true", help="with --fixture: clear .fixture_data first")
    sub.add_parser("judge-gemini", parents=[common],
                   help="Mode B: judge inbox.jsonl with Gemini, write scored.jsonl")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.verbose)
    load_dotenv()
    cfg = load_config(args.config)
    handlers = {"fetch": cmd_fetch, "prefilter": cmd_prefilter, "inbox": cmd_inbox,
                "validate": cmd_validate, "judge-gemini": cmd_judge_gemini,
                "digest": cmd_digest, "mark-seen": cmd_mark_seen, "tracker": cmd_tracker,
                "stats": cmd_stats, "run": cmd_run}
    return handlers[args.command](args, cfg)


if __name__ == "__main__":
    sys.exit(main())
