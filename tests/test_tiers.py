"""Risk tiers: what the autopilot may run alone (scripts/tiers.py) and where it stops otherwise."""

import unittest
from unittest import mock

import test_loop_gates as gates
from test_loop_gates import PLAN

import commands
import orchestrate
import tiers

SPEC_NONE = "# Spec\n\nStatus: draft\n\n- Serves: G1\n- Touches: none\n"
SMALL_PLAN = "# Plan: #1\nStatus: draft\n\n## Phase 1: a\n- [ ] Files: `src/a.py`\n\n## Dependencies added (Python and system)\nnone\n"


class PolicyTests(unittest.TestCase):
    def test_only_the_owners_label_makes_a_bug_and_the_issue_text_makes_a_decision(self) -> None:
        self.assertEqual(tiers.issue_kind("Footer is wrong", "x", {"bug"}), "bug")
        self.assertEqual(tiers.issue_kind("Error page", "x", set()), "feature")  # a guess from the title would let features in
        self.assertEqual(tiers.issue_kind("OCR misreads", "Needs a decision before any code: what then?", {"bug"}), "decision")
        self.assertEqual(tiers.issue_kind("t", "b", {"question"}), "decision")

    def test_a_decision_stops_at_the_spec_unless_the_owner_forces_it(self) -> None:
        self.assertEqual(tiers.after_spec("decision").tier, "spec")
        self.assertEqual(tiers.after_spec("decision", "auto").tier, "auto")
        self.assertEqual(tiers.after_spec("bug").tier, "auto")

    def test_a_small_bug_fix_runs_on(self) -> None:
        v = tiers.after_plan("bug", SPEC_NONE, SMALL_PLAN, [])
        self.assertEqual((v.tier, v.reasons), ("auto", []))

    def test_every_reason_to_stop_is_named(self) -> None:
        spec = SPEC_NONE.replace("none", "I2, N1")
        plan = SMALL_PLAN.replace("none", "httpx>=0.27") + "\n## Phase 2: b\n\n## Phase 3: c\n\nSee docs/decisions/0006-x.md\n"
        v = tiers.after_plan("feature", spec, plan, ["deploy/"])
        self.assertEqual(v.tier, "plan")
        text = " | ".join(v.reasons)
        for part in ("not a labelled bug fix", "risky files", "I2, N1", "adds a dependency", "decision record", "3 phases"):
            self.assertIn(part, text)

    def test_forcing_auto_runs_on_but_keeps_the_reasons(self) -> None:
        v = tiers.after_plan("feature", SPEC_NONE, SMALL_PLAN, [], "auto")
        self.assertEqual(v.tier, "auto")
        self.assertIn("you forced the autopilot", v.reasons[0])

    def test_the_template_placeholders_and_none_are_not_a_dependency(self) -> None:
        self.assertEqual(tiers.new_dependencies("## Dependencies added\n<!-- x -->\n"), "")
        self.assertEqual(tiers.new_dependencies("## Dependencies added\nNone.\n\n## Next\n"), "")
        self.assertEqual(tiers.touches("- Touches: N<n>, I<n>, or `none`"), [])

    def test_the_review_line_is_quick_only_for_a_clean_unflagged_bug_fix(self) -> None:
        self.assertIn("Quick review", tiers.review_line(tiers.Verdict("auto"), "CLEAN"))
        self.assertIn("Read closely", tiers.review_line(tiers.Verdict("auto"), "REVIEW (2)"))
        self.assertIn("REVIEW (2)", tiers.review_line(tiers.Verdict("auto"), "REVIEW (2)"))
        forced = tiers.review_line(tiers.Verdict("auto", ["you forced the autopilot; x"]), "CLEAN")
        self.assertIn("Read closely", forced)
        self.assertIn("forced", forced)

    def test_the_tier_option_is_accepted_and_others_refused(self) -> None:
        self.assertEqual(commands.parse_go("tier=auto minutes=3").tier, "auto")
        self.assertTrue(commands.parse_go("tier=yolo").errors)


class LoopTests(unittest.TestCase):
    setUp = gates.GateTests.setUp
    commit_file = gates.GateTests.commit_file
    fake_ci = gates.GateTests.fake_ci
    checkpoint = gates.GateTests.checkpoint
    go = gates.GateTests.go
    bot = gates.GateTests.bot
    steps = gates.GateTests.steps
    fake_dispatch = gates.GateTests.fake_dispatch
    AGENT = gates.GateTests.AGENT

    def prepare(self, spec: str = SPEC_NONE) -> None:
        self.commit_file("spec.md", spec)
        self.checkpoint("after_gate1", "1")
        self.agent_files.update({"threat-model": "| R1 | PROPOSED |\n", "research": "# R\n", "plan": PLAN})

    def test_an_unlabelled_issue_stops_at_the_plan_and_says_why(self) -> None:
        self.prepare()
        with mock.patch.object(orchestrate, "issue_kind_of", return_value="feature"):
            self.go("/go")
        self.assertEqual(self.steps(), ["threat-model", "research", "plan"])  # nothing was built unread
        self.assertIn("CHECKPOINT 3", self.bot())
        self.assertIn("not a labelled bug fix", self.bot())
        self.assertIn("/go tier=auto", self.bot())

    def test_a_labelled_bug_with_a_small_plan_runs_to_cp4_with_a_quick_review_line(self) -> None:
        self.prepare()
        self.commit_file("drift.md", "# Drift\n\nDrift: CLEAN\n\n## Card\n\n- Built: x\n\n## Findings\n\nNone.\n")
        small = SMALL_PLAN
        self.agent_files["plan"] = small
        with mock.patch.object(orchestrate, "issue_kind_of", return_value="bug"):
            self.go("/go")
        self.assertEqual(self.steps()[-1], "validate")
        self.assertIn("CHECKPOINT 4", self.bot())
        self.assertIn("Quick review", self.bot())

    def test_a_dirty_drift_report_turns_a_bug_fix_into_a_close_read(self) -> None:
        self.prepare()
        self.commit_file("drift.md", "# Drift\n\nDrift: REVIEW (1)\n\n## Card\n\n- Built: x\n\n## Findings\n\n1. `src/b.py` not in the plan.\n")
        self.agent_files["plan"] = SMALL_PLAN
        with mock.patch.object(orchestrate, "issue_kind_of", return_value="bug"):
            self.go("/go")
        self.assertIn("Read closely", self.bot())
        self.assertIn("REVIEW (1)", self.bot())

    def test_forcing_auto_builds_a_feature_and_cp4_says_it_was_forced(self) -> None:
        self.prepare()
        with mock.patch.object(orchestrate, "issue_kind_of", return_value="feature"):
            self.go("/go tier=auto")
        self.assertEqual(self.steps()[-1], "validate")
        self.assertIn("You forced the autopilot", self.bot())


if __name__ == "__main__":
    unittest.main()
