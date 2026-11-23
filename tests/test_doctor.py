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


class KitCopiesTests(unittest.TestCase):
    def kit(self, work: Path, *parts: str) -> None:
        (work / "factory-kit" / Path(*parts)).mkdir(parents=True)

    def test_a_second_copy_of_the_code_in_the_kit_is_a_warning(self) -> None:
        doctor.results.clear()
        with tempfile.TemporaryDirectory() as d:
            self.kit(Path(d), "hq", "scripts")
            with mock.patch.object(stage, "WORK", Path(d)), mock.patch("builtins.print"):
                doctor.check_kit()
        self.assertEqual(doctor.results[0][0], "WARN")
        self.assertIn("hq/scripts", doctor.results[0][1])

    def test_a_kit_without_copies_is_fine(self) -> None:
        doctor.results.clear()
        with tempfile.TemporaryDirectory() as d:
            self.kit(Path(d), "app-seed")
            with mock.patch.object(stage, "WORK", Path(d)), mock.patch("builtins.print"):
                doctor.check_kit()
        self.assertEqual(doctor.results[0][0], "ok")


class TemplateDriftTests(unittest.TestCase):
    def tree(self, base: Path) -> tuple[Path, Path]:
        template, app = base / "factory-template", base / "brevkoll"
        for root in (template, app):
            (root / ".claude" / "commands").mkdir(parents=True)
            (root / ".factory" / "templates").mkdir(parents=True)
        return template, app

    def test_an_older_or_missing_copy_is_named_and_an_app_own_threat_model_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            template, app = self.tree(Path(d))
            (template / ".claude/commands/validate.md").write_text("new: write acceptance.md")
            (app / ".claude/commands/validate.md").write_text("old")
            (template / ".claude/commands/plan.md").write_text("same")
            (app / ".claude/commands/plan.md").write_text("same")
            (template / ".factory/templates/acceptance.md").write_text("x")
            (template / ".factory/templates/threat-model.md").write_text("a")
            (app / ".factory/templates/threat-model.md").write_text("b")
            self.assertEqual(doctor.template_drift(template, app),
                             [".claude/commands/validate.md", ".factory/templates/acceptance.md (missing)"])

    def test_the_check_warns_for_an_app_that_is_behind_and_is_ok_when_it_follows(self) -> None:
        doctor.results.clear()
        with tempfile.TemporaryDirectory() as d:
            template, app = self.tree(Path(d))
            (template / ".claude/commands/validate.md").write_text("new")
            (app / ".claude/commands/validate.md").write_text("old")
            with mock.patch.object(stage, "WORK", Path(d)), mock.patch("builtins.print"):
                doctor.check_apps_follow_template()
                (app / ".claude/commands/validate.md").write_text("new")
                doctor.check_apps_follow_template()
        self.assertEqual([lvl for lvl, *_ in doctor.results], ["WARN", "ok"])
        self.assertIn("brevkoll differs", doctor.results[0][1])


class UnitSourceTests(unittest.TestCase):
    def test_linked_from_the_factory_repo_is_fine_and_from_elsewhere_is_not(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            runtime, user, elsewhere = base / "factory", base / "user", base / "kit"
            (runtime / "ops" / "systemd").mkdir(parents=True)
            elsewhere.mkdir()
            user.mkdir()
            (runtime / "ops" / "systemd" / "a.timer").write_text("x")
            (elsewhere / "b.timer").write_text("x")
            (user / "a.timer").symlink_to(runtime / "ops" / "systemd" / "a.timer")
            (user / "b.timer").symlink_to(elsewhere / "b.timer")
            self.assertIsNone(doctor.unit_problem("a.timer", runtime, user))
            self.assertIn("not from the factory repo", doctor.unit_problem("b.timer", runtime, user) or "")
            self.assertIn("not installed", doctor.unit_problem("c.timer", runtime, user) or "")


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
