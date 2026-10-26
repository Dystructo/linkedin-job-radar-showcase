"""Shared helpers: config loading, file paths, JSON file IO and time."""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
IST = timezone(timedelta(hours=5, minutes=30))
BOOLEAN_OPERATOR = re.compile(r"\b(AND|OR|NOT)\b")
REQUIRED_SECTIONS = ("paths", "apify", "queries", "prefilter", "judge", "digest", "tracker")

log = logging.getLogger("jobradar")


class ConfigError(ValueError):
    """config.yaml is missing something or breaks an actor limit."""


# ---------- config ----------

def load_config(path: str | Path | None = None) -> dict:
    path = Path(path) if path else REPO_ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    validate_config(cfg)
    return cfg


def validate_config(cfg: dict) -> None:
    for section in REQUIRED_SECTIONS:
        if section not in cfg:
            raise ConfigError(f"config.yaml is missing the '{section}' section")
    apify = cfg["apify"]
    max_chars = apify["max_query_chars"]
    max_ops = apify["max_boolean_operators"]
    for query in cfg["queries"]:
        if len(query) > max_chars:
            raise ConfigError(f"Query is {len(query)} chars (limit {max_chars}): {query}")
        ops = len(BOOLEAN_OPERATOR.findall(query))
        if ops > max_ops:
            raise ConfigError(f"Query has {ops} boolean operators (limit {max_ops}): {query}")


def load_dotenv(path: Path | None = None) -> None:
    """Read KEY=value lines from .env into os.environ without overriding real env vars."""
    path = path or REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


# ---------- paths ----------

@dataclass(frozen=True)
class Paths:
    data: Path

    @property
    def seen(self) -> Path:
        return self.data / "seen.json"

    @property
    def pending_seen(self) -> Path:
        return self.data / "pending_seen.json"

    @property
    def raw_latest(self) -> Path:
        return self.data / "raw" / "latest.json"

    @property
    def inbox(self) -> Path:
        return self.data / "inbox.jsonl"

    @property
    def scored(self) -> Path:
        return self.data / "scored.jsonl"

    @property
    def scored_parts(self) -> Path:
        """Mode A: Claude writes one JSONL file per judging batch here."""
        return self.data / "scored_parts"

    def read_scored(self) -> list[dict]:
        """scored.jsonl, then scored_parts/*.jsonl in name order. Later lines win on repeats,
        so a retry file (e.g. retry.jsonl, sorted after batch-01.jsonl) overrides."""
        rows = read_jsonl(self.scored, skip_bad=True)
        if self.scored_parts.exists():
            for part in sorted(self.scored_parts.glob("*.jsonl")):
                rows += read_jsonl(part, skip_bad=True)
        return rows

    def clear_scored(self) -> None:
        if self.scored.exists():
            self.scored.unlink()
        if self.scored_parts.exists():
            for part in self.scored_parts.glob("*.jsonl"):
                part.unlink()

    @property
    def run_log(self) -> Path:
        return self.data / "run_log.jsonl"

    @property
    def judge_retry(self) -> Path:
        return self.data / "judge_retry.json"

    @property
    def judged(self) -> Path:
        """One JSONL file per IST day with every validated, capped judgment."""
        return self.data / "judged"

    @property
    def digests(self) -> Path:
        return self.data / "digests"


def get_paths(cfg: dict, fixture: bool = False, data_dir: str | Path | None = None) -> Paths:
    if data_dir is None:
        data_dir = cfg["paths"]["fixture_data_dir" if fixture else "data_dir"]
    data = Path(data_dir)
    if not data.is_absolute():
        data = REPO_ROOT / data
    return Paths(data=data)


def repo_path(cfg: dict, key: str) -> Path:
    """Resolve a file named in the config's paths section, relative to the repo root."""
    return REPO_ROOT / cfg["paths"][key]


def fixture_posts_path(cfg: dict) -> Path:
    return repo_path(cfg, "fixture_posts")


# ---------- file IO ----------

def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, data: Any) -> None:
    """Write atomically so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise


def read_jsonl(path: Path, skip_bad: bool = False) -> list[dict]:
    """Read one JSON object per line. With skip_bad, unparseable lines are logged and skipped
    (used for judge output, which must never crash the run)."""
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                if not skip_bad:
                    raise
                log.warning("Skipping unparseable line %d in %s", n, path.name)
    return rows


def write_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


# ---------- time ----------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ist_date(when: datetime | None = None) -> str:
    """Calendar date in India, used for digest names and seen.json stamps."""
    return (when or now_utc()).astimezone(IST).date().isoformat()


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
