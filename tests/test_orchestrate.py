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


class ReworkAndStagingTests(unittest.TestCase):
    def test_rework_command_is_parsed_with_its_note(self) -> None:
        m = orchestrate.REWORK.match("/rework the .ics has the wrong year\nand the page is too wide")
        self.assertIsNotNone(m)
        self.assertIn("wrong year", m.group(1))  # type: ignore[union-attr]
        self.assertIsNone(orchestrate.REWORK.match("please /rework this"))
        self.assertIsNone(orchestrate.GO.match("/rework"))

    def test_decide_returns_rework(self) -> None:
        class G:
            user = "bot"

            def paged(self, path: str) -> list[dict]:
                return [{"id": 1, "user": {"login": "bot"}, "body": "🛑 CHECKPOINT 4: x\n<!--factory:resume=after_gate4;rework=ok-->"},
                        {"id": 2, "user": {"login": "bot"}, "body": "/rework wrong date"}]

        d = orchestrate.decide(G(), "amine/x", 1)  # type: ignore[arg-type]
        self.assertEqual(d[2], "rework")  # type: ignore[index]

    def test_staging_block_has_both_links_and_the_checklist(self) -> None:
        ctx = FakeCtx()
        ctx.branch = "agent/issue-1"  # type: ignore[attr-defined]
        ctx.branch_file = lambda name: "- [ ] decode the fixture letter"  # type: ignore[method-assign]
        out = mock.Mock(returncode=0, stdout='{"url": "http://box.example.com:8800/", "ip": "100.64.0.9", "port": 8800, "sha": "abcdef123456"}', stderr="")
        with mock.patch.object(orchestrate, "stage_call", return_value=out):
            text = orchestrate.staging_block(ctx)  # type: ignore[arg-type]
        self.assertIn("[Open the staged build](http://box.example.com:8800/)", text)
        self.assertIn("http://100.64.0.9:8800/", text)
        self.assertIn("decode the fixture letter", text)

    def test_staging_failure_does_not_stop_the_checkpoint(self) -> None:
        ctx = FakeCtx()
        out = mock.Mock(returncode=1, stdout="", stderr="STAGING FAILED: no model configured")
        with mock.patch.object(orchestrate, "stage_call", return_value=out), mock.patch.object(orchestrate, "log"):
            text = orchestrate.staging_block(ctx)  # type: ignore[arg-type]
        self.assertIn("Could not stage", text)
        self.assertIn("no model configured", text)


class SpecApprovalTests(unittest.TestCase):
    HUMAN = "me@example.com"

    def repo(self, tmp: str, commits: list[tuple[str, str, str]]) -> Path:
        import subprocess

        r = Path(tmp)
        subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
        (r / "thoughts" / "1").mkdir(parents=True)
        for email, text, msg in commits:
            (r / "thoughts" / "1" / "spec.md").write_text(text)
            subprocess.run(["git", "add", "-A"], cwd=r, check=True)
            subprocess.run(["git", "-c", f"user.email={email}", "-c", "user.name=x", "commit", "-q", "-m", msg], cwd=r, check=True)
        subprocess.run(["git", "update-ref", "refs/remotes/origin/agent/issue-1", "HEAD"], cwd=r, check=True)
        return r

    def check(self, commits: list[tuple[str, str, str]]) -> str | None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            repo = self.repo(d, commits)
            spec = (repo / "thoughts" / "1" / "spec.md").read_text()
            return orchestrate.spec_approval_problem(repo, "agent/issue-1", 1, spec, self.HUMAN)

    def test_draft_is_refused(self) -> None:
        self.assertIn("draft", self.check([("bot@x", "Status: draft\n", "d")]))  # type: ignore[arg-type]

    def test_approval_by_the_human_passes(self) -> None:
        self.assertIsNone(self.check([("bot@x", "Status: draft\n", "d"), (self.HUMAN, "Status: approved\n", "a")]))

    def test_approval_by_someone_else_is_refused(self) -> None:
        self.assertIn("someone other", self.check([("bot@x", "Status: approved\n", "d")]))  # type: ignore[arg-type]

    def test_a_spec_without_a_status_line_keeps_the_old_behaviour(self) -> None:
        self.assertIsNone(self.check([("bot@x", "# Spec\n", "d")]))


class ActiveAndMergeTests(unittest.TestCase):
    def test_idea_issues_are_watched(self) -> None:
        class G:
            def paged(self, path: str) -> list[dict]:
                if "repos/search" in path:
                    return [{"full_name": "o/app"}]
                return [{"number": 5, "labels": [{"name": "stage/idea"}]}, {"number": 6, "labels": [{"name": "bug"}]}]

        self.assertEqual(orchestrate.active_issues(G()), [("o/app", 5)])  # type: ignore[arg-type]

    def test_a_merge_is_announced_once_with_its_reason(self) -> None:
        import json
        import tempfile

        class G:
            user = "o"

            def paged(self, path: str) -> list[dict]:
                return []

            def get(self, path: str) -> list[dict]:
                return [{"number": 7, "merged": True, "merged_at": "2999-01-01T00:00:00Z", "title": "T", "html_url": "u",
                         "body": "Refs #1\n\nBecause the gate was blind.\nmore"}]

        sent: list[tuple] = []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(orchestrate, "STATE", Path(d)), \
                mock.patch.object(orchestrate, "MERGES", Path(d) / "merges.json"), \
                mock.patch.object(orchestrate, "notify", side_effect=lambda *a, **k: sent.append(a)), \
                mock.patch.object(orchestrate, "log"):
            orchestrate.announce_merges(G())  # type: ignore[arg-type]
            self.assertEqual(sent, [])  # first run only records the time
            (Path(d) / "merges.json").write_text(json.dumps({"since": "2000-01-01T00:00:00+00:00", "sent": []}))
            orchestrate.announce_merges(G())  # type: ignore[arg-type]
            orchestrate.announce_merges(G())  # type: ignore[arg-type]
        self.assertEqual(len(sent), 2)  # factory and factory-template, same PR number, announced once each
        self.assertIn("Because the gate was blind.", sent[0][1])


class RuntimeGuardTests(unittest.TestCase):
    def repo(self, d: str) -> Path:
        import subprocess

        r = Path(d)
        subprocess.run(["git", "init", "-q", "-b", "main", str(r)], check=True)
        (r / "scripts").mkdir()
        (r / "data").mkdir()
        (r / "scripts" / "a.py").write_text("x")
        (r / "data" / "d.jsonl").write_text("1")
        subprocess.run(["git", "add", "-A"], cwd=r, check=True)
        subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "-m", "i"], cwd=r, check=True)
        return r

    def test_main_and_clean_is_fine_even_with_data_changes(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            r = self.repo(d)
            (r / "data" / "d.jsonl").write_text("2")  # metrics jobs rewrite this all the time
            self.assertIsNone(orchestrate.runtime_problem(r))

    def test_another_branch_is_a_problem(self) -> None:
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            r = self.repo(d)
            subprocess.run(["git", "switch", "-q", "-c", "feat/x"], cwd=r, check=True)
            self.assertIn("feat/x", orchestrate.runtime_problem(r) or "")

    def test_local_code_changes_are_a_problem(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            r = self.repo(d)
            (r / "scripts" / "a.py").write_text("changed")
            self.assertIn("uncommitted", orchestrate.runtime_problem(r) or "")


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
