"""Work lanes: the tick hands steps to their own processes, local work goes to the local lane, cloud only on request."""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import lanes  # noqa: E402
import orchestrate  # noqa: E402
import status  # noqa: E402
import test_loop_gates as tlg  # noqa: E402


class LaneBookTests(unittest.TestCase):
    def test_a_spawned_item_counts_while_its_process_lives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FACTORY_SPAWN": "popen"}):
            state = Path(tmp)
            script = state / "sleep.py"
            script.write_text("import time; time.sleep(30)\n")
            rec = lanes.spawn(state, "app", 3, "local", [], "plan", script)
            self.assertEqual([r["n"] for r in lanes.busy(state, "local")], [3])
            self.assertFalse(lanes.has_room(state, "local"))
            self.assertTrue(lanes.has_room(state, "cloud"))
            self.assertTrue(lanes.item_running(state, "app", 3))
            os.kill(rec["pid"], 9)
            os.waitpid(rec["pid"], 0)
            self.assertEqual(lanes.running(state), [])  # a dead process frees its slot
            self.assertFalse((state / "lanes" / "app-3.json").exists())

    def test_the_child_gets_the_factory_environment_and_nothing_else(self) -> None:
        env = {"FACTORY_ROLE": "primary", "GIT_CONFIG_COUNT": "2", "GITEA_TOKEN_FILE": "/x", "SECRET_THING": "no",
               "WORK": "/w", "PATH": "/bin"}
        with mock.patch.dict(os.environ, env, clear=True):
            child = lanes.child_env()
        self.assertNotIn("SECRET_THING", child)
        self.assertEqual(child["FACTORY_CHILD"], "1")
        self.assertEqual(child["GITEA_TOKEN_FILE"], "/x")

    def test_lanes_by_model(self) -> None:
        self.assertEqual([lanes.lane_of(m) for m in (None, "local", "fast", "smart", "cheap", "grok")],
                         ["local", "local", "cloud", "cloud", "cloud", "cloud"])


class HandOffTests(unittest.TestCase):
    setUp = tlg.GateTests.setUp
    commit_file = tlg.GateTests.commit_file
    fake_dispatch = tlg.GateTests.fake_dispatch
    fake_ci = tlg.GateTests.fake_ci
    checkpoint = tlg.GateTests.checkpoint

    def tick(self, command: str) -> tuple[str, mock.Mock]:
        self.g.comment("owner/app", 1, command)
        spawn = mock.Mock(return_value={"unit": "u"})
        with mock.patch.dict(os.environ, {"FACTORY_LANES": "on"}), mock.patch.object(lanes, "spawn", spawn):
            os.environ.pop("FACTORY_CHILD", None)
            return orchestrate.process(self.g, "owner/app", 1), spawn  # type: ignore[arg-type]

    def test_a_plain_go_starts_the_item_in_the_local_lane_and_the_tick_runs_nothing(self) -> None:
        self.checkpoint("after_gate3", "3")
        result, spawn = self.tick("/go")
        self.assertEqual(result, "started in the local lane")
        self.assertEqual(spawn.call_args.args[3], "local")
        self.assertEqual(spawn.call_args.args[4], ["run", "app", "1"])
        self.assertEqual(self.calls, [])  # no agent step inside the tick

    def test_a_cloud_model_goes_to_the_cloud_lane_even_when_the_local_one_is_full(self) -> None:
        self.checkpoint("after_gate3", "3")
        full = [{"app": "app", "n": 9, "lane": "local", "started": time.time(), "what": "plan"}]
        with mock.patch.object(lanes, "running", return_value=full):
            result, spawn = self.tick("/go model=fast")
        self.assertEqual(result, "started in the cloud lane")

    def test_a_full_local_lane_leaves_the_command_waiting(self) -> None:
        self.checkpoint("after_gate3", "3")
        full = [{"app": "app", "n": 9, "lane": "local", "started": time.time(), "what": "plan"}]
        with mock.patch.object(lanes, "running", return_value=full):
            result, spawn = self.tick("/go")
        self.assertTrue(result.startswith("queued: the local lane is full (app#9"))
        spawn.assert_not_called()

    def test_stop_and_refusals_are_answered_in_the_tick(self) -> None:
        self.checkpoint("after_gate3", "3")
        result, spawn = self.tick("/go model=gpt5")
        self.assertEqual(result, "refused")
        spawn.assert_not_called()

    def test_the_status_job_says_why_a_command_waits(self) -> None:
        view = {"running": False, "app": "app", "n": 1}
        full = [{"app": "app", "n": 9, "lane": "local", "started": time.time(), "what": "plan"}]
        with mock.patch.dict(os.environ, {"FACTORY_LANES": "on"}), mock.patch.object(lanes, "running", return_value=full):
            os.environ.pop("FACTORY_CHILD", None)
            text = status.ack_text(view, {"body": "/go"}, None, None)
            cloud = status.ack_text(view, {"body": "/go model=fast"}, None, None)
        self.assertIn("The local lane is busy with app#9", text or "")
        self.assertIn("model=fast", text or "")
        self.assertIsNone(cloud)  # the cloud lane has room: the tick starts it, no notice needed


if __name__ == "__main__":
    unittest.main()


class ManualGateAckTests(unittest.TestCase):
    def test_no_promise_to_start_at_a_manual_gate(self) -> None:
        view = {"running": False, "app": "app", "n": 1,
                "pivot": {"body": "🛑 CHECKPOINT x-failed: y\n<!--factory:resume=manual;stop=-->"}}
        full = [{"app": "app", "n": 9, "lane": "local", "started": time.time(), "what": "plan"}]
        with mock.patch.dict(os.environ, {"FACTORY_LANES": "on"}), mock.patch.object(lanes, "running", return_value=full):
            os.environ.pop("FACTORY_CHILD", None)
            self.assertIsNone(status.ack_text(view, {"body": "/go"}, None, None))
