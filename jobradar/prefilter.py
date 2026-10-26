"""Plain-code filter between fetch and judge. No AI and no network.

Drop reasons, checked in this order (first match wins):
    duplicate_seen, duplicate_in_batch, too_old, too_short, hard_scam,
    job_seeker, no_hiring_term, location_other
Posts that pass but exceed the daily cap are counted as over_cap. They are not marked
as seen, so they can be judged on a later run if fetched again.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

LOCATION_TAGS = ("ncr", "remote_india", "other", "unknown")
_QUOTES = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"'})
_DASHES = re.compile(r"[-‐-―_]+")
_SPACES = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    """Lowercase, straight quotes, hyphens to spaces, collapsed whitespace."""
    text = (text or "").translate(_QUOTES).lower()
    text = _DASHES.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


class TermMatcher:
    """Find config terms in text. Terms match whole words: an edge that is a letter or
    digit must not touch another letter or digit, so 'usa' never matches 'usage'."""

    def __init__(self, terms: list[str]):
        self.patterns: list[tuple[str, re.Pattern]] = []
        # Longest first so stripping removes "please refer me" before "refer me".
        for term in sorted({str(t) for t in terms or []}, key=len, reverse=True):
            norm = normalize_for_match(term)
            if not norm:
                continue
            pattern = re.escape(norm)
            if norm[0].isalnum():
                pattern = r"(?<![a-z0-9])" + pattern
            if norm[-1].isalnum():
                pattern = pattern + r"(?![a-z0-9])"
            self.patterns.append((term, re.compile(pattern)))

    def find(self, norm_text: str) -> list[str]:
        return [term for term, rx in self.patterns if rx.search(norm_text)]

    def strip(self, norm_text: str) -> str:
        for _, rx in self.patterns:
            norm_text = rx.sub(" ", norm_text)
        return _SPACES.sub(" ", norm_text)


@dataclass
class PrefilterResult:
    kept: list[dict]                      # sorted and capped: goes to the judge
    over_cap: list[dict]                  # passed the filter but beyond the daily cap
    dropped: list[dict]                   # {id, url, author_name, reason, detail}
    counts: Counter = field(default_factory=Counter)
    location_counts: Counter = field(default_factory=Counter)

    def ids_to_mark_seen(self) -> list[str]:
        """Judged and dropped posts. Over-cap posts stay unseen so they can come back."""
        ids = [p["id"] for p in self.kept]
        ids += [d["id"] for d in self.dropped if d["reason"] != "duplicate_seen"]
        return list(dict.fromkeys(ids))


class Prefilter:
    def __init__(self, cfg: dict):
        pf = cfg["prefilter"]
        loc = pf["location"]
        self.min_chars = int(pf["min_chars"])
        self.max_age_hours = float(pf["max_post_age_hours"])
        self.cap = int(pf["max_posts_for_judging"])
        self.seeker = TermMatcher(pf["seeker_terms"])
        self.strong = TermMatcher(pf["hiring_terms_strong"])
        self.weak = TermMatcher(pf["hiring_terms_weak"])
        self.ncr = TermMatcher(loc["ncr_terms"])
        self.remote = TermMatcher(loc["remote_india_terms"])
        self.other = TermMatcher(loc["other_terms"])
        self.hard_scam = TermMatcher(pf["hard_scam_terms"])
        self.soft_scam = TermMatcher(pf["soft_scam_terms"])

    def location(self, norm_text: str) -> tuple[str, dict]:
        signals = {
            "ncr": self.ncr.find(norm_text),
            "remote_india": self.remote.find(norm_text),
            "other": self.other.find(norm_text),
        }
        for tag in ("ncr", "remote_india", "other"):
            if signals[tag]:
                return tag, signals
        return "unknown", signals

    def check(self, post: dict, now_ms: int | None) -> tuple[str | None, str, dict]:
        """Return (drop_reason or None, detail, extra fields for kept posts)."""
        text = post.get("text", "")
        if now_ms and post.get("posted_ts"):
            age_hours = (now_ms - post["posted_ts"]) / 3_600_000
            if age_hours > self.max_age_hours:
                return "too_old", f"{age_hours:.0f}h old", {}
        if len(text.strip()) < self.min_chars:
            return "too_short", f"{len(text.strip())} chars", {}

        # Scan the attached job card and hidden links too (a t.me link can hide behind text).
        norm = normalize_for_match(f"{text}\n{post.get('extras_text', '')}")
        scam_hits = self.hard_scam.find(norm)
        if scam_hits:
            return "hard_scam", ", ".join(scam_hits), {}

        seeker_hits = self.seeker.find(norm)
        hiring_text = self.seeker.strip(norm) if seeker_hits else norm
        strong_hits = self.strong.find(hiring_text)
        if seeker_hits and not strong_hits:
            return "job_seeker", ", ".join(seeker_hits), {}
        hiring_hits = strong_hits + self.weak.find(hiring_text)
        if not hiring_hits:
            return "no_hiring_term", "", {}

        tag, signals = self.location(norm)
        if tag == "other":
            return "location_other", ", ".join(signals["other"]), {}

        return None, "", {
            "location_tag": tag,
            "location_signals": signals,
            "prefilter_flags": self.soft_scam.find(norm),
            "hiring_signals": hiring_hits,
            "seeker_signals": seeker_hits,
        }

    def run(self, posts: list[dict], seen_ids: set[str] | dict,
            now: datetime | None = None) -> PrefilterResult:
        now_ms = int(now.timestamp() * 1000) if now else None
        passed, dropped = [], []
        counts: Counter = Counter()
        batch_ids: set[str] = set()

        for post in posts:
            pid = post["id"]
            if pid in seen_ids:
                reason, detail, extra = "duplicate_seen", "", {}
            elif pid in batch_ids:
                reason, detail, extra = "duplicate_in_batch", "", {}
            else:
                reason, detail, extra = self.check(post, now_ms)
            batch_ids.add(pid)
            if reason:
                counts[reason] += 1
                dropped.append({
                    "id": pid,
                    "url": post.get("url", ""),
                    "author_name": post.get("author_name", ""),
                    "reason": reason,
                    "detail": detail,
                })
            else:
                passed.append({**post, **extra})

        passed.sort(key=lambda p: (p["location_tag"] != "ncr", -(p.get("posted_ts") or 0)))
        kept, over_cap = passed[: self.cap], passed[self.cap:]
        if over_cap:
            counts["over_cap"] = len(over_cap)
        location_counts = Counter(p["location_tag"] for p in kept)
        return PrefilterResult(kept, over_cap, dropped, counts, location_counts)
