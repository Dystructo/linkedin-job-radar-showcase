"""Map raw scraper output into one internal post shape.

Every actor-specific field name lives in this file (plus the input builder in fetch.py),
so swapping the Apify actor means editing these two places only. The mapper is defensive:
missing or oddly typed fields become empty values instead of crashing the run.

Internal post shape:
    id, url, content, repost_text, text, extras_text, is_repost,
    author_name, author_headline, author_url, author_public_id, author_type,
    repost_author_name, repost_author_headline,
    job_title, job_location, job_company, job_url, links, mentioned_companies,
    posted_at (ISO UTC), posted_ts (epoch ms), likes, comments, shares, source_query

`text` is what the post says (content plus reposted content). `extras_text` holds the attached
job card and the links hidden behind post text; the prefilter scans both, so a scam link hidden
behind friendly words is still caught.

Verified against a real harvestapi run on 2026-10-15: query is an object with a "search"
key, some posts carry a "job" card, and links/mentions live in "contentAttributes".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

ACTIVITY_ID = re.compile(r"(?:activity|ugcPost|share)[:-](\d{15,25})")
LONG_DIGITS = re.compile(r"\d{15,25}")
REPOST_KEYS = ("repost", "resharedPost", "reshared", "reshare", "repostedPost", "sharedPost",
               "originalPost")
QUERY_KEYS = ("query", "searchQuery", "search_query")
JOB_BY = re.compile(r"^\s*job by\s+", re.IGNORECASE)


def _get(d: Any, *keys: str) -> Any:
    """First non-empty value among keys of a dict, else None."""
    if not isinstance(d, dict):
        return None
    for key in keys:
        value = d.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        value = _get(value, "text", "content")
    return str(value).strip() if value is not None else ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clean_url(url: Any) -> str:
    """Drop query strings and fragments (tracking parameters) from LinkedIn URLs."""
    url = _text(url)
    if not url:
        return ""
    parts = urlsplit(url)
    if "linkedin.com" not in parts.netloc:
        return url
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _source_query(item: dict) -> str:
    query = _get(item, *QUERY_KEYS)
    if isinstance(query, dict):
        query = _get(query, "search", "query", "keywords")
    return _text(query)


def _attributes(item: dict) -> tuple[list[str], list[str]]:
    """Links and tagged company names from contentAttributes."""
    links, companies = [], []
    attrs = item.get("contentAttributes")
    for attr in attrs if isinstance(attrs, list) else []:
        if not isinstance(attr, dict):
            continue
        for key in ("textLink", "hyperlink"):
            link = _text(attr.get(key))
            if link and attr.get("type") != "PROFILE_MENTION" and link not in links:
                links.append(link)
        company = attr.get("company")
        if isinstance(company, dict) and _text(company.get("name")):
            name = _text(company["name"])
            if name not in companies:
                companies.append(name)
    return links, companies


def _job_card(item: dict) -> dict:
    job = item.get("job") if isinstance(item.get("job"), dict) else {}
    return {
        "job_title": _text(job.get("title")),
        "job_location": _text(job.get("location")),
        "job_company": JOB_BY.sub("", _text(job.get("subtitle"))),
        "job_url": _clean_url(job.get("linkedinUrl")),
    }


def _post_id(item: dict) -> str:
    raw = _get(item, "id", "urn", "postId", "activityId")
    if raw is not None:
        raw = str(raw)
        match = LONG_DIGITS.search(raw)
        return match.group(0) if match else raw
    url = _get(item, "linkedinUrl", "url") or ""
    match = ACTIVITY_ID.search(url)
    return match.group(1) if match else ""


def _posted(item: dict) -> tuple[str, int]:
    """Return (ISO UTC string, epoch ms); ("", 0) when unknown."""
    posted = item.get("postedAt")
    ts, date = None, None
    if isinstance(posted, dict):
        ts, date = posted.get("timestamp"), posted.get("date")
    elif isinstance(posted, (int, float)):
        ts = posted
    elif isinstance(posted, str):
        date = posted
    ms = 0
    if isinstance(ts, (int, float)) and ts > 0:
        ms = int(ts if ts > 1e12 else ts * 1000)  # accept seconds or milliseconds
    elif date:
        try:
            dt = datetime.fromisoformat(str(date).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ms = int(dt.timestamp() * 1000)
        except ValueError:
            ms = 0
    if not ms:
        return "", 0
    iso = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return iso, ms


def normalize_item(item: Any) -> dict | None:
    """Map one raw item. Returns None when there is no usable id or text."""
    if not isinstance(item, dict):
        return None
    post_id = _post_id(item)
    author = item.get("author") if isinstance(item.get("author"), dict) else {}

    repost = next((item[k] for k in REPOST_KEYS if isinstance(item.get(k), dict)), None) or {}
    repost_author = repost.get("author") if isinstance(repost.get("author"), dict) else {}

    content = _text(_get(item, "content", "text", "commentary"))
    repost_text = _text(_get(repost, "content", "text", "commentary"))
    if not post_id or not (content or repost_text):
        return None

    url = _clean_url(_get(item, "linkedinUrl", "url") or _get(item.get("socialContent"), "shareUrl"))
    if not url:
        url = f"https://www.linkedin.com/feed/update/urn:li:activity:{post_id}/"

    posted_at, posted_ts = _posted(item)
    engagement = item.get("engagement") if isinstance(item.get("engagement"), dict) else {}
    job = _job_card(item)
    links, companies = _attributes(item)
    repost_links, repost_companies = _attributes(repost)
    links += [link for link in repost_links if link not in links]
    companies += [c for c in repost_companies if c not in companies]

    extras = []
    if job["job_title"] or job["job_location"]:
        card = " | ".join(v for v in (job["job_title"], job["job_location"], job["job_company"]) if v)
        extras.append(f"[Attached job: {card}]")
    if links:
        extras.append(f"[Links: {', '.join(links)}]")
    header = _text(_get(item.get("header"), "text"))

    return {
        "id": post_id,
        "url": url,
        "content": content,
        "repost_text": repost_text,
        "text": "\n\n".join(part for part in (content, repost_text) if part),
        "extras_text": "\n".join(extras),
        "is_repost": bool(repost_text) or "repost" in header.lower(),
        "author_name": _text(author.get("name")),
        "author_headline": _text(_get(author, "info", "headline", "occupation")),
        "author_url": _clean_url(author.get("linkedinUrl")),
        "author_public_id": _text(_get(author, "publicIdentifier", "universalName")),
        "author_type": _text(author.get("type")) or "unknown",
        "repost_author_name": _text(repost_author.get("name")),
        "repost_author_headline": _text(_get(repost_author, "info", "headline")),
        **job,
        "links": links,
        "mentioned_companies": companies,
        "posted_at": posted_at,
        "posted_ts": posted_ts,
        "likes": _int(engagement.get("likes")),
        "comments": _int(engagement.get("comments")),
        "shares": _int(engagement.get("shares")),
        "source_query": _source_query(item),
    }


def normalize_items(items: Any) -> tuple[list[dict], int]:
    """Map a list of raw items. Returns (posts, number_of_malformed_items)."""
    if isinstance(items, dict):  # tolerate {"items": [...]} wrappers
        items = items.get("items") or items.get("data") or []
    posts, malformed = [], 0
    for item in items or []:
        post = normalize_item(item)
        if post is None:
            malformed += 1
        else:
            posts.append(post)
    return posts, malformed
