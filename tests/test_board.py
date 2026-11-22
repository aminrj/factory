"""The board page: every work item lands in the column that says whose turn it is, and the page is safe HTML."""

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import board  # noqa: E402

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def cp(label: str, title: str = "x") -> dict:
    return {"body": f"🛑 CHECKPOINT {label}: {title}\n\n<!--factory:resume=a-->", "created_at": "2026-11-22T11:00:00Z"}


def issue(n: int = 1, state: str = "open", labels: tuple[str, ...] = ()) -> dict:
    return {"number": n, "title": "A change <b>", "state": state, "labels": [{"name": x} for x in labels],
            "created_at": "2026-11-20T10:00:00Z", "closed_at": "2026-11-21T10:00:00Z" if state == "closed" else None}


class ColumnTests(unittest.TestCase):
    def test_each_checkpoint_goes_to_the_column_of_whoever_acts_next(self) -> None:
        cases = {"1": "you", "2": "you", "2b": "you", "3": "you", "4": "review", "after_gate4-fix": "blocked",
                 "after_gate3-retry": "blocked", "implement-blocked": "blocked", "build-failed": "blocked"}
        for label, col in cases.items():
            self.assertEqual(board.column_of(cp(label), False)[0], col, label)
        self.assertEqual(board.column_of(None, False)[0], "inbox")
        self.assertEqual(board.column_of(cp("3"), True)[0], "agent")


class CardTests(unittest.TestCase):
    def test_a_running_step_names_the_model_and_the_step(self) -> None:
        claim = {"body": "▶ Picked up `/go` (comment 9). Running `build` on model `local`.",
                 "created_at": "2026-11-22T11:30:00Z"}
        view = {"pivot": claim, "running": True, "pending": []}
        runs = [{"repo": "app", "issue": 1, "cost_usd": 0.5, "duration_s": 120}]
        c = board.card(issue(labels=("night/ok",)), "owner/app", "https://git", view, runs, NOW,
                       {("app", 1): ("implement", 300)})
        self.assertEqual((c["col"], c["who"]), ("agent", "agent · local"))
        self.assertIn("implement", c["why"])
        self.assertTrue(c["night"])
        self.assertEqual((c["runs"], c["cost"]), (1, 0.5))

    def test_a_closed_issue_is_done_whatever_its_last_comment(self) -> None:
        c = board.card(issue(state="closed"), "owner/app", "https://git", {"pivot": cp("4"), "running": False,
                       "pending": []}, [], NOW, {})
        self.assertEqual(c["col"], "done")


class PageTests(unittest.TestCase):
    def test_titles_are_escaped_and_the_page_refreshes_itself(self) -> None:
        c = board.card(issue(), "owner/app", "https://git", None, [], NOW, {})
        with tempfile.TemporaryDirectory() as tmp:
            stats = board.analytics(Path(tmp), NOW)
        page = board.render([c], [("foreman", "idle", "ok")], stats, ["12:00 <x>"], NOW, "host")
        self.assertIn("A change &lt;b&gt;", page)
        self.assertNotIn("<b>\n", page)
        self.assertIn("http-equiv='refresh' content='60'", page)
        self.assertNotIn("http://", page.replace("https://git", ""))  # nothing loaded from outside

    def test_analytics_split_typed_default_and_auto_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "human_minutes.jsonl").write_text("\n".join(json.dumps(x) for x in [
                {"minutes": 3, "source": "typed"}, {"minutes": 10, "source": "default"}, {"minutes": 0, "source": "auto"},
                {"minutes": 7}]) + "\n")
            (d / "agent_runs.jsonl").write_text("\n".join(json.dumps(x) for x in [
                {"ts": "2026-11-22T10:00:00Z", "model": "local", "step": "implement", "duration_s": 60},
                {"ts": "2026-11-22T10:00:00Z", "model": "smart", "step": "plan", "cost_usd": 1.5, "is_error": True},
                {"ts": "2026-10-01T10:00:00Z", "model": "smart", "cost_usd": 9}]) + "\n")
            s = board.analytics(d, NOW)
        self.assertEqual(s["human_min"], {"typed": 10, "default": 10, "auto": 0})
        self.assertEqual((s["runs_7d"], s["cost_7d"], s["errors_7d"], s["local_share"]), (2, 1.5, 1, 0.5))

    def test_the_write_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "board" / "index.html"
            board.write(p, "one")
            board.write(p, "two")
            self.assertEqual(p.read_text(), "two")
            self.assertEqual(sorted(x.name for x in p.parent.iterdir()), ["index.html"])


if __name__ == "__main__":
    unittest.main()


