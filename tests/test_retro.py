"""Retrospectives: an agent run pauses every few turns, writes five lines, and the dispatcher checks them against the
worktree (2026-11-22: local runs went in circles for 30-70 minutes per item and nothing noticed)."""

import os
import time
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch  # noqa: E402
import signals  # noqa: E402

RETRO = "GOAL: limit body size\nDONE: helper written\nSTUCK: pyright error in main.py\nNEXT: fix the call\nFILES: src/app/main.py"


def git(wt: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "a",
        "GIT_AUTHOR_EMAIL": "a@x",
        "GIT_COMMITTER_NAME": "a",
        "GIT_COMMITTER_EMAIL": "a@x",
    }
    subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True, env=env)


class Repo(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name)
        git(self.wt, "init", "-q", "-b", "main")
        (self.wt / "a.py").write_text("x = 1\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", "init")
        self.head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.wt, capture_output=True, text=True
        ).stdout.strip()


class ParseAndJudgeTests(Repo):
    def test_parse_reads_the_five_lines_even_with_markdown(self) -> None:
        r = dispatch.parse_retro(
            "Sure.\n**GOAL:** g\n- DONE: d\nSTUCK: s\nNEXT: n\nFILES: f\n"
        )
        self.assertEqual(
            r, {"GOAL": "g", "DONE": "d", "STUCK": "s", "NEXT": "n", "FILES": "f"}
        )

    def test_facts_see_uncommitted_changes_and_refused_files(self) -> None:
        (self.wt / "tests" / "holdout").mkdir(parents=True)
        (self.wt / "tests" / "holdout" / "test_issue_1.py").write_text(
            "def test(): pass\n"
        )
        (self.wt / "a.py").write_text("x = 2\n")
        f = dispatch.worktree_facts(self.wt, self.head, 1)
        self.assertEqual(f["commits"], 0)
        self.assertIn("a.py", f["changed"])
        self.assertEqual(f["refused"], ["tests/holdout/test_issue_1.py"])
        verdict, msg = dispatch.judge(dispatch.parse_retro(RETRO), f, None)
        self.assertEqual(verdict, "continue")
        self.assertIn(
            "Undo those changes", msg
        )  # the in-run guard: told while it can still fix it
        self.assertIn("fix the call", msg)

    def test_two_pauses_without_any_change_is_stalled(self) -> None:
        r = dispatch.parse_retro(RETRO)
        f1 = dispatch.worktree_facts(self.wt, self.head, 1)
        self.assertEqual(dispatch.judge(r, f1, None)[0], "continue")
        f1["unchanged"] = False
        f2 = dispatch.worktree_facts(self.wt, self.head, 1)
        verdict, msg = dispatch.judge(r, f2, f1)
        self.assertEqual(
            verdict, "continue"
        )  # first pause with no change: told to change approach
        self.assertIn("different approach", msg)
        f2["unchanged"] = True
        verdict, msg = dispatch.judge(
            r, dispatch.worktree_facts(self.wt, self.head, 1), f2
        )
        self.assertEqual(verdict, "stalled")
        self.assertIn("pyright error", msg)


