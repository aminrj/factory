"""Starting work: the owner's `/go` on an issue the factory has not started (brevkoll#29, 2026-11-22: a bug filed and
`/go model=fast` written under it got no answer at all, because only labeled issues were watched)."""

import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import orchestrate  # noqa: E402
from test_loop_smoke import FakeGitea  # noqa: E402


class NowGitea(FakeGitea):
    """Comments stamped with the real time (the start window is measured from now)."""

    def __init__(self) -> None:
        super().__init__()
        self.labels = ["bug"]
        self._tick = datetime.now(UTC) - timedelta(hours=1)

    def paged(self, path: str) -> list[dict]:
        if "/issues?" in path:
            return [{"number": 1, "labels": [{"name": n} for n in self.labels], "comments": len(self.comments)}]
        return super().paged(path)

    def get(self, path: str):  # noqa: ANN201
        if path.endswith("/issues/1"):
            return {"number": 1, "title": "File too large"}
        return super().get(path)


class StartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.g = NowGitea()
        self.adopt = mock.Mock()
        patches = [mock.patch.object(orchestrate, "STATE", base / "state"),
                   mock.patch.object(orchestrate, "REPLIED", base / "replied.json"),
                   mock.patch.object(orchestrate, "log"), mock.patch.object(orchestrate, "notify"),
                   mock.patch.object(orchestrate, "commit_ops"), mock.patch.object(orchestrate, "refresh_board"),
                   mock.patch.object(orchestrate, "adopt", new=self.adopt),
                   mock.patch.dict(os.environ, {"FACTORY_LANES": "off"})]
        for p in patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patches])

    def process(self) -> str:
        return orchestrate.process(self.g, "owner/app", 1)  # type: ignore[arg-type]

    def test_an_issue_with_a_comment_is_watched_even_without_a_stage_label(self) -> None:
        self.assertEqual(orchestrate.active_issues(self.g), [])  # type: ignore[arg-type]
        self.g.comment("owner/app", 1, "/go")
        self.assertEqual(orchestrate.active_issues(self.g), [("owner/app", 1)])  # type: ignore[arg-type]

    def test_go_starts_the_issue_with_the_model_it_names(self) -> None:
        go = self.g.comment("owner/app", 1, "/go model=fast investigate and fix")
        self.assertEqual(self.process(), "started")
        self.adopt.assert_called_once()
        self.assertEqual(self.adopt.call_args.kwargs["go"]["id"], go["id"])

    def test_auto_model_grok_starts_and_does_not_stop_at_the_plan(self) -> None:
        opts = orchestrate.command_options("/auto model=grok")
        self.assertEqual((opts.model, opts.tier, opts.errors), ("grok", "auto", []))
        self.assertIsNone(orchestrate.command_options("/go model=grok").tier)
        go = self.g.comment("owner/app", 1, "/auto model=grok")
        self.assertEqual(self.process(), "started")
        self.assertEqual(self.adopt.call_args.kwargs["go"]["id"], go["id"])

    def test_a_plain_comment_starts_nothing(self) -> None:
        self.g.comment("owner/app", 1, "I saw this on Firefox too.")
        self.assertEqual(self.process(), "waiting")
        self.adopt.assert_not_called()

    def test_an_old_go_is_history_not_a_request(self) -> None:
        self.g._tick = datetime.now(UTC) - timedelta(hours=30)
        self.g.comment("owner/app", 1, "/go")
        self.assertEqual(self.process(), "waiting")

    def test_a_started_issue_is_not_started_again(self) -> None:
        go = self.g.comment("owner/app", 1, "/go")
        self.g.comment("owner/app", 1, f"▶ Picked up `/go` (comment {go['id']}): starting this issue.")
        self.assertIsNone(orchestrate.start_request(self.g, "owner/app", 1))  # type: ignore[arg-type]

    def test_a_plain_go_starts_the_autopilot_after_the_spec(self) -> None:
        self.g.comment("owner/app", 1, "/go")
        steps, hops = [], []
        with mock.patch.object(orchestrate, "adopt", new=ADOPT), \
             mock.patch.object(orchestrate, "dispatch", side_effect=lambda ctx, step, **k: steps.append(step)), \
             mock.patch.object(orchestrate, "run_handler", side_effect=lambda g, r, n, ctx, m, h: hops.append(h)), \
             mock.patch.object(orchestrate, "checkpoint"):
            self.assertEqual(self.process(), "started")
        claim = next(c["body"] for c in self.g.comments if c["body"].startswith("▶"))
        self.assertIn("until a staged build you can try", claim)
        self.assertEqual((steps, hops), (["spec"], ["after_gate1"]))
        self.assertIn("Running `after_gate1`", self.g.comments[-1]["body"])

    def test_the_factory_s_own_comment_never_starts_anything(self) -> None:
        self.g.comment("owner/app", 1, "ℹ️ /go is how you start this", who="owner")
        self.assertIsNone(orchestrate.start_request(self.g, "owner/app", 1))  # type: ignore[arg-type]

    def test_a_bad_option_is_refused_by_name(self) -> None:
        self.g.comment("owner/app", 1, "/go model=gpt")
        self.assertEqual(self.process(), "refused")
        self.assertIn("model=gpt", self.g.comments[-1]["body"])
        self.adopt.assert_not_called()

    def test_another_command_on_an_unstarted_issue_gets_an_answer(self) -> None:
        self.g.comment("owner/app", 1, "/rework make it faster")
        orchestrate.answer_unknown_commands(self.g, "owner/app", 1)  # type: ignore[arg-type]
        self.assertIn("has not started", self.g.comments[-1]["body"])
        n = len(self.g.comments)
        orchestrate.answer_unknown_commands(self.g, "owner/app", 1)  # type: ignore[arg-type]
        self.assertEqual(len(self.g.comments), n)  # once


if __name__ == "__main__":
    unittest.main()


ADOPT = orchestrate.adopt  # the real one; StartTests replaces it with a mock


class RealAdoptTests(unittest.TestCase):
    setUp = StartTests.setUp
    process = StartTests.process

    def test_the_start_claims_every_go_and_runs_the_spec_step_on_the_named_model(self) -> None:
        first = self.g.comment("owner/app", 1, "/go model=fast")
        go = self.g.comment("owner/app", 1, "/go model=fast stop=spec investigate and fix")
        steps, cps = [], []
        with mock.patch.object(orchestrate, "adopt", new=ADOPT), \
             mock.patch.object(orchestrate, "dispatch", side_effect=lambda ctx, step, **k: steps.append((step, ctx.model, ctx.auto))), \
             mock.patch.object(orchestrate, "checkpoint", side_effect=lambda ctx, label, *a, **k: cps.append(label)):
            self.assertEqual(self.process(), "started")
        claim = next(c["body"] for c in self.g.comments if c["body"].startswith("▶"))
        self.assertIn(f"(comment {go['id']})", claim)
        self.assertIn(f"(comment {first['id']})", claim)
        self.assertEqual(steps, [("spec", "fast", False)])
        self.assertEqual(cps, ["1"])
        self.assertIn("until the spec and its acceptance tests", claim)
        self.assertIn("stage/idea", self.g.labels)
        self.assertIsNone(orchestrate.start_request(self.g, "owner/app", 1))  # type: ignore[arg-type]

