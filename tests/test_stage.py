"""Staging runner: the parts that decide what may run and how. Docker itself is not called."""

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stage  # noqa: E402


class FakeGitea:
    def __init__(self, statuses: list[tuple[str, str]]) -> None:
        self.statuses = statuses

    def get(self, path: str) -> dict:
        return {"statuses": [{"context": c, "status": s} for c, s in self.statuses]}


class StageTests(unittest.TestCase):
    def test_only_a_fully_green_commit_is_staged(self) -> None:
        names = ["checks", "gates", "security", "holdout"]
        ok = [(f"gates / {n} (pull_request)", "success") for n in names]
        stage.require_green(FakeGitea(ok), "o/a", "abc1234")  # type: ignore[arg-type]
        bad = ok[:3] + [("gates / holdout (pull_request)", "failure")]
        with self.assertRaises(stage.StageError):
            stage.require_green(FakeGitea(bad), "o/a", "abc1234")  # type: ignore[arg-type]
        with self.assertRaises(stage.StageError):
            stage.require_green(FakeGitea(ok[:2]), "o/a", "abc1234")  # type: ignore[arg-type]

    def test_ports_are_stable_per_app_and_unique(self) -> None:
        reg = {"a": {"port": 8800}, "b": {"port": 8801}}
        self.assertEqual(stage.pick_port(reg, "a"), 8800)
        self.assertEqual(stage.pick_port(reg, "c"), 8802)

    def test_loopback_model_means_host_network(self) -> None:
        self.assertTrue(stage.is_loopback("http://127.0.0.1:8090/v1"))
        self.assertTrue(stage.is_loopback("http://localhost:1/v1"))
        self.assertFalse(stage.is_loopback("https://model.example.com:8743/v1"))

    def test_command_gets_the_tailnet_host_and_port(self) -> None:
        cmd = '["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]'
        with mock.patch.object(stage, "sh", return_value=cmd):
            out = stage.command_for("img", "100.64.0.9", 8800)
        self.assertEqual(out[-4:], ["--host", "100.64.0.9", "--port", "8800"])
        with mock.patch.object(stage, "sh", return_value='["python", "app.py"]'), self.assertRaises(stage.StageError):
            stage.command_for("img", "100.64.0.9", 8800)

    def test_the_loopback_twin_has_the_same_sandbox_and_binds_127_0_0_1_only(self) -> None:
        cmd = '["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]'
        with mock.patch.object(stage, "sh", return_value=cmd):
            tail = stage.run_args("stage-a", "a", "img", "100.64.0.9", 8800, "/e", True)
            twin = stage.run_args("stage-a-local", "a", "img", "127.0.0.1", 8800, "/e", True)
        self.assertEqual(tail[-4:], ["--host", "100.64.0.9", "--port", "8800"])
        self.assertEqual(twin[-4:], ["--host", "127.0.0.1", "--port", "8800"])
        for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--memory", "--pids-limit"):
            self.assertIn(flag, tail)
            self.assertIn(flag, twin)
        self.assertNotIn("0.0.0.0", " ".join(twin))  # never on every interface

    def test_the_twin_function_can_only_ever_bind_loopback(self) -> None:
        cmd = '["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]'
        with mock.patch.object(stage, "sh", return_value=cmd):
            host = stage.twin_args("a", "img", 8800, "/e", True)
        bridge = stage.twin_args("a", "img", 8800, "/e", False)
        self.assertEqual(host[-4:], ["--host", "127.0.0.1", "--port", "8800"])
        self.assertIn("127.0.0.1:8800:8000", bridge)
        self.assertIn("stage-a-local", host)

    def test_a_bridge_twin_publishes_on_loopback_only(self) -> None:
        twin = stage.run_args("stage-a-local", "a", "img", "127.0.0.1", 8800, "/e", False)
        self.assertIn("127.0.0.1:8800:8000", twin)

    def test_down_removes_both_containers(self) -> None:
        removed: list[str] = []
        with mock.patch.object(stage, "sh", side_effect=lambda *a, **k: removed.append(a[-1]) or ""), \
                mock.patch.object(stage, "load_registry", return_value={"a": {}}), mock.patch.object(stage, "save_registry"):
            stage.down("a")
        self.assertEqual(removed, ["stage-a", "stage-a-local"])

    def test_policy_is_the_fixed_sandbox(self) -> None:
        for flag in ("--read-only", "--cap-drop", "no-new-privileges", "--memory", "--pids-limit"):
            self.assertIn(flag, stage.POLICY)
        self.assertNotIn("--privileged", stage.POLICY)

    def test_model_preflight_classifies_answers(self) -> None:
        import urllib.error

        model = {"LLM_BASE_URL": "http://127.0.0.1:1/v1", "LLM_API_KEY": "k"}

        def raising(code: int):
            def f(*a: object, **k: object) -> None:
                raise urllib.error.HTTPError("u", code, "m", {}, None)  # type: ignore[arg-type]
            return f

        with mock.patch.object(stage.urllib.request, "urlopen", raising(401)), self.assertRaises(stage.StageError):
            stage.preflight_model(model)
        with mock.patch.object(stage.urllib.request, "urlopen", raising(403)), self.assertRaises(stage.StageError):
            stage.preflight_model(model)
        with mock.patch.object(stage.urllib.request, "urlopen", raising(400)):
            self.assertIn("key accepted", stage.preflight_model(model))
        with mock.patch.object(stage.urllib.request, "urlopen", raising(503)):
            self.assertIn("busy", stage.preflight_model(model))
        with mock.patch.object(stage.urllib.request, "urlopen", side_effect=OSError("down")), self.assertRaises(stage.StageError):
            stage.preflight_model(model)

    def test_the_probe_posts_an_empty_body_to_the_chat_route(self) -> None:
        seen = {}

        def fake(req: object, timeout: int = 0):  # noqa: ANN202
            seen["url"], seen["data"], seen["auth"] = req.full_url, req.data, req.get_header("Authorization")  # type: ignore[attr-defined]
            raise stage.urllib.error.HTTPError("u", 400, "m", {}, None)  # type: ignore[arg-type]

        with mock.patch.object(stage.urllib.request, "urlopen", fake):
            stage.preflight_model({"LLM_BASE_URL": "http://h/v1/", "LLM_API_KEY": "k"})
        self.assertEqual(seen, {"url": "http://h/v1/chat/completions", "data": b"{}", "auth": "Bearer k"})

    def test_a_pr_build_needs_an_open_pr(self) -> None:
        class G:
            def get(self, path: str) -> list:
                return [{"head": {"ref": "agent/issue-2", "sha": "aaa"}}]

        with mock.patch.object(stage, "sh", return_value=""):
            self.assertEqual(stage.resolve_target(G(), "o/a", Path("."), 2, None), ("aaa", "agent/issue-2"))  # type: ignore[arg-type]
            with self.assertRaises(stage.StageError):
                stage.resolve_target(G(), "o/a", Path("."), 1, None)  # type: ignore[arg-type]

    def test_bind_address_defaults_to_the_tailnet_and_never_every_interface(self) -> None:
        addrs = "2: eth0    inet 10.0.40.12/24 brd x\n3: ts0    inet 100.101.1.2/32 x\n"
        with mock.patch.object(stage, "sh", return_value=addrs):
            self.assertEqual(stage.bind_ip({}), "100.101.1.2")
            self.assertEqual(stage.bind_ip({"STAGING_BIND_IP": "10.0.40.12"}), "10.0.40.12")
            for bad in ("0.0.0.0", "*", "10.0.40.99", "devbox"):
                with self.subTest(bad=bad), self.assertRaises(stage.StageError):
                    stage.bind_ip({"STAGING_BIND_IP": bad})

    def test_without_a_tailnet_address_the_error_names_the_setting(self) -> None:
        with mock.patch.object(stage, "sh", return_value="2: eth0    inet 10.0.40.12/24 brd x\n"):
            with self.assertRaisesRegex(stage.StageError, "STAGING_BIND_IP"):
                stage.bind_ip({})

    def test_env_file_parser_and_staging_keys(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as f:
            f.write("# c\nLLM_BASE_URL=http://x\nSTAGING_HOST='h'\nBAD\n")
        out = stage.read_env(Path(f.name))
        Path(f.name).unlink()
        self.assertEqual(out, {"LLM_BASE_URL": "http://x", "STAGING_HOST": "h"})


class UpTests(unittest.TestCase):
    """stage.up with Docker faked: the link it hands out is for the build it names, and only there."""

    def up(self, env: dict[str, str], running: str) -> dict:
        def sh(*args: str, **kw: object) -> str:
            if args[:2] == ("docker", "inspect"):
                return running
            return "present" if args[:2] == ("docker", "images") else ""
        with mock.patch.object(stage, "Gitea"), \
                mock.patch.object(stage, "resolve_target", return_value=("abcdef1234567", "agent/issue-1")), \
                mock.patch.object(stage, "require_green"), \
                mock.patch.object(stage, "load_model_env", return_value=dict(env)), \
                mock.patch.object(stage, "bind_ip", return_value="10.9.9.9"), \
                mock.patch.object(stage, "preflight_model", return_value="ok"), \
                mock.patch.object(stage, "load_registry", return_value={}), \
                mock.patch.object(stage, "save_registry"), \
                mock.patch.object(stage, "wait_healthy"), \
                mock.patch.object(stage, "sh", side_effect=sh):
            return stage.up("app", 1)

    def test_no_localhost_link_unless_asked_for(self) -> None:
        info = self.up({"LLM_BASE_URL": "http://m"}, "app-stage:abcdef1")
        self.assertEqual(info["local_url"], "")
        info = self.up({"LLM_BASE_URL": "http://m", "STAGING_LOCAL_LINK": "on"}, "app-stage:abcdef1")
        self.assertTrue(info["local_url"].startswith("http://localhost:"))

    def test_the_container_is_told_which_build_it_runs(self) -> None:
        written = []
        real = stage.tempfile.NamedTemporaryFile

        def capture(*a: object, **k: object):  # type: ignore[no-untyped-def]
            f = real(*a, **k)
            orig = f.write
            f.write = lambda text: (written.append(text), orig(text))[1]  # type: ignore[method-assign]
            return f
        with mock.patch.object(stage.tempfile, "NamedTemporaryFile", side_effect=capture):
            self.up({"LLM_BASE_URL": "http://m"}, "app-stage:abcdef1")
        self.assertIn("APP_VERSION=abcdef1\n", "".join(written))

    def test_refuses_when_the_container_runs_another_build(self) -> None:
        with self.assertRaisesRegex(stage.StageError, "not app-stage:abcdef1"):
            self.up({"LLM_BASE_URL": "http://m"}, "app-stage:0a6bfbd")


if __name__ == "__main__":
    unittest.main()
