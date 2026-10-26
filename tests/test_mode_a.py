"""Mode A helpers: batch files written by Claude, inbox view, stats, digest note."""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from jobradar.__main__ import main
from jobradar.common import read_json, read_jsonl, write_jsonl
from tests.helpers import make_judgment

KETTLEBROOK = "7384000000000000001"


class ModeAFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.args = ["--fixture", "--data-dir", str(self.data)]
        self.assertEqual(main(["prefilter", *self.args]), 0)
        self.inbox_ids = [p["id"] for p in read_jsonl(self.data / "inbox.jsonl")]

    def tearDown(self):
        self.tmp.cleanup()

    def cli_stdout(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(main([*argv, *self.args]), 0)
        return buf.getvalue()

    def test_inbox_prints_compact_batches(self):
        out = self.cli_stdout("inbox", "--start", "2", "--count", "3")
        rows = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["id"] for r in rows], self.inbox_ids[2:5])
        self.assertIn("text", rows[0])
        self.assertNotIn("likes", rows[0])          # trimmed to judging fields

    def test_batches_retry_and_digest(self):
        parts = self.data / "scored_parts"
        good = [make_judgment(pid, company=f"Co{pid[-2:]}") for pid in self.inbox_ids]
        good[0] = make_judgment(self.inbox_ids[0], location="Mars")      # invalid on purpose
        write_jsonl(parts / "batch-01.jsonl", good[:5])
        write_jsonl(parts / "batch-02.jsonl", good[5:])

        self.assertEqual(main(["validate", *self.args]), 0)
        retry = read_json(self.data / "judge_retry.json")
        self.assertEqual([r["id"] for r in retry], [self.inbox_ids[0]])

        write_jsonl(parts / "retry.jsonl", [make_judgment(self.inbox_ids[0])])
        self.assertEqual(main(["validate", *self.args]), 0)
        self.assertFalse((self.data / "judge_retry.json").exists())

        self.assertEqual(main(["digest", "--note", "Test note", *self.args]), 0)
        digest = next((self.data / "digests").glob("*.md")).read_text(encoding="utf-8")
        self.assertIn("> **Note:** Test note", digest)
        judged = read_jsonl(next((self.data / "judged").glob("*.jsonl")))
        self.assertEqual(len(judged), len(self.inbox_ids))
        kettle = next(r for r in judged if r["id"] == KETTLEBROOK)
        self.assertIn("Kettlebrook", kettle["post_text"])                # kept for outreach

    def test_new_prefilter_clears_old_judgments(self):
        write_jsonl(self.data / "scored_parts" / "batch-01.jsonl", [make_judgment("old")])
        self.assertEqual(main(["prefilter", *self.args]), 0)
        self.assertEqual(list((self.data / "scored_parts").glob("*.jsonl")), [])

    def test_stats_after_fixture_run(self):
        self.assertEqual(main(["run", *self.args]), 0)
        stats = json.loads(self.cli_stdout("stats", "--days", "3"))
        self.assertGreater(stats["prefilter_drops"]["location_other"], 0)
        self.assertEqual(sum(q["top_picks"] for q in stats["queries"].values()), 6)

    def test_tracker_marked_filter(self):
        self.assertEqual(main(["run", *self.args]), 0)
        self.assertEqual(self.cli_stdout("tracker", "--marked").strip(), "")
        self.assertEqual(len(self.cli_stdout("tracker").splitlines()), 7)


if __name__ == "__main__":
    unittest.main()
