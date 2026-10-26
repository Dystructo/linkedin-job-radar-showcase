"""Canned judge for --fixture runs and tests. Makes no network calls.

tests/fixtures/stub_judgments.json maps post id -> judgment. An entry shaped
{"first": {...}, "retry": {...}} returns "first" on attempt 1 and "retry" on attempt 2,
which exercises the retry-once path.
"""
from __future__ import annotations

from pathlib import Path

from .common import read_json


class StubJudge:
    def __init__(self, path: Path):
        self.data = read_json(path, default={})

    def __call__(self, posts: list[dict], attempt: int) -> list[dict]:
        out = []
        for post in posts:
            entry = self.data.get(post["id"])
            if entry is None:
                continue
            if "first" in entry:
                entry = entry["first"] if attempt == 1 else entry["retry"]
            out.append({"id": post["id"], **entry})
        return out
