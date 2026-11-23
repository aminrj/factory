"""Release candidates: CP5 after the merge, the owner accepts the image they tried on the devbox."""

import json
import os
import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import orchestrate  # noqa: E402
import release  # noqa: E402
import test_loop_gates as tlg  # noqa: E402

RC = {"version": "v2026.11.22.1", "sha": "a" * 40, "digest": "sha256:" + "b" * 64, "url": "http://box:8800/",
      "local_url": "http://localhost:8800/", "previous": "v0.1.0", "changes": ["#26 split create_app"],
      "findings": {"fixed": 0, "unfixed": 3, "critical": 1}}


class PureTests(unittest.TestCase):
    def test_versions_count_every_candidate_of_the_day(self) -> None:
        today = datetime(2026, 11, 22, tzinfo=UTC)
        self.assertEqual(release.next_version(["v0.1.0"], today), "v2026.11.22.1")
        self.assertEqual(release.next_version(["v2026.11.22.1", "v2026.11.22.2", "v2026.11.21.9"], today),
                         "v2026.11.22.3")

    def test_only_fixable_findings_are_counted_as_fixed(self) -> None:
        report = {"Results": [{"Vulnerabilities": [
            {"Severity": "CRITICAL", "FixedVersion": "1.2"}, {"Severity": "HIGH"}, {"Severity": "MEDIUM", "FixedVersion": "x"}]}]}
        self.assertEqual(release.scan_counts(report), {"fixed": 1, "unfixed": 1, "critical": 1})


    def test_the_fixable_packages_are_named_once_each(self) -> None:
        v = {"Severity": "HIGH", "PkgName": "libkrb5-3", "InstalledVersion": "1.21.3-5+deb13u1", "FixedVersion": "1.21.3-5+deb13u2"}
        report = {"Results": [{"Vulnerabilities": [v, {**v, "VulnerabilityID": "other"}, {**v, "PkgName": "a", "Severity": "LOW"},
                                                   {"Severity": "HIGH", "PkgName": "unfixed"}]}]}
        self.assertEqual(release.fixable_packages(report), ["libkrb5-3 1.21.3-5+deb13u1 -> 1.21.3-5+deb13u2"])

    def test_a_release_is_always_built_without_the_cache_and_with_a_fresh_base(self) -> None:
        cmd = release.build_command(["r/app:v1", "r/app:sha-abc"])
        self.assertEqual(cmd[:2], ["docker", "build"])
        self.assertIn("--no-cache", cmd)
        self.assertIn("--pull", cmd)
        self.assertEqual(cmd[-5:], ["-t", "r/app:v1", "-t", "r/app:sha-abc", "-"])


class FlowTests(unittest.TestCase):
    setUp = tlg.GateTests.setUp
    fake_dispatch = tlg.GateTests.fake_dispatch
    fake_ci = tlg.GateTests.fake_ci
    checkpoint = tlg.GateTests.checkpoint
    go = tlg.GateTests.go
    bot = tlg.GateTests.bot

    def merged(self) -> None:
        self.checkpoint("after_gate4", "4")
        self.g.closed_prs = [{**tlg.PR, "merged": True}]
        self.g.open_prs = []

    def test_after_the_merge_the_candidate_is_staged_and_cp5_asks_for_acceptance(self) -> None:
        self.merged()
        calls = []
        with mock.patch.dict(os.environ, {"FACTORY_RELEASES": "on"}), \
                mock.patch.object(orchestrate, "release_call", side_effect=lambda *a: calls.append(a) or json.dumps(RC)):
            self.assertEqual(self.go(), "ran after_gate4")
        self.assertEqual(calls, [("candidate", "app")])
        cp = self.bot()
        self.assertIn("CHECKPOINT 5: try release candidate v2026.11.22.1", cp)
        self.assertIn("http://box:8800/", cp)
        self.assertIn("rework=ok", cp)
        self.assertIn("version=v2026.11.22.1", cp)

    def test_go_at_cp5_tags_the_release_and_closes_the_item(self) -> None:
        self.checkpoint("after_release", "5", extra=f"x;version={RC['version']};sha={RC['sha']}")
        calls = []
        patched: list[dict] = []
        real = self.g.call
        self.g.call = lambda m, p, b=None, **k: (patched.append(b or {}) if m == "PATCH" and p.endswith("/issues/1")  # type: ignore[method-assign]
                                                 else None) or real(m, p, b, **k)
        with mock.patch.object(orchestrate, "release_call", side_effect=lambda *a: calls.append(a) or "tagged"):
            self.assertEqual(self.go("/go"), "ran after_release")
        self.assertEqual(calls, [("accept", "app", RC["version"], RC["sha"])])
        self.assertIn({"state": "closed"}, patched)

    def test_a_refused_candidate_says_why_and_can_be_retried(self) -> None:
        self.merged()
        with mock.patch.dict(os.environ, {"FACTORY_RELEASES": "on"}), mock.patch.object(
                orchestrate, "release_call", side_effect=orchestrate.NeedsHuman("RELEASE REFUSED: main is not green")):
            self.go()
        self.assertIn("CHECKPOINT 5-fix: no release candidate", self.bot())
        self.assertIn("resume=release_candidate", self.bot())
        self.assertIn("Tried at", self.bot())  # a retry that gets the same answer must look like a retry
        self.assertIn("You can still test this build", self.bot())
        self.assertIn("http://box.example.com:8800/", self.bot())
        self.assertIn("not a release candidate", self.bot())

    def test_a_refusal_whose_staging_also_fails_says_so_in_one_line(self) -> None:
        self.merged()
        failed = mock.Mock(returncode=1, stdout="", stderr="STAGING FAILED: main is not green")
        with mock.patch.dict(os.environ, {"FACTORY_RELEASES": "on"}), mock.patch.object(
                orchestrate, "release_call", side_effect=orchestrate.NeedsHuman("RELEASE REFUSED: x")), mock.patch.object(
                orchestrate, "stage_call", return_value=failed):
            self.go()
        self.assertIn("could not stage `main` for testing either", self.bot())
        self.assertIn("main is not green", self.bot())

    def test_the_night_shift_never_accepts_a_release(self) -> None:
        import night
        self.assertNotIn("after_release", night.PRE_CODE)
        self.assertNotIn("release_candidate", night.PRE_CODE)


class RegistryVersionTests(unittest.TestCase):
    def test_a_version_already_in_the_registry_is_never_reused(self) -> None:
        today = datetime(2026, 11, 22, tzinfo=UTC)
        in_registry = ["sha-283e0a0", "v2026.11.22.1"]  # the candidate pushed by hand on 2026-11-22
        self.assertEqual(release.next_version(["v0.1.0"] + in_registry, today), "v2026.11.22.2")
