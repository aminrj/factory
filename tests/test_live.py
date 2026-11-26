"""The live view (scripts/live.py) and dispatch's inner loop: streamed runs, local checks, self-fix rounds."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch  # noqa: E402
from live import Live  # noqa: E402

# Shapes seen from Claude Code 2.1.330 with --output-format stream-json --verbose (2026-11-22)
EVENTS = [
    {"type": "system", "subtype": "init", "session_id": "s1"},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "I'll fix the upload limit."},
                                                  {"type": "tool_use", "name": "TaskCreate",
                                                   "input": {"subject": "Find the limit", "description": "x"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "Task #1 created successfully"}]},
     "tool_use_result": {"task": {"id": "1", "subject": "Find the limit"}}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "TaskUpdate",
                                                   "input": {"taskId": "1", "status": "in_progress"}}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit",
                                                   "input": {"file_path": "src/app/upload.py"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": True, "content": "old_string not found"}]}},
    {"type": "result", "subtype": "success", "is_error": False, "session_id": "s1", "total_cost_usd": 0.01,
     "num_turns": 3, "result": "done", "usage": {"input_tokens": 10}},
]


class LiveTests(unittest.TestCase):
    def test_events_become_what_the_board_shows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Live(Path(tmp), "app", 29, "implement", "local")
            for ev in EVENTS[:-1]:
                live.event(ev)
            live.write(force=True)
            board = Path(tmp) / "state" / "board" / "live"
            item = json.loads((board / "index.json").read_text())["items"][0]
            self.assertEqual((item["app"], item["n"], item["step"]), ("app", 29, "implement"))
            self.assertEqual(item["todos"], [{"id": "1", "text": "Find the limit", "status": "in_progress"}])
            self.assertEqual(item["files"], ["src/app/upload.py"])
            self.assertEqual(item["action"], "edits src/app/upload.py")
            self.assertTrue(any("old_string not found" in r for r in item["recent"]))
            live.close()
            self.assertEqual(json.loads((board / "index.json").read_text())["items"], [])
            self.assertIn("edits src/app/upload.py", (board / "app-29.last.log").read_text())

    def test_the_board_shows_what_commands_returned_and_what_the_model_thinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Live(Path(tmp), "app", 1, "implement", "local")
            live.event({"type": "assistant", "message": {"content": [
                {"type": "thinking", "thinking": "The limit counts the multipart overhead, so " + "x" * 2000},
                {"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest -q tests/unit"}}]}})
            out = "\n".join(f"line {i}" for i in range(20))
            live.event({"type": "user", "message": {"content": [
                {"type": "tool_result", "content": [{"type": "text", "text": out}]}]}})
            recent = "\n".join(live.state["recent"])
            self.assertIn("💭 (", recent)
            self.assertIn("multipart overhead", recent)
            self.assertIn("→ runs `uv run pytest -q tests/unit`", recent)
            self.assertIn("← line 0", recent)
            self.assertIn("… 14 more lines", recent)  # the board shows 6; the log keeps all 20
            live.close()
            self.assertIn("line 19", (Path(tmp) / "state/board/live/app-1.last.log").read_text())

    def test_grok_tool_names_read_as_the_same_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Live(Path(tmp), "app", 1, "implement", "grok", Path("/w/app-issue-1"))
            live.event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "read_file", "input": {"path": "/w/app-issue-1/src/x.py"}}]}})
            self.assertEqual(live.state["action"], "reads src/x.py")
            live.close()

    def test_paths_are_shown_relative_to_the_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            live = Live(Path(tmp), "app", 1, "implement", "local", Path("/w/app-issue-1"))
            live.event({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "name": "Write", "input": {"file_path": "/w/app-issue-1/src/x.py"}}]}})
            self.assertEqual(live.state["files"], ["src/x.py"])
            live.close()

    def test_a_board_that_cannot_be_written_never_fails_the_step(self) -> None:
        live = Live(Path("/proc/nonexistent"), "app", 1, "spec", "local")
        live.event(EVENTS[1])
        live.close()


class RunAgentTests(unittest.TestCase):
    def test_a_streamed_run_returns_the_result_event_and_feeds_the_board(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text("#!/bin/sh\ncat >/dev/null\n" + "".join(f"echo '{json.dumps(e)}'\n" for e in EVENTS))
            fake.chmod(0o755)
            live = Live(Path(tmp), "app", 1, "implement", "local")
            res = dispatch.run_agent([str(fake), "-p"], "do it", Path(tmp), dict(os.environ), live, time.time() + 30)
            self.assertEqual(res["session_id"], "s1")
            self.assertFalse(res["is_error"])
            self.assertEqual(live.state["files"], ["src/app/upload.py"])

    def test_a_run_that_hits_the_turn_limit_is_a_stall_with_a_hint(self) -> None:
        # what Claude Code 2.1.330 sends when --max-turns is reached (seen on the devbox, 2026-11-22)
        last = {"type": "result", "subtype": "error_max_turns", "is_error": True, "num_turns": 81, "result": None}
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text(f"#!/bin/sh\ncat >/dev/null\necho '{json.dumps(last)}'\n")
            fake.chmod(0o755)
            live = Live(Path(tmp), "app", 1, "implement", "local")
            res = dispatch.run_agent([str(fake)], "", Path(tmp), dict(os.environ), live, time.time() + 30)
        self.assertTrue(res["is_error"])
        self.assertEqual(dispatch.classify(res["result"]), "stalled")
        self.assertIn("/go model=fast", dispatch.HINTS["stalled"])

    def test_a_run_that_hangs_is_killed_at_the_deadline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "claude"
            fake.write_text("#!/bin/sh\nexec sleep 30\n")
            fake.chmod(0o755)
            live = Live(Path(tmp), "app", 1, "implement", "local")
            t0 = time.time()
            res = dispatch.run_agent([str(fake)], "", Path(tmp), dict(os.environ), live, time.time() + 1)
            self.assertLess(time.time() - t0, 10)
            self.assertTrue(res["is_error"])
            self.assertIn("timed out", res["result"])


class CheckEnvTests(unittest.TestCase):
    def test_no_credentials_reach_the_checks(self) -> None:
        with mock.patch.dict(os.environ, {"GITEA_TOKEN_FILE": "/x", "GIT_CONFIG_KEY_0": "credential.helper",
                                          "ANTHROPIC_API_KEY": "k", "MY_TOKEN": "t", "PATH": "/usr/bin"}):
            env = dispatch.check_env()
        self.assertEqual({k for k in env if k in ("GITEA_TOKEN_FILE", "GIT_CONFIG_KEY_0", "ANTHROPIC_API_KEY",
                                                  "MY_TOKEN")}, set())
        self.assertEqual(env["PATH"], "/usr/bin")


class ChecksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.wt)], check=True)
        (self.wt / ".factory").mkdir()
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
                    "GIT_COMMITTER_EMAIL": "a@x"}

    def commit(self) -> str:
        subprocess.run(["git", "add", "-A"], cwd=self.wt, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=self.wt, check=True, env=self.env)
        return dispatch.run("git", "rev-parse", "HEAD", cwd=self.wt)

    def test_the_app_s_own_list_wins(self) -> None:
        (self.wt / ".factory" / "checks").write_text("# mine\nmake lint\n\nmake test\n")
        self.assertEqual(dispatch.app_checks(self.wt), ["make lint", "make test"])

    def test_acceptance_tests_and_quality_wait_for_validate(self) -> None:
        (self.wt / "tests" / "holdout").mkdir(parents=True)
        (self.wt / "tests" / "holdout" / "test_issue_29.py").write_text("")
        (self.wt / "scripts").mkdir()
        (self.wt / "scripts" / "quality.sh").write_text("")
        (self.wt / "scripts" / "check-tests-fail-before-patch.sh").write_text("")
        (self.wt / ".factory" / "checks").unlink(missing_ok=True)
        (self.wt / ".factory").rmdir()
        phase = dispatch.app_checks(self.wt, "implement")
        self.assertIn("bash scripts/check-tests-fail-before-patch.sh origin/main", phase)
        self.assertNotIn("bash scripts/quality.sh", phase)
        self.assertNotIn("uv run pytest -q tests/holdout", phase)
        final = dispatch.app_checks(self.wt, "validate", 29)
        self.assertIn("bash scripts/quality.sh", final)
        self.assertIn("uv run pytest -q tests/holdout/test_issue_29.py", final)

    def test_a_check_the_agent_broke_is_handed_back(self) -> None:
        (self.wt / ".factory" / "checks").write_text("test -f good\n")
        (self.wt / "good").write_text("x")
        base = self.commit()
        (self.wt / "good").unlink()
        self.commit()
        live = mock.Mock()
        found = dispatch.first_failing_check(self.wt, base, set(), live)
        self.assertEqual(found[0] if found else None, "test -f good")

    def test_a_check_that_was_red_before_the_run_is_not_the_agent_s(self) -> None:
        (self.wt / ".factory" / "checks").write_text("test -f never\n")
        base = self.commit()
        skip: set[str] = set()
        self.assertIsNone(dispatch.first_failing_check(self.wt, base, skip, mock.Mock()))
        self.assertEqual(skip, {"test -f never"})


if __name__ == "__main__":
    unittest.main()


class RefusedRunTests(unittest.TestCase):
    def test_a_refused_run_leaves_nothing_behind_for_the_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt = Path(tmp)
            env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@x", "GIT_COMMITTER_NAME": "a",
                   "GIT_COMMITTER_EMAIL": "a@x"}
            subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True)
            (wt / "a").write_text("1")
            subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=wt, check=True, env=env)
            base = dispatch.run("git", "rev-parse", "HEAD", cwd=wt)
            (wt / "a").write_text("2")
            subprocess.run(["git", "commit", "-q", "-am", "agent"], cwd=wt, check=True, env=env)
            (wt / "scratch.py").write_text("x")
            keep = dispatch.keep_refused(wt, "agent/issue-29", base)
            self.assertEqual(dispatch.run("git", "rev-parse", "HEAD", cwd=wt), base)
            self.assertFalse((wt / "scratch.py").exists())
            self.assertIn(keep, dispatch.run("git", "branch", cwd=wt))
