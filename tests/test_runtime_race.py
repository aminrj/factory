"""The race that paused the orchestrator: its own metrics commit against a merge on origin/main.

Real git: a bare origin, the runtime clone, and a second clone that plays the owner merging a pull request.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import orchestrate  # noqa: E402


def env(name: str) -> dict[str, str]:
    return {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": f"{name}@x", "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": f"{name}@x"}


def git(cwd: Path, *args: str, who: str = "factory-orchestrator") -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, env=env(who)).stdout.strip()


class RuntimeRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        origin = base / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.runtime, self.owner = base / "runtime", base / "owner"
        for clone in (self.runtime, self.owner):
            subprocess.run(["git", "clone", "-q", str(origin), str(clone)], check=True, capture_output=True)
        git(self.runtime, "switch", "-q", "-c", "main")
        (self.runtime / "scripts").mkdir()
        (self.runtime / "data").mkdir()
        (self.runtime / "scripts" / "a.py").write_text("1")
        (self.runtime / "data" / "d.jsonl").write_text("1\n")
        (self.runtime / "BOARD.md").write_text("b")
        git(self.runtime, "add", "-A")
        git(self.runtime, "commit", "-q", "-m", "init")
        git(self.runtime, "push", "-q", "-u", "origin", "main")
        git(self.owner, "pull", "-q", "origin", "main")
        self.addCleanup(self.tmp.cleanup)

    def metrics_commit(self, subject: str = "Metrics refresh", path: str = "data/d.jsonl") -> None:
        (self.runtime / path).write_text("2\n")
        git(self.runtime, "add", "-A")
        git(self.runtime, "commit", "-q", "-m", subject)

    def owner_merges(self, rel: str = "scripts/a.py") -> None:
        (self.owner / rel).write_text("merged by the owner")
        git(self.owner, "add", "-A")
        git(self.owner, "commit", "-q", "-m", "Merge pull request", who="owner")
        git(self.owner, "push", "-q", "origin", "main")

    def ahead_behind(self) -> tuple[int, int]:
        git(self.runtime, "fetch", "-q", "origin")
        ahead, behind = git(self.runtime, "rev-list", "--left-right", "--count", "HEAD...origin/main").split()
        return int(ahead), int(behind)

    def test_a_metrics_commit_and_a_merge_at_the_same_time_resolve_themselves(self) -> None:
        self.metrics_commit()  # the orchestrator's bookkeeping commit, push rejected because the owner merged first
        self.owner_merges()
        self.assertEqual(self.ahead_behind(), (1, 1))  # diverged: what paused the orchestrator
        orchestrate.update_runtime(self.runtime)
        self.assertEqual(self.ahead_behind(), (0, 0))  # rebased on top of the merge and pushed
        self.assertEqual((self.runtime / "scripts" / "a.py").read_text(), "merged by the owner")
        self.assertEqual((self.runtime / "data" / "d.jsonl").read_text(), "2\n")  # the metrics data survived
        self.assertIsNone(orchestrate.runtime_behind(self.runtime))

    def test_a_rejected_push_is_retried_on_top_of_the_merge(self) -> None:
        self.metrics_commit()
        self.owner_merges()
        orchestrate.push_metrics(self.runtime)  # the first push is rejected, then it syncs and pushes
        self.assertEqual(self.ahead_behind(), (0, 0))

    def test_other_local_commits_are_still_a_problem(self) -> None:
        self.metrics_commit(subject="my own change", path="scripts/a.py")
        self.owner_merges("data/d.jsonl")
        orchestrate.update_runtime(self.runtime)
        self.assertEqual(self.ahead_behind(), (1, 1))  # untouched
        self.assertIn("local commits", orchestrate.runtime_behind(self.runtime) or "")

    def test_a_metrics_named_commit_that_touches_code_is_not_trusted(self) -> None:
        self.metrics_commit(path="scripts/a.py")  # called Metrics refresh but edits a script
        self.owner_merges("data/d.jsonl")
        git(self.runtime, "fetch", "-q", "origin")
        self.assertFalse(orchestrate.local_commits_are_metrics_only(self.runtime))
        orchestrate.update_runtime(self.runtime)
        self.assertIn("local commits", orchestrate.runtime_behind(self.runtime) or "")

    def test_a_conflicting_rebase_is_aborted_and_left_for_the_owner(self) -> None:
        self.metrics_commit()  # edits data/d.jsonl
        self.owner_merges("data/d.jsonl")  # the owner edits the same file differently
        orchestrate.update_runtime(self.runtime)
        self.assertEqual(git(self.runtime, "status", "--porcelain"), "")  # no half-finished rebase left behind
        self.assertIn("local commits", orchestrate.runtime_behind(self.runtime) or "")

    def test_nothing_to_do_when_in_sync(self) -> None:
        orchestrate.update_runtime(self.runtime)
        self.assertEqual(self.ahead_behind(), (0, 0))


if __name__ == "__main__":
    unittest.main()
