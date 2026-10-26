import unittest

from jobradar.pipeline import judge_with_retry
from jobradar.validate import apply_caps, validate_batch, validate_judgment
from tests.helpers import CFG, make_judgment, make_post


class ValidateJudgmentTests(unittest.TestCase):
    def test_valid(self):
        clean, errors = validate_judgment(make_judgment(), CFG)
        self.assertEqual(errors, [])
        self.assertEqual(clean["relevance"], 9)

    def test_missing_field(self):
        raw = make_judgment()
        del raw["scam_risk"]
        clean, errors = validate_judgment(raw, CFG)
        self.assertIsNone(clean)
        self.assertIn("missing field 'scam_risk'", errors)

    def test_bad_enum(self):
        clean, errors = validate_judgment(make_judgment(location="Mars"), CFG)
        self.assertIsNone(clean)
        self.assertTrue(any("location" in e for e in errors))

    def test_bad_relevance_and_bool(self):
        _, errors = validate_judgment(make_judgment(relevance="high", is_hiring_post="yes"), CFG)
        self.assertEqual(len(errors), 2)

    def test_coercions(self):
        clean, errors = validate_judgment(make_judgment(
            relevance="12", is_hiring_post="true", role_category="Founders Office",
            poster_type="founder-or-exec", scam_reasons="none really", company=None), CFG)
        self.assertEqual(errors, [])
        self.assertEqual(clean["relevance"], 10)          # clamped to 0-10
        self.assertIs(clean["is_hiring_post"], True)
        self.assertEqual(clean["role_category"], "founders_office")
        self.assertEqual(clean["poster_type"], "founder_or_exec")
        self.assertEqual(clean["scam_reasons"], ["none really"])
        self.assertEqual(clean["company"], "")

    def test_negative_relevance_clamped(self):
        clean, _ = validate_judgment(make_judgment(relevance=-3), CFG)
        self.assertEqual(clean["relevance"], 0)

    def test_summary_trimmed(self):
        clean, _ = validate_judgment(make_judgment(summary=" ".join(["word"] * 30)), CFG)
        self.assertEqual(len(clean["summary"].split()), CFG["judge"]["summary_max_words"])

    def test_not_an_object(self):
        self.assertEqual(validate_judgment("nope", CFG)[0], None)


class ValidateBatchTests(unittest.TestCase):
    def test_missing_unknown_and_retry_override(self):
        raw = [make_judgment("a", location="bad"), make_judgment("a"), make_judgment("zzz")]
        valid, errors = validate_batch(raw, ["a", "b"], CFG)
        self.assertEqual(set(valid), {"a"})                      # later valid line wins
        self.assertEqual(errors, {"b": ["no judgment returned"]})  # zzz ignored


class CapTests(unittest.TestCase):
    def capped(self, **overrides):
        clean, errors = validate_judgment(make_judgment(relevance=10, **overrides), CFG)
        self.assertEqual(errors, [])
        return apply_caps(clean, CFG)

    def test_each_cap(self):
        caps = CFG["judge"]["caps"]
        cases = [
            ({"is_hiring_post": False}, caps["not_hiring_post"]),
            ({"location": "outside_india"}, caps["outside_india_or_other_city"]),
            ({"location": "other_india"}, caps["outside_india_or_other_city"]),
            ({"fresher_fit": "no"}, caps["fresher_fit_no"]),
            ({"role_category": "vc_non_investment"}, caps["vc_non_investment"]),
            ({"scam_risk": "high"}, caps["scam_risk_high"]),
            ({"poster_type": "job_aggregator"}, caps["job_aggregator"]),
        ]
        for overrides, cap in cases:
            with self.subTest(**overrides):
                j = self.capped(**overrides)
                self.assertEqual(j["relevance"], cap)
                self.assertEqual(j["raw_relevance"], 10)

    def test_lowest_cap_wins(self):
        j = self.capped(scam_risk="high", fresher_fit="no")
        self.assertEqual(j["relevance"], 2)

    def test_no_cap_for_clean_post(self):
        j = self.capped()
        self.assertEqual(j["relevance"], 10)
        self.assertEqual(j["caps_applied"], [])

    def test_aggregator_says_verify_at_source(self):
        j = self.capped(poster_type="job_aggregator", why="Good role")
        self.assertTrue(j["verify_at_source"])
        self.assertIn("Verify at source", j["why"])


class RetryTests(unittest.TestCase):
    posts = [make_post("x", pid="p1"), make_post("y", pid="p2")]

    def test_retry_fixes_invalid(self):
        calls = []

        def judge(posts, attempt):
            calls.append([p["id"] for p in posts])
            if attempt == 1:
                return [make_judgment("p1"), make_judgment("p2", relevance="lots")]
            return [make_judgment("p2")]

        valid, errors = judge_with_retry(self.posts, judge, CFG)
        self.assertEqual(set(valid), {"p1", "p2"})
        self.assertEqual(errors, {})
        self.assertEqual(calls, [["p1", "p2"], ["p2"]])          # only the bad one re-judged

    def test_still_invalid_is_skipped_not_crashed(self):
        valid, errors = judge_with_retry(
            self.posts, lambda posts, attempt: [make_judgment(p["id"], location="?") for p in posts], CFG)
        self.assertEqual(valid, {})
        self.assertEqual(set(errors), {"p1", "p2"})

    def test_judge_exception_is_contained(self):
        def judge(posts, attempt):
            raise RuntimeError("API down")

        valid, errors = judge_with_retry(self.posts, judge, CFG)
        self.assertEqual(valid, {})
        self.assertIn("judge error: API down", errors["p1"])


if __name__ == "__main__":
    unittest.main()
