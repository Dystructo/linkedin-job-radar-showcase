import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from jobradar.common import ConfigError, Paths, validate_config
from jobradar.normalize import normalize_items
from jobradar.prefilter import Prefilter, TermMatcher, normalize_for_match
from jobradar.seen import load_seen, mark_seen
from tests.helpers import CFG, make_post, raw_fixtures

NOW = datetime(2026, 10, 15, 3, 0, tzinfo=timezone.utc)
LONG_TAIL = " We move fast, care about craft, and want people who love solving messy problems."


class FixturePipelineTests(unittest.TestCase):
    """The 14 fixture posts must land exactly where the plan says."""

    @classmethod
    def setUpClass(cls):
        posts, _ = normalize_items(raw_fixtures())
        cls.result = Prefilter(CFG).run(posts, seen_ids={}, now=NOW)
        cls.dropped = {d["id"]: d["reason"] for d in cls.result.dropped}
        cls.kept = {p["id"]: p for p in cls.result.kept}

    def test_drop_reasons(self):
        self.assertEqual(self.dropped["7384000000000000004"], "location_other")   # Bengaluru
        self.assertEqual(self.dropped["7384000000000000005"], "location_other")   # New York
        self.assertEqual(self.dropped["7384000000000000006"], "hard_scam")        # registration fee
        self.assertEqual(self.dropped["7384000000000000008"], "job_seeker")       # #opentowork
        self.assertEqual(self.result.counts["duplicate_in_batch"], 1)

    def test_kept_posts(self):
        expected = {"7384000000000000001", "7384000000000000002", "7384000000000000003",
                    "7384000000000000007", "7384000000000000009", "7384000000000000010",
                    "7384000000000000011", "7384000000000000013", "7384000000000000014"}
        self.assertEqual(set(self.kept), expected)

    def test_location_tags(self):
        self.assertEqual(self.kept["7384000000000000001"]["location_tag"], "ncr")
        self.assertEqual(self.kept["7384000000000000009"]["location_tag"], "ncr")  # Noida in repost
        self.assertEqual(self.kept["7384000000000000010"]["location_tag"], "remote_india")
        self.assertEqual(self.kept["7384000000000000011"]["location_tag"], "unknown")

    def test_soft_scam_flags(self):
        flags = set(self.kept["7384000000000000007"]["prefilter_flags"])
        self.assertTrue({"whatsapp", "wa.me", "bulk hiring", "immediate joining", "no interview"} <= flags)
        self.assertEqual(self.kept["7384000000000000001"]["prefilter_flags"], [])

    def test_sorted_ncr_first_then_newest(self):
        tags = [p["location_tag"] for p in self.result.kept]
        first_non_ncr = next(i for i, t in enumerate(tags) if t != "ncr")
        self.assertTrue(all(t != "ncr" for t in tags[first_non_ncr:]))
        ncr_ts = [p["posted_ts"] for p in self.result.kept[:first_non_ncr]]
        self.assertEqual(ncr_ts, sorted(ncr_ts, reverse=True))

    def test_ids_to_mark_seen(self):
        ids = self.result.ids_to_mark_seen()
        self.assertEqual(len(ids), len(set(ids)))
        self.assertIn("7384000000000000006", ids)   # dropped scams are remembered too
        self.assertEqual(len(ids), 13)               # 14 items minus one duplicate


