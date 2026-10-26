"""Render the daily digest: data/digests/YYYY-MM-DD.md."""
from __future__ import annotations

import re
from datetime import date

LOCATION_LABELS = {
    "ncr": "Delhi NCR", "remote_india": "Remote (India)", "other_india": "Other Indian city",
    "outside_india": "Outside India", "unknown": "Location unknown",
}
ROLE_LABELS = {
    "founders_office": "Founder's Office", "ceo_office": "CEO's Office",
    "chief_of_staff": "Chief of Staff", "eir": "EIR", "vc_investment": "Investment team",
    "vc_non_investment": "Fund, non-investment", "strategy_bizops": "Strategy / BizOps",
    "other": "Other",
}
POSTER_LABELS = {
    "founder_or_exec": "founder/exec", "employee": "employee", "recruiter_agency": "recruiter/agency",
    "job_aggregator": "job aggregator", "reshare": "reshare", "unknown": "unknown",
}
DROP_LABELS = {
    "duplicate_in_batch": "Duplicate (same post from two searches)",
    "too_old": "Older than the time window",
    "too_short": "Too short to judge",
    "hard_scam": "Scam terms (fees, deposits, Telegram)",
    "job_seeker": "Job-seeker posts",
    "no_hiring_term": "No hiring language",
    "location_other": "Other city or abroad only",
    "over_cap": "Over the daily cap (not judged)",
    "malformed": "Unreadable scraper items",
}
HIDDEN_DROPS = {"duplicate_seen"}  # already handled on an earlier run; not news


def no_em_dashes(text: str) -> str:
    return text.replace(" — ", ", ").replace("—", "-").replace("–", "-")


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def _title(r: dict) -> str:
    role = _one_line(r["role_title"]) or ROLE_LABELS.get(r["role_category"], "Role")
    company = _one_line(r["company"])
    return f"{role} at {company}" if company else role


def _where(r: dict) -> str:
    label = LOCATION_LABELS.get(r["location"], r["location"])
    detail = _one_line(r.get("location_detail"))
    if not detail or detail.lower() in label.lower():
        return label
    label_words = re.findall(r"[a-z]+", label.lower())
    if all(w in detail.lower() for w in label_words):  # "Remote within India" says it all
        return detail
    return f"{label} ({detail})"


def _by_score_then_newest(rows: list[dict]) -> list[dict]:
    rows = sorted(rows, key=lambda r: r.get("posted_at") or "", reverse=True)
    return sorted(rows, key=lambda r: -r["relevance"])  # stable: keeps newest first within a score


def split_sections(records: list[dict], cfg: dict) -> tuple[list, list, list]:
    d = cfg["digest"]
    top, worth, low = [], [], []
    for r in records:
        score = r["relevance"]
        if score >= d["top_pick_min"]:
            top.append(r)
        elif score >= d["worth_a_look_min"] or (
                r["location"] == "unknown" and score >= d["unknown_location_min"]):
            worth.append(r)
        else:
            low.append(r)
    return _by_score_then_newest(top), _by_score_then_newest(worth), low


def _top_pick(i: int, r: dict) -> list[str]:
    author = _one_line(r.get("author"))
    headline = _one_line(r.get("author_headline"))
    if len(headline) > 80:
        headline = headline[:77].rsplit(" ", 1)[0] + "..."
    lines = [
        f"### {i}. {_title(r)}",
        f"**{r['relevance']}/10** | {_where(r)} | Fresher fit: {r['fresher_fit']} | "
        f"Poster: {POSTER_LABELS.get(r['poster_type'], r['poster_type'])} | "
        f"Scam risk: {r['scam_risk']}",
        "",
        f"- {_one_line(r['summary'])}",
        f"- Why: {_one_line(r['why'])}",
    ]
    if r.get("apply_method"):
        lines.append(f"- Apply: {_one_line(r['apply_method'])}")
    if r.get("verify_at_source"):
        lines.append("- **Verify at source:** posted by a job aggregator, check the original post.")
    if r.get("scam_reasons"):
        lines.append(f"- Scam notes: {'; '.join(r['scam_reasons'])}")
    by = f" by {author}" + (f" ({headline})" if headline else "") if author else ""
    lines += [f"- [Open post]({r['post_url']}){by}", ""]
    return lines


def _worth_line(r: dict) -> list[str]:
    return [
        f"- **{r['relevance']}/10** {_title(r)} | {_where(r)} | fresher fit: {r['fresher_fit']} | "
        f"scam risk: {r['scam_risk']} | [post]({r['post_url']})",
        f"  {_one_line(r['summary'])} Why: {_one_line(r['why'])}",
    ]


def render_digest(day: str, records: list[dict], counts: dict, cfg: dict, note: str = "") -> str:
    top, worth, low = split_sections(records, cfg)
    d = cfg["digest"]
    pretty = date.fromisoformat(day).strftime("%A, %d %B %Y")
    picks = f"{len(top)} top pick" + ("" if len(top) == 1 else "s")
    out = [f"# Job Radar {day}: {picks}", ""]
    if note:
        out += [f"> **Note:** {_one_line(note)}", ""]
    out += [
        f"{pretty}. Fetched {counts.get('fetched', 0)} posts, judged {len(records)}: "
        f"{len(top)} top picks, {len(worth)} worth a look.",
        "",
        f"## Top picks ({d['top_pick_min']}+)",
        "",
    ]
    if top:
        for i, r in enumerate(top, 1):
            out += _top_pick(i, r)
    else:
        out += [f"Nothing scored {d['top_pick_min']} or more today.", ""]

    out += [f"## Worth a look ({d['worth_a_look_min']} to {d['top_pick_min'] - 1}, "
            f"plus unknown-location posts)", ""]
    if worth:
        for r in worth:
            out += _worth_line(r)
        out.append("")
    else:
        out += ["Nothing in this band today.", ""]

    drops = {k: v for k, v in counts.get("prefilter_drops", {}).items()
             if k not in HIDDEN_DROPS and v}
    high = sum(1 for r in records if r["scam_risk"] == "high")
    medium = sum(1 for r in records if r["scam_risk"] == "medium")
    out += ["## Filtered today", ""]
    out.append(f"- Prefilter removed {sum(drops.values())} posts before judging")
    for reason, n in sorted(drops.items(), key=lambda kv: -kv[1]):
        out.append(f"  - {DROP_LABELS.get(reason, reason)}: {n}")
    out.append(f"- Judged below {d['worth_a_look_min']}: {len(low)}")
    out.append(f"- Flagged scam risk among judged posts: {high} high, {medium} medium")
    if counts.get("judge_failed"):
        out.append(f"- Judge output invalid after one retry (skipped, see run_log): "
                   f"{counts['judge_failed']}")
    out.append("")
    return no_em_dashes("\n".join(out))
