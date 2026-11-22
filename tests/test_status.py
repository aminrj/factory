"""The status page and the immediate answers (scripts/status.py). Fake Gitea, no network, no real push."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import orchestrate as o  # noqa: E402
import status  # noqa: E402

NOW = datetime(2026, 11, 21, 16, 0, tzinfo=UTC)


def c(cid: int, body: str, at: str = "2026-11-21T15:30:00Z", who: str = "owner") -> dict:
    return {"id": cid, "body": body, "created_at": at, "user": {"login": who}}


CHECKPOINT = c(10, "🛑 CHECKPOINT 3: approve the plan or send it back\n<!--factory:resume=after_gate3-->",
               "2026-11-21T15:00:00Z")
CLAIM = c(12, "▶ Picked up `/go` (comment 11, 22 min). Running `after_gate3` on model `local`.", "2026-11-21T15:50:00Z")


class FakeGitea:
    user = "owner"

    def __init__(self, comments: dict[int, list[dict]], closed: list[dict] | None = None) -> None:
        self.comments = comments
        self.closed = closed or []
        self.posted: list[tuple[int, str]] = []
        self.calls: list[tuple[str, str, dict | None]] = []

    def repo_url(self, repo: str) -> str:
        return f"https://git.example/{repo}.git"

    def paged(self, path: str) -> list[dict]:
        if "/comments" in path:
            return list(self.comments[int(path.split("/issues/")[1].split("/")[0])])
        if "state=closed" in path:
            return self.closed
        if "/issues?" in path:
            return []
        return []

    def comment(self, repo: str, n: int, body: str) -> None:
        self.posted.append((n, body))
        self.comments.setdefault(n, []).append(c(900 + len(self.posted), body))

    def call(self, method: str, path: str, body: dict | None = None, **kw: object) -> dict:
        self.calls.append((method, path, body))
        return {"number": 77}


class StateDir(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.patch = mock.patch.object(o, "STATE", Path(self.tmp.name))
        self.patch.start()
        self.notify = mock.patch.object(o, "notify").start()
        # an item whose last comment is a claim is running here: a real run holds its lock (stale claims: test_board)
        mock.patch.object(o, "item_is_running", return_value=True).start()

    def tearDown(self) -> None:
        mock.patch.stopall()
        self.tmp.cleanup()


class AnswerTests(StateDir):
    def test_a_command_during_a_run_of_the_same_issue_is_told_it_will_not_run(self) -> None:
        g = FakeGitea({19: [c(11, "/go"), CLAIM, c(13, "/go minutes=3", "2026-11-21T15:55:00Z")]})
        v = status.issue_view(g, "owner/app", 19)
        running = {"app": "app", "n": 19, "step": "implement"}
        status.answer_commands(g, [v], 300, running, NOW)
        (n, text), = g.posted
        self.assertEqual(n, 19)
        self.assertIn("will not run", text)
        self.assertIn("`implement`", text)

    def test_stop_during_a_run_is_heard_between_phases(self) -> None:
        g = FakeGitea({19: [c(11, "/go"), CLAIM, c(13, "/stop")]})
        status.answer_commands(g, [status.issue_view(g, "owner/app", 19)], 300, {"app": "app", "n": 19, "step": "x"}, NOW)
        self.assertIn("between two phases", g.posted[0][1])

    def test_a_command_on_another_issue_is_told_it_runs_after(self) -> None:
        g = FakeGitea({19: [c(11, "/go"), CLAIM], 15: [CHECKPOINT, c(20, "/rework the label")]})
        views = [status.issue_view(g, "owner/app", 19), status.issue_view(g, "owner/app", 15)]
        status.answer_commands(g, views, 420, {"app": "app", "n": 19, "step": "implement"}, NOW)
        (n, text), = g.posted
        self.assertEqual(n, 15)
        self.assertIn("busy with app#19", text)
        self.assertIn("7 min so far", text)

    def test_no_answer_when_the_tick_is_free_it_acts_within_a_minute(self) -> None:
        g = FakeGitea({15: [CHECKPOINT, c(20, "/go")]})
        status.answer_commands(g, [status.issue_view(g, "owner/app", 15)], None, None, NOW)
        self.assertEqual(g.posted, [])

    def test_each_command_is_answered_once(self) -> None:
        g = FakeGitea({19: [c(11, "/go"), CLAIM, c(13, "/go")]})
        for _ in range(3):
            v = status.issue_view(g, "owner/app", 19)
            status.answer_commands(g, [v], 300, {"app": "app", "n": 19, "step": "x"}, NOW)
        self.assertEqual(len(g.posted), 1)

    def test_an_answer_never_marks_the_command_as_handled_for_the_foreman(self) -> None:
        # The foreman skips commands that a notice names as "(comment N)". An answer that says "it runs after" must not
        # make the foreman skip it.
        g = FakeGitea({19: [c(11, "/go"), CLAIM], 15: [CHECKPOINT, c(20, "/go")]})
        views = [status.issue_view(g, "owner/app", 19), status.issue_view(g, "owner/app", 15)]
        status.answer_commands(g, views, 60, {"app": "app", "n": 19, "step": "x"}, NOW)
        self.assertEqual(o.answered_ids(g.comments[15]), set())
        self.assertIsNotNone(o.decide(g, "owner/app", 15))

    def test_a_command_on_a_closed_issue_is_told_it_never_runs(self) -> None:
        g = FakeGitea({15: [CHECKPOINT, c(30, "/rework the label", "2026-11-21T15:50:39Z"),
                            c(31, "looks good", "2026-11-21T15:51:00Z")]})
        status.answer_closed(g, [("owner/app", 15, "2026-11-21T15:15:56Z")])
        status.answer_closed(g, [("owner/app", 15, "2026-11-21T15:15:56Z")])
        (n, text), = g.posted
        self.assertIn("closed", text)
        self.assertIn("will not run", text)
        self.notify.assert_called_once()

    def test_commands_from_before_the_close_are_history(self) -> None:
        g = FakeGitea({15: [c(30, "/go", "2026-11-21T15:00:00Z")]})
        status.answer_closed(g, [("owner/app", 15, "2026-11-21T15:15:56Z")])
        self.assertEqual(g.posted, [])


class PageTests(StateDir):
    def page(self, views: list[dict], tick_s: int | None = None, beat: float | None = 30, **kw: object) -> str:
        g = FakeGitea({})
        args = {"steps": {}, "builds": [], "mirrors": {"factory": "in sync"}, "spend": {}, "log_lines": [], **kw}
        return status.render(g, NOW, "devbox", tick_s, beat, True, views, **args)

    def test_waiting_running_and_received(self) -> None:
        g = FakeGitea({19: [c(11, "/go"), CLAIM], 15: [CHECKPOINT], 16: [CHECKPOINT, c(20, "/go")]})
        views = [status.issue_view(g, "owner/app", n) for n in (19, 15, 16)]
        text = self.page(views, tick_s=600, steps={("app", 19): ("implement", 240)})
        self.assertIn("**Foreman: busy** for 10 min", text)
        self.assertIn("agent step `implement` for 4 min (model `local`", text)
        waiting = text.split("## Waiting for you")[1].split("##")[0]
        self.assertIn("app#15", waiting)
        self.assertIn("3: approve the plan", waiting)
        self.assertNotIn("app#16", waiting)  # answered: it is in the received list instead
        self.assertIn("app#16](https://git.example/owner/app/issues/16): `/go`", text)

    def test_a_silent_foreman_is_flagged(self) -> None:
        self.assertIn("no tick for 12 min", self.page([], beat=720))

    def test_staged_builds_mirrors_and_spend(self) -> None:
        text = self.page([], builds=[{"app": "app", "issue": 15, "ref": "agent/issue-15", "url": "http://h:8800/",
                                      "started": "2026-11-21T08:00:00+00:00", "alive": False}],
                         mirrors={"factory": "in sync", "app": "behind since 15:00 UTC"},
                         spend={"default": 3.5, "local": 0.0})
        self.assertIn("http://h:8800/ (⚠️ container gone", text)
        self.assertIn("- app: behind since 15:00 UTC", text)
        self.assertIn("$3.50 (default $3.50, local $0.00)", text)

    def test_spend_counts_today_only(self) -> None:
        with tempfile.TemporaryDirectory() as d, mock.patch.object(o, "DATA", Path(d)):
            rows = [{"ts": "2026-11-21T10:00:00Z", "cost_usd": 1.25, "model": "default"},
                    {"ts": "2026-11-20T10:00:00Z", "cost_usd": 9, "model": "default"},
                    {"ts": "2026-11-21T11:00:00Z", "cost_usd": 0.5, "model": None}]
            (Path(d) / "agent_runs.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
            self.assertEqual(status.spend_today(NOW), {"default": 1.75})


class ProcessTests(unittest.TestCase):
    PS = [(400, "/usr/bin/python3 -I /x/factory/scripts/orchestrate.py tick"),
          (130, "/usr/bin/python3 -I /x/factory/scripts/dispatch.py brevkoll 19 implement"),
          (5, "bash")]

    def test_busy_tick_and_agent_steps(self) -> None:
        self.assertEqual(status.busy_tick(self.PS), 400)
        self.assertEqual(status.agent_steps(self.PS), {("brevkoll", 19): ("implement", 130)})
        self.assertIsNone(status.busy_tick(self.PS[2:]))


if __name__ == "__main__":
    unittest.main()


class MirrorStateTests(unittest.TestCase):
    def test_a_fresh_clone_reads_the_mirror_from_github(self) -> None:
        # 2026-11-22: on the new foreman host the GitHub ref had never been fetched, so the mirrors stayed "unknown"
        # and were never published
        import mirror
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@t"}
            for bare in ("origin.git", "github.git"):
                subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(root / bare)], check=True)
            subprocess.run(["git", "clone", "-q", str(root / "origin.git"), str(root / "src")], check=True)
            (root / "src" / "a.txt").write_text("a\n")
            subprocess.run(["git", "add", "-A"], cwd=root / "src", check=True)
            subprocess.run(["git", "commit", "-q", "-m", "a"], cwd=root / "src", check=True, env=env)
            subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=root / "src", check=True)
            subprocess.run(["git", "push", "-q", str(root / "github.git"), "HEAD:main"], cwd=root / "src", check=True)
            subprocess.run(["git", "clone", "-q", str(root / "origin.git"), str(root / "fresh")], check=True)
            subprocess.run(["git", "remote", "add", "github", str(root / "github.git")], cwd=root / "fresh", check=True)
            self.assertEqual(status.mirror_state(root / "fresh"), "in sync")
            self.assertTrue(mirror.git(root / "fresh", "rev-parse", "-q", "--verify", mirror.MIRROR_REF, check=False))
