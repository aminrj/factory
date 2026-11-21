"""new_change.py: the PR is opened at Gate 1 and never twice."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import new_change  # noqa: E402


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
        pr = new_change.ensure_wip_pr(g, "o/a", "agent/issue-4", 4, "T")  # type: ignore[arg-type]
        self.assertEqual(pr["title"], "WIP: T")
        self.assertEqual(g.posted[0][2]["head"], "agent/issue-4")
        self.assertIn("Refs #4", g.posted[0][2]["body"])

    def test_reuses_an_open_pr(self) -> None:
        g = FakeG([{"head": {"ref": "agent/issue-4"}, "number": 3, "html_url": "x"}])
        pr = new_change.ensure_wip_pr(g, "o/a", "agent/issue-4", 4, "T")  # type: ignore[arg-type]
        self.assertEqual(pr["number"], 3)
        self.assertEqual(g.posted, [])


if __name__ == "__main__":
    unittest.main()
