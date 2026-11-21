"""Staging runner: the parts that decide what may run and how. Docker itself is not called."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stage  # noqa: E402


class FakeGitea:
    def __init__(self, statuses: list[tuple[str, str]]) -> None:
        self.statuses = statuses

    def get(self, path: str) -> dict:
        return {"statuses": [{"context": c, "status": s} for c, s in self.statuses]}


class StageTests(unittest.TestCase):
    def test_only_a_fully_green_commit_is_staged(self) -> None:
        names = ["checks", "gates", "security", "holdout"]
        ok = [(f"gates / {n} (pull_request)", "success") for n in names]
        stage.require_green(FakeGitea(ok), "o/a", "abc1234")  # type: ignore[arg-type]
        bad = ok[:3] + [("gates / holdout (pull_request)", "failure")]
        with self.assertRaises(stage.StageError):
            stage.require_green(FakeGitea(bad), "o/a", "abc1234")  # type: ignore[arg-type]
        with self.assertRaises(stage.StageError):
            stage.require_green(FakeGitea(ok[:2]), "o/a", "abc1234")  # type: ignore[arg-type]

    def test_ports_are_stable_per_app_and_unique(self) -> None:
        reg = {"a": {"port": 8800}, "b": {"port": 8801}}
        self.assertEqual(stage.pick_port(reg, "a"), 8800)
        self.assertEqual(stage.pick_port(reg, "c"), 8802)

    def test_loopback_model_means_host_network(self) -> None:
        self.assertTrue(stage.is_loopback("http://127.0.0.1:8090/v1"))
        self.assertTrue(stage.is_loopback("http://localhost:1/v1"))
        self.assertFalse(stage.is_loopback("https://model.example.com:8743/v1"))

    def test_command_gets_the_tailnet_host_and_port(self) -> None:
        cmd = '["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]'
        with mock.patch.object(stage, "sh", return_value=cmd):
            out = stage.command_for("img", "100.64.0.9", 8800)
        self.assertEqual(out[-4:], ["--host", "100.64.0.9", "--port", "8800"])
        with mock.patch.object(stage, "sh", return_value='["python", "app.py"]'), self.assertRaises(stage.StageError):
            stage.command_for("img", "100.64.0.9", 8800)

    def test_policy_is_the_fixed_sandbox(self) -> None:
        for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--memory", "--pids-limit"):
            self.assertIn(flag, stage.POLICY)
        self.assertNotIn("--privileged", stage.POLICY)

    def test_model_preflight_classifies_answers(self) -> None:
        import urllib.error

        model = {"LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_API_KEY": "k"}

        def raising(code: int):
            def f(*a: object, **k: object) -> None:
                raise urllib.error.HTTPError("u", code, "m", {}, None)  # type: ignore[arg-type]
            return f

        with mock.patch.object(stage.urllib.request, "urlopen", raising(401)), self.assertRaises(stage.StageError):
            stage.preflight_model(model)
        with mock.patch.object(stage.urllib.request, "urlopen", raising(503)):
            self.assertIn("busy", stage.preflight_model(model))
        with mock.patch.object(stage.urllib.request, "urlopen", side_effect=OSError("down")), self.assertRaises(stage.StageError):
            stage.preflight_model(model)

    def test_a_pr_build_needs_an_open_pr(self) -> None:
        class G:
            def get(self, path: str) -> list:
                return [{"head": {"ref": "agent/issue-2", "sha": "aaa"}}]

        with mock.patch.object(stage, "sh", return_value=""):
            self.assertEqual(stage.resolve_target(G(), "o/a", Path("."), 2, None), ("aaa", "agent/issue-2"))  # type: ignore[arg-type]
            with self.assertRaises(stage.StageError):
                stage.resolve_target(G(), "o/a", Path("."), 1, None)  # type: ignore[arg-type]

    def test_env_file_parser_and_staging_keys(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# c\nLLM_BASE_URL=http://x\nSTAGING_HOST='h'\nBAD\n")
        out = stage.read_env(Path(f.name))
        Path(f.name).unlink()
        self.assertEqual(out, {"LLM_BASE_URL": "http://x", "STAGING_HOST": "h"})


if __name__ == "__main__":
    unittest.main()