class DropReasonTests(unittest.TestCase):
    def setUp(self):
        self.pf = Prefilter(CFG)

    def reason(self, text, **kw):
        return self.pf.check(make_post(text, **kw), int(NOW.timestamp() * 1000))[0]

    def test_too_short(self):
        self.assertEqual(self.reason("Hiring! DM me."), "too_short")

    def test_too_old(self):
        limit_h = CFG["prefilter"]["max_post_age_hours"]
        now_ms = int(NOW.timestamp() * 1000)
        text = "We're hiring in Gurugram." + LONG_TAIL
        self.assertEqual(self.reason(text, ts=now_ms - (limit_h + 1) * 3_600_000), "too_old")
        self.assertIsNone(self.reason(text, ts=now_ms - (limit_h - 1) * 3_600_000))

    def test_no_hiring_term(self):
        text = ("Five lessons from my first year in venture capital: conviction matters, founders "
                "matter more, and patience is underrated. Markets reward people who keep learning.")
        self.assertEqual(self.reason(text), "no_hiring_term")

    def test_each_hard_scam_term(self):
        for term in CFG["prefilter"]["hard_scam_terms"]:
            with self.subTest(term=term):
                text = f"We're hiring in Delhi. Note: {term} applies to all candidates." + LONG_TAIL
                self.assertEqual(self.reason(text), "hard_scam")

    def test_seeker_with_real_hiring_is_kept(self):
        text = "Open to work? We're hiring a Founder's Office associate in Noida." + LONG_TAIL
        self.assertIsNone(self.reason(text))

    def test_seeker_looking_for_does_not_count_as_hiring(self):
        text = "I am looking for analyst roles in Gurugram, please refer me." + LONG_TAIL
        self.assertEqual(self.reason(text), "job_seeker")

    def test_other_city_with_ncr_is_kept(self):
        text = "We're hiring for offices in Bengaluru and Gurugram." + LONG_TAIL
        self.assertIsNone(self.reason(text))

    def test_other_city_with_remote_india_is_kept(self):
        text = "We're hiring: Mumbai HQ, but this role is remote within India." + LONG_TAIL
        self.assertIsNone(self.reason(text))

    def test_hidden_scam_link_is_caught(self):
        text = "We're hiring a Founder's Office associate in Gurugram. Apply here." + LONG_TAIL
        reason = self.reason(text, extras_text="[Links: https://t.me/quickjobs]")
        self.assertEqual(reason, "hard_scam")

    def test_job_card_location_counts(self):
        text = "We're hiring a Founder's Office associate. Freshers welcome." + LONG_TAIL
        post = make_post(text, extras_text="[Attached job: Associate | Bengaluru, Karnataka, India]")
        self.assertEqual(self.pf.check(post, None)[0], "location_other")
        post["extras_text"] = "[Attached job: Associate | Gurugram, Haryana, India (On-site)]"
        self.assertEqual(self.pf.check(post, None)[2]["location_tag"], "ncr")

    def test_extras_do_not_count_toward_length(self):
        post = make_post("Hiring! DM me.", extras_text="[Links: " + "https://lnkd.in/x, " * 20 + "]")
        self.assertEqual(self.pf.check(post, None)[0], "too_short")

    def test_duplicate_seen(self):
        post = make_post("We're hiring a chief of staff in Delhi." + LONG_TAIL, pid="1234567890123456789")
        result = self.pf.run([post], seen_ids={"1234567890123456789": "2026-10-01"}, now=NOW)
        self.assertEqual(result.counts["duplicate_seen"], 1)
        self.assertEqual(result.kept, [])
        self.assertEqual(result.ids_to_mark_seen(), [])

    def test_cap(self):
        posts = [make_post("We're hiring in Noida." + LONG_TAIL, pid=f"90000000000000{i:05d}")
                 for i in range(70)]
        result = self.pf.run(posts, seen_ids={}, now=NOW)
        self.assertEqual(len(result.kept), CFG["prefilter"]["max_posts_for_judging"])
        self.assertEqual(result.counts["over_cap"], 10)
        self.assertEqual(len(result.ids_to_mark_seen()), 60)


class MatchingTests(unittest.TestCase):
    def test_word_boundaries(self):
        m = TermMatcher(["usa", "uk", "role"])
        self.assertEqual(m.find(normalize_for_match("Our usage based pricing, ukulele, control")), [])
        self.assertEqual(sorted(m.find(normalize_for_match("USA or UK-based role"))), ["role", "uk", "usa"])

    def test_curly_quotes_and_hyphens(self):
        m = TermMatcher(["we're hiring", "pan india", "founder's office"])
        text = normalize_for_match("We’re hiring! Pan-India. Founder’s Office")
        self.assertEqual(len(m.find(text)), 3)

    def test_symbol_edges(self):
        m = TermMatcher(["t.me/", "@gmail.com", "#opentowork", "pay ₹"])
        text = normalize_for_match("join https://t.me/abc, mail x@gmail.com #OpenToWork pay ₹500")
        self.assertEqual(len(m.find(text)), 4)


class SeenTests(unittest.TestCase):
    def test_mark_and_prune(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = Paths(data=Path(tmp))
            mark_seen(paths, ["old"], retention_days=60, today="2026-08-01")
            stats = mark_seen(paths, ["a", "b", "a"], retention_days=60, today="2026-10-15")
            self.assertEqual(stats, {"added": 2, "pruned": 1, "total": 2})
            self.assertEqual(load_seen(paths), {"a": "2026-10-15", "b": "2026-10-15"})


class ConfigTests(unittest.TestCase):
    def test_real_config_is_valid(self):
        validate_config(CFG)  # raises if a query breaks actor limits
        self.assertGreaterEqual(len(CFG["queries"]), 10)

    def test_operator_limit_enforced(self):
        bad = {**CFG, "queries": ["a OR b OR c OR d OR e OR f OR g"]}
        with self.assertRaises(ConfigError):
            validate_config(bad)


if __name__ == "__main__":
    unittest.main()
