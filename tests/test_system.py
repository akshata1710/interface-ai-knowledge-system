"""Unit tests on small synthetic snapshots (no supplied website data needed).

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "harness"))
sys.path.insert(0, str(ROOT / "solution"))

from contract import Page, Snapshot  # noqa: E402
import solution  # noqa: E402

RATES_V1 = """# Rates

- [Term-Share Certificate Rates](https://cu.test/rates#)

Special

| Certificate | Dividend Rate | Annual Percentage Yield (APY) | Min. to Earn |
| --- | --- | --- | --- |
| 10 Month Fixed Jumbo Certificate Special | 3.86% | 3.90%1 | $100,000.00 |
| 10 Month Fixed Certificate Special | 3.76% | 3.80%2 | $1,000.00 |

Rates are effective as of 03/03/2026 and are subject to change without notice.

Auto Loans

| Loan Program | Term | Annual Percentage Rate (As Low As) |
| --- | --- | --- |
| 2024-27 Model Year | 60 Months | 5.19% APR1 |
| 2021-23 Model Year | 60 Months | 5.44% APR1 |

Rates are effective as of 05/01/2026. All loans are subject to approval.

Share Savings

| Account Type | Balance Tier | Dividend Rate | Annual Percentage Yield (APY)1 | Min. to Open | Min. to Earn |
| --- | --- | --- | --- | --- | --- |
| Share Savings | $25,000+ | 0.20% | 0.20% | $25.00 | $25,000.00 |
| Share Savings | $10,000 – $24,999.99 | 0.15% | 0.15% | $25.00 | $10,000.00 |
| Share Savings | $0 – $9,999.99 | 0.00% | 0.00% | $25.00 | $0.00 |

Rates are effective as of 03/03/2026 and are subject to change without notice.
"""
RATES_V2 = RATES_V1.replace("3.96", "x").replace("3.86% | 3.90%1", "3.96% | 4.00%1") \
    .replace("5.19% APR1", "5.39% APR1").replace("05/01/2026", "08/31/2026").replace("03/03/2026", "09/01/2026")
CALC = """# Vehicle Affordability Calculator

Auto Loan Rates

| Loan Program | Term | APR |
| --- | --- | --- |
| 2023-26 Model Year | 60 Months | 5.34% APR |

Rates are effective as of 1/1/2026.
"""
NEWS_OLD = """[Author](https://cu.test/a)

March 10, 2026

# Example CU news

Example CU is a member-owned cooperative. It has 600 employees and 23 branch office locations.
"""
NEWS_NEW = NEWS_OLD.replace("March 10", "May 05").replace("23 branch", "22 branch").replace("news", "update")
CONTACT = """# Contact

**Routing Number:** 123456789

### Example CU
Example Branch
100 Main St., Springfield, IA, 52001
"""


def snap(sid, day, pages):
    return Snapshot(sid, day, [Page(url=u, title=t + " | Example CU", markdown=m, fetched_at=day + "T00:00:00Z")
                               for u, t, m in pages])


def build():
    ks = solution.build()
    ks.ingest(snap("01_2026-05-12", "2026-05-12", [
        ("https://cu.test/rates", "Rates", RATES_V1),
        ("https://cu.test/calculators/vehicle-affordability", "Vehicle Calculator", CALC),
        ("https://cu.test/newsroom/old", "Old news", NEWS_OLD),
        ("https://cu.test/contact", "Contact", CONTACT),
    ]))
    ks.ingest(snap("02_2026-09-28", "2026-09-28", [
        ("https://cu.test/rates", "Rates", RATES_V2),
        ("https://cu.test/calculators/vehicle-affordability", "Vehicle Calculator", CALC),
        ("https://cu.test/newsroom/old", "Old news", NEWS_OLD),
        ("https://cu.test/newsroom/new", "New news", NEWS_NEW),
        # contact page missing from this crawl -> carried forward
    ]))
    return ks


class SystemTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ks = build()

    def ask(self, q, as_of=None):
        return self.ks.answer(q, as_of=as_of)

    def test_current_value_beats_stale_calculator(self):
        a = self.ask("What's the 60-month auto loan APR for a 2025 vehicle?")
        self.assertIn("5.39%", a.text)
        self.assertNotIn("5.34%", a.text)
        self.assertNotIn("5.19%", a.text)

    def test_as_of_uses_the_site_as_it_was(self):
        a = self.ask("What was the 10-month jumbo certificate special APY?", as_of="2026-05-31")
        self.assertIn("3.90%", a.text)
        self.assertNotIn("4.00%", a.text)

    def test_jumbo_is_a_tier_not_a_conflict(self):
        a = self.ask("What is the 10-month certificate special APY?")
        self.assertIn("3.80%", a.text)          # non-jumbo row
        self.assertNotIn("4.00%", a.text)       # jumbo row not mixed in

    def test_balance_tier_selection(self):
        a = self.ask("What does Share Savings pay on a $15,000 balance?")
        self.assertIn("0.15%", a.text)
        self.assertNotIn("0.20%", a.text)

    def test_highest_tier(self):
        a = self.ask("What is the highest Share Savings APY?")
        self.assertIn("0.20%", a.text)
        self.assertNotIn("0.15%", a.text)

    def test_missing_term_is_refused_without_numbers(self):
        a = self.ask("What is the 60-month jumbo certificate rate?")
        self.assertFalse(a.confident)
        self.assertFalse(re.search(r"\d", a.text.replace("60", "")))

    def test_unpublished_product_refused(self):
        a = self.ask("What rate do you charge on a construction loan?")
        self.assertFalse(a.confident)

    def test_newest_press_release_wins_counts(self):
        a = self.ask("How many branches does Example CU have?")
        self.assertIn("22", a.text)
        self.assertNotIn("23", a.text)

    def test_carried_forward_page_still_answers(self):
        a = self.ask("What is the routing number?")
        self.assertIn("123456789", a.text)

    def test_sources_are_cited(self):
        a = self.ask("What's the 60-month auto loan APR for a 2025 vehicle?")
        self.assertTrue(any(s.url.endswith("/rates") for s in a.sources))

    def test_repeat_is_identical(self):
        q = "What's the 60-month auto loan APR for a 2025 vehicle?"
        self.assertEqual(self.ask(q).text, self.ask(q).text)

    def test_conflicts_list_stale_and_changed_values(self):
        cs = self.ks.conflicts()
        auto = [c for c in cs if "60-month" in c.topic and "Auto" in c.topic and "2024" in c.topic]
        self.assertTrue(auto, [c.topic for c in cs])
        joined = " | ".join(auto[0].values)
        self.assertIn("5.34%", joined)           # calculator page
        self.assertIn("5.19%", joined)           # older crawl of the rate sheet
        self.assertIn("5.39%", auto[0].resolution)
        branches = [c for c in cs if "branch" in c.topic.lower()]
        self.assertTrue(branches and branches[0].resolution == "22")


class SelfTestFixture(unittest.TestCase):
    """The kit's own tiny fixture (harness/selftest) must score perfectly."""

    def test_selftest(self):
        import json
        from run_eval import load_snapshots
        from scoring import score_conflicts, score_item
        base = ROOT / "harness" / "selftest"
        ks = solution.build()
        for s in load_snapshots(base / "snapshots"):
            ks.ingest(s)
        items = [json.loads(l) for l in (base / "eval.jsonl").read_text().splitlines() if l.strip()]
        for it in items:
            r = score_item(it, ks.answer(it["question"], it.get("as_of")))
            self.assertEqual(r["score"], 1.0, (it["id"], r))
        rep = score_conflicts(json.loads((base / "conflicts_gold.json").read_text()), ks.conflicts())
        self.assertEqual(rep["f1"], 1.0)


if __name__ == "__main__":
    unittest.main()
