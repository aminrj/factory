"""The entry gate of an agent step (dispatch.check_entry), against a real git repository.

It decides whether a step may start: the spec exists, the threat model has no undecided rows, the plan says
`Status: approved` in a commit that the owner made. The factory identities never count as the owner, a web-editor commit does.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch  # noqa: E402

OWNER = "owner@example.com"
WEB = "1+owner@noreply.gitea.example.com"


class EntryGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.wt)], check=True)
        (self.wt / "thoughts" / "1").mkdir(parents=True)
        self.addCleanup(self.tmp.cleanup)

    def commit(self, rel: str, text: str, email: str, name: str = "x") -> None:
        (self.wt / "thoughts" / "1" / rel).write_text(text)
        subprocess.run(["git", "add", "-A"], cwd=self.wt, check=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
               "GIT_COMMITTER_EMAIL": email}  # explicit: the caller's GIT_* variables must not decide the author
        subprocess.run(["git", "commit", "-q", "-m", rel], cwd=self.wt, check=True, env=env)

    def ready(self) -> None:
        self.commit("spec.md", "# Spec\n", "bot@x")
        self.commit("threat-model.md", "| R1 | ACCEPTED |\n", OWNER)
        self.commit("research.md", "# Research\n", "bot@x")

    def entry(self, step: str) -> None:
        dispatch.check_entry(step, self.wt, 1, OWNER, "owner")

    def refused(self, step: str) -> str:
        with self.assertRaises(SystemExit) as ctx:
            self.entry(step)
        return str(ctx.exception)

    def test_no_spec_no_step(self) -> None:
        self.assertIn("spec.md is missing", self.refused("threat-model"))

    def test_threat_model_step_only_needs_the_spec(self) -> None:
        self.commit("spec.md", "# Spec\n", "bot@x")
        self.entry("threat-model")

    def test_undecided_threat_model_blocks_research(self) -> None:
        self.commit("spec.md", "# Spec\n", "bot@x")
        self.commit("threat-model.md", "| R1 | PROPOSED |\n", "bot@x")
        self.assertIn("PROPOSED", self.refused("research"))

    def test_implement_needs_an_approved_plan(self) -> None:
        self.ready()
        self.commit("plan.md", "Status: draft\n", "bot@x")
        self.assertIn("Status: approved", self.refused("implement"))

    def test_approval_by_the_owner_in_the_web_editor_passes(self) -> None:
        self.ready()
        self.commit("plan.md", "Status: draft\n", "bot@x")
        self.commit("plan.md", "Status: approved\n", WEB)
        self.entry("implement")
        self.entry("validate")

    def test_approval_by_the_owners_git_identity_passes(self) -> None:
        self.ready()
        self.commit("plan.md", "Status: approved\n", OWNER)
        self.entry("implement")

    def test_approval_by_the_agent_is_refused(self) -> None:
        self.ready()
        self.commit("plan.md", "Status: approved\n", "factory-agent@users.noreply.local", name="factory-agent")
        self.assertIn("not by the human", self.refused("implement"))

    def test_approval_by_another_gitea_user_is_refused(self) -> None:
        self.ready()
        self.commit("plan.md", "Status: approved\n", "2+someone@noreply.gitea.example.com")
        self.assertIn("not by the human", self.refused("implement"))

    def test_an_uncommitted_approval_does_not_count(self) -> None:
        self.ready()
        self.commit("plan.md", "Status: draft\n", "bot@x")
        (self.wt / "thoughts" / "1" / "plan.md").write_text("Status: approved\n")
        self.assertIn("not in any commit", self.refused("implement"))


class DeclinedStepTests(unittest.TestCase):
    def test_a_step_that_changed_nothing_exits_with_the_declined_code_and_prints_the_reply(self) -> None:
        import io
        from contextlib import redirect_stderr

        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            dispatch.declined("implement", "Phase 3 needs your approval.")
        self.assertEqual(caught.exception.code, dispatch.DECLINED_EXIT)
        self.assertIn("Phase 3 needs your approval.", err.getvalue())

    def test_the_two_scripts_agree_on_the_exit_code(self) -> None:
        import orchestrate

        self.assertEqual(orchestrate.DECLINED_EXIT, dispatch.DECLINED_EXIT)

    def test_dispatch_turns_that_exit_code_into_a_question(self) -> None:
        import orchestrate

        ctx = mock.Mock(app="app", n=1)
        done = mock.Mock(returncode=dispatch.DECLINED_EXIT, stdout="", stderr="the agent said no")
        with mock.patch.object(orchestrate.subprocess, "run", return_value=done), mock.patch.object(orchestrate, "log"):
            with self.assertRaises(orchestrate.AgentDeclined) as caught:
                orchestrate.dispatch(ctx, "implement")
        self.assertIsInstance(caught.exception, orchestrate.NeedsHuman)
        self.assertIn("the agent said no", str(caught.exception))


class ChangedFileLinksTests(unittest.TestCase):
    URL = "https://gitea.example/o/app.git"

    def test_links_use_the_branch_and_put_the_thoughts_of_the_issue_first(self) -> None:
        out = dispatch.changed_file_links(self.URL, "agent/issue-3", ["src/app/ui.py", "thoughts/3/plan.md", "README.md"], 3)
        rows = out.splitlines()
        self.assertEqual(rows[0], "### Files this step changed")
        self.assertEqual(rows[2], "- [thoughts/3/plan.md](https://gitea.example/o/app/src/branch/agent/issue-3/thoughts/3/plan.md)")
        self.assertIn("(https://gitea.example/o/app/src/branch/agent/issue-3/src/app/ui.py)", out)
        self.assertNotIn(".git/", out)

    def test_the_dispatchers_own_snapshot_is_not_listed_and_long_lists_are_capped(self) -> None:
        files = ["thoughts/3/issue.md"] + [f"src/f{i}.py" for i in range(15)]
        out = dispatch.changed_file_links(self.URL, "b", files, 3, limit=12)
        self.assertNotIn("issue.md", out)
        self.assertIn("and 3 more in the pull request", out)

    def test_no_files_no_section(self) -> None:
        self.assertEqual(dispatch.changed_file_links(self.URL, "b", [], 3), "")


class NoCommitCallSiteTests(unittest.TestCase):
    def test_a_run_without_a_commit_is_a_declined_step_not_a_refusal(self) -> None:
        """The path that stopped brevkoll#15 sits in main(), which needs a model run; check the source instead."""
        src = Path(dispatch.__file__).read_text()
        self.assertIn('declined(step, str(result.get("result")', src)
        self.assertNotIn('die("the agent produced no commit', src)


