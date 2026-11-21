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


class LogExcerptTests(unittest.TestCase):
    RAW = (
        "2026-11-20T20:07:16.9902272Z ::group::Run gitleaks\n"
        "2026-11-20T20:07:18.0140201Z [90m8:07PM[0m [32mINF[0m scan\n"
        "2026-11-20T20:07:18.0630816Z RuleID:      generic-api-key\n"
        "2026-11-20T20:07:18.0631223Z File:        tests/unit/test_model_settings.py\n"
        "2026-11-20T20:07:18.0631366Z Email:       someone@example.com\n"
        "2026-11-20T20:07:18.0631367Z Authorization: Bearer abc123\n"
        "2026-11-20T20:07:18.1744180Z ##[error]Process completed with exit code 1.\n"
        "2026-11-20T20:07:18.1756583Z Run Post actions/checkout\n"
        "2026-11-20T20:07:19.1380165Z Job failed\n"
    )

    def test_excerpt_ends_at_the_error_and_is_clean(self) -> None:
        out = orchestrate.log_excerpt(self.RAW)
        self.assertIn("File:        tests/unit/test_model_settings.py", out)
        self.assertTrue(out.endswith("exit code 1."))
        self.assertNotIn("Post actions", out)
        self.assertNotIn("2026-11-20T", out)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("[90m", out)

    def test_secrets_and_emails_are_scrubbed(self) -> None:
        out = orchestrate.log_excerpt(self.RAW)
        self.assertNotIn("abc123", out)
        self.assertNotIn("someone@example.com", out)

    def test_excerpt_is_capped(self) -> None:
        raw = "\n".join(f"line {i}" for i in range(500)) + "\n##[error]boom"
        self.assertLessEqual(len(orchestrate.log_excerpt(raw).splitlines()), 60)

    def test_api_errors_do_not_break_the_rework(self) -> None:
        ctx = FakeCtx()
        ctx.g.get = mock.Mock(side_effect=RuntimeError("403"))  # type: ignore[attr-defined]
        with mock.patch.object(orchestrate, "log"):
            self.assertEqual(orchestrate.failing_logs(ctx, "abc", ["security"]), "")  # type: ignore[arg-type]


class PrBodyTests(unittest.TestCase):
    def test_pr_body_does_not_use_a_closing_keyword(self) -> None:
        src = (ROOT / "scripts" / "dispatch.py").read_text()
        self.assertNotIn("Closes #", src)


class HoldoutFormatTests(unittest.TestCase):
    def test_unformatted_holdout_stops_the_step(self) -> None:
        import dispatch  # noqa: PLC0415
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            (wt / "tests" / "holdout").mkdir(parents=True)
            fake = mock.Mock(returncode=1, stdout="", stderr="")
            with mock.patch.object(dispatch.subprocess, "run", return_value=fake):
                with self.assertRaises(SystemExit):
                    dispatch.check_holdout_formatted(wt)
            fake.returncode = 0
            with mock.patch.object(dispatch.subprocess, "run", return_value=fake):
                dispatch.check_holdout_formatted(wt)  # clean: no exit

    def test_no_holdout_dir_is_fine(self) -> None:
        import dispatch  # noqa: PLC0415
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            dispatch.check_holdout_formatted(Path(d))


if __name__ == "__main__":
    unittest.main()
