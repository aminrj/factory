"""The entry gate of an agent step (dispatch.check_entry), against a real git repository.

It decides whether a step may start: the spec exists, the threat model has no undecided rows, the plan says
`Status: approved` in a commit that the owner made. The factory identities never count as the owner, a web-editor commit does.
"""

import os
import subprocess
import sys
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()