class PlanPromptTests(unittest.TestCase):
    def test_the_planner_may_not_write_conditional_phases(self) -> None:
        self.assertIn("## Deferred", dispatch.NO_CONDITIONAL_PHASES)
        self.assertIn("only if the human approves", dispatch.NO_CONDITIONAL_PHASES)
        self.assertIn(dispatch.NO_CONDITIONAL_PHASES, dispatch.plan_revision_prompt(1, "a", "b"))


class PlanRevisionTests(unittest.TestCase):
    def test_the_revision_prompt_keeps_the_owners_edits_and_resets_the_status(self) -> None:
        prompt = dispatch.plan_revision_prompt(15, "app", "agent/issue-15")
        for needle in ("REVISION", "Owner feedback on the previous plan draft", "keep the owner's own edits",
                       "Status: draft", "never write `Status: approved`", "other colours"):
            self.assertIn(needle, prompt)

    def test_a_plan_run_must_end_as_a_draft(self) -> None:
        self.assertIsNone(dispatch.plan_status_problem("Status: draft\n# Plan"))
        self.assertIsNone(dispatch.plan_status_problem("**Status: draft**\n"))
        self.assertIn("rework", dispatch.plan_status_problem("Status: rework\n") or "")
        self.assertIn("approved", dispatch.plan_status_problem("Status: approved\n") or "")
        self.assertIn("?", dispatch.plan_status_problem("# Plan with no status\n") or "")


if __name__ == "__main__":
    unittest.main()
