"""Tracker: one row per promising post, deduped by post URL.

Backends:
    SheetTracker  Google Sheet through the Apps Script web app in jobradar/apps_script.gs
    CsvTracker    a local CSV (used for --fixture runs, or until the Sheet is set up)

If the Sheet cannot be reached, rows go to an "unsent" CSV and are retried on the next run,
so a Sheet outage never loses rows or crashes the run.
"""
from __future__ import annotations

import csv
import logging
import os
from pathlib import Path

import requests

from .common import Paths

log = logging.getLogger("jobradar")

COLUMNS = ["date_found", "relevance", "role_category", "role_title", "company", "location",
           "fresher_fit", "poster_type", "scam_risk", "post_url", "author", "author_headline",
           "summary", "why", "status", "notes"]
FORMULA_START = ("=", "+", "-", "@")


class TrackerError(RuntimeError):
    pass


def _safe_cell(value) -> str:
    """Stop text like '=HYPERLINK(...)' from a post being run as a spreadsheet formula."""
    text = "" if value is None else str(value)
    if text.startswith(FORMULA_START) and not text.lstrip("+-").replace(".", "", 1).isdigit():
        return "'" + text
    return text


def to_row(record: dict, date_found: str) -> dict:
    return {
        "date_found": date_found,
        "relevance": record["relevance"],
        "role_category": record["role_category"],
        "role_title": _safe_cell(record["role_title"]),
        "company": _safe_cell(record["company"]),
        "location": record["location"],
        "fresher_fit": record["fresher_fit"],
        "poster_type": record["poster_type"],
        "scam_risk": record["scam_risk"],
        "post_url": record["post_url"],
        "author": _safe_cell(record.get("author", "")),
        "author_headline": _safe_cell(record.get("author_headline", "")),
        "summary": _safe_cell(record["summary"]),
        "why": _safe_cell(record["why"]),
        "status": "",
        "notes": "",
    }


class CsvTracker:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))

    def append(self, rows: list[dict]) -> tuple[int, int]:
        existing = {r.get("post_url") for r in self.read()}
        new_rows = []
        for row in rows:
            if row["post_url"] in existing:
                continue
            existing.add(row["post_url"])
            new_rows.append(row)
        if new_rows:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            write_header = not self.path.exists()
            with open(self.path, "a", encoding="utf-8-sig" if write_header else "utf-8",
                      newline="") as f:
                writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
                if write_header:
                    writer.writeheader()
                writer.writerows(new_rows)
        return len(new_rows), len(rows) - len(new_rows)

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()

    @property
    def name(self) -> str:
        return f"CSV ({self.path.name})"


class SheetTracker:
    def __init__(self, url: str, secret: str, timeout: float = 30):
        self.url, self.secret, self.timeout = url, secret, timeout

    def _call(self, payload: dict) -> dict:
        try:
            # Apps Script answers a POST with a redirect; requests follows it as a GET.
            resp = requests.post(self.url, json={"secret": self.secret, **payload},
                                 timeout=self.timeout)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise TrackerError(f"Sheet request failed: {exc}") from exc
        if not data.get("ok"):
            raise TrackerError(f"Sheet rejected the request: {data.get('error', 'unknown error')}")
        return data

    def append(self, rows: list[dict]) -> tuple[int, int]:
        if not rows:
            return 0, 0
        data = self._call({"action": "append", "columns": COLUMNS, "rows": rows})
        return int(data.get("added", 0)), int(data.get("skipped", 0))

    def read(self) -> list[dict]:
        return self._call({"action": "read"}).get("rows", [])

    @property
    def name(self) -> str:
        return "Google Sheet"


def get_tracker(cfg: dict, paths: Paths, fixture: bool):
    tcfg = cfg["tracker"]
    csv_tracker = CsvTracker(paths.data / tcfg["csv_file"])
    if fixture or tcfg["backend"] == "csv":
        return csv_tracker
    url, secret = os.environ.get("SHEET_WEBAPP_URL"), os.environ.get("SHEET_SHARED_SECRET")
    if not (url and secret):
        log.warning("SHEET_WEBAPP_URL / SHEET_SHARED_SECRET not set; writing the tracker to %s",
                    csv_tracker.path)
        return csv_tracker
    return SheetTracker(url, secret, float(tcfg["sheet_timeout_seconds"]))


def push_rows(tracker, rows: list[dict], cfg: dict, paths: Paths) -> dict:
    """Send rows (plus any left over from a failed run). Never raises."""
    unsent = CsvTracker(paths.data / cfg["tracker"]["unsent_file"])
    pending = unsent.read() if isinstance(tracker, SheetTracker) else []
    all_rows = pending + rows
    try:
        added, skipped = tracker.append(all_rows)
    except TrackerError as exc:
        log.error("%s; saved %d rows to %s for the next run", exc, len(rows), unsent.path)
        unsent.append(rows)
        return {"backend": tracker.name, "added": 0, "skipped": 0, "unsent": len(all_rows),
                "error": str(exc)}
    if pending:
        unsent.clear()
    return {"backend": tracker.name, "added": added, "skipped": skipped, "unsent": 0}
