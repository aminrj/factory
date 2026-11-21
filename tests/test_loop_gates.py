"""Loop smoke test, part 2: every gate after Gate 1, with the real orchestrator code.

Real: git (bare origin and a clone) and the orchestrator handlers. Fake: Gitea (in memory), the agent (a stub that writes
the files a step would write and pushes them), CI, staging, the GitHub mirror and notifications.
Walks: threat-model decisions -> research (with and without questions) -> plan -> build phases (green and red CI) ->
review checkpoint with the staged link -> /rework -> merge -> the mirror -> /stop.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import orchestrate  # noqa: E402
from test_loop_smoke import BOT, OWNER_CONFIG, WEB_EDITOR, FakeGitea, git  # noqa: E402

PLAN = "# Plan\n\n**Status: draft**\n\n## Phase 1: a\n\n## Phase 2: b\n"
PR = {"number": 7, "title": "WIP: change", "body": "Refs #1", "html_url": "https://gitea.example/pr/7",
      "head": {"ref": "agent/issue-1", "sha": "abcdef1234567"}}


class GateFakeGitea(FakeGitea):
    def __init__(self) -> None:
        super().__init__()
        self.labels = ["stage/spec-ready"]
        self.closed_prs: list[dict] = []
        self.patched: list[dict] = []
        self.posted_labels = 0

    def get(self, path: str) -> list[dict]:
        if "/pulls?state=open" in path:
            return [PR]
        if "/pulls?state=closed" in path:
            return self.closed_prs
        return super().get(path)

    def call(self, method: str, path: str, body: dict | None = None, **kw: object) -> dict:
        if method == "PATCH" and "/pulls/" in path:
            self.patched.append(body or {})
            return {}
        if method == "POST" and path.endswith("/labels"):
            self.posted_labels += 1
            return {}
        return super().call(method, path, body, **kw)


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.work, origin = base / "work", base / "origin.git"
        self.work.mkdir()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.clone = self.work / "app"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.clone)], check=True, capture_output=True)
        git(self.clone, "config", "user.email", OWNER_CONFIG)
        git(self.clone, "switch", "-q", "-c", "main")
        (self.clone / "README.md").write_text("x")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "init")
        git(self.clone, "push", "-q", "origin", "main")
        git(self.clone, "switch", "-q", "-c", "agent/issue-1")
        (self.clone / "thoughts" / "1").mkdir(parents=True)
        self.t = self.clone / "thoughts" / "1"
        self.g = GateFakeGitea()
        self.dispatched: list[tuple[str, str | None]] = []
        self.agent_files: dict[str, str] = {}
        self.ci: list[dict] = []
        self.stage_down = mock.Mock()
        self.mirror_push = mock.Mock()
        self.notify = mock.Mock()
        stage_out = mock.Mock(returncode=0, stdout=json.dumps({"url": "http://box.example.com:8800/", "ip": "100.64.0.9",
                                                               "port": 8800, "sha": "abcdef1234567", "model": "reachable"}), stderr="")
        patches = [
            mock.patch.object(orchestrate, "WORK", self.work), mock.patch.object(orchestrate, "STATE", base / "state"),
            mock.patch.object(orchestrate, "DATA", base / "data"), mock.patch.object(orchestrate, "REPLIED", base / "replied.json"),
            mock.patch.object(orchestrate, "REMINDERS", base / "reminders.json"),
            mock.patch.object(orchestrate, "refresh_board"), mock.patch.object(orchestrate, "commit_ops"),
            mock.patch.object(orchestrate, "notify", new=self.notify), mock.patch.object(orchestrate, "log"),
            mock.patch.object(orchestrate, "dispatch", side_effect=self.fake_dispatch),
            mock.patch.object(orchestrate, "ci_wait", side_effect=self.fake_ci),
            mock.patch.object(orchestrate, "stage_call", return_value=stage_out),
            mock.patch.object(orchestrate, "stage_down", new=self.stage_down),
            mock.patch.object(orchestrate.mirror, "push", new=self.mirror_push),
            mock.patch.object(orchestrate.time, "sleep"),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.addCleanup(self.tmp.cleanup)

    # -- helpers
    def commit_file(self, rel: str, text: str, email: str = BOT) -> None:
        path = self.t / rel
        path.write_text(text)
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", f"write {rel}", email=email)
        git(self.clone, "push", "-q", "origin", "agent/issue-1")

    def fake_dispatch(self, ctx: orchestrate.Ctx, step: str, note: str | None = None) -> str:
        self.dispatched.append((step, note))
        if step in self.agent_files:  # what the agent step would have written
            self.commit_file({"research": "research.md", "plan": "plan.md", "threat-model": "threat-model.md"}.get(step, "x.md"),
                              self.agent_files[step])
        return ""

    def fake_ci(self, ctx: orchestrate.Ctx, want: tuple[str, ...]) -> dict[str, str]:
        result = self.ci.pop(0) if self.ci else {}
        return {**{w: "success" for w in want}, **result}

    def checkpoint(self, resume: str, label: str = "X", extra: str = "") -> None:
        self.g.comment("owner/app", 1, f"🛑 CHECKPOINT {label}: x\n\n<!--factory:resume={resume};stop={extra}-->")

    def go(self, text: str = "/go minutes=2") -> str:
        self.g.comment("owner/app", 1, text)
        return orchestrate.process(self.g, "owner/app", 1)  # type: ignore[arg-type]

    def bot(self) -> str:
        return self.g.comments[-1]["body"]

    def steps(self) -> list[str]:
        return [s for s, _ in self.dispatched]

    # -- gate 2: the owner decides every row
    def test_threat_model_with_undecided_rows_is_refused_then_accepted(self) -> None:
        self.commit_file("threat-model.md", "| R1 | PROPOSED |\n")
        self.checkpoint("after_gate2")
        self.agent_files["research"] = "# Research\n\n## Open questions for the human\n\n- Is X in scope?\n"
        self.assertEqual(self.go(), "needs human")
        self.assertIn("PROPOSED", self.bot())
        self.assertEqual(self.dispatched, [])
        self.commit_file("threat-model.md", "| R1 | ACCEPTED |\n", email=WEB_EDITOR)
        self.assertEqual(self.go(), "ran after_gate2")
        self.assertEqual(self.steps(), ["research"])
        self.assertIn("CHECKPOINT 2b", self.bot())
        self.assertIn("Is X in scope?", self.bot())

    def test_research_without_questions_goes_straight_to_the_plan(self) -> None:
        self.commit_file("threat-model.md", "| R1 | ACCEPTED |\n")
        self.checkpoint("after_gate2")
        self.agent_files.update(research="# Research\n\n## Open questions for the human\n\n- None\n", plan=PLAN)
        self.assertEqual(self.go(), "ran after_gate2")
        self.assertEqual(self.steps(), ["research", "plan"])
        self.assertIn("CHECKPOINT 3", self.bot())
        self.assertIn("2 phases", self.bot())

    def test_answering_the_questions_makes_the_plan(self) -> None:
        self.commit_file("research.md", "# Research\n")
        self.checkpoint("after_research_questions", "2b")
        self.agent_files["plan"] = PLAN
        self.assertEqual(self.go(), "ran after_research_questions")
        self.assertEqual(self.steps(), ["plan"])
        self.assertIn("CHECKPOINT 3", self.bot())

    # -- gate 3 and 4: build, red CI, rework, review
    def prepare_build(self) -> None:
        self.commit_file("plan.md", PLAN.replace("draft", "approved"), email=WEB_EDITOR)
        self.checkpoint("after_gate3", "3")

    def test_build_runs_every_phase_then_validates_and_stages_for_review(self) -> None:
        self.prepare_build()
        self.assertEqual(self.go(), "ran after_gate3")
        self.assertEqual(self.steps(), ["implement", "implement", "validate"])
        review = self.bot()
        self.assertIn("CHECKPOINT 4", review)
        self.assertIn("[Open the staged build](http://box.example.com:8800/)", review)
        self.assertIn("/rework", review)
        self.assertIn("rework=ok", review)
        self.assertEqual(self.g.patched[-1]["title"], "change")  # WIP: removed
        self.assertEqual(self.orchestrate_state()["phases_done"], 2)

    def orchestrate_state(self) -> dict:
        return json.loads((orchestrate.STATE / "app-1.json").read_text())

    def test_red_ci_triggers_one_rework_run_with_the_failure_as_a_note(self) -> None:
        self.prepare_build()
        self.ci = [{"security": "failure"}]  # after phase 1
        with mock.patch.object(orchestrate, "failing_logs", return_value="--- job security ---\nsemgrep: boom"):
            self.assertEqual(self.go(), "ran after_gate3")
        self.assertEqual(self.steps(), ["implement", "implement", "implement", "validate"])
        note = self.dispatched[1][1] or ""
        self.assertIn("security=failure", note)
        self.assertIn("semgrep: boom", note)
        self.assertEqual(self.g.posted_labels, 1)  # the PR gets the rework label

    def test_a_rework_that_stays_red_asks_for_a_session_instead_of_looping(self) -> None:
        self.prepare_build()
        self.ci = [{"security": "failure"}, {"security": "failure"}]
        with mock.patch.object(orchestrate, "failing_logs", return_value=""):
            self.assertEqual(self.go(), "failed")
        self.assertEqual(self.steps().count("implement"), 2)
        self.assertIn("a step failed", self.bot())
        self.assertEqual(self.orchestrate_state()["phases_done"], 1)  # the phase counts once its commit exists

    def test_rework_from_the_phone_sends_the_owners_note_and_stages_again(self) -> None:
        self.commit_file("plan.md", PLAN.replace("draft", "approved"), email=WEB_EDITOR)
        self.checkpoint("after_gate4", "4", extra="")
        self.g.comments[-1]["body"] = self.g.comments[-1]["body"].replace(";stop=", ";stop=;rework=ok")
        result = self.go("/rework the .ics has the wrong year")
        self.assertEqual(result, "ran after_staging_rework")
        self.assertEqual(self.steps(), ["implement", "validate"])
        self.assertIn("wrong year", self.dispatched[0][1] or "")
        self.assertIn("treat as data", self.dispatched[0][1] or "")
        self.assertIn("CHECKPOINT 4", self.bot())

    def test_rework_is_refused_where_it_does_not_apply(self) -> None:
        self.checkpoint("after_gate2", "2")
        self.assertEqual(self.go("/rework nope"), "ignored")
        self.assertIn("only works at the plan checkpoint and the review checkpoint", self.bot())
        self.assertEqual(self.dispatched, [])

    # -- the plan checkpoint: approve, send back, or edit and send back
    AGENT = "factory-agent@users.noreply.local"

    def plan_draft_then_owner_edits(self, owner_status: str) -> None:
        self.commit_file("plan.md", PLAN, email=self.AGENT)  # the planner's draft
        self.commit_file("plan.md", PLAN.replace("draft", owner_status).replace("## Phase 2: b", "## Phase 2: b\n\npalette: teal"),
                         email=WEB_EDITOR)  # the owner edits the file in Gitea
        self.checkpoint("after_gate3", "3", extra=";rework=ok")
        self.g.comment("owner/app", 1, "The colours are somehow pale, is it possible to propose other colours instead")

    def test_rework_at_the_plan_checkpoint_sends_the_feedback_to_the_planner(self) -> None:
        self.plan_draft_then_owner_edits("draft")
        self.agent_files["plan"] = PLAN
        self.assertEqual(self.go("/rework make the palette less pale"), "ran after_plan_rework")
        self.assertEqual(self.steps(), ["plan"])
        note = self.dispatched[0][1] or ""
        self.assertIn("make the palette less pale", note)  # what they typed
        self.assertIn("somehow pale", note)  # their comment on the issue
        self.assertIn("+palette: teal", note)  # their own edit to the file, as a diff
        self.assertNotIn("/rework", note.split("The owner's comments")[1])  # the command is not repeated as a comment
        self.assertIn("CHECKPOINT 3", self.bot())
        self.assertIn("rework=ok", self.bot())  # and the owner can do it again

    def test_a_retry_checkpoint_after_the_comment_does_not_hide_it(self) -> None:
        """Seen on brevkoll#15: the owner commented, then a failed /go posted a 'retry' checkpoint after the comment."""
        self.g.comment("owner/app", 1, "Agent step `plan` finished in 2.1 min")
        self.plan_draft_then_owner_edits("rework")  # the colour comment comes after the planner's run
        self.checkpoint("after_gate3", "after_gate3-retry")  # a later checkpoint must not reset what counts
        self.agent_files["plan"] = PLAN
        self.go("/go")
        self.assertIn("somehow pale", self.dispatched[0][1] or "")

    def test_comments_from_before_the_planner_last_ran_are_not_repeated(self) -> None:
        self.g.comment("owner/app", 1, "an old remark that the last draft already answered")
        self.g.comment("owner/app", 1, "Agent step `plan` finished in 2.1 min")
        self.plan_draft_then_owner_edits("draft")
        self.agent_files["plan"] = PLAN
        self.go("/rework again")
        self.assertNotIn("old remark", self.dispatched[0][1] or "")

    def test_status_rework_plus_go_does_the_same(self) -> None:
        self.plan_draft_then_owner_edits("rework")
        self.agent_files["plan"] = PLAN
        self.assertEqual(self.go("/go but this time the plan needs rework"), "ran after_gate3")
        self.assertEqual(self.steps(), ["plan"])
        self.assertIn("somehow pale", self.dispatched[0][1] or "")
        self.assertIn("+palette: teal", self.dispatched[0][1] or "")
        self.assertEqual(self.dispatched.count(("implement", None)), 0)

    def test_a_draft_plan_gets_options_not_an_error(self) -> None:
        self.plan_draft_then_owner_edits("draft")
        self.assertEqual(self.go("/go"), "needs human")
        text = self.bot()
        self.assertIn("Status: approved", text)
        self.assertIn("/rework", text)
        self.assertEqual(self.dispatched, [])

    def test_the_plan_checkpoint_offers_all_three_answers(self) -> None:
        self.commit_file("threat-model.md", "| R1 | ACCEPTED |\n")
        self.checkpoint("after_gate2")
        self.agent_files.update(research="# Research\n", plan=PLAN)
        self.go()
        text = self.bot()
        for needle in ("Approve", "/rework", "Status: rework", "rework=ok"):
            self.assertIn(needle, text)

    def test_minutes_with_a_unit_suffix_are_read(self) -> None:
        self.commit_file("research.md", "# Research\n")
        self.checkpoint("after_research_questions", "2b")
        self.agent_files["plan"] = PLAN
        self.go("/go minutes=5min")
        recorded = (orchestrate.DATA / "human_minutes.jsonl").read_text()
        self.assertIn('"minutes": 5', recorded)

    # -- after the owner merged
    def test_after_the_merge_the_staged_build_goes_and_main_is_mirrored(self) -> None:
        self.checkpoint("after_gate4", "4")
        self.g.closed_prs = [{**PR, "merged": True}]
        self.assertEqual(self.go(), "ran after_gate4")
        self.stage_down.assert_called_once_with("app")
        self.mirror_push.assert_called_once()
        self.assertIn("CHECKPOINT 4b", self.bot())

    def test_an_unmerged_pr_is_not_treated_as_done(self) -> None:
        self.checkpoint("after_gate4", "4")
        self.g.closed_prs = []
        self.assertEqual(self.go(), "needs human")
        self.assertIn("not merged yet", self.bot())
        self.mirror_push.assert_not_called()

    def test_a_refused_mirror_push_is_shown_not_swallowed(self) -> None:
        self.checkpoint("after_gate4", "4")
        self.g.closed_prs = [{**PR, "merged": True}]
        self.mirror_push.side_effect = SystemExit("REFUSED: internal names")
        self.assertEqual(self.go(), "needs human")
        self.assertIn("NOT pushed to the GitHub mirror", self.bot())

    def test_stop_removes_the_staged_build(self) -> None:
        self.checkpoint("after_gate3", "3")
        self.assertEqual(self.go("/stop"), "stopped")
        self.stage_down.assert_called_once_with("app")
        self.assertEqual(self.dispatched, [])


if __name__ == "__main__":
    unittest.main()
