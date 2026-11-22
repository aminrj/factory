"""The night shift (scripts/night.py): what it may pass on its own, and a walk through the real handlers."""

import os
import subprocess
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import night  # noqa: E402
import orchestrate  # noqa: E402
import test_loop_gates as tlg  # noqa: E402

NOW = datetime(2026, 11, 22, 1, 0, tzinfo=UTC)


def cp(label: str, resume: str, body: str = "", age_min: int = 60) -> dict:
    return {"id": 50, "body": f"🛑 CHECKPOINT {label}: x\n\n{body}\n<!--factory:resume={resume};stop=-->",
            "created_at": (NOW - timedelta(minutes=age_min)).isoformat().replace("+00:00", "Z")}


def may(c: dict, labels: tuple[str, ...] = (night.LABEL,), recent: list[dict] | None = None) -> str | None:
    return night.may_pass(c, orchestrate.parse_marker(c["body"]), set(labels), recent or [c], NOW)


class PolicyTests(unittest.TestCase):
    def test_only_the_gates_before_the_code_and_only_with_the_label(self) -> None:
        for label, resume in (("1", "after_gate1"), ("2", "after_gate2"), ("2b", "after_research_questions"),
                              ("3", "after_gate3")):
            self.assertIsNotNone(may(cp(label, resume)), label)
            self.assertIsNone(may(cp(label, resume), labels=("stage/building",)), label)
        self.assertIsNone(may(cp("4", "after_gate4")))
        self.assertIsNone(may(cp("after_gate4-fix", "after_gate4")))
        self.assertIsNone(may(cp("x-failed", "manual")))

    def test_a_question_for_the_owner_is_never_answered_by_the_night_shift(self) -> None:
        self.assertIsNone(may(cp("2-fix", "after_gate2")))
        self.assertIsNotNone(may(cp("after_gate3-retry", "after_gate3")))

    def test_a_busy_model_is_tried_again_later_and_not_forever(self) -> None:
        busy = cp("after_gate3-blocked", "after_gate3", "[class: model_unavailable] loading")
        self.assertIsNotNone(may(busy))
        self.assertIsNone(may(cp("after_gate3-blocked", "after_gate3", "[class: model_unavailable]", age_min=5)))
        self.assertIsNone(may(cp("after_gate3-blocked", "after_gate3", "[class: credit_exhausted]")))
        self.assertIsNone(may(busy, recent=[busy] * (night.BLOCKED_MAX + 1)))

    def test_the_window_wraps_midnight(self) -> None:
        with mock.patch.dict(os.environ, {"FACTORY_NIGHT_WINDOW": "22-07", "FACTORY_TZ": "UTC", "FACTORY_NIGHT": "on"}):
            self.assertTrue(night.in_window(NOW.replace(hour=23)))
            self.assertTrue(night.in_window(NOW.replace(hour=3)))
            self.assertFalse(night.in_window(NOW.replace(hour=7)))
            self.assertFalse(night.in_window(NOW.replace(hour=15)))
        with mock.patch.dict(os.environ, {"FACTORY_NIGHT_WINDOW": "always", "FACTORY_NIGHT": "on"}):
            self.assertTrue(night.in_window(NOW.replace(hour=15)))

    def test_risky_words_in_a_plan(self) -> None:
        self.assertEqual(night.risky_mentions("Phase 1: edit src/app/ui.py and the template"), [])
        self.assertIn("deploy/", night.risky_mentions("Phase 2: change deploy/base/deployment.yaml"))
        self.assertIn("auth", night.risky_mentions("add an auth check"))
        self.assertEqual(night.risky_files(["src/app/ui.py", ".gitea/workflows/ci.yml", "uv.lock"]),
                         [".gitea/workflows/ci.yml", "uv.lock"])

    def test_the_morning_summary_comes_once_after_the_window(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"FACTORY_NIGHT_WINDOW": "22-07", "FACTORY_TZ": "UTC"}):
            state = Path(tmp)
            night.record(state, NOW, "app#3", "after_gate3", "ran after_gate3")
            self.assertIsNone(night.summary_due(state, NOW))  # still night
            rows = night.summary_due(state, NOW.replace(hour=7, minute=5))
            self.assertEqual([r["item"] for r in rows or []], ["app#3"])
            self.assertIsNone(night.summary_due(state, NOW.replace(hour=8)))