class SessionTests(Repo):
    """agent_session with Claude Code faked: chunks, retrospectives, and the decision."""

    def fake_runs(self, script: list[dict]) -> mock.Mock:
        calls = []

        def run(
            cmd: list[str], prompt: str, *a: object, interrupt: object = None
        ) -> dict:
            calls.append((cmd, prompt))
            res = dict(script.pop(0))
            if "--max-turns" in cmd:  # like Claude Code: never more turns than allowed
                res["num_turns"] = min(
                    res.get("num_turns", 0), int(cmd[cmd.index("--max-turns") + 1])
                )
            return res

        m = mock.Mock(side_effect=run)
        m.calls = calls
        return m

    def session(self, script: list[dict], model: str = "local") -> tuple[dict, list]:
        fake = self.fake_runs(script)
        live = mock.Mock()
        live.state = {}
        with mock.patch.object(dispatch, "run_agent", new=fake):
            res = dispatch.agent_session(
                ["claude", "-p"],
                "do it",
                self.wt,
                {},
                live,
                9e12,
                model,
                self.head,
                1,
                app="app",
                state=getattr(self, "state", None),
            )
        return res, fake.calls

    def test_a_short_run_has_no_retrospective(self) -> None:
        res, calls = self.session(
            [
                {
                    "result": "done",
                    "num_turns": 7,
                    "total_cost_usd": 0.1,
                    "session_id": "s",
                }
            ]
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("--max-turns", calls[0][0])
        self.assertEqual(res["retros"], [])

    def test_progress_continues_with_the_facts_and_sums_the_cost(self) -> None:
        (self.wt / "a.py").write_text("x = 3\n")  # what the first chunk changed
        res, calls = self.session(
            [
                {
                    "subtype": "error_max_turns",
                    "num_turns": 25,
                    "total_cost_usd": 0.5,
                    "session_id": "s",
                },
                {
                    "result": RETRO,
                    "num_turns": 1,
                    "total_cost_usd": 0.01,
                    "session_id": "s",
                },
                {
                    "result": "all done",
                    "num_turns": 5,
                    "total_cost_usd": 0.2,
                    "session_id": "s",
                },
            ]
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[1][1], dispatch.RETRO_PROMPT)
        self.assertIn("--resume", calls[2][0])
        self.assertIn("a.py", calls[2][1])  # the continue prompt carries the facts
        self.assertEqual(res["num_turns"], 31)
        self.assertAlmostEqual(res["total_cost_usd"], 0.71)
        self.assertEqual(res["retros"][0]["verdict"], "continue")
        self.assertIn("after 25 turns (continue)", dispatch.retro_digest(res["retros"]))

    def test_going_in_circles_stops_as_stalled_with_a_class(self) -> None:
        chunk = {"subtype": "error_max_turns", "num_turns": 25, "session_id": "s"}
        retro = {"result": RETRO, "num_turns": 1, "session_id": "s"}
        res, calls = self.session(
            [
                dict(chunk),
                dict(retro),
                dict(chunk),
                dict(retro),
                dict(chunk),
                dict(retro),
            ]
        )
        self.assertTrue(res["is_error"])
        self.assertEqual(dispatch.classify(res["result"]), "stalled")
        self.assertEqual(
            [r["verdict"] for r in res["retros"]], ["continue", "continue", "stalled"]
        )

    def test_the_turn_cap_ends_a_run_that_keeps_changing_things(self) -> None:
        script = []
        for i in range(10):
            script.append(
                {"subtype": "error_max_turns", "num_turns": 25, "session_id": "s"}
            )
            script.append({"result": RETRO, "num_turns": 0, "session_id": "s"})
        counter = iter(range(100))

        def worktree_facts(*a: object) -> dict:
            return {
                "commits": 1,
                "changed": ["a.py"],
                "refused": [],
                "fingerprint": next(counter),
            }

        with mock.patch.object(dispatch, "worktree_facts", side_effect=worktree_facts):
            res, calls = self.session(script)
        self.assertTrue(res["is_error"])
        self.assertIn("turn limit", res["result"])
        self.assertLessEqual(res["num_turns"], dispatch.LOCAL_MAX_TURNS)


if __name__ == "__main__":
    unittest.main()


BROKEN = """<testsuites><testsuite><testcase classname="t" name="test_ac1"><failure message="AttributeError: 'list' object has
 no attribute 'items'">def test_ac1():
&gt;       headers = Headers([("content-length", "100")])

tests/holdout/test_issue_7.py:106:
.venv/lib/python3.13/site-packages/starlette/datastructures.py:531: in __init__
    self._list = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
E   AttributeError: 'list' object has no attribute 'items'
.venv/lib/python3.13/site-packages/starlette/datastructures.py:531: AttributeError</failure></testcase></testsuite></testsuites>"""

MISSING = """<testsuites><testsuite><testcase classname="t" name="test_ac1"><failure message="ImportError: cannot import
 name 'limit_body' from 'app.main' (/w/src/app/main.py)">E   ImportError: cannot import name 'limit_body' from 'app.main' (/w/src/app/main.py)
tests/holdout/test_issue_7.py:129: ImportError</failure></testcase></testsuite></testsuites>"""

PASSING = """<testsuites><testsuite><testcase classname="t" name="test_ac1"/></testsuite></testsuites>"""


class OwnerSignalTests(SessionTests):
    """/stop and /steer from the owner while a step runs (scripts/signals.py)."""

    def setUp(self) -> None:
        super().setUp()
        self.state = self.wt / "state"

    def test_a_steer_note_goes_to_the_same_session_which_continues(self) -> None:
        signals.add_steer(
            self.state, "app", 1, 77, "the acceptance test is wrong, report it"
        )
        res, calls = self.session(
            [
                {
                    "interrupted": "steer",
                    "session_id": "s",
                    "num_turns": 3,
                    "is_error": True,
                },
                {"result": "reported", "num_turns": 2, "session_id": "s"},
            ]
        )
        self.assertIn("--resume", calls[1][0])
        self.assertIn("the acceptance test is wrong", calls[1][1])
        self.assertEqual([x["comment"] for x in res["steered"]], [77])
        self.assertFalse(signals.steers_waiting(self.state, "app", 1))  # given once
        self.assertFalse(res.get("is_error"))

    def test_a_stop_ends_the_run_with_the_comment_id(self) -> None:
        signals.request_stop(self.state, "app", 1, 88)
        res, calls = self.session(
            [
                {
                    "interrupted": "stop",
                    "session_id": "s",
                    "num_turns": 4,
                    "is_error": True,
                }
            ]
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(res["stopped"], 88)
        self.assertTrue(res["is_error"])

    def test_the_watch_callback_reports_a_new_loop_warning_then_a_steer(self) -> None:
        seen = []
        live = mock.Mock()
        live.state = {
            "warning_id": 1
        }  # a warning from before this session does not count

        def run(
            cmd: list[str], prompt: str, *a: object, interrupt: object = None
        ) -> dict:
            seen.append(interrupt())  # type: ignore[operator]
            live.state["warning_id"] += 1  # the board sees a loop during this run
            seen.append(interrupt())  # type: ignore[operator]
            signals.add_steer(self.state, "app", 1, 5, "x")
            seen.append(interrupt())  # type: ignore[operator]
            return {"result": "ok", "num_turns": 1, "session_id": "s"}

        with mock.patch.object(dispatch, "run_agent", side_effect=run):
            dispatch.agent_session(
                ["c"],
                "p",
                self.wt,
                {},
                live,
                9e12,
                "local",
                self.head,
                1,
                app="app",
                state=self.state,
            )
        self.assertEqual(seen, [None, "loop", "steer"])


class LoopWarningTests(unittest.TestCase):
    def live(self) -> "dispatch.Live":
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return dispatch.Live(Path(tmp.name), "app", 1, "implement", "local")

    def use(
        self, live: "dispatch.Live", name: str, inp: dict, error: bool = False
    ) -> None:
        live.event(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "tool_use", "name": name, "input": inp}]
                },
            }
        )
        live.event(
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "content": "x", "is_error": error}
                    ]
                },
            }
        )

    def test_the_same_call_again_and_again_raises_a_warning_once(self) -> None:
        live = self.live()
        for _ in range(5):
            self.use(live, "ListAgents", {})
        self.assertIn("going in circles", live.state["warning"])
        self.assertEqual(live.state["warning_id"], 1)

    def test_refused_calls_in_a_row_raise_a_warning(self) -> None:
        live = self.live()
        for i in range(6):
            self.use(
                live, "Bash", {"command": f"cat tests/holdout/x{i}.py"}, error=True
            )
        self.assertIn("refused", live.state["warning"])

    def test_normal_work_raises_none(self) -> None:
        live = self.live()
        for i in range(12):
            self.use(live, "Edit" if i % 2 else "Read", {"file_path": f"src/a{i}.py"})
        self.assertEqual(live.state["warning"], "")


