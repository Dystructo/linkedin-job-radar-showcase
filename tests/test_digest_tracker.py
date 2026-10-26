import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from jobradar.common import Paths
from jobradar.digest import render_digest
from jobradar.tracker import COLUMNS, CsvTracker, SheetTracker, push_rows, to_row
from jobradar.validate import apply_caps, validate_judgment
from tests.helpers import CFG, make_judgment


def record(pid, **overrides):
    clean, errors = validate_judgment(make_judgment(pid, **overrides), CFG)
    assert not errors, errors
    r = apply_caps(clean, CFG)
    r.update(post_url=f"https://www.linkedin.com/posts/{pid}", author="A. Founder",
             author_headline="Founder at Acme", posted_at="2026-10-15T02:00:00Z")
    return r


class DigestTests(unittest.TestCase):
    def test_sections_and_order(self):
        records = [
            record("r8", relevance=8, company="Eight"),
            record("r9", relevance=9, company="Nine"),
            record("r6", relevance=6, company="Six"),
            record("u4", relevance=4, location="unknown", company="UnknownLoc"),
            record("r3", relevance=3, company="Three"),
            record("scam", scam_risk="high", company="Scammy"),
        ]
        counts = {"fetched": 20, "prefilter_drops": {"hard_scam": 2, "duplicate_seen": 5}}
        md = render_digest("2026-10-15", records, counts, CFG)
        top, rest = md.split("## Worth a look")
        worth, filtered = rest.split("## Filtered today")
        self.assertTrue(md.startswith("# Job Radar 2026-10-15: 2 top picks"))
        self.assertLess(top.index("Nine"), top.index("Eight"))
        self.assertIn("Six", worth)
        self.assertIn("UnknownLoc", worth)                 # unknown location, score 4
        self.assertNotIn("Three", top + worth)
        self.assertIn("Judged below 5: 2", filtered)        # Three + capped scam
        self.assertIn("1 high", filtered)
        self.assertIn("Scam terms", filtered)
        self.assertNotIn("duplicate_seen", md)

    def test_zero_picks(self):
        md = render_digest("2026-10-15", [], {"fetched": 0}, CFG)
        self.assertIn("0 top picks", md)
        self.assertIn("Nothing scored 7 or more today.", md)

    def test_no_em_dashes(self):
        md = render_digest("2026-10-15", [record("x", summary="Great role — apply now",
                                                 why="0–2 years")], {}, CFG)
        self.assertNotIn("—", md)
        self.assertNotIn("–", md)


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.paths = Paths(data=Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_csv_dedupe_by_url(self):
        t = CsvTracker(self.paths.data / "tracker.csv")
        rows = [to_row(record("a"), "2026-10-15"), to_row(record("b"), "2026-10-15")]
        self.assertEqual(t.append(rows), (2, 0))
        self.assertEqual(t.append(rows + [to_row(record("c"), "2026-10-16")]), (1, 2))
        saved = t.read()
        self.assertEqual(len(saved), 3)
        self.assertEqual(list(saved[0].keys()), COLUMNS)
        self.assertEqual(saved[0]["status"], "")

    def test_formula_injection_neutralised(self):
        row = to_row(record("a", company="=HYPERLINK(\"http://evil\")"), "2026-10-15")
        self.assertTrue(row["company"].startswith("'="))

    def test_sheet_failure_saves_rows_for_next_run(self):
        sheet = SheetTracker("https://script.google.com/macros/s/x/exec", "secret")
        rows = [to_row(record("a"), "2026-10-15")]
        with mock.patch("jobradar.tracker.requests.post",
                        side_effect=requests.ConnectionError("offline")):
            stats = push_rows(sheet, rows, CFG, self.paths)
        self.assertEqual(stats["unsent"], 1)
        unsent = CsvTracker(self.paths.data / CFG["tracker"]["unsent_file"])
        self.assertEqual(len(unsent.read()), 1)

        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"ok": True, "added": 2, "skipped": 0}
        ok.raise_for_status.return_value = None
        with mock.patch("jobradar.tracker.requests.post", return_value=ok) as post:
            stats = push_rows(sheet, [to_row(record("b"), "2026-10-16")], CFG, self.paths)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["secret"], "secret")
        self.assertEqual([r["post_url"][-1] for r in sent["rows"]], ["a", "b"])  # retried + new
        self.assertEqual(stats["added"], 2)
        self.assertFalse(unsent.path.exists())

    def test_sheet_rejects_bad_secret(self):
        sheet = SheetTracker("https://script.google.com/macros/s/x/exec", "wrong")
        resp = mock.Mock()
        resp.json.return_value = {"ok": False, "error": "unauthorized"}
        resp.raise_for_status.return_value = None
        with mock.patch("jobradar.tracker.requests.post", return_value=resp):
            stats = push_rows(sheet, [to_row(record("a"), "2026-10-15")], CFG, self.paths)
        self.assertIn("unauthorized", stats["error"])


if __name__ == "__main__":
    unittest.main()
