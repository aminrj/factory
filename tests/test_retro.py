"""Retrospectives: an agent run pauses every few turns, writes five lines, and the dispatcher checks them against the
worktree (2026-11-22: local runs went in circles for 30-70 minutes per item and nothing noticed)."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch  # noqa: E402

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

        def run(cmd: list[str], prompt: str, *a: object) -> dict:
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
        with mock.patch.object(dispatch, "run_agent", new=fake):
            res = dispatch.agent_session(
                ["claude", "-p"], "do it", self.wt, {}, live, 9e12, model, self.head, 1
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
