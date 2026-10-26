"""Fetch tests against a scripted fake of the Apify API. No network."""
import tempfile
import unittest
from pathlib import Path

import requests

from jobradar.common import Paths, append_jsonl, ist_date, read_json, read_jsonl
from jobradar.fetch import ApifyClient, FetchError, build_actor_input, fetch_posts, plan_volume
from tests.helpers import CFG, raw_fixtures


class FakeResponse:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.text = str(data)

    def json(self):
        return self._data


class FakeSession:
    """Answers requests from a script of (method, path fragment) -> response."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        expected_method, fragment, response = self.script.pop(0)
        assert method == expected_method and fragment in url, (method, url, expected_method, fragment)
        if isinstance(response, Exception):
            raise response
        return response


def run(status, dataset="ds1", run_id="run1", **extra):
    return FakeResponse(data={"data": {"id": run_id, "status": status,
                                       "defaultDatasetId": dataset, **extra}})


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = Paths(data=Path(self.tmp.name))
        self.clock = FakeClock()

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, script, token="tok"):
        c = ApifyClient(CFG, token=token, sleep=self.clock.sleep, clock=self.clock)
        c.session = FakeSession(script)
        if token:
            c.session.headers["Authorization"] = f"Bearer {token}"
        return c

    def test_actor_input_shape(self):
        inp = build_actor_input(["a", "b"], CFG, 6)
        self.assertEqual(inp, {"searchQueries": ["a", "b"],
                               "postedLimit": CFG["apify"]["posted_limit"],
                               "sortBy": "date", "maxPosts": 6})

    def test_happy_path(self):
        items = raw_fixtures()
        c = self.client([
            ("POST", "/actors/harvestapi~linkedin-post-search/runs", run("READY")),
            ("GET", "/actor-runs/run1", run("RUNNING")),
            ("GET", "/actor-runs/run1", run("SUCCEEDED", usageTotalUsd=0.03)),
            ("GET", "/datasets/ds1/items", FakeResponse(data=items)),
        ])
        entry = fetch_posts(CFG, self.paths, client=c)
        self.assertEqual(entry["status"], "SUCCEEDED")
        self.assertEqual(entry["items"], len(items))
        self.assertEqual(len(read_json(self.paths.raw_latest)), len(items))
        post_kwargs = c.session.calls[0][2]
        self.assertEqual(post_kwargs["json"]["maxPosts"], CFG["apify"]["max_posts_per_query"])
        self.assertEqual(len(post_kwargs["json"]["searchQueries"]), len(CFG["queries"]))
        self.assertEqual(post_kwargs["params"]["maxTotalChargeUsd"],
                         CFG["apify"]["max_charge_usd_per_run"])
        self.assertEqual(c.session.headers["Authorization"], "Bearer tok")

    def test_no_token_sends_no_auth_header(self):
        c = ApifyClient(CFG, token=None)
        self.assertNotIn("Authorization", c.session.headers)

    def test_test_mode_limits(self):
        c = self.client([
            ("POST", "/runs", run("SUCCEEDED")),
            ("GET", "/actor-runs/run1", run("SUCCEEDED")),
            ("GET", "/datasets/ds1/items", FakeResponse(data=[])),
        ])
        fetch_posts(CFG, self.paths, max_posts=5, n_queries=2, client=c)
        body = c.session.calls[0][2]["json"]
        self.assertEqual((body["maxPosts"], len(body["searchQueries"])), (5, 2))

    def test_failed_run_without_items_raises_and_logs(self):
        c = self.client([
            ("POST", "/runs", run("READY")),
            ("GET", "/actor-runs/run1", run("FAILED", statusMessage="boom")),
            ("GET", "/datasets/ds1/items", FakeResponse(data=[])),
        ])
        with self.assertRaises(FetchError):
            fetch_posts(CFG, self.paths, client=c)
        self.assertEqual(read_jsonl(self.paths.run_log)[-1]["status"], "FAILED")

    def test_failed_run_with_partial_items_keeps_them(self):
        c = self.client([
            ("POST", "/runs", run("READY")),
            ("GET", "/actor-runs/run1", run("TIMED-OUT")),
            ("GET", "/datasets/ds1/items", FakeResponse(data=raw_fixtures()[:3])),
        ])
        entry = fetch_posts(CFG, self.paths, client=c)
        self.assertEqual(entry["items"], 3)

    def test_timeout_aborts_run(self):
        timeout = CFG["apify"]["run_timeout_seconds"]

        class SlowSession(FakeSession):
            def request(inner, method, url, **kwargs):
                if "waitForFinish" in kwargs.get("params", {}):
                    self.clock.t += kwargs["params"]["waitForFinish"]
                return FakeSession.request(inner, method, url, **kwargs)

        script = [("POST", "/runs", run("READY"))]
        script += [("GET", "/actor-runs/run1", run("RUNNING"))] * (timeout // 60)
        script += [("POST", "/actor-runs/run1/abort", run("ABORTING")),
                   ("GET", "/datasets/ds1/items", FakeResponse(data=[]))]
        c = ApifyClient(CFG, token="t", sleep=self.clock.sleep, clock=self.clock)
        c.session = SlowSession(script)
        with self.assertRaises(FetchError) as ctx:
            fetch_posts(CFG, self.paths, client=c)
        self.assertIn("TIMED-OUT", str(ctx.exception))
        self.assertTrue(any(call[1].endswith("/abort") for call in c.session.calls))

    def test_transient_errors_are_retried(self):
        c = self.client([
            ("POST", "/runs", requests.ConnectionError("blip")),
            ("POST", "/runs", FakeResponse(status=503, data={})),
            ("POST", "/runs", run("READY")),
        ])
        self.assertEqual(c.start_run("a/b", {}, 60, 0.1)["id"], "run1")

    def test_bad_token_message(self):
        c = self.client([("POST", "/runs", FakeResponse(status=401, data={}))])
        with self.assertRaises(FetchError) as ctx:
            c.start_run("a/b", {}, 60, 0.1)
        self.assertIn("token", str(ctx.exception))

    def test_blocked_host_message(self):
        blocked = FakeResponse(status=403, data={}, headers={"x-deny-reason": "host_not_allowed"})
        c = self.client([("POST", "/runs", blocked)])
        with self.assertRaises(FetchError) as ctx:
            c.start_run("a/b", {}, 60, 0.1)
        self.assertIn("allowed domains", str(ctx.exception))


class BudgetTests(unittest.TestCase):
    def test_plan_volume(self):
        budget = CFG["apify"]["monthly_post_budget"]
        per = CFG["apify"]["max_posts_per_query"]
        self.assertEqual(plan_volume(CFG, 12, 0), per)
        self.assertEqual(plan_volume(CFG, 12, budget - 24), 2)     # shrinks near the limit
        self.assertEqual(plan_volume(CFG, 12, budget - 5), 0)      # not enough for 1 each
        self.assertEqual(plan_volume(CFG, 12, budget + 10), 0)

    def test_budget_exhausted_skips_without_calling_apify(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(data=Path(tmp))
            append_jsonl(paths.run_log, {"stage": "fetch", "date": ist_date(),
                                         "items": CFG["apify"]["monthly_post_budget"]})
            entry = fetch_posts(CFG, paths, client=object())  # any call would crash
            self.assertEqual(entry["status"], "SKIPPED_BUDGET")
            self.assertEqual(read_json(paths.raw_latest), [])


if __name__ == "__main__":
    unittest.main()