class BoardV2Tests(unittest.TestCase):
    def test_queued_commands_and_the_error_of_a_blocked_item_are_on_the_card(self) -> None:
        blocked = {"body": "🛑 CHECKPOINT implement-blocked: blocked: model_unavailable\n\nDiagnosis:\n\n```\nline a\n"
                           "model=local: no local model is serving [class: model_unavailable]\n```\n\nNo retry.",
                   "created_at": "2026-11-22T11:00:00Z"}
        view = {"pivot": blocked, "running": False,
                "pending": [{"body": "/go model=fast\nplease", "created_at": "2026-11-22T11:50:00Z"}]}
        c = board.card(issue(), "owner/app", "https://git", view, [], NOW, {})
        self.assertEqual(c["col"], "blocked")
        self.assertEqual(c["queued"], [{"text": "/go model=fast", "age": "10 min"}])
        self.assertIn("no local model is serving", c["diagnosis"])
        with tempfile.TemporaryDirectory() as tmp:
            page = board.render([c], [], board.analytics(Path(tmp), NOW), [], NOW, "host", busy="busy with app#2")
        self.assertIn("command(s) waiting for the foreman", page)
        self.assertIn("What went wrong", page)
        self.assertIn("busy with app#2", page)
        self.assertIn("data-tags='blocked app mine queued'", page)

    def test_failures_are_listed_by_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "agent_runs.jsonl").write_text("\n".join(json.dumps(x) for x in [
                {"ts": "2026-11-22T10:00:00Z", "repo": "app", "issue": 3, "step": "implement", "is_error": True,
                 "class": "credit_exhausted", "model": "smart"},
                {"ts": "2026-11-22T10:30:00Z", "repo": "app", "issue": 3, "step": "plan", "is_error": True},
                {"ts": "2026-11-22T11:00:00Z", "repo": "app", "issue": 3, "step": "plan"}]) + "\n")
            s = board.analytics(d, NOW)
        self.assertEqual(dict(s["classes"]), {"credit_exhausted": 1, "unknown (recorded before classes)": 1})
        self.assertEqual([f["step"] for f in s["failures"]], ["plan", "implement"])  # newest first
        page = board.render([], [], s, [], NOW, "host")
        self.assertIn("credit_exhausted", page)
        self.assertIn("What the checkpoints mean", page)
        self.assertIn("id='q'", page)  # the filter


class CostTests(unittest.TestCase):
    def test_local_runs_cost_nothing_whatever_claude_code_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "agent_runs.jsonl").write_text("\n".join(json.dumps(x) for x in [
                {"ts": "2026-11-22T10:00:00Z", "model": "local", "cost_usd": 4.5, "step": "threat-model"},
                {"ts": "2026-11-22T10:00:00Z", "model": "fast", "cost_usd": 0.7, "step": "plan"}]) + "\n")
            s = board.analytics(d, NOW)
        self.assertAlmostEqual(s["cost_7d"], 0.7)


class LocalBenchmarkTests(unittest.TestCase):
    def test_local_work_is_shown_at_fast_prices_with_its_energy_and_kept_out_of_cloud_spend(self) -> None:
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"FACTORY_KWH_PRICE": "2.0"}):
            d = Path(tmp)
            (d / "agent_runs.jsonl").write_text(json.dumps(
                {"ts": "2026-11-22T10:00:00Z", "model": "local", "cost_usd": 1.25, "cost_basis": "fast-equivalent",
                 "energy_kwh": 0.15, "step": "spec"}) + "\n")
            s = board.analytics(d, NOW)
            page = board.render([], [], s, [], NOW, "host")
        self.assertEqual((s["cost_7d"], s["local_fast_equiv"], s["local_kwh"]), (0.0, 1.25, 0.15))
        self.assertIn("$1.25", page)
        self.assertIn("0.30 SEK", page)


class StaleClaimTests(unittest.TestCase):
    def test_a_claim_without_a_process_is_not_agent_working(self) -> None:
        claim = {"body": "▶ Nothing automated is left for this gate.", "created_at": "2026-11-22T07:08:00Z"}
        c = board.card(issue(), "owner/app", "https://git", {"pivot": claim, "running": False, "stale": True,
                                                              "pending": []}, [], NOW, {})
        self.assertEqual(c["col"], "blocked")
        self.assertIn("stopped without posting a checkpoint", c["why"])


class ArchiveTests(unittest.TestCase):
    def test_done_cards_can_be_archived_on_this_device_only(self) -> None:
        done = board.card(issue(state="closed"), "owner/app", "https://git", None, [], NOW, {})
        mine = board.card(issue(n=2), "owner/app", "https://git", None, [], NOW, {})
        with tempfile.TemporaryDirectory() as tmp:
            page = board.render([done, mine], [], board.analytics(Path(tmp), NOW), [], NOW, "host")
        self.assertIn("data-k='app#1'", page)  # the done card has the button
        self.assertNotIn("data-k='app#2'", page)  # an open one has not
        self.assertIn("data-col='archived'", page)
        self.assertIn("localStorage", page)
        self.assertNotIn("method='post'", page.lower())  # nothing is written anywhere but the browser
