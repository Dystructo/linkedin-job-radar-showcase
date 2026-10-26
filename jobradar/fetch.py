"""Fetch LinkedIn posts through an Apify actor that needs no LinkedIn login.

Flow: start one actor run with every query -> long-poll until it ends -> download the dataset
-> save data/raw/latest.json. Never uses LinkedIn cookies or accounts.

Actor-specific code is build_actor_input() here plus jobradar/normalize.py. To swap actors,
change apify.actor in config.yaml and those two places.

Auth: if APIFY_TOKEN is set (your laptop, GitHub Actions) it is sent as a bearer token. In the
Claude Code routine the token is a network secret that Anthropic's proxy adds on the way out,
so the code sends no header and never sees the value.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

import requests

from .common import Paths, append_jsonl, ist_date, now_utc, read_jsonl, write_json

log = logging.getLogger("jobradar")

TERMINAL = {"SUCCEEDED", "FAILED", "TIMED-OUT", "ABORTED"}
RETRY_STATUS = {429, 500, 502, 503, 504}


class FetchError(RuntimeError):
    pass


def build_actor_input(queries: list[str], cfg: dict, max_posts: int) -> dict:
    """Input for harvestapi/linkedin-post-search. Actor-specific."""
    a = cfg["apify"]
    return {
        "searchQueries": queries,
        "postedLimit": a["posted_limit"],
        "sortBy": a["sort_by"],
        "maxPosts": max_posts,
    }


class ApifyClient:
    def __init__(self, cfg: dict, token: str | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic):
        a = cfg["apify"]
        self.base = a["api_base"].rstrip("/")
        self.retries = int(a["http_retries"])
        self.pause = float(a["poll_interval_seconds"])
        self.session = requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        self.sleep, self.clock = sleep, clock

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        url = f"{self.base}{path}"
        kwargs.setdefault("timeout", 90)  # > the 60s server-side long poll
        for attempt in range(self.retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                if attempt == self.retries:
                    raise FetchError(f"Could not reach Apify ({type(exc).__name__})") from exc
                self.sleep(self.pause * (attempt + 1))
                continue
            if resp.status_code in RETRY_STATUS and attempt < self.retries:
                self.sleep(self.pause * (attempt + 1))
                continue
            if resp.status_code == 401:
                raise FetchError("Apify rejected the token (401). Locally: check APIFY_TOKEN in "
                                 ".env. In the routine: check the api.apify.com network secret.")
            if resp.status_code == 403 and "host_not_allowed" in resp.headers.get("x-deny-reason", ""):
                raise FetchError("The cloud environment blocked api.apify.com. Add it to the "
                                 "environment's allowed domains (see README).")
            if resp.status_code >= 400:
                raise FetchError(f"Apify {method} {path} failed: HTTP {resp.status_code} "
                                 f"{resp.text[:300]}")
            return resp.json()
        raise FetchError(f"Apify {method} {path} kept failing")  # pragma: no cover

    def start_run(self, actor: str, actor_input: dict, timeout_s: int, max_charge_usd: float) -> dict:
        actor_id = actor.replace("/", "~")
        params = {"timeout": timeout_s, "maxTotalChargeUsd": max_charge_usd}
        return self._request("POST", f"/actors/{actor_id}/runs", json=actor_input,
                             params=params)["data"]

    def wait_for_run(self, run_id: str, timeout_s: int) -> dict:
        deadline = self.clock() + timeout_s
        while True:
            remaining = deadline - self.clock()
            wait = max(1, min(60, int(remaining)))
            run = self._request("GET", f"/actor-runs/{run_id}",
                                params={"waitForFinish": wait})["data"]
            if run.get("status") in TERMINAL:
                return run
            if self.clock() >= deadline:
                log.error("Apify run %s still %s after %ss; aborting it", run_id,
                          run.get("status"), timeout_s)
                try:
                    self._request("POST", f"/actor-runs/{run_id}/abort")
                except FetchError as exc:
                    log.warning("Abort failed: %s", exc)
                return {**run, "status": "TIMED-OUT"}

    def dataset_items(self, dataset_id: str) -> list:
        items = self._request("GET", f"/datasets/{dataset_id}/items",
                              params={"clean": "true", "format": "json"})
        return items if isinstance(items, list) else []


def month_posts_used(paths: Paths, month: str) -> int:
    return sum(e.get("items", 0) for e in read_jsonl(paths.run_log, skip_bad=True)
               if e.get("stage") == "fetch" and str(e.get("date", "")).startswith(month))


def plan_volume(cfg: dict, n_queries: int, used: int, max_posts: int | None = None) -> int:
    """Posts per query for this run, shrunk to fit the monthly budget. 0 means skip."""
    per_query = max_posts or int(cfg["apify"]["max_posts_per_query"])
    remaining = int(cfg["apify"]["monthly_post_budget"]) - used
    if remaining <= 0 or n_queries == 0:
        return 0
    return min(per_query, remaining // n_queries)


def fetch_posts(cfg: dict, paths: Paths, max_posts: int | None = None,
                n_queries: int | None = None, client: ApifyClient | None = None) -> dict:
    """Run the actor and save raw items to data/raw/latest.json. Returns the run_log entry."""
    a = cfg["apify"]
    queries = cfg["queries"][: n_queries or None]
    today = ist_date()
    used = month_posts_used(paths, today[:7])
    per_query = plan_volume(cfg, len(queries), used, max_posts)
    entry = {"run_at": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "date": today, "stage": "fetch",
             "actor": a["actor"], "queries": len(queries), "per_query": per_query,
             "month_used_before": used, "items": 0}

    if per_query == 0:
        entry["status"] = "SKIPPED_BUDGET"
        log.warning("Monthly post budget reached (%d of %d used). Skipping fetch.", used,
                    a["monthly_post_budget"])
        write_json(paths.raw_latest, [])
        append_jsonl(paths.run_log, entry)
        return entry
    if per_query < (max_posts or int(a["max_posts_per_query"])):
        log.warning("Near the monthly budget: fetching %d posts per query instead of %d",
                    per_query, max_posts or a["max_posts_per_query"])

    client = client or ApifyClient(cfg, token=os.environ.get("APIFY_TOKEN"))
    actor_input = build_actor_input(queries, cfg, per_query)
    run = client.start_run(a["actor"], actor_input, int(a["run_timeout_seconds"]),
                           float(a["max_charge_usd_per_run"]))
    log.info("Started Apify run %s (%d queries x up to %d posts)", run.get("id"), len(queries),
             per_query)
    run = client.wait_for_run(run["id"], int(a["run_timeout_seconds"]))
    status = run.get("status")
    items = client.dataset_items(run["defaultDatasetId"]) if run.get("defaultDatasetId") else []

    entry.update(run_id=run.get("id"), status=status, items=len(items),
                 usage_usd=run.get("usageTotalUsd"))
    if status != "SUCCEEDED":
        msg = f"Apify run ended {status}: {run.get('statusMessage') or 'no message'}"
        if not items:
            entry["error"] = msg
            append_jsonl(paths.run_log, entry)
            raise FetchError(msg)
        log.warning("%s; keeping the %d posts it collected", msg, len(items))

    write_json(paths.raw_latest, items)
    append_jsonl(paths.run_log, entry)
    log.info("Fetched %d posts -> %s", len(items), paths.raw_latest)
    return entry
