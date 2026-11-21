"""new_change.py: the PR is opened at Gate 1 and never twice."""

import sys
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import new_app  # noqa: E402


class FakeG:
    def __init__(self, open_prs: list[dict]) -> None:
        self.open_prs, self.posted = open_prs, []

    def get(self, path: str) -> list[dict]:
        return self.open_prs

    def call(self, method: str, path: str, body: dict) -> dict:
        self.posted.append((method, path, body))
        return {"number": 9, "html_url": "u", **body}


class WipPrTests(unittest.TestCase):
    def test_opens_a_wip_pr_for_the_branch(self) -> None:
        g = FakeG([])
        pr = new_app.ensure_wip_pr(g, "o/a", "agent/issue-4", 4, "T")  # type: ignore[arg-type]
        self.assertEqual(pr["title"], "WIP: T")
        self.assertEqual(g.posted[0][2]["head"], "agent/issue-4")
        self.assertIn("Refs #4", g.posted[0][2]["body"])

    def test_reuses_an_open_pr(self) -> None:
        g = FakeG([{"head": {"ref": "agent/issue-4"}, "number": 3, "html_url": "x"}])
        pr = new_app.ensure_wip_pr(g, "o/a", "agent/issue-4", 4, "T")  # type: ignore[arg-type]
        self.assertEqual(pr["number"], 3)
        self.assertEqual(g.posted, [])


class OpenWorkItemTests(unittest.TestCase):
    def run_open(self, exists: bool = True, **kw: object) -> tuple[mock.Mock, mock.Mock, dict]:
        import os

        import orchestrate

        g = FakeG([])
        g.user = "o"  # type: ignore[attr-defined]
        g.exists = lambda path: exists  # type: ignore[attr-defined]
        seen: dict = {}

        def create(*a: object, **k: object) -> tuple[int, str]:
            seen["author"] = os.environ.get("GIT_AUTHOR_NAME")  # what the seed commit would be made as
            return 4, "agent/issue-4"

        with mock.patch.object(new_app, "create_app", side_effect=create) as made, \
                mock.patch.object(orchestrate, "checkpoint") as checkpoint, \
                mock.patch.object(orchestrate, "Ctx") as ctx, mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("GIT_AUTHOR_NAME", None)
            ctx.return_value.url.return_value = "u"
            ctx.return_value.file_link.return_value = "f"
            new_app.open_work_item(g, "a", "T", "", None, **kw)  # type: ignore[arg-type]
        return made, checkpoint, seen

    def test_every_open_creates_the_item_the_pr_and_the_checkpoint(self) -> None:
        made, checkpoint, _ = self.run_open()
        made.assert_called_once()
        self.assertEqual(checkpoint.call_args.args[1], "1")
        self.assertEqual(checkpoint.call_args.args[-1], "after_gate1")

    def test_a_change_on_a_missing_app_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_open(exists=False, existing_only=True)

    def test_a_factory_draft_uses_the_factory_identity_and_an_owner_seed_does_not(self) -> None:
        self.assertEqual(self.run_open(as_factory=True)[2]["author"], "factory-orchestrator")
        self.assertIsNone(self.run_open()[2]["author"])


if __name__ == "__main__":
    unittest.main()
