"""Crash and restart safety: what the owner sees when the orchestrator dies, and /stop during a build.

A BaseException raised from the stubbed agent step escapes process() the way a kill does: no cleanup, the claim comment
stays the last thing the bot said. The lock is released when the process ends; the tests release it the same way.
"""

import fcntl
import gc
import unittest
from pathlib import Path
from unittest import mock

import test_loop_gates as gates
from test_loop_gates import PLAN

import orchestrate


class Crash(BaseException):
    """Stands for SIGKILL, a reboot or the out-of-memory killer: nothing in the orchestrator catches it."""


class CrashTests(unittest.TestCase):
    # the harness of the gate tests, without re-running their tests
    setUp = gates.GateTests.setUp
    commit_file = gates.GateTests.commit_file
    fake_ci = gates.GateTests.fake_ci
    checkpoint = gates.GateTests.checkpoint
    go = gates.GateTests.go
    bot = gates.GateTests.bot
    steps = gates.GateTests.steps
    prepare_build = gates.GateTests.prepare_build
    orchestrate_state = gates.GateTests.orchestrate_state
    AGENT = gates.GateTests.AGENT

    def fake_dispatch(self, ctx: orchestrate.Ctx, step: str, note: str | None = None, note_kind: str = "ci") -> str:
        if getattr(self, "to_raise", None):
            raise self.to_raise.pop(0)
        if getattr(self, "on_dispatch", None):
            self.on_dispatch(step)
        return gates.GateTests.fake_dispatch(self, ctx, step, note, note_kind)

    def die_during(self, text: str = "/go minutes=2") -> None:
        """Run one /go in which the process dies, then let the 'OS' release its lock."""
        with self.assertRaises(Crash) as caught:
            self.go(text)
        del caught
        gc.collect()

    def lock_path(self) -> Path:
        return orchestrate.STATE / "app-1.lock"

    # -- the process dies after it claimed the command
    def test_a_dead_process_is_recovered_with_a_checkpoint_the_owner_can_answer(self) -> None:
        self.commit_file("research.md", "# Research\n")
        self.checkpoint("after_research_questions", "2b")
        self.g.comment("owner/app", 1, "My answers: yes to both.")
        self.agent_files["plan"] = PLAN
        self.to_raise = [Crash()]
        self.die_during()
        self.assertTrue(self.bot().startswith(orchestrate.CLAIM_PREFIX))  # the last word is a claim: silent until now
        self.assertIsNone(orchestrate.decide(self.g, "owner/app", 1))  # type: ignore[arg-type]  # a /go would be ignored
        self.assertEqual(orchestrate.stuck_handler(self.g, "owner/app", 1), "after_research_questions")  # type: ignore[arg-type]
        self.assertTrue(orchestrate.recover_interrupted(self.g, "owner/app", 1))  # type: ignore[arg-type]
        self.assertIn("interrupted", self.bot())
        self.assertIn("resume=after_research_questions", self.bot())
        self.assertTrue(any("did not finish" in str(c) for c in self.notify.call_args_list))
        # the owner answers it and the work goes on from where it stopped
        self.assertEqual(self.go("/go"), "ran after_research_questions")
        self.assertIn("CHECKPOINT 3", self.bot())

    def test_nothing_is_recovered_while_a_process_still_runs_the_item(self) -> None:
        self.commit_file("research.md", "# Research\n")
        self.checkpoint("after_research_questions", "2b")
        self.g.comment("owner/app", 1, "My answers: yes to both.")
        self.to_raise = [Crash()]
        self.die_during()
        orchestrate.STATE.mkdir(exist_ok=True)
        held = self.lock_path().open("w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process is running it
        before = len(self.g.comments)
        self.assertFalse(orchestrate.recover_interrupted(self.g, "owner/app", 1))  # type: ignore[arg-type]
        self.assertEqual(len(self.g.comments), before)
        held.close()

    def test_recovery_happens_once_and_a_normal_checkpoint_is_not_a_crash(self) -> None:
        self.checkpoint("after_gate2", "2")
        self.assertFalse(orchestrate.recover_interrupted(self.g, "owner/app", 1))  # type: ignore[arg-type]
        self.commit_file("research.md", "# Research\n")
        self.checkpoint("after_research_questions", "2b")
        self.g.comment("owner/app", 1, "My answers: yes to both.")
        self.to_raise = [Crash()]
        self.die_during()
        self.assertTrue(orchestrate.recover_interrupted(self.g, "owner/app", 1))  # type: ignore[arg-type]
        self.assertFalse(orchestrate.recover_interrupted(self.g, "owner/app", 1))  # type: ignore[arg-type]

    def test_an_unexpected_error_does_not_leave_the_item_silent_either(self) -> None:
        self.commit_file("research.md", "# Research\n")
        self.checkpoint("after_research_questions", "2b")
        self.g.comment("owner/app", 1, "My answers: yes to both.")
        self.agent_files["plan"] = PLAN
        self.to_raise = [RuntimeError("boom")]  # an ordinary exception: process() reports it and ends
        self.assertEqual(self.go(), "error")
        self.assertIn("Unexpected error", self.bot())
        self.assertIsNone(orchestrate.decide(self.g, "owner/app", 1))  # type: ignore[arg-type]
        self.assertTrue(orchestrate.recover_interrupted(self.g, "owner/app", 1))  # type: ignore[arg-type]
        self.assertEqual(self.go("/go"), "ran after_research_questions")  # after the fix, the owner can retry

    def test_a_crash_while_reworking_the_plan_keeps_the_owners_note(self) -> None:
        self.commit_file("plan.md", PLAN, email=self.AGENT)
        self.checkpoint("after_gate3", "3", extra=";rework=ok")
        self.agent_files["plan"] = PLAN + "\nrevised: deep teal accent\n"  # a revision always changes the file
        self.to_raise = [Crash()]
        self.die_during("/rework use a deep teal accent")
        self.assertTrue(orchestrate.recover_interrupted(self.g, "owner/app", 1))  # type: ignore[arg-type]
        self.assertEqual(self.go("/go"), "ran after_plan_rework")
        self.assertIn("deep teal accent", self.dispatched[-1][1] or "")  # the retry still has what the owner typed
        self.assertNotIn("rework_note", self.orchestrate_state())  # and forgets it once the plan is back

    def test_an_item_with_a_live_process_is_running_not_stuck(self) -> None:
        orchestrate.STATE.mkdir(exist_ok=True)
        self.assertFalse(orchestrate.item_is_running("app", 1))  # no lock file yet
        held = self.lock_path().open("w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertTrue(orchestrate.item_is_running("app", 1))
        held.close()
        self.assertFalse(orchestrate.item_is_running("app", 1))  # the process ended: the lock is free again

    def test_two_ticks_never_run_the_same_item_at_once(self) -> None:
        self.checkpoint("after_gate2", "2")
        self.g.comment("owner/app", 1, "/go")
        orchestrate.STATE.mkdir(exist_ok=True)
        held = self.lock_path().open("w")
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertEqual(orchestrate.process(self.g, "owner/app", 1), "busy")  # type: ignore[arg-type]
        self.assertEqual(self.dispatched, [])
        held.close()

    # -- a retry resumes after the step that already worked
    def rework_at_the_review_checkpoint(self, note: str) -> str:
        self.commit_file("plan.md", PLAN.replace("draft", "approved"), email=gates.WEB_EDITOR)
        self.checkpoint("after_gate4", "4", extra=";rework=ok")
        return self.go(f"/rework {note}")

    def test_a_validate_that_fails_after_the_rework_does_not_repeat_the_implement_step(self) -> None:
        failures = [orchestrate.StepFailed("`validate` failed: usage limit")]

        def validate_fails_once(step: str) -> None:
            if step == "validate" and failures:
                raise failures.pop()

        self.on_dispatch = validate_fails_once
        self.assertEqual(self.rework_at_the_review_checkpoint("make it warmer"), "failed")
        self.assertEqual(self.steps(), ["implement"])  # the stub raised before validate was recorded
        self.assertEqual(self.go("/go"), "ran after_staging_rework")
        self.assertEqual(self.steps().count("implement"), 1)  # not run again
        self.assertEqual(self.steps().count("validate"), 1)
        self.assertIn("CHECKPOINT 4", self.bot())
        self.assertFalse(self.orchestrate_state()["rework_implemented"])  # finished: the next rework starts clean

    def test_a_stale_flag_never_swallows_a_new_rework_request(self) -> None:
        import json

        orchestrate.STATE.mkdir(exist_ok=True)
        (orchestrate.STATE / "app-1.json").write_text(json.dumps({"rework_implemented": True}))  # left over from before
        self.assertEqual(self.rework_at_the_review_checkpoint("a new request"), "ran after_staging_rework")
        self.assertEqual(self.steps(), ["implement", "validate"])  # implement did run for the new request

    def test_a_new_rework_request_runs_the_implement_step_again(self) -> None:
        self.assertEqual(self.rework_at_the_review_checkpoint("first"), "ran after_staging_rework")
        self.assertEqual(self.steps(), ["implement", "validate"])
        self.g.comment("owner/app", 1, "/rework second")
        self.assertEqual(orchestrate.process(self.g, "owner/app", 1), "ran after_staging_rework")  # type: ignore[arg-type]
        self.assertEqual(self.steps(), ["implement", "validate", "implement", "validate"])

    # -- /stop is heard during a build
    def test_stop_written_during_a_phase_ends_the_build_before_the_next_one(self) -> None:
        self.prepare_build()
        calls: list[str] = []

        def owner_types_stop(step: str) -> None:
            calls.append(step)
            if len(calls) == 1:
                self.g.comment("owner/app", 1, "/stop")

        self.on_dispatch = owner_types_stop
        self.assertEqual(self.go(), "stopped")
        self.assertEqual(self.steps(), ["implement"])  # phase 2 never started
        self.stage_down.assert_called_once_with("app")
        self.assertIn("between two steps", self.bot())

    def test_stop_written_during_the_last_phase_prevents_validation(self) -> None:
        self.prepare_build()
        calls: list[str] = []

        def owner_types_stop(step: str) -> None:
            calls.append(step)
            if len(calls) == 2:
                self.g.comment("owner/app", 1, "/stop")

        self.on_dispatch = owner_types_stop
        self.assertEqual(self.go(), "stopped")
        self.assertEqual(self.steps(), ["implement", "implement"])
        self.assertNotIn("validate", self.steps())

    def test_a_stop_from_before_the_go_does_not_cancel_the_next_run(self) -> None:
        self.prepare_build()
        self.g.comment("owner/app", 1, "/stop")  # an old one: older than the /go that follows
        self.checkpoint("after_gate3", "3")  # a newer checkpoint: the old /stop belongs to the past
        self.assertEqual(self.go(), "ran after_gate3")
        self.assertEqual(self.steps(), ["implement", "implement", "validate"])


if __name__ == "__main__":
    unittest.main()
