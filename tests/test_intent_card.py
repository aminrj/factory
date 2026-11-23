"""The intent card at Checkpoint 4, the decision hold at Checkpoint 1 and the item lock that autopilot hops hold.

2026-11-22, brevkoll#6: the autopilot approved a spec for an issue that said "needs a decision before any code", the
checkpoint showed a model's own "no deviations" report, and two false `interrupted` checkpoints were posted while the
run was alive (an `adopt` run held no item lock).
"""

import fcntl
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_loop_gates as gates
from test_loop_gates import PLAN

import dispatch
import orchestrate

REPORT = """# Drift report: #1

Drift: REVIEW (3)

_Written by `scripts/drift.py`._

## Card

- Asked for: Add a second notice on the result page.
- Acceptance criteria: 3; without their own test: 3.
- Built: 10 files changed, 477 insertions(+), 3 deletions(-).

## Findings

1. `docs/intent.md` does not exist.
2. AC1 is matched only by a test from another change: `tests/holdout/test_acceptance.py::test_x`.
3. `acceptance.md` is missing or still the template.

## Intent

- `docs/intent.md`: missing
"""


class IntentCardTests(unittest.TestCase):
    setUp = gates.GateTests.setUp
    commit_file = gates.GateTests.commit_file
    fake_ci = gates.GateTests.fake_ci
    checkpoint = gates.GateTests.checkpoint
    go = gates.GateTests.go
    bot = gates.GateTests.bot
    steps = gates.GateTests.steps
    fake_dispatch = gates.GateTests.fake_dispatch
    AGENT = gates.GateTests.AGENT

    # -- the card
    def test_the_summary_shows_the_verdict_the_card_and_every_finding(self) -> None:
        text = orchestrate.drift_summary(REPORT, "http://x/drift.md")
        self.assertIn("REVIEW (3)", text)
        self.assertIn("Asked for: Add a second notice", text)
        self.assertIn("Built: 10 files changed", text)
        self.assertIn("1. `docs/intent.md` does not exist.", text)
        self.assertIn("3. `acceptance.md` is missing", text)
        self.assertIn("Full report: http://x/drift.md", text)
        self.assertNotIn("## Intent", text)  # the evidence stays in the file

    def test_a_clean_report_says_so_and_a_long_one_is_cut(self) -> None:
        clean = REPORT.replace("REVIEW (3)", "CLEAN").split("## Findings")[0] + "## Findings\n\nNone.\n\n## Intent\n"
        self.assertIn("✅", orchestrate.drift_summary(clean, "l"))
        many = REPORT.replace("3. `acceptance.md`", "\n".join(f"{i}. f{i}" for i in range(3, 14)) + "\n14. `acceptance.md`")
        out = orchestrate.drift_summary(many, "l")
        self.assertIn("8. f8", out)
        self.assertNotIn("9. f9", out)
        self.assertIn("more in the report", out)

    def test_checkpoint_4_leads_with_the_drift_report_or_says_there_is_none(self) -> None:
        self.commit_file("drift.md", REPORT)
        ctx = orchestrate.Ctx(self.g, "owner/app", 1, None, 0, {})  # type: ignore[arg-type]
        self.assertIn("What was asked, what was built (code, not the model): REVIEW (3)", orchestrate.drift_block(ctx))
        self.assertIn("No drift report", orchestrate.drift_block(orchestrate.Ctx(self.g, "owner/app", 2, None, 0, {})))  # type: ignore[arg-type]

    # -- the hold at checkpoint 1
    def test_a_spec_that_needs_a_decision_stops_once_and_the_owners_go_continues(self) -> None:
        spec = ("# Spec\n\nStatus: draft\n\n- Serves: G1\n- Decision needed from the owner before any code: yes\n"
                "\n## Problem\n\nOption A or option B?\n")
        self.commit_file("spec.md", spec)
        self.checkpoint("after_gate1", "1")
        self.agent_files.update({"threat-model": "| R1 | PROPOSED |\n", "research": "# R\n", "plan": PLAN})
        self.go("/go")
        self.assertEqual(self.steps(), [])  # nothing was built or drafted behind the owner's back
        self.assertIn("a decision is needed before any code", self.bot())
        self.assertIn("/go <your choice", self.bot())
        self.go("/go option B, please")
        self.assertEqual(self.steps()[:3], ["threat-model", "research", "plan"])  # the second /go is the decision

    def test_a_spec_that_needs_no_decision_is_not_held(self) -> None:
        self.commit_file("spec.md", "# Spec\n\nStatus: draft\n- Decision needed from the owner before any code: no\n")
        self.checkpoint("after_gate1", "1")
        self.agent_files.update({"threat-model": "| R1 | PROPOSED |\n", "research": "# R\n", "plan": PLAN})
        self.go("/go")
        self.assertEqual(self.steps()[0], "threat-model")

    # -- the lock
    def test_a_lane_child_that_holds_no_item_lock_is_not_recovered_as_dead(self) -> None:
        self.checkpoint("after_gate3", "3")
        self.g.comment("owner/app", 1, "▶ Autopilot: the plan is written; building it. Running `after_gate3` on model `local`.")
        with mock.patch.object(orchestrate.lanes, "item_running", return_value=True):
            self.assertFalse(orchestrate.recover_interrupted(self.g, "owner/app", 1))  # type: ignore[arg-type]
        self.assertIn("Autopilot", self.bot())  # no false "interrupted" checkpoint was posted
        self.assertTrue(orchestrate.recover_interrupted(self.g, "owner/app", 1))  # type: ignore[arg-type]  # truly dead: still found

    def test_the_item_lock_is_taken_once_and_released_with_the_process(self) -> None:
        first = orchestrate.take_item_lock("app", 7)
        self.assertIsNotNone(first)
        self.assertIsNone(orchestrate.take_item_lock("app", 7))
        self.assertTrue(orchestrate.item_is_running("app", 7))
        assert first is not None
        fcntl.flock(first, fcntl.LOCK_UN)
        first.close()
        self.assertFalse(orchestrate.item_is_running("app", 7))


