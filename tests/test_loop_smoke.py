"""Loop smoke test: one work item walked through Gate 1 with the real orchestrator code.

Real: git (a bare origin and a clone), the orchestrator's decide/process/checkpoint. Fake: Gitea (in memory), the agent
(dispatch is stubbed), notifications and the metrics refresh. Every bug found on the first real run (an idea issue that
was not watched, an owner whose web-editor commit was not recognised, a /go with a note that was ignored in silence)
fails this test when it comes back.
"""

import json
import os
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

OWNER_CONFIG = "owner@example.com"
WEB_EDITOR = "1+owner@noreply.gitea.example.com"
BOT = "factory-orchestrator@users.noreply.local"


class FakeGitea:
    user = "owner"

    def __init__(self) -> None:
        self.comments: list[dict] = []
        self.labels = ["stage/idea"]
        self._tick = datetime(2026, 1, 1, tzinfo=UTC)

    def repo_url(self, repo: str) -> str:
        return f"https://gitea.example/{repo}.git"

    def paged(self, path: str) -> list[dict]:
        if "repos/search" in path:
            return [{"full_name": "owner/app"}]
        if path.endswith("/comments"):
            return list(self.comments)
        if "/issues?" in path:
            return [{"number": 1, "labels": [{"name": n} for n in self.labels]}]
        return []

    def get(self, path: str) -> list[dict]:
        if path.endswith("/labels"):
            return [{"name": n, "id": i} for i, n in enumerate(self.labels)]
        return []

    def call(self, method: str, path: str, body: dict | None = None, **kw: object) -> dict:
        if path.endswith("/comments"):
            return self.comment("owner/app", 1, (body or {})["body"])
        return {}

    def comment(self, repo: str, n: int, body: str, who: str = "owner") -> dict:
        self._tick += timedelta(minutes=7)
        c = {"id": len(self.comments) + 1, "user": {"login": who}, "body": body,
             "created_at": self._tick.isoformat().replace("+00:00", "Z")}
        self.comments.append(c)
        return c

    def set_stage(self, repo: str, n: int, stage: str) -> None:
        self.labels = [n_ for n_ in self.labels if not n_.startswith("stage/")] + [f"stage/{stage}"]

    def label_id(self, repo: str, name: str) -> int:
        return 1


def identity(email: str, name: str = "x") -> dict[str, str]:
    """Explicit author and committer in the environment: the caller's own GIT_* variables (the orchestrator sets them)
    must never decide who a test commit is by."""
    return {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name,
            "GIT_COMMITTER_EMAIL": email}


def git(cwd: Path, *args: str, email: str = BOT) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=identity(email)).stdout.strip()


class LoopSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.work, origin = base / "work", base / "origin.git"
        (self.work).mkdir()
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
        self.spec = self.clone / "thoughts" / "1" / "spec.md"
        self.spec.write_text("# Spec: #1 change\n\nStatus: draft\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "draft")
        git(self.clone, "push", "-q", "origin", "agent/issue-1")
        self.g = FakeGitea()
        self.g.comment("owner/app", 1, "🛑 CHECKPOINT 1: review\n\n<!--factory:resume=after_gate1;stop=-->")
        self.dispatched: list[str] = []
        self.notify = mock.Mock()
        patches = [
            mock.patch.object(orchestrate, "WORK", self.work), mock.patch.object(orchestrate, "STATE", base / "state"),
            mock.patch.object(orchestrate, "DATA", base / "data"), mock.patch.object(orchestrate, "REPLIED", base / "replied.json"),
            mock.patch.object(orchestrate, "REMINDERS", base / "reminders.json"),
            mock.patch.object(orchestrate, "refresh_board"), mock.patch.object(orchestrate, "commit_ops"),
            mock.patch.object(orchestrate, "notify", new=self.notify), mock.patch.object(orchestrate, "log"),
            mock.patch.object(orchestrate, "dispatch", side_effect=lambda ctx, step, note=None: self.dispatched.append(step) or ""),
        ]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])
        self.addCleanup(self.tmp.cleanup)
        self.data = base / "data"

    def owner_says(self, body: str) -> None:
        self.g.comment("owner/app", 1, body)

    def approve_in_the_web_editor(self) -> None:
        self.spec.write_text("# Spec: #1 change\n\nStatus: approved\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "Update thoughts/1/spec.md", email=WEB_EDITOR)
        git(self.clone, "push", "-q", "origin", "agent/issue-1")

    def last_bot_text(self) -> str:
        return self.g.comments[-1]["body"]

    def test_the_idle_issue_is_watched(self) -> None:
        self.assertEqual(orchestrate.active_issues(self.g), [("owner/app", 1)])  # type: ignore[arg-type]

    def test_gate_1_end_to_end(self) -> None:
        # 1. /go with a note on the same line, but the spec is still a draft: refused, and said so
        self.owner_says("/go minutes=5 looks fine to me, approving now")
        self.assertEqual(orchestrate.process(self.g, "owner/app", 1), "needs human")  # type: ignore[arg-type]
        self.assertIn("Status: draft", self.last_bot_text())
        self.assertEqual(self.dispatched, [])
        # 2. approved in Gitea's web editor (a noreply author), then /go: the agent step runs and the next gate opens
        self.approve_in_the_web_editor()
        self.owner_says("/go minutes=3")
        self.assertEqual(orchestrate.process(self.g, "owner/app", 1), "ran after_gate1")  # type: ignore[arg-type]
        self.assertEqual(self.dispatched, ["threat-model"])
        self.assertIn("stage/spec-ready", self.g.labels)
        self.assertIn("CHECKPOINT 2", self.last_bot_text())
        # 3. the data the retro needs: the owner's minutes and how long each checkpoint waited
        minutes = [json.loads(x) for x in (self.data / "human_minutes.jsonl").read_text().splitlines()]
        self.assertEqual([m["minutes"] for m in minutes], [3])
        waits = [json.loads(x) for x in (self.data / "gate_waits.jsonl").read_text().splitlines()]
        self.assertEqual(len(waits), 2)
        self.assertEqual(waits[0]["checkpoint"], "after_gate1")
        self.assertGreater(waits[0]["wait_min"], 0)

    def test_an_approval_by_the_factory_is_refused(self) -> None:
        self.spec.write_text("# Spec: #1 change\n\nStatus: approved\n")
        git(self.clone, "add", "-A")
        git(self.clone, "commit", "-q", "-m", "agent approves", email=BOT)
        git(self.clone, "push", "-q", "origin", "agent/issue-1")
        self.owner_says("/go")
        self.assertEqual(orchestrate.process(self.g, "owner/app", 1), "needs human")  # type: ignore[arg-type]
        self.assertIn("someone other", self.last_bot_text())
        self.assertEqual(self.dispatched, [])

    def test_an_unknown_command_gets_one_reply(self) -> None:
        self.owner_says("/approve")
        orchestrate.answer_unknown_commands(self.g, "owner/app", 1)  # type: ignore[arg-type]
        orchestrate.answer_unknown_commands(self.g, "owner/app", 1)  # type: ignore[arg-type]
        replies = [c for c in self.g.comments if c["body"].startswith(orchestrate.NOTICE_PREFIX)]
        self.assertEqual(len(replies), 1)
        self.assertIn("/approve", replies[0]["body"])
        self.owner_says("/go")  # the reply must not have disturbed the checkpoint
        self.assertIsNotNone(orchestrate.decide(self.g, "owner/app", 1))  # type: ignore[arg-type]

    def test_waiting_list_and_reminder(self) -> None:
        old = datetime.now(UTC) - timedelta(hours=5)
        self.g.comments[0]["created_at"] = old.isoformat().replace("+00:00", "Z")
        waiting = orchestrate.waiting_list(self.g)  # type: ignore[arg-type]
        self.assertEqual(len(waiting), 1)
        self.assertGreaterEqual(waiting[0]["age_h"], 5)
        orchestrate.remind_waiting(self.g)  # type: ignore[arg-type]
        orchestrate.remind_waiting(self.g)  # type: ignore[arg-type]
        self.assertEqual(self.notify.call_count, 1)  # once, not once per tick
        self.owner_says("/go")
        self.assertEqual(orchestrate.waiting_list(self.g), [])  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
