"""Orchestrator regressions. Run: python3 -m unittest discover -s tests -v"""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import orchestrate  # noqa: E402


class FakeGitea:
    def set_stage(self, *args: object) -> None:
        pass


class FakeCtx:
    repo, n, app = "amine/x", 1, "x"

    def __init__(self) -> None:
        self.g = FakeGitea()
        self._state: dict = {}

    def state(self) -> dict:
        return dict(self._state)

    def save(self, st: dict) -> None:
        self._state = dict(st)

    def branch_file(self, name: str) -> str:
        return "## Phase 1: a\n## Phase 2: b\n"

    def comment(self, body: str) -> None:
        pass


class BuildCounterTests(unittest.TestCase):
    def test_phase_counted_before_ci_so_a_failed_rework_does_not_repeat_it(self) -> None:
        ctx = FakeCtx()
        with (
            mock.patch.object(orchestrate, "dispatch"),
            mock.patch.object(orchestrate, "ci_wait", return_value={"security": "failure"}),
            mock.patch.object(orchestrate, "rework", side_effect=orchestrate.StepFailed("still red")),
        ):
            with self.assertRaises(orchestrate.StepFailed):
                orchestrate.build(ctx)  # type: ignore[arg-type]
        self.assertEqual(ctx.state()["phases_done"], 1)

    def test_retry_continues_with_the_next_phase(self) -> None:
        ctx = FakeCtx()
        ctx.save({"build_start": 1e12, "phases_done": 1})
        calls: list[str] = []
        with (
            mock.patch.object(orchestrate, "dispatch", side_effect=lambda c, step, note=None: calls.append(step)),
            mock.patch.object(
                orchestrate, "ci_wait", return_value={"checks": "success", "gates": "success", "security": "success"}
            ),
            mock.patch.object(orchestrate, "validate_and_checkpoint"),
        ):
            orchestrate.build(ctx)  # type: ignore[arg-type]
        self.assertEqual(calls, ["implement"])  # only phase 2
        self.assertEqual(ctx.state()["phases_done"], 2)


class PrBodyTests(unittest.TestCase):
    def test_pr_body_does_not_use_a_closing_keyword(self) -> None:
        src = (ROOT / "scripts" / "dispatch.py").read_text()
        self.assertNotIn("Closes #", src)


if __name__ == "__main__":
    unittest.main()