class WriteDriftTests(unittest.TestCase):
    """dispatch.write_drift: the app's scripts/drift.py runs after validate and its report is committed."""

    def repo(self, tmp: Path, script: str | None) -> Path:
        wt = tmp / "wt"
        (wt / "thoughts" / "5").mkdir(parents=True)
        (wt / "README.md").write_text("x")
        if script is not None:
            (wt / "scripts").mkdir()
            (wt / "scripts" / "drift.py").write_text(script)
        for cmd in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "i"]):
            subprocess.run(["git", *cmd], cwd=wt, check=True, capture_output=True)
        return wt

    def test_the_report_is_written_and_committed(self) -> None:
        script = ("import pathlib, sys\n"
                  "pathlib.Path('thoughts/5/drift.md').write_text('# Drift report: #5\\n\\nDrift: CLEAN\\n')\n")
        with tempfile.TemporaryDirectory() as d:
            wt = self.repo(Path(d), script)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@t"}
            with mock.patch.object(dispatch, "agent_env", return_value=env):
                self.assertIsNone(dispatch.write_drift(wt, 5, "agent/issue-5"))
            log = subprocess.run(["git", "log", "--format=%s", "-1"], cwd=wt, capture_output=True, text=True, check=True)
            self.assertIn("#5 drift", log.stdout)
            self.assertTrue((wt / "thoughts" / "5" / "drift.md").exists())

    def test_an_app_without_the_script_is_left_alone_and_a_crash_is_only_a_note(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(dispatch.write_drift(self.repo(Path(d), None), 5, "b"))
        with tempfile.TemporaryDirectory() as d:
            note = dispatch.write_drift(self.repo(Path(d), "raise SystemExit(2)\n"), 5, "b")
            self.assertIn("drift report failed", note or "")


if __name__ == "__main__":
    unittest.main()
