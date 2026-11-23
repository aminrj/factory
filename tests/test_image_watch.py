"""Weekly image watch: the Dockerfile remedies, and what is opened when (brevkoll#36, 2026-11-23)."""

import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import image_watch  # noqa: E402

OLD, NEW = "a" * 64, "b" * 64
BREVKOLL = f"""FROM python:3.12-slim@sha256:{OLD} AS builder
RUN apt-get update && apt-get install -y build-essential

FROM python:3.12-slim@sha256:{OLD}
RUN apt-get update \\
    && apt-get install -y --no-install-recommends tesseract-ocr zlib1g \\
    && rm -rf /var/lib/apt/lists/*
USER app
"""


class RemedyTests(unittest.TestCase):
    def test_the_upgrade_goes_into_the_runtime_stage_only(self) -> None:
        out = image_watch.add_apt_upgrade(BREVKOLL)
        assert out is not None
        runtime = image_watch.stages(out)[-1]
        self.assertIn("&& apt-get upgrade -y --no-install-recommends \\\n    && apt-get install", runtime)
        self.assertNotIn("upgrade", image_watch.stages(out)[0])  # the builder stage is thrown away: left alone

    def test_a_one_line_update_install_is_handled_and_a_dockerfile_that_upgrades_or_has_no_apt_is_not_touched(self) -> None:
        one = "FROM x@sha256:" + OLD + "\nRUN apt-get update && apt-get install -y curl\n"
        self.assertIn("apt-get update && apt-get upgrade -y --no-install-recommends && apt-get install", image_watch.add_apt_upgrade(one) or "")
        self.assertIsNone(image_watch.add_apt_upgrade(image_watch.add_apt_upgrade(BREVKOLL) or ""))
        self.assertIsNone(image_watch.add_apt_upgrade("FROM x@sha256:" + OLD + "\nRUN echo hi\n"))

    def test_the_base_digest_is_bumped_everywhere_it_appears_and_only_when_it_moved(self) -> None:
        out = image_watch.bump_base(BREVKOLL, lambda ref: NEW)
        assert out is not None
        text, changes = out
        self.assertEqual(text.count(f"@sha256:{NEW}"), 2)
        self.assertNotIn(OLD, text)
        self.assertEqual(changes, [f"python:3.12-slim: {OLD[:12]} -> {NEW[:12]}"])
        self.assertIsNone(image_watch.bump_base(BREVKOLL, lambda ref: OLD))
        self.assertIsNone(image_watch.bump_base(BREVKOLL, lambda ref: None))  # cannot resolve: leave it

    def test_the_signature_ignores_order(self) -> None:
        self.assertEqual(image_watch.signature(["a", "b"]), image_watch.signature(["b", "a"]))


