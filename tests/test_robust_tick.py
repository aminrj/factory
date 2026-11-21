"""A stalled network path must not stop the orchestrator from reading a command (seen 2026-11-21: ~1.5 h of failed ticks
while the owner's /go from the phone waited, because one connect timeout crashed the whole tick)."""

import argparse
import socket
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import gitea  # noqa: E402
import orchestrate  # noqa: E402


class FakeResponse:
    status = 200

    def __init__(self, body: bytes = b"[]") -> None:
        self.body = body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


def client() -> gitea.Gitea:
    g = gitea.Gitea.__new__(gitea.Gitea)
    g.base, g._token, g.user = "https://gitea.test", "t", "amine"
    return g


class ClientTests(unittest.TestCase):
    def setUp(self) -> None:
        patch = mock.patch.object(gitea.time, "sleep")
        patch.start()
        self.addCleanup(patch.stop)

    def test_a_read_that_times_out_twice_then_answers_succeeds(self) -> None:
        answers = [socket.timeout("slow"), urllib.error.URLError("stalled"), FakeResponse(b'{"ok": 1}')]
        with mock.patch.object(gitea.urllib.request, "urlopen", side_effect=answers) as urlopen:
            self.assertEqual(client().get("/x"), {"ok": 1})
        self.assertEqual(urlopen.call_count, 3)

    def test_a_read_that_never_answers_raises_unreachable_not_a_raw_urlerror(self) -> None:
        with mock.patch.object(gitea.urllib.request, "urlopen", side_effect=urllib.error.URLError("stalled")) as urlopen:
            with self.assertRaises(gitea.Unreachable):
                client().get("/x")
        self.assertEqual(urlopen.call_count, gitea.READ_TRIES)

    def test_a_write_is_never_repeated(self) -> None:
        with mock.patch.object(gitea.urllib.request, "urlopen", side_effect=socket.timeout("slow")) as urlopen:
            with self.assertRaises(gitea.Unreachable):
                client().call("POST", "/x", {"body": "hi"})
        self.assertEqual(urlopen.call_count, 1)

    def test_requests_use_the_short_timeout(self) -> None:
        with mock.patch.object(gitea.urllib.request, "urlopen", return_value=FakeResponse()) as urlopen:
            client().get("/x")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], gitea.TIMEOUT)
        self.assertLessEqual(gitea.TIMEOUT, 20)


class PagingTests(unittest.TestCase):
    def test_an_endpoint_that_ignores_paging_does_not_loop_forever(self) -> None:
        everything = [{"id": i} for i in range(51)]  # what Gitea returns for the comments of an issue, for any page
        g = client()
        with mock.patch.object(g, "get", return_value=everything) as get:
            self.assertEqual(g.paged("/repos/a/b/issues/1/comments"), everything)
        self.assertEqual(get.call_count, 2)  # the second page had nothing new

    def test_a_real_paged_endpoint_is_still_read_to_the_end(self) -> None:
        pages = {1: [{"id": i} for i in range(50)], 2: [{"id": i} for i in range(50, 60)]}
        g = client()
        with mock.patch.object(g, "get", side_effect=lambda path: pages[int(path.rsplit("=", 1)[1])]):
            self.assertEqual(len(g.paged("/x")), 60)

    def test_an_exact_multiple_of_the_page_size_ends_on_the_empty_page(self) -> None:
        pages = {1: [{"id": i} for i in range(50)], 2: []}
        g = client()
        with mock.patch.object(g, "get", side_effect=lambda path: pages[int(path.rsplit("=", 1)[1])]):
            self.assertEqual(len(g.paged("/x")), 50)

    def test_items_without_ids_stop_at_the_hard_cap(self) -> None:
        g = client()
        with mock.patch.object(g, "get", return_value=["x"] * 50) as get:
            g.paged("/x")
        self.assertEqual(get.call_count, gitea.MAX_PAGES)


class TickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        state = Path(self.tmp.name) / "state"
        for patch in (mock.patch.object(orchestrate, "STATE", state),
                      mock.patch.object(orchestrate, "HEARTBEAT", state / "heartbeat"),
                      mock.patch.object(orchestrate, "log"),
                      mock.patch.object(orchestrate, "guard_runtime", return_value=True),
                      mock.patch.object(orchestrate, "announce_merges"),
                      mock.patch.object(orchestrate, "answer_unknown_commands"),
                      mock.patch.object(orchestrate, "recover_interrupted"),
                      mock.patch.object(orchestrate, "remind_waiting")):
            patch.start()
            self.addCleanup(patch.stop)
        self.notify = mock.patch.object(orchestrate, "notify").start()
        self.addCleanup(mock.patch.stopall)
        self.args = argparse.Namespace(cmd="tick", dry_run=False)

    def test_one_failing_issue_does_not_stop_the_next(self) -> None:
        seen: list[int] = []

        def process(g: object, repo: str, n: int, dry: bool = False) -> str:
            seen.append(n)
            if n == 1:
                raise ValueError("boom")
            return "ok"

        with mock.patch.object(orchestrate, "active_issues", return_value=[("a/b", 1), ("a/b", 2)]), \
                mock.patch.object(orchestrate, "process", process):
            orchestrate.run_command(mock.Mock(), self.args)
        self.assertEqual(seen, [1, 2])

    def test_a_failing_issue_is_pushed_once_not_every_minute(self) -> None:
        with mock.patch.object(orchestrate, "active_issues", return_value=[("a/b", 1)]), \
                mock.patch.object(orchestrate, "process", side_effect=ValueError("boom")):
            for _ in range(3):  # three ticks in a row
                orchestrate.run_command(mock.Mock(), self.args)
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("b#1", self.notify.call_args.args[0])
        self.assertIn("boom", self.notify.call_args.args[1])

    def test_the_same_alarm_comes_back_after_the_quiet_period(self) -> None:
        orchestrate.alarm("k", "t", "m")
        with mock.patch.object(orchestrate.time, "time", return_value=orchestrate.time.time() + 7 * 3600):
            orchestrate.alarm("k", "t", "m")
        self.assertEqual(self.notify.call_count, 2)

    def test_a_crashed_tick_pushes_and_still_fails_for_systemd(self) -> None:
        with mock.patch.object(orchestrate, "Gitea", side_effect=KeyError("token")):
            with mock.patch.object(sys, "argv", ["orchestrate.py", "tick"]), self.assertRaises(KeyError):
                orchestrate.main()
        self.notify.assert_called_once()
        self.assertIn("crashed", self.notify.call_args.args[0])

    def test_alarms_never_write_outside_the_state_folder(self) -> None:
        orchestrate.alarm("k", "t", "m")
        self.assertTrue((orchestrate.STATE / "alarms.json").exists())

    def test_the_issue_list_is_read_once_per_tick(self) -> None:
        with mock.patch.object(orchestrate, "active_issues", return_value=[("a/b", 1)]) as listing, \
                mock.patch.object(orchestrate, "process", return_value="ok"):
            orchestrate.run_command(mock.Mock(), self.args)
        self.assertEqual(listing.call_count, 1)

    def test_an_unreachable_gitea_ends_the_tick_quietly_and_skips_the_heartbeat(self) -> None:
        with mock.patch.object(orchestrate, "Gitea", side_effect=gitea.Unreachable("down")):
            with mock.patch.object(sys, "argv", ["orchestrate.py", "tick"]):
                orchestrate.main()  # must return, not raise: systemd must not see a crash every minute
        self.assertFalse(orchestrate.HEARTBEAT.exists())

    def test_unreachable_in_the_middle_of_a_tick_is_not_swallowed_per_issue(self) -> None:
        with mock.patch.object(orchestrate, "active_issues", return_value=[("a/b", 1), ("a/b", 2)]), \
                mock.patch.object(orchestrate, "process", side_effect=gitea.Unreachable("down")) as process:
            with self.assertRaises(gitea.Unreachable):
                orchestrate.run_command(mock.Mock(), self.args)
        self.assertEqual(process.call_count, 1)  # no point hammering a dead network issue after issue

    def test_coming_back_after_a_long_blind_spell_sends_one_notice(self) -> None:
        orchestrate.beat(1_000.0)
        self.notify.assert_not_called()  # the first beat has nothing to compare with
        orchestrate.beat(1_000.0 + 60)
        self.notify.assert_not_called()  # a normal one-minute gap says nothing
        orchestrate.beat(1_000.0 + 60 + 95 * 60)
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("95", self.notify.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
