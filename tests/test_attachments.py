"""Files the owner attaches to an issue reach the agent (local model only), and a guess is called a guess.

brevkoll#36, 2026-11-23: the failing PDF was attached to two comments; four agent runs said they could not reproduce it.
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import dispatch  # noqa: E402

PDF = b"%PDF-1.7 a letter"


class G:
    user = "owner"

    def __init__(self, size: int = 18109) -> None:
        self.size = size
        self.downloads: list[str] = []

    def get(self, path: str) -> object:
        if path.endswith("/comments"):
            return [{"id": 11, "user": {"login": "owner"}, "created_at": "t", "body": "/rework see the file"},
                    {"id": 12, "user": {"login": "owner"}, "created_at": "t", "body": "▶ Picked up"}]
        if path.endswith("/comments/11/assets"):
            return [{"name": "kunde jagare.pdf", "size": self.size, "uuid": "abc"},
                    {"name": "run.exe", "size": 5, "uuid": "x"}]
        if path.endswith("/assets"):
            return []
        return {"title": "t", "body": "the issue"}

    def download(self, uuid: str, limit: int) -> bytes | None:
        self.downloads.append(uuid)
        return PDF if self.size <= limit else None


class AttachmentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.wt = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.wt, check=True)
        (self.wt / "thoughts" / "1").mkdir(parents=True)

    def issue(self) -> str:
        return (self.wt / "thoughts" / "1" / "issue.md").read_text()

    def test_a_local_run_gets_the_file_outside_git_and_is_told_where(self) -> None:
        g = G()
        dispatch.write_issue_snapshot(g, "o/r", 1, self.wt, local=True)  # type: ignore[arg-type]
        saved = self.wt / ".attachments" / "comment-11-kunde_jagare.pdf"
        self.assertEqual(saved.read_bytes(), PDF)
        self.assertIn("saved as `.attachments/comment-11-kunde_jagare.pdf`", self.issue())
        self.assertIn("run.exe", self.issue())
        self.assertIn("not a type", self.issue())
        self.assertEqual(g.downloads, ["abc"])  # the .exe was never fetched
        subprocess.run(["git", "add", "-A"], cwd=self.wt, check=True)
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=self.wt, capture_output=True, text=True,
                                check=True).stdout
        self.assertNotIn(".attachments", staged)  # the owner's letter can never be committed by `git add -A`

    def test_a_cloud_run_is_told_the_file_exists_and_never_gets_it(self) -> None:
        g = G()
        dispatch.write_issue_snapshot(g, "o/r", 1, self.wt, local=False)  # type: ignore[arg-type]
        self.assertEqual(g.downloads, [])
        self.assertFalse((self.wt / ".attachments").exists())
        self.assertIn("withheld", self.issue())
        self.assertIn("model=local", self.issue())

    def test_a_file_over_the_limit_is_named_not_downloaded(self) -> None:
        dispatch.write_issue_snapshot(G(size=dispatch.ATTACH_MAX_BYTES + 1), "o/r", 1, self.wt, local=True)  # type: ignore[arg-type]
        self.assertIn("larger than", self.issue())
        self.assertFalse((self.wt / ".attachments" / "comment-11-kunde_jagare.pdf").exists())

    def test_an_issue_without_attachments_has_no_section(self) -> None:
        class Plain(G):
            def get(self, path: str) -> object:
                return [] if path.endswith("/assets") else super().get(path)

        dispatch.write_issue_snapshot(Plain(), "o/r", 1, self.wt, local=True)  # type: ignore[arg-type]
        self.assertNotIn("Attachments", self.issue())


class GuessTests(unittest.TestCase):
    def test_the_agents_own_admission_is_put_first(self) -> None:
        for said in ("I couldn't reproduce the error, so this is my best guess.", "Unable to reproduce your json_invalid.",
                     "I could not reproduce it with your file"):
            self.assertIn("could not reproduce", dispatch.guess_warning(said))
        self.assertEqual(dispatch.guess_warning("Fixed the date. Tests pass."), "")


if __name__ == "__main__":
    unittest.main()
