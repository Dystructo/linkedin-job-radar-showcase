"""Shared test helpers."""
from __future__ import annotations

import json

from jobradar.common import REPO_ROOT, load_config

CFG = load_config()
FIXTURE_PATH = REPO_ROOT / CFG["paths"]["fixture_posts"]


def raw_fixtures() -> list[dict]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def make_judgment(pid: str = "9000000000000000001", **overrides) -> dict:
    """A valid raw judgment for a genuine NCR Founder's Office role."""
    j = {
        "id": pid, "is_hiring_post": True, "role_category": "founders_office",
        "role_title": "Associate, Founder's Office", "company": "Acme",
        "location": "ncr", "location_detail": "Gurugram", "fresher_fit": "yes",
        "poster_type": "founder_or_exec", "apply_method": "Email jobs@acme.in",
        "scam_risk": "low", "scam_reasons": [], "relevance": 9,
        "summary": "Founder's Office associate.", "why": "Target role in NCR.",
    }
    j.update(overrides)
    return j


def make_post(text: str, pid: str = "9000000000000000001", ts: int = 1792029600000, **extra) -> dict:
    """A normalized post with only the fields the prefilter reads."""
    post = {"id": pid, "url": f"https://example.test/{pid}", "text": text,
            "author_name": "Test Author", "posted_ts": ts}
    post.update(extra)
    return post