class BrokenAcceptanceTestTests(unittest.TestCase):
    """brevkoll#7: acceptance tests that fail inside Starlette whatever the app does."""

    def test_a_failure_raised_in_a_library_without_app_code_is_a_broken_test(
        self,
    ) -> None:
        msg = dispatch.junit_problem(BROKEN)
        self.assertIn("the acceptance tests are broken", msg or "")
        self.assertIn("starlette/datastructures.py", msg or "")

    def test_a_missing_feature_is_the_expected_failure(self) -> None:
        self.assertIsNone(dispatch.junit_problem(MISSING, require_failing=True))

    def test_tests_that_already_pass_prove_nothing_at_the_spec_step_only(self) -> None:
        self.assertIn(
            "already pass", dispatch.junit_problem(PASSING, require_failing=True) or ""
        )
        self.assertIsNone(dispatch.junit_problem(PASSING))


class RunAgentInterruptTests(unittest.TestCase):
    """run_agent with a real child process standing in for Claude Code."""

    def test_an_interrupt_ends_the_process_and_keeps_the_session_id(self) -> None:
        script = (
            "import json,sys,time; sys.stdin.read(); "
            "print(json.dumps({'type':'system','subtype':'init','session_id':'abc'}), flush=True); time.sleep(60)"
        )
        live = mock.Mock()
        asks = iter([None, "stop"])
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(dispatch, "WATCH_EVERY_S", 0.2),
        ):
            t0 = time.time()
            res = dispatch.run_agent(
                [sys.executable, "-c", script],
                "prompt",
                Path(tmp),
                dict(os.environ),
                live,
                time.time() + 120,
                interrupt=lambda: next(asks, "stop"),
            )
        self.assertLess(time.time() - t0, 15)
        self.assertEqual((res["interrupted"], res["session_id"]), ("stop", "abc"))
