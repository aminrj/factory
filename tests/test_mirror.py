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

    def test_clean_push(self) -> None:
        self.commit("b.md", "nothing internal here, see https://example.com\n")
        self.assertEqual(mirror.check(self.repo), [])

    def test_flags_tailnet_lab_and_private_ips(self) -> None:
        self.commit("b.md", "ok\nopen https://box.tail1234.ts.net:8443\nhttp://git.lab.example.com/x\n10.1.2.3\n100.106.11.119\n")
        hits = mirror.check(self.repo)
        self.assertEqual(len(hits), 4)
        self.assertTrue(all(h.startswith("b.md:") for h in hits))

    def test_only_added_lines_count(self) -> None:
        (self.repo / "old.md").write_text("192.168.0.12\n")  # already mirrored earlier
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-q", "-m", "old")
        git(self.repo, "update-ref", "refs/remotes/github/main", "HEAD")
        self.commit("c.md", "fine\n")
        self.assertEqual(mirror.check(self.repo), [])
        self.assertEqual(len(mirror.check(self.repo, whole_tree=True)), 1)

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

    def test_never_mirrors_factory_ops(self) -> None:
        ops = self.repo / "factory-ops"
        ops.mkdir()
        with self.assertRaises(SystemExit):
            mirror.push(ops)


if __name__ == "__main__":
    unittest.main()