class FakeGitea:
    user = "owner"

    def __init__(self, open_issues: list[dict] | None = None, open_prs: list[dict] | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.open_issues, self.open_prs = open_issues or [], open_prs or []

    def get(self, path: str):  # noqa: ANN201
        if "/pulls?" in path:
            return self.open_prs
        if "/contents/" in path:
            return {"sha": "s1"}
        return []

    def paged(self, path: str):  # noqa: ANN201
        return self.open_issues if "/issues?" in path else []

    def call(self, method: str, path: str, body: dict | None = None):  # noqa: ANN201
        self.calls.append((method, path, body))
        return {"number": 7}

    def ensure_label(self, *a: str) -> None:
        pass

    def label_id(self, repo: str, name: str) -> int:
        return 1


def clone_with(text: str):  # noqa: ANN201
    @contextlib.contextmanager
    def cm(g: object, repo: str):  # noqa: ANN202
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Dockerfile").write_text(text)
            yield Path(d)
    return cm


def scanner(*results: tuple[int, list[str]]):  # noqa: ANN201
    queue = list(results)

    def scan(root: Path, tag: str) -> dict:
        fixed, pkgs = queue.pop(0)
        return {"fixed": fixed, "unfixed": 93, "critical": 0, "packages": pkgs}
    return scan


class WatchTests(unittest.TestCase):
    def run_watch(self, g: FakeGitea, text: str, scan: object, dry: bool = False) -> dict:
        with mock.patch.object(image_watch, "read_only_clone", clone_with(text)), mock.patch.object(
                image_watch, "notify"), mock.patch.object(image_watch, "app_repos", return_value=["app"]):
            return image_watch.watch(g, "app", dry, scan=scan, resolve=lambda ref: NEW)  # type: ignore[arg-type]

    def test_a_clean_image_opens_nothing(self) -> None:
        g = FakeGitea()
        rec = self.run_watch(g, BREVKOLL, scanner((0, [])))
        self.assertEqual((rec["fixed"], rec["action"]), (0, "none"))
        self.assertEqual(g.calls, [])

    def test_the_upgrade_that_clears_the_findings_becomes_a_pr(self) -> None:
        g = FakeGitea()
        rec = self.run_watch(g, BREVKOLL, scanner((56, ["libkrb5-3 1 -> 2"]), (0, [])))
        self.assertEqual(rec["action"], "pr_opened:7")
        put = next(c for c in g.calls if c[0] == "PUT")
        self.assertIn("/contents/Dockerfile", put[1])
        self.assertEqual(put[2]["author"]["name"], "factory-orchestrator")
        self.assertIn("apt-get upgrade", __import__("base64").b64decode(put[2]["content"]).decode())
        pr = next(c for c in g.calls if c[1].endswith("/pulls"))
        self.assertIn("**0** fixable findings", pr[2]["body"])

    def test_when_the_upgrade_does_not_help_the_newer_base_is_tried(self) -> None:
        upgraded = image_watch.add_apt_upgrade(BREVKOLL) or ""
        g = FakeGitea()
        rec = self.run_watch(g, upgraded, scanner((10, ["x 1 -> 2"]), (0, [])))  # no apt remedy left: base bump clears it
        self.assertEqual(rec["action"], "pr_opened:7")
        put = next(c for c in g.calls if c[0] == "PUT")
        self.assertIn(NEW, __import__("base64").b64decode(put[2]["content"]).decode())

    def test_a_remedy_that_changes_nothing_ends_in_one_issue_not_a_pr(self) -> None:
        g = FakeGitea()
        rec = self.run_watch(g, BREVKOLL, scanner((5, ["a 1 -> 2"]), (5, []), (5, [])))
        self.assertEqual(rec["action"], "issue_opened:7")
        self.assertFalse([c for c in g.calls if c[0] == "PUT"])
        issue = next(c for c in g.calls if c[1].endswith("/issues"))
        self.assertIn(image_watch.MARKER, issue[2]["body"])

    def test_the_same_findings_are_not_filed_twice_and_an_open_pr_is_not_duplicated(self) -> None:
        sig = image_watch.signature(["a 1 -> 2"])
        g = FakeGitea(open_issues=[{"body": f"x {image_watch.MARKER}{sig}-->"}])
        self.assertEqual(self.run_watch(g, "FROM scratch\n", scanner((5, ["a 1 -> 2"]))) ["action"], "issue_already_open")
        g2 = FakeGitea(open_prs=[{"head": {"ref": image_watch.BRANCH_PREFIX + "-20261120"}}])
        self.assertEqual(self.run_watch(g2, BREVKOLL, scanner((5, ["a"]), (0, [])))["action"], "pr_already_open")

    def test_a_dry_run_writes_nothing(self) -> None:
        g = FakeGitea()
        self.assertEqual(self.run_watch(g, BREVKOLL, scanner((5, ["a"]), (0, [])), dry=True)["action"], "dry_run")
        self.assertEqual(g.calls, [])


if __name__ == "__main__":
    unittest.main()
