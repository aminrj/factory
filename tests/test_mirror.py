"""Mirror guard. Run: python3 -m pytest tests"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import mirror  # noqa: E402


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True,
                   capture_output=True)


class MirrorGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        mirror.DENY_FILE = Path(self.tmp.name + "-deny.txt")  # outside the repo, like the real one
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "a.md").write_text("hello\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "one")
        git(self.repo, "update-ref", "refs/remotes/github/main", "HEAD")  # what the mirror already has

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def commit(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", name)

    def test_clean_tree(self) -> None:
        self.commit("b.md", "nothing internal here, see https://example.com\n")
        self.assertEqual(mirror.check(self.repo), [])

    def test_flags_tailnet_lab_and_private_ips(self) -> None:
        self.commit("b.md", "ok\nopen https://box.tail1234.ts.net:8443\nhttp://git.lab.example.com/x\n10.1.2.3\n100.106.11.119\n")
        hits = mirror.check(self.repo)
        self.assertEqual(len(hits), 4)
        self.assertTrue(all(h.startswith("b.md:") for h in hits))

    def test_a_leak_fixed_in_a_new_commit_no_longer_blocks(self) -> None:
        # The snapshot publishes files, not history: the fix the refusal asks for must be enough (2026-11-21: both
        # mirrors were stuck 37 commits behind because every past commit was scanned).
        self.commit("b.md", "host box.tail1234.ts.net\n")
        self.assertEqual(len(mirror.check(self.repo)), 1)
        self.commit("b.md", "clean now\n")
        self.assertEqual(mirror.check(self.repo), [])

    def test_every_file_counts_not_only_new_lines(self) -> None:
        (self.repo / "old.md").write_text("192.168.0.12\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "old")
        git(self.repo, "update-ref", "refs/remotes/github/main", "HEAD")
        self.commit("c.md", "fine\n")
        self.assertEqual(len(mirror.check(self.repo)), 1)

    def test_only_committed_files_count(self) -> None:
        (self.repo / "scratch.md").write_text("box.tail1234.ts.net\n")  # untracked: never published
        self.assertEqual(mirror.check(self.repo), [])

    def test_binary_files_are_skipped(self) -> None:
        (self.repo / "img.bin").write_bytes(b"\x00\x01box.tail1234.ts.net")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "bin")
        self.assertEqual(mirror.check(self.repo), [])

    def test_private_ips_are_fine_in_test_fixtures_only(self) -> None:
        (self.repo / "tests").mkdir()
        self.commit("tests/test_x.py", "IP = '10.0.0.7'\n")
        self.assertEqual(mirror.check(self.repo), [])
        self.commit("tests/y.md", "host box.tail1.ts.net\n")
        self.assertEqual(len(mirror.check(self.repo)), 1)

    def test_deny_list_file(self) -> None:
        mirror.DENY_FILE.write_text("# mine\nmy-secret-host\n")
        self.commit("b.md", "see My-Secret-Host\n")
        self.assertEqual(len(mirror.check(self.repo)), 1)


class SnapshotTests(unittest.TestCase):
    """Push into a real bare repo standing in for GitHub."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        mirror.DENY_FILE = root / "deny.txt"
        self.remote = root / "github.git"
        git(root, "init", "-q", "--bare", str(self.remote))
        self.repo = root / "work"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "remote", "add", "github", str(self.remote))
        self.commit("a.md", "hello\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def commit(self, name: str, text: str) -> None:
        (self.repo / name).write_text(text)
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", f"add {name}")

    def published(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.remote, check=True, capture_output=True, text=True).stdout

    def test_first_push_is_one_commit_with_the_tree_of_main(self) -> None:
        self.commit("b.md", "two\n")
        self.assertTrue(mirror.push(self.repo).startswith("pushed snapshot"))
        self.assertEqual(self.published("rev-list", "--count", "main").strip(), "1")
        self.assertEqual(self.published("rev-parse", "main^{tree}"),
                         subprocess.run(["git", "rev-parse", "main^{tree}"], cwd=self.repo, check=True,
                                        capture_output=True, text=True).stdout)

    def test_the_private_history_is_not_published(self) -> None:
        self.commit("b.md", "host box.tail1234.ts.net\n")  # a leak in history
        self.commit("b.md", "fixed\n")
        mirror.push(self.repo)
        log = self.published("log", "-p", "main")
        self.assertNotIn("ts.net", log)
        self.assertIn("factory-orchestrator", self.published("log", "--format=%an", "main"))

    def test_next_push_builds_on_the_mirror_and_is_a_fast_forward(self) -> None:
        mirror.push(self.repo)
        first = self.published("rev-parse", "main").strip()
        self.commit("c.md", "three\n")
        mirror.push(self.repo)
        self.assertEqual(self.published("rev-parse", "main^").strip(), first)
        self.assertIn("add c.md", self.published("log", "-1", "--format=%s", "main"))

    def test_nothing_to_push_when_the_tree_is_unchanged(self) -> None:
        mirror.push(self.repo)
        self.assertEqual(mirror.push(self.repo), "the mirror already has this tree")
        self.assertEqual(self.published("rev-list", "--count", "main").strip(), "1")

    def test_a_refused_push_publishes_nothing(self) -> None:
        self.commit("b.md", "http://git.lab.example.com\n")
        with self.assertRaises(SystemExit):
            mirror.push(self.repo)
        self.assertEqual(self.published("for-each-ref").strip(), "")

    def test_never_mirrors_factory_ops(self) -> None:
        ops = self.repo / "factory-ops"
        ops.mkdir()
        with self.assertRaises(SystemExit):
            mirror.push(ops)


if __name__ == "__main__":
    unittest.main()
