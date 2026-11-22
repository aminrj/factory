"""/llm from the phone: only the owner, one switch at a time, through `llm.sh` only, never while a local step runs."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import llm_switch  # noqa: E402
import orchestrate  # noqa: E402
from gitea import GiteaError  # noqa: E402


class FakeG:
    user, bot = "owner", "factory-bot"

    def __init__(self) -> None:
        self.comments: list[dict] = []
        self.vars: dict[str, str] = {}

    def paged(self, path: str) -> list[dict]:
        if path.endswith("/comments"):
            return list(self.comments)
        return [{"number": 69, "title": "Factory status"}]

    def comment(self, repo: str, n: int, body: str, who: str = "factory-bot") -> dict:
        c = {"id": len(self.comments) + 1, "user": {"login": who}, "body": body}
        self.comments.append(c)
        return c

    def call(self, method: str, path: str, body: object = None, ok: tuple[int, ...] = (200,)) -> object:
        name = path.rsplit("/", 1)[1]
        if method == "GET":
            if name not in self.vars:
                raise GiteaError(f"GET {path} -> 404")
            return {"data": self.vars[name]}
        if method == "PUT" and name not in self.vars:
            raise GiteaError(f"PUT {path} -> 404")
        self.vars[name] = body["value"]  # type: ignore[index]
        return None

    def bot_said(self) -> str:
        return self.comments[-1]["body"]


class SwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.g = FakeG()
        self.runs: list[list[str]] = []
        patches = [mock.patch.object(llm_switch, "STATE", Path(self.tmp.name)),
                   mock.patch.object(llm_switch, "run_llm", side_effect=self.fake_llm),
                   mock.patch.dict(os.environ, {"FACTORY_LEASE_REPO": "factory-bot/foreman-state"})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.now = 1000.0
        llm_switch.tick(self.g, now=self.now)  # first run: only commands from now on

    def fake_llm(self, args: list[str], timeout: int = 900) -> tuple[int, str]:
        self.runs.append(args)
        return 0, "Serving:\n  :8081  qwen3.6-35b-a3b"

    def lanes(self, local: list[str], at: float) -> None:
        self.g.vars["FOREMAN_LANES"] = json.dumps({"local": local, "cloud": [], "at": at})

    def owner(self, text: str) -> None:
        self.g.comment("owner/factory", 69, text, who="owner")

    def test_only_the_owners_command_counts(self) -> None:
        self.g.comment("owner/factory", 69, "/llm off", who="factory-bot")  # an agent misusing the bot token
        self.g.comment("owner/factory", 69, "/llm off", who="someone")
        self.assertEqual(llm_switch.tick(self.g, now=self.now + 60), "idle")
        self.assertEqual(self.runs, [])

    def test_status_is_answered_with_the_real_output(self) -> None:
        self.owner("/llm status")
        llm_switch.tick(self.g, now=self.now + 60)
        self.assertIn("qwen3.6-35b-a3b", self.g.bot_said())

    def test_a_switch_waits_for_a_fresh_report_and_an_empty_local_lane(self) -> None:
        self.lanes(["app#7"], at=self.now + 10)
        self.owner("/llm code")
        self.assertEqual(llm_switch.tick(self.g, now=self.now + 60), "waiting for the foreman's next report")
        self.assertIn("I switch when it ends", self.g.bot_said())
        self.assertEqual(json.loads(self.g.vars["FOREMAN_LLM_SWITCH"])["target"], "code")
        self.lanes(["app#7"], at=self.now + 90)
        self.assertEqual(llm_switch.tick(self.g, now=self.now + 120), "waiting for app#7")
        self.assertEqual(self.runs, [])
        self.lanes([], at=self.now + 150)
        self.assertEqual(llm_switch.tick(self.g, now=self.now + 180), "switched to code")
        self.assertEqual(self.runs, [["code"]])
        self.assertEqual(json.loads(self.g.vars["FOREMAN_LLM_SWITCH"]), {})  # the foreman may start local steps again
        self.assertIn("Switched", self.g.bot_said())

    def test_now_switches_while_a_step_runs(self) -> None:
        self.lanes(["app#7"], at=self.now + 50)
        self.owner("/llm 38 now")
        self.assertEqual(llm_switch.tick(self.g, now=self.now + 60), "switched to 38")
        self.assertEqual(self.runs, [["38"]])

    def test_agent_stops_everything_first_and_unknown_targets_are_refused(self) -> None:
        self.owner("/llm agent")
        llm_switch.tick(self.g, now=self.now + 80)
        self.lanes([], at=self.now + 85)
        llm_switch.tick(self.g, now=self.now + 90)
        self.assertEqual(self.runs, [["off", "agent"]])
        self.owner("/llm gemma4")
        llm_switch.tick(self.g, now=self.now + 120)
        self.assertIn("Not understood", self.g.bot_said())
        self.assertEqual(len(self.runs), 1)

    def test_the_only_way_to_load_is_the_owners_llm_script(self) -> None:
        self.assertEqual(set(llm_switch.TARGETS), {"status", "code", "38", "agent", "off"})
        self.assertTrue(llm_switch.LLM.endswith("llm.sh"))


class ForemanGateTests(unittest.TestCase):
    def test_no_new_local_step_while_a_switch_is_pending(self) -> None:
        g = FakeG()
        g.vars["FOREMAN_LLM_SWITCH"] = json.dumps({"target": "code", "since": time.time()})
        with mock.patch.dict(os.environ, {"FACTORY_LEASE": "on", "FACTORY_LEASE_REPO": "factory-bot/foreman-state"}):
            self.assertEqual(orchestrate.model_switch_pending(g), "code")  # type: ignore[arg-type]
            g.vars["FOREMAN_LLM_SWITCH"] = json.dumps({"target": "code", "since": time.time() - 4 * 3600})
            self.assertEqual(orchestrate.model_switch_pending(g), "")  # type: ignore[arg-type]


class RunLlmTests(unittest.TestCase):
    def test_status_is_printed_once(self) -> None:
        done = mock.Mock(returncode=0, stdout="Serving:\n  :8081  qwen3.6-35b-a3b\n", stderr="")
        with mock.patch.object(llm_switch.subprocess, "run", return_value=done) as run:
            code, text = llm_switch.run_llm(["status"])
        self.assertEqual((code, text.count("Serving:"), run.call_count), (0, 1, 1))


class WrongPlaceTests(unittest.TestCase):
    def test_llm_on_a_work_issue_points_to_the_status_issue(self) -> None:
        import tempfile as tf
        import test_loop_smoke as tls
        g = tls.FakeGitea()
        g.paged = lambda path: ([{"number": 69, "title": "Factory status"}] if "/factory/issues?" in path  # type: ignore[method-assign]
                                else list(g.comments))
        g.comment("owner/app", 1, "🛑 CHECKPOINT 3: x\n<!--factory:resume=after_gate3;stop=-->")
        g.comment("owner/app", 1, "/llm code")
        with tf.TemporaryDirectory() as tmp, mock.patch.object(orchestrate, "STATE", Path(tmp)), \
                mock.patch.object(orchestrate, "REPLIED", Path(tmp) / "r.json"):
            orchestrate.answer_unknown_commands(g, "owner/app", 1)  # type: ignore[arg-type]
        self.assertIn("issues/69", g.comments[-1]["body"])
