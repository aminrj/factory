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



class NightPolicyEntryTests(unittest.TestCase):
    """A plan approved by the night policy counts only with the label on the issue, and only from factory-orchestrator."""

    setUp = EntryGateTests.setUp
    commit = EntryGateTests.commit
    ready = EntryGateTests.ready
    entry = EntryGateTests.entry
    refused = EntryGateTests.refused

    def policy_commit(self, name: str = dispatch.ORCH_NAME, email: str = dispatch.ORCH_EMAIL,
                      trailer: str = dispatch.POLICY_TRAILER) -> None:
        (self.wt / "thoughts" / "1" / "plan.md").write_text("Status: approved\n")
        subprocess.run(["git", "add", "-A"], cwd=self.wt, check=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
               "GIT_COMMITTER_EMAIL": email}
        subprocess.run(["git", "commit", "-q", "-m", f"#1 plan: approved by the night policy\n\n{trailer}"],
                       cwd=self.wt, check=True, env=env)

    def test_with_the_label_the_policy_approval_counts(self) -> None:
        self.ready()
        self.policy_commit()
        dispatch.check_entry("implement", self.wt, 1, OWNER, "owner", policy=True)

    def test_without_the_label_it_does_not(self) -> None:
        self.ready()
        self.policy_commit()
        self.assertIn("not by the human", self.refused("implement"))

    def test_the_agent_cannot_approve_with_the_trailer(self) -> None:
        self.ready()
        self.policy_commit(name=dispatch.AGENT, email=dispatch.AGENT_EMAIL)
        with self.assertRaises(SystemExit):
            dispatch.check_entry("implement", self.wt, 1, OWNER, "owner", policy=True)

    def test_the_spec_step_only_starts_without_a_spec(self) -> None:
        dispatch.check_entry("spec", self.wt, 1, OWNER, "owner")
        self.commit("spec.md", "# Spec\n", "bot@x")
        with self.assertRaises(SystemExit):
            dispatch.check_entry("spec", self.wt, 1, OWNER, "owner")


class SpecDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.wt)], check=True)
        subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=self.wt, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x",
                            "GIT_COMMITTER_EMAIL": "x@x"})
        self.base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.wt, capture_output=True, text=True).stdout.strip()

    def write(self, rel: str, text: str) -> None:
        path = self.wt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        subprocess.run(["git", "add", "-A"], cwd=self.wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", rel], cwd=self.wt, check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x",
                            "GIT_COMMITTER_EMAIL": "x@x"})

    def test_a_good_draft(self) -> None:
        self.write("thoughts/3/spec.md", "# Spec\n\nStatus: draft\n")
        self.write("tests/holdout/test_issue_3.py", "def test_ac1():\n    assert False\n")
        self.assertIsNone(dispatch.spec_draft_problem(self.wt, 3, self.base))

    def test_an_approved_or_placeholder_spec_or_code_changes_are_refused(self) -> None:
        self.write("thoughts/3/spec.md", "# Spec\n\nStatus: approved\n")
        self.assertIn("draft", dispatch.spec_draft_problem(self.wt, 3, self.base) or "")
        self.write("thoughts/3/spec.md", "# Spec\n\nStatus: draft\n<fill in>\n")
        self.assertIn("placeholder", dispatch.spec_draft_problem(self.wt, 3, self.base) or "")
        self.write("thoughts/3/spec.md", "# Spec\n\nStatus: draft\n")
        self.write("src/app/x.py", "x = 1\n")
        self.assertIn("src/app/x.py", dispatch.spec_draft_problem(self.wt, 3, self.base) or "")

if __name__ == "__main__":
    unittest.main()


class SnapshotTests(unittest.TestCase):
    def test_the_foremans_notices_never_reach_the_snapshot(self) -> None:
        # a staging notice with a machine name was copied into thoughts/15/issue.md and blocked the mirror push
        import tempfile

        class G:
            user = "owner"

            def get(self, path: str) -> object:
                if path.endswith("/comments"):
                    return [{"user": {"login": "owner"}, "created_at": "t", "body": b} for b in
                            ("ℹ️ The staged build runs on http://box.internal:8800/", "▶ Picked up", "please fix the date")]
                return {"title": "x", "body": "the issue"}

        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            (wt / "thoughts" / "1").mkdir(parents=True)
            dispatch.write_issue_snapshot(G(), "o/r", 1, wt)  # type: ignore[arg-type]
            text = (wt / "thoughts" / "1" / "issue.md").read_text()
        self.assertIn("please fix the date", text)
        self.assertNotIn("box.internal", text)
        self.assertNotIn("Picked up", text)


class DraftStatusTests(unittest.TestCase):
    def test_a_missing_status_line_is_added_as_draft_and_nothing_else_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp) / "spec.md"
            spec.write_text("# Spec: #7 x\n\n**Real user:** the owner\n")
            dispatch.add_draft_status(spec)
            self.assertEqual(spec.read_text(), "# Spec: #7 x\n\nStatus: draft\n\n**Real user:** the owner\n")
            spec.write_text("# Spec\n\nStatus: approved\n")
            dispatch.add_draft_status(spec)
            self.assertEqual(spec.read_text(), "# Spec\n\nStatus: approved\n")  # an existing line is never touched


