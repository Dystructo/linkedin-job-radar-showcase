"""Gemini judge and the Mode B `run`, against fakes. No network."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from jobradar import __main__ as cli
from jobradar.common import read_json, read_jsonl, repo_path, write_json
from jobradar.fetch import FetchError
from jobradar.judge_gemini import GeminiError, GeminiJudge, response_schema
from jobradar.judge_stub import StubJudge
from jobradar.pipeline import judge_with_retry
from tests.helpers import CFG, make_judgment, make_post, raw_fixtures


class FakeResp:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code, self._data, self.headers = status, data, headers or {}
        self.text = json.dumps(data)

    def json(self):
        return self._data


def answer(judgments):
    return FakeResp(data={"candidates": [{"content": {"parts": [{"text": json.dumps(judgments)}]},
                                          "finishReason": "STOP"}]})


def echo_judge(body):
    """Answer a request by judging every post in it."""
    posts = json.loads(body["contents"][0]["parts"][0]["text"])
    return answer([make_judgment(p["id"]) for p in posts])


class GeminiJudgeTests(unittest.TestCase):
    def setUp(self):
        self.t = 0.0
        self.judge = GeminiJudge(CFG, "test-key", sleep=self.sleep, clock=lambda: self.t)
        self.calls = []

    def sleep(self, seconds):
        self.t += seconds

    def script(self, responses):
        def post(url, json=None, timeout=None):
            self.calls.append((url, json))
            r = responses.pop(0)
            if callable(r):
                return r(json)
            if isinstance(r, Exception):
                raise r
            return r
        self.judge.session.post = post

    def posts(self, n):
        return [make_post("We're hiring in Noida.", pid=f"p{i}") for i in range(n)]

    def test_missing_key(self):
        with self.assertRaises(GeminiError):
            GeminiJudge(CFG, None)

    def test_key_in_header_not_url(self):
        self.script([echo_judge])
        self.judge(self.posts(1), 1)
        url, body = self.calls[0]
        self.assertNotIn("key=", url)
        self.assertEqual(self.judge.session.headers["x-goog-api-key"], "test-key")
        self.assertTrue(url.endswith(f"/models/{CFG['judge']['gemini']['models'][0]}:generateContent"))
        self.assertIn("Job Radar judging rubric", body["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")

    def test_batches_and_throttle(self):
        self.script([echo_judge, echo_judge])
        out = self.judge(self.posts(9), 1)                # batch_size 8 -> 2 requests
        self.assertEqual(len(out), 9)
        self.assertEqual(len(self.calls), 2)
        self.assertGreaterEqual(self.t, 60 / CFG["judge"]["gemini"]["requests_per_minute"])

    def test_retry_pass_uses_smaller_batches(self):
        self.script([echo_judge] * 3)
        self.judge(self.posts(9), 2)                      # 8 // 2 = 4 per request
        self.assertEqual(len(self.calls), 3)

    def test_429_retry_honours_retry_delay(self):
        busy = FakeResp(429, {"error": {"details": [{"retryDelay": "17s"}]}})
        self.script([busy, echo_judge])
        self.assertEqual(len(self.judge(self.posts(1), 1)), 1)
        self.assertGreaterEqual(self.t, 17)

    def test_model_fallback_on_404(self):
        self.script([FakeResp(404, {"error": {"message": "not found"}}), echo_judge])
        self.judge(self.posts(1), 1)
        self.assertIn(CFG["judge"]["gemini"]["models"][1], self.calls[1][0])

    def test_bad_json_batch_is_dropped_not_raised(self):
        bad = FakeResp(data={"candidates": [{"content": {"parts": [{"text": "not json"}]},
                                             "finishReason": "MAX_TOKENS"}]})
        self.script([bad])
        self.assertEqual(self.judge(self.posts(2), 1), [])

    def test_blocked_prompt(self):
        self.script([FakeResp(data={"promptFeedback": {"blockReason": "SAFETY"}})])
        self.assertEqual(self.judge(self.posts(1), 1), [])

    def test_thought_parts_ignored(self):
        resp = FakeResp(data={"candidates": [{"content": {"parts": [
            {"text": "thinking...", "thought": True},
            {"text": json.dumps([make_judgment("p0")])}]}}]})
        self.script([resp])
        self.assertEqual(self.judge(self.posts(1), 1)[0]["id"], "p0")

    def test_with_retry_wrapper_recovers(self):
        bad = FakeResp(data={"candidates": [{"content": {"parts": [{"text": "{oops"}]}}]})
        self.script([bad, echo_judge])
        valid, errors = judge_with_retry(self.posts(3), self.judge, CFG)
        self.assertEqual((len(valid), errors), (3, {}))

    def test_schema_matches_validator_enums(self):
        item = response_schema()["items"]
        self.assertEqual(item["properties"]["location"]["enum"],
                         ["ncr", "remote_india", "other_india", "outside_india", "unknown"])
        self.assertEqual(set(item["required"]), set(item["properties"]))


class StubWithName(StubJudge):
    name = "stub-gemini"


class ModeBRunTests(unittest.TestCase):
    """`python -m jobradar run` (no --fixture) with fetch and Gemini faked."""

    NOW = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(cli, "now_utc", return_value=self.NOW),
            mock.patch.dict("os.environ", {"SHEET_WEBAPP_URL": "", "SHEET_SHARED_SECRET": ""}),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def fake_fetch(self, cfg, paths, **kwargs):
        write_json(paths.raw_latest, raw_fixtures())
        return {"status": "SUCCEEDED", "items": 14}

    def run_cli(self):
        return cli.main(["run", "--data-dir", str(self.data)])

    def digest(self):
        return next((self.data / "digests").glob("*.md")).read_text(encoding="utf-8")

    def test_full_mode_b_run(self):
        stub = StubWithName(repo_path(CFG, "fixture_judgments"))
        with mock.patch.object(cli, "fetch_posts", self.fake_fetch), \
                mock.patch.object(cli, "_gemini_judge", return_value=stub):
            self.assertEqual(self.run_cli(), 0)
        self.assertIn("6 top picks", self.digest().splitlines()[0])
        self.assertEqual(len(read_json(self.data / "seen.json")), 13)
        judge_log = [e for e in read_jsonl(self.data / "run_log.jsonl") if e["stage"] == "judge"]
        self.assertEqual(judge_log[0]["judge"], "stub-gemini")

    def test_fetch_failure_still_writes_digest(self):
        with mock.patch.object(cli, "fetch_posts", side_effect=FetchError("Apify down")):
            self.assertEqual(self.run_cli(), 1)
        self.assertIn("Fetch failed: Apify down", self.digest())

    def test_missing_gemini_key_leaves_posts_unseen(self):
        with mock.patch.object(cli, "fetch_posts", self.fake_fetch), \
                mock.patch.dict("os.environ", {"GEMINI_API_KEY": ""}):
            self.assertEqual(self.run_cli(), 1)
        self.assertIn("Judge unavailable", self.digest())
        self.assertEqual(read_json(self.data / "seen.json", default={}), {})


if __name__ == "__main__":
    unittest.main()
