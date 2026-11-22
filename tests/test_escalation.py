"""A failed local step goes to Claude once by itself, inside the item's cloud budget, and says so (2026-11-22)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import orchestrate  # noqa: E402


class Ctx:
    def __init__(self, model: str = "local", auto: bool = False) -> None:
        self.app, self.n, self.model, self.auto = "app", 1, model, auto
        self.st: dict = {}
        self.comments: list[str] = []

    def state(self) -> dict:
        return dict(self.st)

    def save(self, st: dict) -> None:
        self.st = dict(st)

    def comment(self, body: str) -> dict:
        self.comments.append(body)
        return {"id": len(self.comments)}


class EscalationTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        for p in (
            mock.patch.object(orchestrate, "DATA", self.data),
            mock.patch.object(orchestrate, "log"),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.models: list[str] = []

    def run_step(self, ctx: Ctx, errors: list[Exception | None]) -> str:
        def once(
            c: Ctx, step: str, note: str | None = None, note_kind: str = "ci"
        ) -> str:
            self.models.append(c.model)
            err = errors.pop(0)
            if err:
                raise err
            return "ok"

        with mock.patch.object(orchestrate, "dispatch_once", side_effect=once):
            return orchestrate.dispatch(ctx, "implement")  # type: ignore[arg-type]

    def test_a_failed_local_step_runs_again_on_fast_and_says_so(self) -> None:
        ctx = Ctx()
        err = orchestrate.StepFailed(
            "`implement` failed:\nstalled: no change in the worktree [class: stalled]"
        )
        self.assertEqual(self.run_step(ctx, [err, None]), "ok")
        self.assertEqual(self.models, ["local", "fast"])
        self.assertEqual(ctx.model, "local")  # the next step is local again
        self.assertIn("running it again on `fast`", ctx.comments[0])
        self.assertIn("stalled", ctx.comments[0])
        self.assertEqual(ctx.st["escalations"], 1)

    def test_no_switch_for_infrastructure_problems_the_night_or_cloud_runs(
        self,
    ) -> None:
        infra = orchestrate.StepFailed(
            "`implement` failed: 503 [class: model_unavailable]"
        )
        plain = orchestrate.StepFailed("`implement` failed: REFUSED: x")
        for ctx, err in (
            (Ctx(), infra),
            (Ctx(auto=True), plain),
            (Ctx(model="fast"), plain),
        ):
            self.models = []
            with self.assertRaises(orchestrate.StepFailed):
                self.run_step(ctx, [err])
            self.assertEqual(len(self.models), 1)
            self.assertEqual(ctx.comments, [])

    def test_the_budget_and_the_count_are_respected(self) -> None:
        with (self.data / "agent_runs.jsonl").open("w") as fh:
            fh.write(
                json.dumps(
                    {"repo": "app", "issue": 1, "model": "fast", "cost_usd": 5.5}
                )
                + "\n"
            )
        with self.assertRaises(orchestrate.StepFailed):
            self.run_step(Ctx(), [orchestrate.StepFailed("REFUSED: x")])
        (self.data / "agent_runs.jsonl").unlink()
        ctx = Ctx()
        ctx.st["escalations"] = orchestrate.MAX_ESCALATIONS
        with self.assertRaises(orchestrate.StepFailed):
            self.run_step(ctx, [orchestrate.StepFailed("REFUSED: x")])

    def test_a_failure_on_fast_too_is_the_usual_failure(self) -> None:
        ctx = Ctx()
        with self.assertRaisesRegex(orchestrate.StepFailed, "second"):
            self.run_step(
                ctx,
                [
                    orchestrate.StepFailed("REFUSED: first"),
                    orchestrate.StepFailed("REFUSED: second"),
                ],
            )


if __name__ == "__main__":
    unittest.main()


class BrokenTestExitTests(unittest.TestCase):
    def test_a_broken_acceptance_test_goes_to_the_owner_not_to_claude(self) -> None:
        ctx = Ctx()
        done = mock.Mock(
            returncode=orchestrate.BROKEN_TEST_EXIT,
            stdout="",
            stderr="I did not start an agent: broken",
        )
        with (
            mock.patch.object(orchestrate.subprocess, "run", return_value=done),
            mock.patch.object(orchestrate, "log"),
        ):
            with self.assertRaisesRegex(
                orchestrate.NeedsHuman, "did not start an agent"
            ):
                orchestrate.dispatch(ctx, "validate")  # type: ignore[arg-type]
        self.assertEqual(
            ctx.comments, []
        )  # no switch to fast: the test is wrong, not the model


class StopNowTests(unittest.TestCase):
    def test_a_stopped_step_raises_step_stopped_with_the_comment_and_is_not_escalated(
        self,
    ) -> None:
        ctx = Ctx()
        done = mock.Mock(
            returncode=orchestrate.STOPPED_EXIT,
            stdout="",
            stderr="Stopped by your /stop (comment 1262) after 3.0 min, while `implement` was running.",
        )
        with (
            mock.patch.object(orchestrate.subprocess, "run", return_value=done),
            mock.patch.object(orchestrate, "log"),
        ):
            with self.assertRaises(orchestrate.StepStopped) as e:
                orchestrate.dispatch(ctx, "implement")  # type: ignore[arg-type]
        self.assertEqual(e.exception.comment_id, 1262)
        self.assertEqual(ctx.comments, [])

    def test_the_item_waits_at_a_stopped_checkpoint_instead_of_ending(self) -> None:
        posted = []

        def boom(ctx: object) -> None:
            raise orchestrate.StepStopped(1262, "Nothing was pushed.")

        with (
            mock.patch.dict(orchestrate.HANDLERS, {"after_gate3": boom}),
            mock.patch.object(
                orchestrate, "checkpoint", side_effect=lambda *a, **k: posted.append(a)
            ),
            mock.patch.object(orchestrate, "stop_item") as stop_item,
        ):
            out = orchestrate.run_handler(
                None, "owner/app", 1, Ctx(), {}, "after_gate3"
            )  # type: ignore[arg-type]
        self.assertEqual(out, "step stopped")
        stop_item.assert_not_called()
        self.assertEqual(posted[0][1], "after_gate3-stopped")
        self.assertIn("comment 1262", posted[0][3])
        self.assertEqual(posted[0][4], "after_gate3")  # `/go` runs the step again


class PhaseCountTests(unittest.TestCase):
    def test_placeholder_and_deferred_phases_are_not_built(self) -> None:
        plan = (
            "# Plan\n\n## Phase 1: read the version\n\nx\n\n## Phase 2: (none)\n\n## Phase 3: -\n\n"
            "## Deferred\n\n## Phase 4: later\n"
        )
        self.assertEqual(orchestrate.phases(plan), 1)
        self.assertEqual(orchestrate.phases("## Phase 1: a\n## Phase 2: b\n"), 2)
        self.assertEqual(
            orchestrate.phases("## Phase 1\n"), 1
        )  # a heading without a title still counts
