"""Doctor: what it flags in the loose config files. Docker, Gitea and the model are not called."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import doctor  # noqa: E402
import stage  # noqa: E402


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.conf = Path(self.tmp.name) / "factory"
        self.conf.mkdir(mode=0o700)
        os.chmod(self.conf, 0o700)
        doctor.results.clear()
        self.patches = [mock.patch.object(doctor, "CONF", self.conf), mock.patch.object(stage, "CONF", self.conf),
                        mock.patch("builtins.print")]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def write(self, name: str, text: str, mode: int = 0o600) -> Path:
        f = self.conf / name
        f.write_text(text)
        os.chmod(f, mode)
        return f

    def levels(self) -> dict[str, str]:
        return {what: lvl for lvl, what, _ in doctor.results}

    def test_missing_staging_env_fails(self) -> None:
        doctor.check_files(None)
        self.assertEqual(self.levels()["staging.env is missing"], "FAIL")

    def test_world_readable_secret_file_fails(self) -> None:
        self.write("staging.env", "LLM_PROVIDER=x\nLLM_BASE_URL=http://h\nLLM_MODEL=m\n", 0o644)
        doctor.check_files(None)
        self.assertEqual(self.levels()["staging.env is readable by others"], "FAIL")

    def test_copied_key_is_a_warning(self) -> None:
        self.write("staging.env", "LLM_PROVIDER=x\nLLM_BASE_URL=http://h\nLLM_MODEL=m\nLLM_API_KEY=abc\n")
        doctor.check_files(None)
        self.assertEqual(self.levels()["staging.env holds a copy of the model key"], "WARN")
        self.assertNotIn("abc", " ".join(w for _, w, h in doctor.results) + " ".join(h for *_, h in doctor.results))

    def test_missing_model_keys_fail(self) -> None:
        self.write("staging.env", "LLM_PROVIDER=x\n")
        doctor.check_files(None)
        self.assertTrue(any(lvl == "FAIL" and "LLM_BASE_URL" in what for lvl, what, _ in doctor.results))


class KitDriftTests(unittest.TestCase):
    def test_drift_is_a_warning(self) -> None:
        doctor.results.clear()
        with tempfile.TemporaryDirectory() as d:
            work = Path(d)
            kit = work / "factory-kit" / "hq" / "scripts"
            kit.mkdir(parents=True)
            (kit / "doctor.py").write_text("old")  # differs from the real scripts/doctor.py
            with mock.patch.object(stage, "WORK", work), mock.patch("builtins.print"):
                doctor.check_kit()
        self.assertEqual(doctor.results[0][0], "WARN")
        self.assertIn("doctor.py", doctor.results[0][1])


class HookTests(unittest.TestCase):
    """The guard hooks: refuse commits in the runtime folder, leave its worktrees alone."""

    def setUp(self) -> None:
        import subprocess

        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.tmp.name, "factory").resolve()
        self.runtime.mkdir()
        run = lambda *a, cwd=self.runtime, **kw: subprocess.run(a, cwd=cwd, capture_output=True, text=True, **kw)  # noqa: E731
        self.run = run
        run("git", "init", "-q", "-b", "main")
        run("git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q", "--allow-empty", "-m", "i", env={**os.environ, "GIT_AUTHOR_NAME": "factory-orchestrator", "GIT_COMMITTER_NAME": "x", "GIT_COMMITTER_EMAIL": "a@b", "GIT_AUTHOR_EMAIL": "a@b"})
        self.worktree = Path(self.tmp.name, "wt")
        run("git", "worktree", "add", "-q", str(self.worktree), "-b", "topic")
        doctor.install_hooks(self.runtime)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def commit(self, cwd: Path, author: str) -> int:
        env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": "a@b", "GIT_COMMITTER_NAME": author, "GIT_COMMITTER_EMAIL": "a@b"}
        return self.run("git", "commit", "-q", "--allow-empty", "-m", "x", cwd=cwd, env=env).returncode

    def test_installed(self) -> None:
        self.assertTrue(doctor.hooks_installed(self.runtime))

    def test_a_session_cannot_commit_in_the_runtime_folder(self) -> None:
        self.assertNotEqual(self.commit(self.runtime, "someone"), 0)

    def test_the_orchestrator_can(self) -> None:
        self.assertEqual(self.commit(self.runtime, "factory-orchestrator"), 0)

    def test_a_worktree_of_the_same_repo_is_not_affected(self) -> None:
        self.assertEqual(self.commit(self.worktree, "someone"), 0)

    def test_switching_the_runtime_off_main_warns(self) -> None:
        res = self.run("git", "switch", "-q", "-c", "feat/x")
        self.assertIn("switched the factory runtime", res.stderr)


class SecretFileTests(unittest.TestCase):
    def test_key_is_read_from_its_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d)
            (conf / "key").write_text("s3cret\n")
            (conf / "staging.env").write_text(f"LLM_BASE_URL=http://h\nLLM_API_KEY_FILE={conf / 'key'}\n")
            with mock.patch.object(stage, "CONF", conf):
                env = stage.load_model_env("a")
        self.assertEqual(env["LLM_API_KEY"], "s3cret")
        self.assertNotIn("LLM_API_KEY_FILE", env)

    def test_a_missing_key_file_is_an_error(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            conf = Path(d)
            (conf / "staging.env").write_text("LLM_API_KEY_FILE=/nope/key\n")
            with mock.patch.object(stage, "CONF", conf), self.assertRaises(stage.StageError):
                stage.load_model_env("a")


if __name__ == "__main__":
    unittest.main()
