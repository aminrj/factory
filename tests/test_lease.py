"""The foreman lease: one host acts; the standby takes over when the primary goes dark, and hands back."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import lease  # noqa: E402
import orchestrate  # noqa: E402
from gitea import GiteaError  # noqa: E402


class FakeVars:
    """Gitea's Actions variables API, in memory (404 for a missing one, like Gitea)."""

    user = "owner"

    def __init__(self) -> None:
        self.vars: dict[str, str] = {}

    def call(self, method: str, path: str, body: object = None, ok: tuple[int, ...] = (200,)) -> object:
        name = path.rsplit("/", 1)[1]
        if method == "GET":
            if name not in self.vars:
                raise GiteaError(f"GET {path} -> 404: not found")
            return {"name": name, "data": self.vars[name]}
        if method == "PUT" and name not in self.vars:
            raise GiteaError(f"PUT {path} -> 404: not found")
        self.vars[name] = body["value"]  # type: ignore[index]
        return None


class LeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.g = FakeVars()
        self.t = [1000.0]
        clock = lambda: self.t[0]  # noqa: E731
        self.primary = lease.Lease(self.g, "devbox", "primary", clock)
        self.standby = lease.Lease(self.g, "workstation", "standby", clock)

    def test_the_primary_acts_and_the_standby_waits_while_it_renews(self) -> None:
        self.assertEqual(self.standby.claim()[0], False)  # no lease yet: the primary creates it
        self.assertEqual(self.primary.claim(), (True, "holding the lease"))
        for _ in range(10):
            self.t[0] += 60
            self.assertTrue(self.primary.claim()[0])
            ok, why = self.standby.claim()
            self.assertFalse(ok)
            self.assertIn("alive", why)

    def test_the_standby_takes_over_only_after_the_lease_ran_out_plus_the_grace(self) -> None:
        self.primary.claim()
        self.t[0] += lease.LEASE_S + lease.TAKEOVER_S - 1
        self.assertFalse(self.standby.claim()[0])
        self.t[0] += 2
        self.assertEqual(self.standby.claim(), (True, "took over from devbox"))
        self.assertTrue(self.standby.claim()[0])  # and keeps it

    def test_the_primary_comes_back_waits_then_gets_the_lease_back(self) -> None:
        self.primary.claim()
        self.t[0] += lease.LEASE_S + lease.TAKEOVER_S + 1
        self.standby.claim()
        self.t[0] += 30
        ok, why = self.primary.claim()  # back, but the standby may be in the middle of a step
        self.assertFalse(ok)
        self.assertIn("hands back", why)
        self.t[0] += 30
        ok, why = self.standby.claim()  # its next tick: hands back instead of acting
        self.assertFalse(ok)
        self.assertIn("handed the lease back", why)
        self.t[0] += 30
        self.assertTrue(self.primary.claim()[0])
        self.assertFalse(self.standby.claim()[0])

    def test_never_two_actors_in_any_minute(self) -> None:
        down = range(20, 40)  # the primary is dark from minute 20 to 39; gaps: the takeover grace, one hand-back tick
        for minute in range(80):
            self.t[0] = 1000.0 + minute * 60
            p = minute not in down and self.primary.claim()[0]
            s = self.standby.claim()[0]
            self.assertFalse(p and s, f"both acted in minute {minute}")
            self.assertTrue(p or s or minute in (*range(20, 25), 40), f"nobody acted in minute {minute}")

    def test_the_status_job_runs_only_where_the_foreman_does(self) -> None:
        self.primary.claim()
        self.assertIsNone(self.primary.elsewhere())
        self.assertEqual(self.standby.elsewhere(), "devbox")
        self.t[0] += lease.LEASE_S + 1
        self.assertEqual(self.standby.elsewhere(), "devbox")  # expired, but the standby has not taken it


class TickTests(unittest.TestCase):
    def test_a_standby_without_the_lease_does_nothing(self) -> None:
        g = FakeVars()
        lease.Lease(g, "devbox", "primary").claim()
        args = mock.Mock(cmd="tick", dry_run=False)
        env = {"FACTORY_LEASE": "on", "FACTORY_ROLE": "standby", "FACTORY_HOST": "workstation"}
        with mock.patch.dict("os.environ", env), mock.patch.object(orchestrate, "guard_runtime", return_value=True), \
                mock.patch.object(orchestrate, "active_issues") as listing:
            orchestrate.run_command(g, args)  # type: ignore[arg-type]
        listing.assert_not_called()


def git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


class RunLogPushTests(unittest.TestCase):
    def test_the_run_log_is_pushed_after_someone_else_moved_main(self) -> None:
        # 2026-11-22: the owner merged a docs PR in factory-ops and every run-log push after it was refused
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            git(root, "init", "-q", "--bare", "-b", "main", "origin.git")
            for name in ("work", "other"):
                git(root, "clone", "-q", "origin.git", name)
                git(root / name, "config", "user.email", "t@t")
                git(root / name, "config", "user.name", "t")
            (root / "work" / "RUNLOG.md").write_text("start\n")
            git(root / "work", "add", "-A")
            git(root / "work", "commit", "-q", "-m", "start")
            git(root / "work", "push", "-q", "-u", "origin", "main")
            git(root / "other", "pull", "-q", "origin", "main")
            (root / "other" / "AGENT.md").write_text("docs\n")
            git(root / "other", "add", "-A")
            git(root / "other", "commit", "-q", "-m", "docs PR")
            git(root / "other", "push", "-q", "origin", "main")
            (root / "work" / "RUNLOG.md").write_text("start\nline\n")
            with mock.patch.object(orchestrate, "WORK", root / "work"), \
                    mock.patch.object(orchestrate, "alarm") as alarm:
                orchestrate.commit_ops("Checkpoint 1: app#1")
            alarm.assert_not_called()
            log = git(root / "work", "log", "--format=%s", "origin/main")
            self.assertEqual(log.splitlines()[:2], ["Checkpoint 1: app#1", "docs PR"])


if __name__ == "__main__":
    unittest.main()
