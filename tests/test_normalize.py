import unittest

from jobradar.normalize import normalize_item, normalize_items
from tests.helpers import raw_fixtures


class NormalizeFixtureTests(unittest.TestCase):
    def setUp(self):
        self.posts, self.malformed = normalize_items(raw_fixtures())
        self.by_id = {p["id"]: p for p in self.posts}

    def test_all_fixture_items_map(self):
        self.assertEqual(self.malformed, 0)
        self.assertEqual(len(self.posts), 14)

    def test_core_fields(self):
        p = self.by_id["7384000000000000001"]
        self.assertEqual(p["author_name"], "Riya Mehta")
        self.assertIn("Kettlebrook Foods", p["author_headline"])
        self.assertEqual(p["author_type"], "profile")
        self.assertEqual(p["posted_at"], "2026-10-15T02:00:00Z")
        self.assertEqual(p["posted_ts"], 1792029600000)
        self.assertEqual(p["likes"], 214)
        self.assertTrue(p["url"].startswith("https://www.linkedin.com/posts/"))
        self.assertFalse(p["is_repost"])

    def test_repost_text_is_merged(self):
        p = self.by_id["7384000000000000009"]
        self.assertTrue(p["is_repost"])
        self.assertIn("Noida", p["repost_text"])
        self.assertIn("Noida", p["text"])
        self.assertEqual(p["repost_author_name"], "Ishaan Kapoor")
        self.assertEqual(p["author_type"], "company")


    def test_real_shape_extras(self):
        """Fields seen in the real harvestapi run: query object, job card, contentAttributes."""
        p = self.by_id["7384000000000000014"]
        self.assertIn("strategy analyst", p["source_query"])
        self.assertEqual(p["job_title"], "Strategy Analyst")
        self.assertEqual(p["job_company"], "Brightwater Logistics")      # "Job by " stripped
        self.assertEqual(p["job_url"], "https://www.linkedin.com/jobs/view/4400000014/")
        self.assertIn("Noida, Uttar Pradesh", p["extras_text"])
        self.assertEqual(p["mentioned_companies"], ["Brightwater Logistics"])
        self.assertNotIn("Attached job", p["text"])                       # kept separate


class NormalizeDefensiveTests(unittest.TestCase):
    def test_tracking_params_stripped(self):
        p = normalize_item({
            "id": "7384000000000000088", "content": "x",
            "linkedinUrl": "https://www.linkedin.com/posts/a_b-activity-7384000000000000088-xy?utm_source=share",
            "author": {"linkedinUrl": "https://www.linkedin.com/in/someone?miniProfileUrn=urn%3Ali%3Afs"},
        })
        self.assertEqual(p["url"], "https://www.linkedin.com/posts/a_b-activity-7384000000000000088-xy")
        self.assertEqual(p["author_url"], "https://www.linkedin.com/in/someone")

    def test_links_collected_but_not_profile_mentions(self):
        p = normalize_item({"id": "7384000000000000033", "content": "x", "contentAttributes": [
            {"type": "TEXT_LINK", "textLink": "mailto:jobs@acme.in", "hyperlink": None},
            {"type": "PROFILE_MENTION", "hyperlink": "https://www.linkedin.com/in/ACoAA"},
            {"type": "TEXT_LINK", "textLink": "https://lnkd.in/abc"},
        ]})
        self.assertEqual(p["links"], ["mailto:jobs@acme.in", "https://lnkd.in/abc"])
        self.assertIn("mailto:jobs@acme.in", p["extras_text"])

    def test_repost_header(self):
        p = normalize_item({"id": "7384000000000000022", "content": "x",
                            "header": {"text": "Asha Rao reposted this"}})
        self.assertTrue(p["is_repost"])

    def test_missing_optional_fields(self):
        p = normalize_item({"id": "7384000000000000099", "content": "Hiring a generalist in Noida"})
        self.assertEqual(p["author_name"], "")
        self.assertEqual(p["posted_ts"], 0)
        self.assertEqual(p["likes"], 0)
        self.assertIn("7384000000000000099", p["url"])

    def test_urn_id_and_seconds_timestamp(self):
        p = normalize_item({
            "id": "urn:li:activity:7384000000000000077",
            "content": "text",
            "postedAt": {"timestamp": 1792029600},
        })
        self.assertEqual(p["id"], "7384000000000000077")
        self.assertEqual(p["posted_ts"], 1792029600000)

    def test_id_from_url_and_date_string(self):
        p = normalize_item({
            "linkedinUrl": "https://www.linkedin.com/posts/x_y-activity-7384000000000000055-abcd",
            "content": "text",
            "postedAt": "2026-10-15T02:00:00Z",
        })
        self.assertEqual(p["id"], "7384000000000000055")
        self.assertEqual(p["posted_ts"], 1792029600000)

    def test_unusable_items_are_counted_not_crashed(self):
        posts, malformed = normalize_items([None, "junk", {"id": "1"}, {"content": "no id"}])
        self.assertEqual(posts, [])
        self.assertEqual(malformed, 4)

    def test_wrapped_items(self):
        posts, _ = normalize_items({"items": [{"id": "7384000000000000066", "content": "x"}]})
        self.assertEqual(len(posts), 1)

    def test_bad_engagement_types(self):
        p = normalize_item({"id": "7384000000000000044", "content": "x",
                            "engagement": {"likes": "lots", "comments": None}, "author": "oops"})
        self.assertEqual(p["likes"], 0)
        self.assertEqual(p["author_name"], "")


if __name__ == "__main__":
    unittest.main()
