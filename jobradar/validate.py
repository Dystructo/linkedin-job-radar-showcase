"""Check judge output against the rubric's schema and re-apply the hard caps in code.

Both judges go through here, so a judge that ignores the rubric can never push a scam or an
off-target post above its cap. Enum values must match jobradar/rubric.md.
"""
from __future__ import annotations

from typing import Any

ENUMS = {
    "role_category": ("founders_office", "ceo_office", "chief_of_staff", "eir", "vc_investment",
                      "vc_non_investment", "strategy_bizops", "other"),
    "location": ("ncr", "remote_india", "other_india", "outside_india", "unknown"),
    "fresher_fit": ("yes", "maybe", "no"),
    "poster_type": ("founder_or_exec", "employee", "recruiter_agency", "job_aggregator",
                    "reshare", "unknown"),
    "scam_risk": ("low", "medium", "high"),
}
STRING_FIELDS = ("role_title", "company", "location_detail", "apply_method", "summary", "why")
SHORT_FIELDS = ("summary", "why")
FIELDS = ("id", "is_hiring_post", *ENUMS, *STRING_FIELDS, "scam_reasons", "relevance")
VERIFY_NOTE = "verify at source"


def _enum(value: Any) -> str:
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def _trim_words(text: str, max_words: int) -> str:
    words = text.split()
    return text if len(words) <= max_words else " ".join(words[:max_words]) + "..."


def validate_judgment(raw: Any, cfg: dict) -> tuple[dict | None, list[str]]:
    """Return (clean judgment, []) or (None, [errors])."""
    if not isinstance(raw, dict):
        return None, ["not a JSON object"]
    errors = [f"missing field '{f}'" for f in FIELDS if f not in raw]
    if errors:
        return None, errors

    out: dict[str, Any] = {"id": str(raw["id"]).strip()}
    if not out["id"]:
        errors.append("empty id")

    hiring = _bool(raw["is_hiring_post"])
    if hiring is None:
        errors.append(f"is_hiring_post must be true/false, got {raw['is_hiring_post']!r}")
    out["is_hiring_post"] = hiring

    for field, allowed in ENUMS.items():
        value = _enum(raw[field])
        if value not in allowed:
            errors.append(f"{field} '{raw[field]}' not in {allowed}")
        out[field] = value

    max_words = int(cfg["judge"]["summary_max_words"])
    for field in STRING_FIELDS:
        value = raw[field]
        if value is None:
            value = ""
        if not isinstance(value, (str, int, float)):
            errors.append(f"{field} must be a string")
            continue
        value = " ".join(str(value).split())
        out[field] = _trim_words(value, max_words) if field in SHORT_FIELDS else value

    reasons = raw["scam_reasons"]
    if reasons is None:
        reasons = []
    if isinstance(reasons, str):
        reasons = [reasons] if reasons.strip() else []
    if not isinstance(reasons, list):
        errors.append("scam_reasons must be a list of strings")
    else:
        out["scam_reasons"] = [" ".join(str(r).split()) for r in reasons if str(r).strip()]

    try:
        score = float(raw["relevance"])
        out["relevance"] = max(0, min(10, int(round(score))))
    except (TypeError, ValueError):
        errors.append(f"relevance must be a number 0-10, got {raw['relevance']!r}")

    return (None, errors) if errors else (out, [])


def validate_batch(raw_items: list, expected_ids: set[str] | list[str], cfg: dict
                   ) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """Validate a judge's output for a batch.

    Returns (valid by id, errors by id). Ids with no judgment get an error; output for ids that
    were not in the batch is ignored. If an id appears twice, the last valid line wins, so a
    retry can simply append corrected lines.
    """
    expected = set(expected_ids)
    valid: dict[str, dict] = {}
    errors: dict[str, list[str]] = {}
    for raw in raw_items or []:
        rid = str(raw.get("id", "")).strip() if isinstance(raw, dict) else ""
        if rid not in expected:
            continue
        clean, errs = validate_judgment(raw, cfg)
        if clean:
            valid[rid] = clean
            errors.pop(rid, None)
        elif rid not in valid:
            errors[rid] = errs
    for rid in expected - valid.keys() - errors.keys():
        errors[rid] = ["no judgment returned"]
    return valid, errors


def apply_caps(judgment: dict, cfg: dict) -> dict:
    """Clamp relevance to every hard cap that applies; record which caps fired."""
    caps = cfg["judge"]["caps"]
    j = dict(judgment)
    rules = [
        ("not_hiring_post", j["is_hiring_post"] is False),
        ("outside_india_or_other_city", j["location"] in ("outside_india", "other_india")),
        ("fresher_fit_no", j["fresher_fit"] == "no"),
        ("vc_non_investment", j["role_category"] == "vc_non_investment"),
        ("scam_risk_high", j["scam_risk"] == "high"),
        ("job_aggregator", j["poster_type"] == "job_aggregator"),
    ]
    j["raw_relevance"] = j["relevance"]
    j["caps_applied"] = []
    for name, applies in rules:
        if applies and j["relevance"] > caps[name]:
            j["relevance"] = int(caps[name])
            j["caps_applied"].append(name)
    j["verify_at_source"] = j["poster_type"] == "job_aggregator"
    if j["verify_at_source"] and VERIFY_NOTE not in j["why"].lower():
        j["why"] = f"{j['why'].rstrip('. ')}. Verify at source." if j["why"] else "Verify at source."
    return j
