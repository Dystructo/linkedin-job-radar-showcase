"""End to end on fixtures with the stub judge: correct digest and tracker, zero network calls,
and a same-day re-run that neither duplicates tracker rows nor re-judges seen posts."""
import socket
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jobradar.__main__ import main
from jobradar.common import read_json, read_jsonl
from jobradar.tracker import CsvTracker


def _no_network(*args, **kwargs):
    raise AssertionError("network call attempted during a fixture run")


class FixtureEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)
        self.patch = mock.patch.object(socket.socket, "connect", _no_network)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def run_fixture(self):
        self.assertEqual(main(["run", "--fixture", "--data-dir", str(self.data)]), 0)

    def digest_text(self):
        files = list((self.data / "digests").glob("*.md"))
        self.assertEqual(len(files), 1)
        return files[0].read_text(encoding="utf-8")

    def test_full_run_then_same_day_rerun(self):
        self.run_fixture()
        md = self.digest_text()
        top = md.split("## Worth a look")[0]
        self.assertIn("6 top picks", md.splitlines()[0])
        for company in ("Kettlebrook Foods", "Northgate Ventures", "Ledgerline",
                        "Saltwater Family Office", "Mosaic Health", "Paperkite"):
            self.assertIn(company, top)
        self.assertNotIn("Lattice Capital", top)         # VC marketing capped to 4
        self.assertNotIn("Arcbright", md)                # Bengaluru dropped before judging
        self.assertIn("Brightwater Logistics", md.split("## Worth a look")[1])

        tracker = CsvTracker(self.data / "tracker.csv")
        rows = tracker.read()
        self.assertEqual(len(rows), 7)                   # every post scoring 5+
        scored = {r["id"]: r for r in read_jsonl(next((self.data / "judged").glob("*.jsonl")))}
        self.assertEqual(scored["7384000000000000003"]["relevance"], 4)   # vc_non_investment cap
        self.assertEqual(scored["7384000000000000007"]["relevance"], 2)   # scam_risk high cap
        self.assertEqual(scored["7384000000000000009"]["relevance"], 8)   # aggregator cap
        self.assertNotIn("7384000000000000006", scored)  # hard scam never reached the judge
        self.assertEqual(len(read_json(self.data / "seen.json")), 13)

        # Same-day re-run: nothing new is judged, tracker unchanged, digest keeps the picks.
        self.run_fixture()
        self.assertEqual(len(tracker.read()), 7)
        self.assertIn("6 top picks", self.digest_text().splitlines()[0])
        judge_logs = [e for e in read_jsonl(self.data / "run_log.jsonl") if e["stage"] == "judge"]
        self.assertEqual([e["judged"] for e in judge_logs], [9, 0])


if __name__ == "__main__":
    unittest.main()