class PlaceHoldoutTests(unittest.TestCase):
    def test_stray_test_files_of_the_issue_go_to_the_holdout_folder(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            env = {**os.environ, "GIT_AUTHOR_NAME": "x", "GIT_AUTHOR_EMAIL": "x@x", "GIT_COMMITTER_NAME": "x",
                   "GIT_COMMITTER_EMAIL": "x@x"}
            subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
            (wt / "tests" / "holdout").mkdir(parents=True)
            (wt / "tests" / "holdout" / "test_old.py").write_text("x = 1\n")
            subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=wt, check=True, env=env)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=wt, capture_output=True, text=True).stdout.strip()
            # what the local model did on brevkoll#7, plus a file of another issue that must stay
            (wt / "tests" / "holdout_issue_7.py").write_text("def test_ac1():\n    assert False\n")
            (wt / "tests" / "holdout_test_issue_7.py").write_text("")
            (wt / "tests" / "test_issue_70.py").write_text("def test_x():\n    pass\n")
            subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "agent"], cwd=wt, check=True, env=env)
            with mock.patch.dict(os.environ, env):
                moved = dispatch.place_holdout_tests(wt, 7, base)
            self.assertTrue((wt / "tests" / "holdout" / "test_issue_7.py").read_text().startswith("def test_ac1"))
            self.assertFalse((wt / "tests" / "holdout_issue_7.py").exists())
            self.assertFalse((wt / "tests" / "holdout_test_issue_7.py").exists())
            self.assertTrue((wt / "tests" / "test_issue_70.py").exists())
            self.assertEqual(len(moved), 2)


class GateEvasionTests(unittest.TestCase):
    """brevkoll#26: the local model hid tests from the "new tests must fail on main" gate instead of saying the plan
    could not meet it. Such a run must reach the owner, not the branch."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.wt)], check=True)
        self.write("tests/unit/test_trace.py", "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
        self.write("src/app/main.py", "X = 1\n")
        self.base = self.commit("phase 2")

    def write(self, rel: str, text: str) -> None:
        p = self.wt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def commit(self, msg: str) -> str:
        env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
               "GIT_COMMITTER_EMAIL": "a@x"}
        subprocess.run(["git", "add", "-A"], cwd=self.wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=self.wt, check=True, env=env)
        return dispatch.run("git", "rev-parse", "HEAD", cwd=self.wt)

    def test_the_brevkoll_26_rework_is_caught_three_ways(self) -> None:
        self.write("tests/unit/test_trace.py", "from app.main import _helper  # noqa: F401\n")
        self.write("tests/tooling/test_trace_t6.py", "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n")
        self.write("tests/tooling/conftest.py", "def pytest_ignore_collect(collection_path, config):\n    return True\n")
        self.commit("move traceability tests to tooling, causing an ImportError on main that makes the gate pass")
        found = dispatch.gate_evasion(self.wt, self.base, "the gate doesn't check this directory")
        self.assertEqual(len(found), 3, found)
        self.assertIn("pytest_ignore_collect", found[0])
        self.assertIn("2 -> 0", found[1])
        self.assertIn("says so itself", found[2])

    def test_honest_work_passes(self) -> None:
        self.write("src/app/main.py", "X = 2\n")
        self.write("tests/unit/test_trace.py", "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n\n\ndef test_c():\n"
                   "    pass\n")
        self.commit("phase 3: X is 2")
        self.assertEqual(dispatch.gate_evasion(self.wt, self.base, "Fixed the code so the gate passes."), [])

    def test_merging_tests_inside_the_gated_folders_is_not_a_move(self) -> None:
        self.write("tests/unit/test_trace.py", "def test_ab():\n    pass\n")
        self.commit("merge two tests")
        self.assertEqual(dispatch.gate_evasion(self.wt, self.base, ""), [])

    def test_a_refused_run_is_kept_off_the_branch_and_exits_with_its_own_code(self) -> None:
        import io
        from contextlib import redirect_stderr

        self.write("tests/conftest.py", "collect_ignore = ['unit']\n")
        self.commit("hide")
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as caught:
            dispatch.refuse_evasion(self.wt, "agent/issue-1", self.base, ["x"])
        self.assertEqual(caught.exception.code, dispatch.EVASION_EXIT)
        self.assertEqual(dispatch.run("git", "rev-parse", "HEAD", cwd=self.wt), self.base)
        self.assertIn("refused/agent-issue-1-", dispatch.run("git", "branch", cwd=self.wt))
        self.assertIn("did not push", err.getvalue())

    def test_the_foreman_turns_that_code_into_a_question_not_a_retry(self) -> None:
        import orchestrate

        self.assertEqual(orchestrate.EVASION_EXIT, dispatch.EVASION_EXIT)
        done = mock.Mock(returncode=dispatch.EVASION_EXIT, stdout="", stderr="hid tests")
        with mock.patch.object(orchestrate.subprocess, "run", return_value=done), mock.patch.object(orchestrate, "log"):
            with self.assertRaises(orchestrate.GateEvasion) as caught:
                orchestrate.dispatch(mock.Mock(app="app", n=1), "implement")
        self.assertIsInstance(caught.exception, orchestrate.NeedsHuman)
