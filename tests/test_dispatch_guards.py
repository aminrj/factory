"""Guards that make sure of what a prompt only asks (brevkoll#6, 2026-11-22): AC markers, acceptance.md, test naming."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch  # noqa: E402

SPEC = "# Spec: #6\n\nStatus: draft\n\n## Acceptance criteria\n- [ ] AC1: Given a letter, when decoded, then a notice shows.\n- [ ] **AC2** Given the API, then it is unchanged.\n"
PYPROJECT = '[tool.pytest.ini_options]\ntestpaths = ["tests/unit", "tests/tooling"]\n'
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(wt: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=wt, check=True, capture_output=True, text=True, env=ENV).stdout.strip()


class GuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name)
        (self.wt / "thoughts" / "6").mkdir(parents=True)
        (self.wt / "thoughts" / "6" / "spec.md").write_text(SPEC)
        (self.wt / "pyproject.toml").write_text(PYPROJECT)
        (self.wt / "tests" / "holdout").mkdir(parents=True)
        git(self.wt, "init", "-q", "-b", "main")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", "base")
        self.base = git(self.wt, "rev-parse", "HEAD")
        p = mock.patch.object(dispatch, "agent_env", return_value=ENV)
        p.start()
        self.addCleanup(p.stop)

    def write(self, rel: str, text: str) -> None:
        path = self.wt / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    # -- AC markers
    def test_markers_are_added_by_code_above_decorators_and_only_once(self) -> None:
        self.write("tests/holdout/test_issue_6.py",
                   "import pytest\n\n\ndef test_ac1_notice() -> None:\n    assert True\n\n\n"
                   "@pytest.mark.parametrize('x', [1])\ndef test_ac2_api(x: int) -> None:\n    assert x\n\n\n"
                   "def test_helper_not_a_criterion() -> None:\n    assert True\n")
        self.assertEqual(dispatch.tag_ac_markers(self.wt, 6), ["AC1", "AC2"])
        text = (self.wt / "tests/holdout/test_issue_6.py").read_text()
        self.assertIn("# AC1\ndef test_ac1_notice", text)
        self.assertIn("# AC2\n@pytest.mark.parametrize", text)
        self.assertNotIn("# AC\n", text)
        self.assertEqual(dispatch.tag_ac_markers(self.wt, 6), [])  # idempotent
        self.assertIsNone(dispatch.ac_marker_problem(self.wt, 6))

    def test_a_criterion_with_no_marker_and_no_name_to_read_it_from_is_refused_by_name(self) -> None:
        self.write("tests/holdout/test_issue_6.py", "def test_notice() -> None:\n    assert True\n")
        self.assertEqual(dispatch.tag_ac_markers(self.wt, 6), [])
        problem = dispatch.ac_marker_problem(self.wt, 6) or ""
        self.assertIn("AC1, AC2", problem)

    def test_a_docstring_mention_is_not_a_marker(self) -> None:
        self.write("tests/holdout/test_issue_6.py", 'def test_x() -> None:\n    """AC1: a notice"""\n')
        self.assertIn("AC1", dispatch.ac_marker_problem(self.wt, 6) or "")

    # -- collectable tests
    def test_test_files_pytest_would_never_run_are_named(self) -> None:
        for rel in ("tests/holdout_test_issue_6.py", "tests/other/test_y.py", "tests/unit/helper_tests.py",
                    "tests/unit/test_ok.py", "tests/holdout/test_issue_6.py", "tests/unit/conftest.py"):
            self.write(rel, "def test_a() -> None:\n    assert True\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", "work")
        self.assertEqual(dispatch.uncollected_tests(self.wt, self.base),
                         ["tests/holdout_test_issue_6.py", "tests/other/test_y.py", "tests/unit/helper_tests.py"])

    def test_a_file_without_tests_in_it_is_not_flagged(self) -> None:
        self.write("tests/odd/data.py", "X = 1\n")
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-q", "-m", "work")
        self.assertEqual(dispatch.uncollected_tests(self.wt, self.base), [])

    # -- acceptance.md
    def test_acceptance_is_generated_from_the_spec_when_validate_wrote_none(self) -> None:
        self.assertTrue(dispatch.ensure_acceptance(self.wt, 6))
        text = (self.wt / "thoughts/6/acceptance.md").read_text()
        self.assertIn("- [ ] AC1: Given a letter", text)
        self.assertIn("- [ ] AC2: Given the API", text)
        self.assertIn("Written by the foreman", text)
        self.assertIn("acceptance: checklist generated", git(self.wt, "log", "-1", "--format=%s"))
        self.assertFalse(dispatch.ensure_acceptance(self.wt, 6))  # not twice

    def test_a_real_checklist_is_left_alone_and_a_template_one_is_replaced(self) -> None:
        self.write("thoughts/6/acceptance.md", "- [ ] Upload a letter -> the notice shows\n")
        self.assertFalse(dispatch.ensure_acceptance(self.wt, 6))
        self.assertIn("Upload a letter", (self.wt / "thoughts/6/acceptance.md").read_text())
        self.write("thoughts/6/acceptance.md", "- [ ] <what a person does in the browser> -> <what they see>\n")
        self.assertTrue(dispatch.ensure_acceptance(self.wt, 6))


if __name__ == "__main__":
    unittest.main()