class NightLoopTests(unittest.TestCase):
    """The real handlers with the night shift on: it passes the gates before the code and stops for the owner."""

    setUp = tlg.GateTests.setUp
    def commit_file(self, rel: str, text: str, email: str = tlg.BOT) -> None:
        # like dispatch's worktree: fast-forward to what the policy pushed before the agent commits
        if subprocess.run(["git", "ls-remote", "--heads", "origin", "agent/issue-1"], cwd=self.clone,
                          capture_output=True, text=True).stdout:
            subprocess.run(["git", "pull", "-q", "--ff-only", "origin", "agent/issue-1"], cwd=self.clone, check=True)
        tlg.GateTests.commit_file(self, rel, text, email)
    fake_dispatch = tlg.GateTests.fake_dispatch
    fake_ci = tlg.GateTests.fake_ci
    checkpoint = tlg.GateTests.checkpoint
    bot = tlg.GateTests.bot
    steps = tlg.GateTests.steps

    def tick(self) -> str:
        env = {"FACTORY_NIGHT": "on", "FACTORY_NIGHT_WINDOW": "always"}
        with mock.patch.dict(os.environ, env):
            orchestrate.NIGHT["taken"] = False
            return orchestrate.process(self.g, "owner/app", 1)  # type: ignore[arg-type]

    def origin_log(self) -> list[str]:
        git_out = subprocess.run(["git", "log", "--format=%an|%B%x00", "origin/agent/issue-1"], cwd=self.clone,
                                 capture_output=True, text=True, check=True).stdout
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=self.clone, check=True)
        return [x.strip() for x in git_out.split("\0") if x.strip()]

    def test_without_the_label_nothing_happens(self) -> None:
        self.commit_file("threat-model.md", "| T1 | x | PROPOSED |\n")
        self.checkpoint("after_gate2", "2")
        self.assertEqual(self.tick(), "waiting")
        self.assertEqual(self.dispatched, [])

    def test_the_threat_model_is_accepted_by_policy_and_research_runs_on_the_local_model(self) -> None:
        self.g.labels.append(night.LABEL)
        self.commit_file("threat-model.md", "| T1 | x | PROPOSED |\n| T2 | y | PROPOSED |\n")
        self.agent_files["research"] = "# Research\n"
        self.agent_files["plan"] = tlg.PLAN
        self.checkpoint("after_gate2", "2")
        self.assertEqual(self.tick(), "ran after_gate2")
        self.assertEqual(self.steps(), ["research", "plan"])
        self.assertTrue(all(c["model"] == "local" for c in self.calls))
        claim = next(c["body"] for c in self.g.comments if c["body"].startswith("▶"))
        self.assertIn("Night shift", claim)
        subprocess.run(["git", "fetch", "-q", "origin"], cwd=self.clone, check=True)
        tm = subprocess.run(["git", "show", "origin/agent/issue-1:thoughts/1/threat-model.md"], cwd=self.clone,
                            capture_output=True, text=True, check=True).stdout
        self.assertNotIn("PROPOSED", tm)
        log = subprocess.run(["git", "log", "-1", "--format=%an|%B", "origin/agent/issue-1", "--",
                              "thoughts/1/threat-model.md"], cwd=self.clone, capture_output=True, text=True).stdout
        self.assertTrue(log.startswith("factory-orchestrator|"))
        self.assertIn(night.TRAILER, log)
        self.assertIn("CHECKPOINT 3", self.bot())
        self.assertEqual(self.notify.call_args.kwargs.get("priority"), 1)  # silent at night

    def test_a_plan_that_touches_deploy_is_left_to_the_owner(self) -> None:
        self.g.labels.append(night.LABEL)
        self.commit_file("plan.md", tlg.PLAN + "\n## Phase 3: edit deploy/base/deployment.yaml\n")
        self.checkpoint("after_gate3", "3")
        self.assertEqual(self.tick(), "needs human")
        self.assertIn("risky", self.bot())
        self.assertEqual(self.dispatched, [])

    def test_one_night_item_per_tick(self) -> None:
        self.g.labels.append(night.LABEL)
        self.commit_file("threat-model.md", "| T1 | x | PROPOSED |\n")
        self.checkpoint("after_gate2", "2")
        orchestrate.NIGHT["taken"] = True
        with mock.patch.dict(os.environ, {"FACTORY_NIGHT": "on", "FACTORY_NIGHT_WINDOW": "always"}):
            self.assertEqual(orchestrate.process(self.g, "owner/app", 1), "waiting")  # type: ignore[arg-type]

    def test_checkpoint_4_is_never_passed(self) -> None:
        self.g.labels.append(night.LABEL)
        self.checkpoint("after_gate4", "4")
        self.assertEqual(self.tick(), "waiting")


if __name__ == "__main__":
    unittest.main()


class PickUpRetryTests(unittest.TestCase):
    """brevkoll#7: the night pick-up timed out once and got a checkpoint only a session could answer."""

    setUp = tlg.GateTests.setUp
    fake_dispatch = tlg.GateTests.fake_dispatch
    fake_ci = tlg.GateTests.fake_ci
    bot = tlg.GateTests.bot

    def test_a_failed_pick_up_is_retried_with_go(self) -> None:
        with mock.patch.object(orchestrate, "dispatch", side_effect=orchestrate.StepFailed("`spec` timed out")):
            orchestrate.adopt(self.g, "owner/app", 1, "a bug")  # type: ignore[arg-type]
        cp = self.bot()
        self.assertIn("CHECKPOINT spec-retry", cp)
        self.assertIn("resume=pick_up", cp)
        self.assertIsNotNone(may(dict(cp_from(cp))))  # the night shift may retry it
        self.g.comment("owner/app", 1, "/go model=fast")
        self.assertEqual(orchestrate.process(self.g, "owner/app", 1), "ran pick_up")  # type: ignore[arg-type]
        self.assertEqual(self.steps_run(), ["spec"])
        self.assertIn("CHECKPOINT 1", self.bot())

    def steps_run(self) -> list[str]:
        return [s for s, _ in self.dispatched]

    def test_a_busy_model_blocks_without_spending_a_retry(self) -> None:
        err = orchestrate.StepFailed("`spec` failed: [class: model_unavailable] loading")
        with mock.patch.object(orchestrate, "dispatch", side_effect=err):
            orchestrate.adopt(self.g, "owner/app", 1, "a bug")  # type: ignore[arg-type]
        self.assertIn("CHECKPOINT spec-blocked", self.bot())
        self.assertNotIn("retry-of", self.bot())


def cp_from(body: str) -> dict:
    return {"id": 99, "body": body, "created_at": "2026-11-22T00:00:00Z"}
