"""Tests for the parts where a silent bug would corrupt every published number.

Run: python3 -m unittest discover -s tests -v

Deliberately stdlib unittest, no pytest — the whole repo installs with nothing.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shelf import db, extract, prompts as promptgen, score  # noqa: E402


class TestWilson(unittest.TestCase):
    def test_zero_successes_never_goes_negative(self):
        """The reason we use Wilson at all: the normal approximation returns a
        negative lower bound for 0/20, which would print as a negative rate."""
        p, lo, hi = score.wilson(0, 20)
        self.assertEqual(p, 0.0)
        self.assertGreaterEqual(lo, 0.0)
        self.assertGreater(hi, 0.0)          # genuine uncertainty remains
        self.assertLess(hi, 0.20)

    def test_all_successes_capped_at_one(self):
        _, lo, hi = score.wilson(20, 20)
        self.assertLessEqual(hi, 1.0)
        self.assertLess(lo, 1.0)

    def test_half(self):
        p, lo, hi = score.wilson(50, 100)
        self.assertAlmostEqual(p, 0.5, places=6)
        self.assertAlmostEqual(lo, 0.4038, places=3)
        self.assertAlmostEqual(hi, 0.5962, places=3)

    def test_interval_narrows_with_n(self):
        _, lo_s, hi_s = score.wilson(5, 10)
        _, lo_l, hi_l = score.wilson(500, 1000)
        self.assertGreater(hi_s - lo_s, hi_l - lo_l)

    def test_empty(self):
        self.assertEqual(score.wilson(0, 0), (0.0, 0.0, 0.0))


class TestJaccard(unittest.TestCase):
    def test_identical(self):
        self.assertEqual(score.jaccard({1, 2}, {1, 2}), 1.0)

    def test_disjoint(self):
        self.assertEqual(score.jaccard({1}, {2}), 0.0)

    def test_both_empty_is_agreement(self):
        # Two answers that recommend nobody agree perfectly; scoring this as 0
        # would understate stability on prompts that decline to recommend.
        self.assertEqual(score.jaccard(set(), set()), 1.0)

    def test_partial(self):
        self.assertAlmostEqual(score.jaccard({1, 2}, {2, 3}), 1 / 3)


class TestMentions(unittest.TestCase):
    BRANDS = [
        {"id": 1, "name": "Clay", "aliases": ["Claygent"], "case_sensitive": 1},
        {"id": 2, "name": "Instantly", "aliases": [], "case_sensitive": 1},
        {"id": 3, "name": "Outreach", "aliases": ["Outreach.io"], "case_sensitive": 1},
        {"id": 4, "name": "11x", "aliases": ["11x.ai"], "case_sensitive": 0},
        {"id": 5, "name": "Amplemarket", "aliases": [], "case_sensitive": 0},
    ]

    def _by_id(self, hits):
        return {h["brand_id"]: h for h in hits}

    def test_recommended_vs_merely_mentioned(self):
        text = ("Here are my picks:\n\n"
                "1. **Outreach** — strong sequencing.\n"
                "2. **11x** — autonomous sending.\n\n"
                "Amplemarket also exists but is less common.")
        got = self._by_id(extract.find_mentions(text, self.BRANDS))
        self.assertEqual(got[3]["recommended"], 1)
        self.assertEqual(got[4]["recommended"], 1)
        self.assertIn(5, got)
        self.assertEqual(got[5]["recommended"], 0, "prose aside is not a recommendation")

    def test_rank_follows_document_order(self):
        text = "1. **Outreach** — first.\n2. **11x** — second."
        got = self._by_id(extract.find_mentions(text, self.BRANDS))
        self.assertEqual(got[3]["rank_pos"], 1)
        self.assertEqual(got[4]["rank_pos"], 2)

    def test_case_sensitive_brands_ignore_common_words(self):
        """'respond instantly' and 'moulded from clay' must not be brand hits."""
        text = "Your team should respond instantly to leads, and not be moulded from clay."
        self.assertEqual(extract.find_mentions(text, self.BRANDS), [])

    def test_case_sensitive_brand_still_matches_proper_use(self):
        text = "- **Instantly** is a sending platform."
        got = self._by_id(extract.find_mentions(text, self.BRANDS))
        self.assertEqual(got[2]["recommended"], 1)

    def test_word_boundaries(self):
        # 'Clayton' must not match Clay; '111x' must not match 11x.
        text = "Clayton Industries evaluated 111x options."
        self.assertEqual(extract.find_mentions(text, self.BRANDS), [])

    def test_alias_matching(self):
        text = "- **Claygent** handles the research step."
        got = self._by_id(extract.find_mentions(text, self.BRANDS))
        self.assertIn(1, got)

    def test_domain_suffix_is_not_a_word_boundary_failure(self):
        text = "- **Outreach.io** is the incumbent."
        self.assertIn(3, self._by_id(extract.find_mentions(text, self.BRANDS)))

    def test_prompted_self_reference_flagged(self):
        """'Alternatives to X' answers always repeat X. That is the question
        echoing back, not visibility, so it must be flagged for exclusion."""
        text = "Since you're moving off Instantly, consider:\n1. **Outreach** — mature."
        got = self._by_id(extract.find_mentions(text, self.BRANDS, subject="Instantly"))
        self.assertEqual(got[2]["prompted"], 1)
        self.assertEqual(got[3]["prompted"], 0)

    def test_empty_text(self):
        self.assertEqual(extract.find_mentions("", self.BRANDS), [])


class TestAmbiguousBrandNames(unittest.TestCase):
    """Calibration found one failure mode behind 7 of 13 extractor errors:
    a brand whose name is an ordinary English word matches title-case prose.
    Case sensitivity alone does not fix it - 'Automated Outreach' and
    'Qualified Meetings' are capitalised too."""

    BRANDS = [
        {"id": 1, "name": "Outreach", "aliases": ["Outreach.io"], "case_sensitive": 1},
        {"id": 2, "name": "Qualified", "aliases": [], "case_sensitive": 1},
        {"id": 3, "name": "Apollo", "aliases": [], "case_sensitive": 1},
        {"id": 4, "name": "Amplemarket", "aliases": [], "case_sensitive": 0},
    ]

    def _names(self, text, subject=None):
        by_id = {b["id"]: b["name"] for b in self.BRANDS}
        return {by_id[h["brand_id"]] for h in
                extract.find_mentions(text, self.BRANDS, subject)}

    def test_title_case_prose_is_not_a_brand(self):
        text = ("## Automated Outreach\n\nTrack Qualified Meetings and improve "
                "Reply Rates across your Sales Outreach programme.")
        self.assertEqual(self._names(text), set())

    def test_bolded_ambiguous_name_still_counts(self):
        self.assertIn("Outreach", self._names("- **Outreach** is the incumbent."))

    def test_list_head_ambiguous_name_still_counts(self):
        self.assertIn("Qualified", self._names("1. Qualified - inbound agent."))

    def test_domain_suffix_counts(self):
        self.assertIn("Outreach", self._names("We evaluated Outreach.io last year."))

    def test_vendor_verb_context_counts(self):
        self.assertIn("Apollo", self._names("We are moving off Apollo this quarter."))

    def test_unambiguous_names_unaffected_by_the_rule(self):
        self.assertIn("Amplemarket", self._names("Amplemarket appears in prose."))


class TestCitations(unittest.TestCase):
    def test_extracts_and_dedupes(self):
        text = ("See https://www.g2.com/categories/ai-sdr and "
                "https://reddit.com/r/sales/x and https://www.g2.com/categories/ai-sdr again.")
        cites = extract.find_citations(text)
        self.assertEqual(len(cites), 2)
        self.assertEqual({c["domain"] for c in cites}, {"g2.com", "reddit.com"})

    def test_trailing_punctuation_stripped(self):
        cites = extract.find_citations("Source: https://example.com/page.")
        self.assertEqual(cites[0]["url"], "https://example.com/page")

    def test_grounding_metadata_takes_precedence(self):
        provided = [{"url": "https://vendor.com/pricing", "title": "Pricing", "position": 1}]
        cites = extract.find_citations("no urls in prose", provided)
        self.assertEqual(cites[0]["domain"], "vendor.com")


class TestPromptGeneration(unittest.TestCase):
    CFG = {
        "category": "widget platforms", "category_short": "widget tools",
        "segments": ["a 20-person startup", "an enterprise"],
        "requirements": ["SOC 2 Type II certification", "pricing under $500 per month"],
        "integrations": ["Salesforce"],
        "roles": {"practitioner": "ops lead", "economic_buyer": "VP of Sales",
                  "technical": "security reviewer", "procurement": "procurement manager"},
        "focus_brands": [{"name": "Alpha", "domain": "a.com", "aliases": []}],
        "competitor_brands": [{"name": "Beta", "domain": "b.com", "aliases": []},
                              {"name": "Gamma", "domain": "g.com", "aliases": []}],
        "fact_probes": ["How much does {brand} cost?"],
    }

    def test_persona_is_written_into_the_text(self):
        """If persona lives only in metadata, all four personas render identical
        strings, dedup keeps one, and the persona axis silently disappears."""
        ps = promptgen.generate(self.CFG, max_prompts=500)
        for p in ps:
            if p["intent"] == "fact_probe":
                continue
            role = self.CFG["roles"][p["persona"]]
            self.assertIn(role, p["text"], f"persona missing from: {p['text']}")

    def test_all_personas_survive_trimming(self):
        ps = promptgen.generate(self.CFG, max_prompts=40)
        personas = {p["persona"] for p in ps}
        self.assertEqual(personas, set(self.CFG["roles"]),
                         "stratified trim dropped an entire persona")

    def test_trim_preserves_intent_diversity(self):
        full = {p["intent"] for p in promptgen.generate(self.CFG, max_prompts=1000)}
        trimmed = {p["intent"] for p in promptgen.generate(self.CFG, max_prompts=40)}
        self.assertGreaterEqual(len(trimmed), len(full) - 1)

    def test_respects_limit_and_is_deterministic(self):
        a = promptgen.generate(self.CFG, max_prompts=30)
        b = promptgen.generate(self.CFG, max_prompts=30)
        self.assertEqual(len(a), 30)
        self.assertEqual([p["text"] for p in a], [p["text"] for p in b])

    def test_no_unfilled_placeholders(self):
        for p in promptgen.generate(self.CFG, max_prompts=500):
            self.assertNotIn("{", p["text"])

    def test_no_self_comparison(self):
        for p in promptgen.generate(self.CFG, max_prompts=500):
            if " vs " in p["text"]:
                left = p["text"].split(" vs ")[0].split(". ")[-1].strip()
                self.assertNotIn(f"{left} vs {left}", p["text"])


class TestAllocator(unittest.TestCase):
    """Regression tests for the sampling bug that skewed the whole study.

    A flat round-robin over (intent, persona, subject) looks balanced but is
    not: intents with one bucket per vendor collect a slot every pass, while
    category-wide intents have a single bucket. In the real config that gave
    60 'alternatives' prompts and 2 'shortlist' prompts — starving the exact
    question type where vendors get recommended.
    """

    def _items(self):
        items = []
        # brand-specific intent: 14 vendors x 4 variants
        for brand in [f"V{i}" for i in range(14)]:
            for k in range(4):
                items.append({"intent": "alternatives", "persona": "economic_buyer",
                              "subject": brand, "id": f"a-{brand}-{k}"})
        # category-wide intent: no subject, so only two buckets
        for persona in ("economic_buyer", "procurement"):
            for k in range(20):
                items.append({"intent": "shortlist", "persona": persona,
                              "subject": None, "id": f"s-{persona}-{k}"})
        return items

    def test_category_wide_intent_is_not_starved(self):
        picked = promptgen.allocate(self._items(), 40, group="intent",
                                    subkeys=("persona", "subject"))
        counts = {}
        for p in picked:
            counts[p["intent"]] = counts.get(p["intent"], 0) + 1
        self.assertEqual(len(picked), 40)
        self.assertGreaterEqual(counts.get("shortlist", 0), 15,
                                f"category-wide intent starved: {counts}")
        self.assertGreaterEqual(counts.get("alternatives", 0), 15, counts)

    def test_no_single_subject_dominates(self):
        picked = promptgen.allocate(self._items(), 40, group="intent",
                                    subkeys=("persona", "subject"))
        subjects = [p["subject"] for p in picked if p["subject"]]
        worst = max(subjects.count(s) for s in set(subjects))
        self.assertLessEqual(worst, 3, "one vendor absorbed its intent's budget")

    def test_quota_capped_at_availability_and_redistributed(self):
        items = ([{"intent": "rare", "persona": "p", "subject": None, "id": "r1"}]
                 + [{"intent": "common", "persona": "p", "subject": None, "id": f"c{i}"}
                    for i in range(50)])
        picked = promptgen.allocate(items, 20, group="intent", subkeys=("persona", "subject"))
        self.assertEqual(len(picked), 20, "scarce group's unused quota was not redistributed")
        self.assertEqual(sum(1 for p in picked if p["intent"] == "rare"), 1)

    def test_returns_everything_when_under_limit(self):
        items = self._items()
        self.assertEqual(len(promptgen.allocate(items, 10_000, "intent",
                                                ("persona", "subject"))), len(items))

    def test_weights_shift_the_split(self):
        items = self._items()
        heavy = promptgen.allocate(items, 30, "intent", ("persona", "subject"),
                                   weights={"shortlist": 5.0, "alternatives": 1.0})
        n_short = sum(1 for p in heavy if p["intent"] == "shortlist")
        self.assertGreater(n_short, 15, "weighting had no effect")

    def test_deterministic(self):
        items = self._items()
        a = promptgen.allocate(items, 30, "intent", ("persona", "subject"))
        b = promptgen.allocate(items, 30, "intent", ("persona", "subject"))
        self.assertEqual([x["id"] for x in a], [x["id"] for x in b])


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.init(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _prompt(self, text="q?"):
        return db.insert_prompt(self.conn, text, "discovery", "practitioner", "problem", None)

    def test_migration_is_idempotent(self):
        db.init(self.conn)
        db.init(self.conn)
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(brands)")}
        self.assertIn("case_sensitive", cols)

    def test_failed_run_can_be_retried(self):
        """A rate-limited run must not be permanently stuck: the resume query
        re-queues it, so the write path has to accept the second attempt."""
        pid = self._prompt()
        first = db.record_run(self.conn, prompt_id=pid, engine="e", model="m",
                              grounded=0, rep=0, error="HTTP 429")
        self.assertIsNotNone(first)
        second = db.record_run(self.conn, prompt_id=pid, engine="e", model="m",
                               grounded=0, rep=0, response="recovered")
        self.assertIsNotNone(second, "retry of a failed run was silently dropped")
        row = self.conn.execute("SELECT response, error FROM runs WHERE id=?", (first,)).fetchone()
        self.assertEqual(row["response"], "recovered")
        self.assertIsNone(row["error"])

    def test_successful_run_is_never_overwritten(self):
        pid = self._prompt()
        rid = db.record_run(self.conn, prompt_id=pid, engine="e", model="m",
                            grounded=0, rep=0, response="original")
        again = db.record_run(self.conn, prompt_id=pid, engine="e", model="m",
                              grounded=0, rep=0, response="tampered")
        self.assertIsNone(again)
        row = self.conn.execute("SELECT response FROM runs WHERE id=?", (rid,)).fetchone()
        self.assertEqual(row["response"], "original")

    def test_pending_runs_shuffled_but_complete_and_deterministic(self):
        ids = [self._prompt(f"q{i}?") for i in range(30)]
        a = db.pending_runs(self.conn, "e", "m", False, 2)
        b = db.pending_runs(self.conn, "e", "m", False, 2)
        self.assertEqual(len(a), 60)
        self.assertEqual([(p["id"], r) for p, r in a], [(p["id"], r) for p, r in b])
        self.assertEqual(len({(p["id"], r) for p, r in a}), 60)
        # Ordered by prompt id would mean an interrupted sweep covers one intent
        # only; assert we are genuinely not in id order.
        self.assertNotEqual([p["id"] for p, _ in a][:30], ids)

    def test_pending_excludes_completed(self):
        pid = self._prompt()
        db.record_run(self.conn, prompt_id=pid, engine="e", model="m",
                      grounded=0, rep=0, response="done")
        pend = db.pending_runs(self.conn, "e", "m", False, 1)
        self.assertEqual(pend, [])

    def test_brand_case_sensitive_roundtrip(self):
        db.upsert_brand(self.conn, "Clay", "clay.com", True, ["Claygent"], True)
        row = self.conn.execute("SELECT * FROM brands WHERE name='Clay'").fetchone()
        self.assertEqual(row["case_sensitive"], 1)
        self.assertEqual(json.loads(row["aliases"]), ["Claygent"])


class TestScoringExcludesSelfReference(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "t.db")
        db.init(self.conn)
        self.b1 = db.upsert_brand(self.conn, "Alpha", "a.com", True, [])
        self.b2 = db.upsert_brand(self.conn, "Beta", "b.com", True, [])
        pid = db.insert_prompt(self.conn, "alternatives to Alpha?", "alternatives",
                               "practitioner", "shortlist", "Alpha")
        for rep in range(4):
            rid = db.record_run(self.conn, prompt_id=pid, engine="e", model="m",
                                grounded=0, rep=rep, response="text")
            # Alpha appears only because the prompt named it.
            self.conn.execute("INSERT INTO mentions (run_id, brand_id, mentioned,"
                              " recommended, prompted) VALUES (?,?,1,1,1)", (rid, self.b1))
            self.conn.execute("INSERT INTO mentions (run_id, brand_id, mentioned,"
                              " recommended, prompted) VALUES (?,?,1,1,0)", (rid, self.b2))
        self.conn.commit()

    def tearDown(self):
        self.conn.close(); self.tmp.cleanup()

    def test_prompted_mentions_excluded_by_default(self):
        rows = {r["brand"]: r for r in score.Scores(self.conn).visibility()}
        self.assertEqual(rows["Alpha"]["rec_rate"], 0.0,
                         "self-reference inflated the subject brand's visibility")
        self.assertEqual(rows["Beta"]["rec_rate"], 1.0)

    def test_stability_perfect_when_every_rep_agrees(self):
        inst = score.Scores(self.conn).instability()
        self.assertEqual(inst["set_stability"], 1.0)
        self.assertEqual(inst["coinflip_rate"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
