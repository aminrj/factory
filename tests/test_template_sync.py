"""Template sync: which files differ, what an app may keep, and the single PR (2026-11-22: validate.md was a release behind)."""

import base64
import contextlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import template_sync  # noqa: E402
from gitea import GiteaError  # noqa: E402


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)


class DiffTests(unittest.TestCase):
    def trees(self) -> tuple[Path, Path]:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        t, a = tmp / "t", tmp / "a"
        write(t, ".factory/template.json", json.dumps({"owned": [".claude/commands/", "scripts/trace.py"]}))
        write(t, ".claude/commands/validate.md", "new")
        write(t, ".claude/commands/plan.md", "same")
        write(t, "scripts/trace.py", "v2")
        write(t, "scripts/other.py", "not owned")
        write(a, ".claude/commands/validate.md", "old")
        write(a, ".claude/commands/plan.md", "same")
        write(a, "scripts/trace.py", "v2")
        write(a, ".claude/commands/own.md", "the app's own command")
        return t, a

    def test_only_owned_files_that_differ_or_are_missing_are_listed(self) -> None:
        t, a = self.trees()
        self.assertEqual(template_sync.differences(t, a, template_sync.owned_paths(t), []), [".claude/commands/validate.md"])
        (a / "scripts/trace.py").unlink()
        self.assertEqual(template_sync.differences(t, a, template_sync.owned_paths(t), []),
                         [".claude/commands/validate.md", "scripts/trace.py"])

    def test_an_exception_the_app_declares_is_respected_and_extras_are_never_touched(self) -> None:
        t, a = self.trees()
        write(a, ".factory/template-exceptions", "# on purpose\n.claude/commands/validate.md\n")
        self.assertEqual(template_sync.exceptions(a), [".claude/commands/validate.md"])
        self.assertEqual(template_sync.differences(t, a, template_sync.owned_paths(t), template_sync.exceptions(a)), [])
        self.assertTrue(template_sync.excepted(".claude/commands/x.md", [".claude/commands/"]))
        self.assertFalse(template_sync.excepted(".claude/commands2/x.md", [".claude/commands"]))

    def test_a_template_without_a_manifest_owns_nothing(self) -> None:
        self.assertEqual(template_sync.owned_paths(Path(tempfile.mkdtemp())), [])


class FakeGitea:
    user = "owner"

    def __init__(self, open_prs: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.open_prs = open_prs or []

    def get(self, path: str):  # noqa: ANN201
        if "/pulls?" in path:
            return self.open_prs
        if path.endswith("validate.md?ref=main"):
            return {"sha": "old-sha"}
        raise GiteaError("404")

    def call(self, method: str, path: str, body: dict | None = None):  # noqa: ANN201
        self.calls.append((method, path, body))
        return {"number": 9}


class SyncTests(unittest.TestCase):
    def run_sync(self, g: FakeGitea, dry: bool = False, exceptions: str = "") -> dict:
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        write(tmp / "t", ".factory/template.json", json.dumps({"owned": [".claude/commands/"]}))
        write(tmp / "t", ".claude/commands/validate.md", "new")
        write(tmp / "t", ".claude/commands/acceptance.md", "brand new")
        write(tmp / "a", ".claude/commands/validate.md", "old")
        if exceptions:
            write(tmp / "a", ".factory/template-exceptions", exceptions)

        @contextlib.contextmanager
        def clone(g_: object, repo: str):  # noqa: ANN202
            yield tmp / ("t" if repo == "factory-template" else "a")

        with mock.patch.object(template_sync, "read_only_clone", clone):
            return template_sync.sync(g, "app", dry)  # type: ignore[arg-type]

    def test_one_pr_with_every_differing_file_updating_existing_ones_and_creating_new(self) -> None:
        g = FakeGitea()
        rec = self.run_sync(g)
        self.assertEqual(rec["action"], "pr_opened:9")
        puts = {c[1].split("/contents/")[1]: c[2] for c in g.calls if c[0] == "PUT"}
        self.assertEqual(set(puts), {".claude/commands/validate.md", ".claude/commands/acceptance.md"})
        self.assertEqual(puts[".claude/commands/validate.md"]["sha"], "old-sha")
        self.assertNotIn("sha", puts[".claude/commands/acceptance.md"])
        self.assertEqual(base64.b64decode(puts[".claude/commands/validate.md"]["content"]), b"new")
        self.assertEqual(puts[".claude/commands/validate.md"]["author"]["name"], "factory-orchestrator")

    def test_an_app_in_sync_dry_runs_and_open_prs_change_nothing(self) -> None:
        g = FakeGitea()
        self.assertEqual(self.run_sync(g, exceptions=".claude/commands/\n")["action"], "none")
        self.assertEqual(self.run_sync(g, dry=True)["action"], "dry_run")
        self.assertEqual(g.calls, [])
        g2 = FakeGitea(open_prs=[{"head": {"ref": template_sync.BRANCH_PREFIX + "-20261120"}}])
        self.assertEqual(self.run_sync(g2)["action"], "pr_already_open")
        self.assertEqual(g2.calls, [])


if __name__ == "__main__":
    unittest.main()
