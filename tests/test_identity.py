"""Two Gitea logins: the factory posts as its own user; only the owner's comments are commands and only the owner's
night/ok label is an approval. One login keeps working as before (the other tests)."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import gitea  # noqa: E402
import night  # noqa: E402
import orchestrate  # noqa: E402
import test_loop_gates as tlg  # noqa: E402

BOT = "factory-bot"


class TwoLogins(tlg.GateFakeGitea):
    bot = BOT

    def __init__(self) -> None:
        super().__init__()
        self.timeline: list[dict] = []

    def call(self, method: str, path: str, body: dict | None = None, **kw: object) -> dict:
        if method == "POST" and path.endswith("/comments"):
            return self.comment("owner/app", 1, (body or {})["body"], who=BOT)
        return super().call(method, path, body, **kw)

    def paged(self, path: str) -> list[dict]:
        if path.endswith("/timeline"):
            return self.timeline
        return super().paged(path)


class HelperTests(unittest.TestCase):
    def test_who_wrote_it(self) -> None:
        g = TwoLogins()
        owner, bot, other = ({"user": {"login": x}} for x in ("owner", BOT, "someone"))
        self.assertEqual([gitea.ours(g, c) for c in (owner, bot, other)], [True, True, False])
        self.assertEqual([gitea.by_owner(g, c) for c in (owner, bot, other)], [True, False, False])

    def test_the_label_counts_only_when_the_owner_added_it_last(self) -> None:
        g = TwoLogins()
        add = lambda who, body="1": {"type": "label", "body": body, "user": {"login": who},  # noqa: E731
                                     "label": {"name": night.LABEL}}
        self.assertFalse(gitea.label_added_by_owner(g, "owner/app", 1, night.LABEL))
        g.timeline = [add(BOT)]
        self.assertFalse(gitea.label_added_by_owner(g, "owner/app", 1, night.LABEL))
        g.timeline = [add("owner")]
        self.assertTrue(gitea.label_added_by_owner(g, "owner/app", 1, night.LABEL))
        g.timeline = [add("owner"), add("owner", body="")]  # removed afterwards
        self.assertFalse(gitea.label_added_by_owner(g, "owner/app", 1, night.LABEL))

    def test_one_login_cannot_tell_and_says_yes(self) -> None:
        self.assertTrue(gitea.label_added_by_owner(tlg.GateFakeGitea(), "owner/app", 1, night.LABEL))

    def test_the_owner_comes_from_the_environment(self) -> None:
        with mock.patch.object(gitea.Gitea, "get", return_value={"login": BOT}), \
                mock.patch.object(gitea, "_server_url", return_value="https://g"), \
                mock.patch.object(gitea.Path, "read_text", return_value="t"), \
                mock.patch.dict(os.environ, {"FACTORY_OWNER": "owner"}):
            g = gitea.Gitea()
        self.assertEqual((g.user, g.bot), ("owner", BOT))


class LoopTests(unittest.TestCase):
    setUp = tlg.GateTests.setUp
    commit_file = tlg.GateTests.commit_file
    fake_dispatch = tlg.GateTests.fake_dispatch
    fake_ci = tlg.GateTests.fake_ci
    steps = tlg.GateTests.steps

    def two_logins(self) -> TwoLogins:
        g = TwoLogins()
        g.comment("owner/app", 1, "🛑 CHECKPOINT 2b: x\n\n<!--factory:resume=after_research_questions;stop=-->", who=BOT)
        g.comment("owner/app", 1, "My answers: yes.")
        self.commit_file("research.md", "# Research\n")
        self.agent_files["plan"] = tlg.PLAN
        return g

    def test_the_owners_go_runs_and_the_factory_answers_as_itself(self) -> None:
        g = self.two_logins()
        g.comment("owner/app", 1, "/go")
        self.assertEqual(orchestrate.process(g, "owner/app", 1), "ran after_research_questions")  # type: ignore[arg-type]
        self.assertEqual(g.comments[-1]["user"]["login"], BOT)
        self.assertIn("CHECKPOINT 3", g.comments[-1]["body"])

    def test_a_go_from_the_factorys_login_is_never_obeyed(self) -> None:
        g = self.two_logins()
        g.comment("owner/app", 1, "/go", who=BOT)  # an agent misusing the factory's token
        self.assertEqual(orchestrate.process(g, "owner/app", 1), "waiting")  # type: ignore[arg-type]
        self.assertEqual(self.steps(), [])

    def test_a_night_label_the_factory_added_is_not_an_approval(self) -> None:
        g = self.two_logins()
        g.labels.append(night.LABEL)
        g.timeline = [{"type": "label", "body": "1", "user": {"login": BOT}, "label": {"name": night.LABEL}}]
        with mock.patch.dict(os.environ, {"FACTORY_NIGHT": "on", "FACTORY_NIGHT_WINDOW": "always"}):
            orchestrate.NIGHT["taken"] = False
            self.assertEqual(orchestrate.process(g, "owner/app", 1), "waiting")  # type: ignore[arg-type]
            g.timeline[0]["user"]["login"] = "owner"
            self.assertEqual(orchestrate.process(g, "owner/app", 1), "ran after_research_questions")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()


class ProtectionTests(unittest.TestCase):
    def setUp(self) -> None:
        sys.path.insert(0, str(ROOT / "scripts"))
        import identity
        self.identity = identity

    def test_only_the_owner_merges_and_the_bot_pushes_only_metrics_and_the_run_log(self) -> None:
        d = self.identity.desired_protection
        self.assertEqual(d("factory", None, "owner")["push_whitelist_usernames"], [BOT])
        self.assertEqual(d("factory-ops", None, "owner")["push_whitelist_usernames"], [BOT])
        app = d("brevkoll", {"enable_status_check": True, "status_check_contexts": ["gates / checks"]}, "owner")
        self.assertEqual((app["enable_push"], app["merge_whitelist_usernames"]), (False, ["owner"]))
        self.assertEqual(app["status_check_contexts"], ["gates / checks"])  # the required checks are kept

    def test_protect_is_idempotent_and_patches_only_what_differs(self) -> None:
        calls: list[tuple[str, str]] = []
        current = {"rule_name": "main", "branch_name": "main", "enable_status_check": False, "status_check_contexts": [],
                   **self.identity.desired_protection("factory", None, "owner")}

        class G:
            user = "owner"

            def get(self, path: str) -> list[dict]:
                return [current] if "/factory/" in path else []

            def call(self, method: str, path: str, body: object = None) -> None:
                calls.append((method, path))
        out = self.identity.protect(G(), ["factory", "brevkoll"], dry=False)  # type: ignore[arg-type]
        self.assertEqual(len(out), 1)
        self.assertEqual(calls, [("POST", "/repos/owner/brevkoll/branch_protections")])

    def test_the_host_files_never_put_the_token_on_a_command_line(self) -> None:
        self.assertIn("read -r TOKEN", self.identity.HOST_SCRIPT)
        self.assertNotIn("$TOKEN", self.identity.CHECK_SCRIPT)
