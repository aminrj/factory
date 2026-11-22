"""Model profiles (`/go model=local|smart|fast|cheap`), strict options, failure classes and the local-model shim.

Nothing here calls a real model: the shim is tested against a fake upstream on a loopback port.
"""

import http.server
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import commands  # noqa: E402
import dispatch  # noqa: E402
import model_shim  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_go_options_and_note(self) -> None:
        p = commands.parse_go(" minutes=12 model=local looks good, ship it")
        self.assertEqual((p.minutes, p.model, p.errors), (12, "local", []))

    def test_go_minutes_with_a_unit_is_still_read(self) -> None:
        self.assertEqual(commands.parse_go(" minutes=5min").minutes, 5)

    def test_each_profile_is_accepted(self) -> None:
        for name in ("local", "smart", "fast", "cheap", "LOCAL"):
            self.assertEqual(commands.parse_go(f" model={name}").model, name.lower())

    def test_unknown_key_and_bad_values_are_refused_by_name(self) -> None:
        self.assertIn("`agent=`", commands.parse_go(" agent=local").errors[0])
        self.assertIn("`model=gpt5`", commands.parse_go(" model=gpt5").errors[0])
        self.assertIn("`minutes=ten`", commands.parse_go(" minutes=ten").errors[0])

    def test_rework_options_come_first_the_rest_is_the_note(self) -> None:
        p, note = commands.parse_rework(" model=fast the width=100% layout is too wide")
        self.assertEqual((p.model, p.errors, note), ("fast", [], "the width=100% layout is too wide"))
        p, note = commands.parse_rework("the button is too small")
        self.assertEqual((p.model, note), (None, "the button is too small"))
        self.assertTrue(commands.parse_rework(" agent=local fix it")[0].errors)


class ProfileTests(unittest.TestCase):
    BASE = {"ANTHROPIC_API_KEY": "sk-cloud", "PATH": "/bin"}

    def test_no_profile_keeps_the_account_default(self) -> None:
        self.assertEqual(dispatch.model_args(None, self.BASE), ([], self.BASE))

    def test_cloud_profiles_pick_a_claude_model(self) -> None:
        for profile, alias in (("smart", "opus"), ("fast", "sonnet"), ("cheap", "haiku")):
            args, env = dispatch.model_args(profile, self.BASE)
            self.assertEqual(args, ["--model", alias])
            self.assertNotIn("ANTHROPIC_BASE_URL", env)

    def test_local_goes_through_the_shim_and_never_carries_the_cloud_key(self) -> None:
        args, env = dispatch.model_args("local", self.BASE, "http://127.0.0.1:5555", "qwen")
        self.assertEqual(args, ["--model", "qwen"])
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:5555")
        self.assertNotIn("ANTHROPIC_API_KEY", env)
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "qwen")  # background calls stay local too

    def test_every_profile_the_parser_accepts_has_an_environment(self) -> None:
        for name in commands.MODELS:
            dispatch.model_args(name, self.BASE, "http://127.0.0.1:1", "m")
        with self.assertRaises(ValueError):
            dispatch.model_args("gpt5", self.BASE)


class ClassifyTests(unittest.TestCase):
    """Recorded outputs: the first one is the exact tail from brevkoll#15 on 2026-11-21."""

    def test_recorded_outputs(self) -> None:
        cases = {
            "You've hit your 5-hour limit · resets 9:50am": "credit_exhausted",
            "Credit balance is too low": "credit_exhausted",
            "Invalid API key · Please run /login": "auth",
            "API Error: 429 rate_limit_error": "rate_limit",
            "API Error: 503 desk mode": "model_unavailable",
            "local model unreachable: ConnectionRefusedError": "model_unavailable",
            "AssertionError in tests/unit/test_x.py": None,
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(dispatch.classify(text), want)

    def test_every_class_has_a_hint(self) -> None:
        self.assertEqual({name for name, _ in dispatch.FAILURE_CLASSES}, set(dispatch.HINTS))


class NormalizeTests(unittest.TestCase):
    def test_system_messages_stay_in_place_inside_the_user_turn(self) -> None:
        body = {"system": [{"type": "text", "text": "A"}],
                "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": [{"type": "text", "text": "B"}]},
                             {"role": "system", "content": "C"}]}
        out = model_shim.normalize(body)
        self.assertEqual(out["system"], [{"type": "text", "text": "A"}])  # the beginning of the prompt never changes
        self.assertEqual([m["role"] for m in out["messages"]], ["user"])
        texts = [b["text"] for b in out["messages"][0]["content"]]
        self.assertEqual(texts, ["hi", "<system-reminder>\nB\n</system-reminder>", "<system-reminder>\nC\n</system-reminder>"])

    def test_turn_after_turn_the_earlier_prompt_is_unchanged(self) -> None:
        # the property the llama.cpp prompt cache needs: request k+1 starts with exactly what request k sent
        turn1 = {"system": "S", "messages": [{"role": "user", "content": "do it"}, {"role": "system", "content": "env 1"}]}
        turn2 = {"system": "S", "messages": [*turn1["messages"], {"role": "assistant", "content": "ok"},
                                             {"role": "user", "content": "next"}, {"role": "system", "content": "env 2"}]}
        a, b = model_shim.normalize(turn1), model_shim.normalize(turn2)
        self.assertEqual(a["system"], b["system"])
        self.assertEqual(b["messages"][:len(a["messages"])], a["messages"])

    def test_a_system_message_after_an_assistant_turn_becomes_its_own_user_turn(self) -> None:
        body = {"messages": [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"},
                             {"role": "system", "content": "note"}]}
        out = model_shim.normalize(body)
        self.assertEqual([m["role"] for m in out["messages"]], ["user", "assistant", "user"])

    def test_a_clean_request_is_unchanged(self) -> None:
        body = {"system": "S", "messages": [{"role": "user", "content": "hi"}]}
        self.assertIs(model_shim.normalize(body), body)


class CapOutputTests(unittest.TestCase):
    """brevkoll#7: one local answer ran 94k tokens (30+ min) until the context was full."""

    def test_a_huge_or_missing_max_tokens_is_capped_and_llama_gets_n_predict(self) -> None:
        for asked in (64000, None):
            body = {"messages": [{"role": "user", "content": "hi"}]}
            if asked:
                body["max_tokens"] = asked
            out = model_shim.cap_output(body, 16384)
            self.assertEqual((out["max_tokens"], out["n_predict"]), (16384, 16384))

    def test_a_smaller_request_is_kept_and_thinking_fits_below_the_cap(self) -> None:
        out = model_shim.cap_output({"messages": [], "max_tokens": 2000}, 16384)
        self.assertEqual(out["max_tokens"], 2000)
        out = model_shim.cap_output({"messages": [], "max_tokens": 32000,
                                     "thinking": {"type": "enabled", "budget_tokens": 31000}}, 16384)
        self.assertEqual(out["thinking"]["budget_tokens"], 15360)

    def test_bodies_that_are_not_messages_pass_unchanged(self) -> None:
        body = {"model": "x"}
        self.assertIs(model_shim.cap_output(body), body)


class FakeUpstream(http.server.BaseHTTPRequestHandler):
    seen: list[dict] = []

    def log_message(self, *a: object) -> None:
        pass

    def do_GET(self) -> None:  # noqa: N802
        self.reply(200, b'{"data": []}')

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeUpstream.seen.append({"path": self.path, "key": self.headers.get("x-api-key"), "body": body})
        if any(m["role"] == "system" for m in body["messages"]):
            self.reply(500, b'{"error": "System message must be at the beginning"}')
        else:
            self.reply(200, b"event: message_start\ndata: {}\n\n")

    def reply(self, code: int, data: bytes) -> None:
        self.send_response(code)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class ShimTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.addCleanup(self.up.shutdown)
        keyfile = Path(self.tmp.name) / "key"
        keyfile.write_text("real-key\n")
        self.cfg = model_shim.load_config(Path(self.tmp.name) / "none.env", {
            "LOCAL_MODEL_URL": f"http://127.0.0.1:{self.up.server_address[1]}", "LOCAL_MODEL_KEY_FILE": str(keyfile)})
        FakeUpstream.seen = []

    def test_claude_codes_request_shape_reaches_the_model_with_the_real_key(self) -> None:
        shim = http.server.ThreadingHTTPServer(("127.0.0.1", 0), model_shim.make_handler(self.cfg))
        threading.Thread(target=shim.serve_forever, daemon=True).start()
        self.addCleanup(shim.shutdown)
        body = {"model": "m", "system": [{"type": "text", "text": "A"}], "stream": True,
                "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": [{"type": "text", "text": "env"}]}]}
        req = urllib.request.Request(f"http://127.0.0.1:{shim.server_address[1]}/v1/messages", data=json.dumps(body).encode(),
                                     headers={"x-api-key": "local-shim", "content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn(b"message_start", resp.read())
        self.assertEqual(FakeUpstream.seen[0]["key"], "real-key")
        self.assertEqual(FakeUpstream.seen[0]["path"], "/v1/messages")
        self.assertEqual(FakeUpstream.seen[0]["body"]["n_predict"], model_shim.MAX_OUTPUT_TOKENS)  # capped on the wire

    def test_openai_style_calls_pass_through_unchanged(self) -> None:
        # a staged app on the devbox calls /v1/chat/completions through the gateway: its system message must stay put
        shim = http.server.ThreadingHTTPServer(("127.0.0.1", 0), model_shim.make_handler(self.cfg))
        threading.Thread(target=shim.serve_forever, daemon=True).start()
        self.addCleanup(shim.shutdown)
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": "x"}]}
        req = urllib.request.Request(f"http://127.0.0.1:{shim.server_address[1]}/v1/chat/completions",
                                     data=json.dumps(body).encode(), headers={"authorization": "Bearer nothing"})
        with self.assertRaises(urllib.error.HTTPError):  # the fake upstream refuses mid-conversation system messages
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(FakeUpstream.seen[0]["body"], body)
        self.assertEqual(FakeUpstream.seen[0]["key"], "real-key")

    def test_serve_on_a_fixed_port(self) -> None:
        sock = __import__("socket").socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        env = {"LOCAL_MODEL_URL": self.cfg["LOCAL_MODEL_URL"], "LOCAL_MODEL_KEY_FILE": self.cfg["LOCAL_MODEL_KEY_FILE"],
               "HOME": self.tmp.name}
        proc = subprocess.Popen([sys.executable, "-I", str(ROOT / "scripts" / "model_shim.py"), "serve", "--port", str(port)],
                                stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(proc.kill)
        self.assertEqual((proc.stdout.readline() if proc.stdout else "").strip(), f"PORT {port}")

    def test_without_the_shim_the_same_request_fails(self) -> None:
        # the known-bad input: proves the fake upstream behaves like llama.cpp with the Qwen template
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}, {"role": "system", "content": "env"}]}
        req = urllib.request.Request(f"{self.cfg['LOCAL_MODEL_URL']}/v1/messages", data=json.dumps(body).encode())
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(req, timeout=10)

    def test_check_reports_a_down_model_in_plain_words(self) -> None:
        self.assertIsNone(model_shim.check(self.cfg))
        sock = __import__("socket").socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()  # a port nobody listens on
        down = {**self.cfg, "LOCAL_MODEL_URL": f"http://127.0.0.1:{port}"}
        self.assertIn("unreachable", model_shim.check(down) or "")

    def test_serve_prints_its_port_first(self) -> None:
        env = {"LOCAL_MODEL_URL": self.cfg["LOCAL_MODEL_URL"], "LOCAL_MODEL_KEY_FILE": self.cfg["LOCAL_MODEL_KEY_FILE"],
               "HOME": self.tmp.name}
        proc = subprocess.Popen([sys.executable, "-I", str(ROOT / "scripts" / "model_shim.py"), "serve"],
                                stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(proc.kill)
        line = proc.stdout.readline() if proc.stdout else ""
        self.assertRegex(line, r"^PORT \d+$")


def free_port() -> int:
    sock = __import__("socket").socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class Named(FakeUpstream):
    name = "qwen3.6-35b-a3b"

    def do_GET(self) -> None:  # noqa: N802
        self.reply(200, json.dumps({"models": [{"name": Named.name}]}).encode())


class PickTests(unittest.TestCase):
    """`local` is whichever local model serves now, in the configured order; the factory never loads one."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Named)
        threading.Thread(target=self.up.serve_forever, daemon=True).start()
        self.addCleanup(self.up.shutdown)
        self.live = f"http://127.0.0.1:{self.up.server_address[1]}"
        self.dead = f"http://127.0.0.1:{free_port()}"

    def cfg(self, **kw: str) -> dict[str, str]:
        return model_shim.load_config(Path(self.tmp.name) / "none.env",
                                      {"LOCAL_MODEL_KEY_FILE": str(Path(self.tmp.name) / "nokey"), **kw})

    def test_the_first_server_that_answers_is_used_with_the_name_it_serves(self) -> None:
        found, _ = model_shim.pick(self.cfg(LOCAL_MODEL_SERVERS=f"code={self.dead},38={self.live},agent={self.dead}"))
        assert found is not None
        self.assertEqual((found["LOCAL_MODEL_SERVER"], found["LOCAL_MODEL_URL"], found["LOCAL_MODEL_NAME"]),
                         ("38", self.live, "qwen3.6-35b-a3b"))

    def test_nothing_serving_names_each_server(self) -> None:
        found, why = model_shim.pick(self.cfg(LOCAL_MODEL_SERVERS=f"code={self.dead},agent={self.dead}"))
        self.assertIsNone(found)
        self.assertIn("code: unreachable", why)
        self.assertIn("agent: unreachable", why)

    def test_without_a_server_list_the_single_url_is_used(self) -> None:
        found, _ = model_shim.pick(self.cfg(LOCAL_MODEL_URL=self.live))
        self.assertEqual(found and found["LOCAL_MODEL_SERVER"], "local")

    def test_an_unreachable_host_is_woken_once_then_the_model_is_used(self) -> None:
        marker = Path(self.tmp.name) / "woken"
        cfg = self.cfg(LOCAL_MODEL_SERVERS=f"code={self.dead}", LOCAL_MODEL_ARBITER_URL=self.dead,
                       LOCAL_MODEL_WAKE_CMD=f"echo x >> {marker}")
        t = [0.0]
        ticks = [0]

        def sleep(s: float) -> None:
            t[0] += s
            ticks[0] += 1
            if ticks[0] == 3:  # the PC came up on the third poll
                cfg["LOCAL_MODEL_SERVERS"] = f"code={self.live}"
        found, why = model_shim.wait_for_model(cfg, 600, sleep=sleep, clock=lambda: t[0])
        self.assertIsNotNone(found, why)
        self.assertEqual(marker.read_text().count("x"), 1)

    def test_a_gpu_the_owner_holds_is_never_asked_for(self) -> None:
        class Desk(FakeUpstream):
            def do_GET(self) -> None:  # noqa: N802
                self.reply(200, b'{"state": "held", "reason": "llm desk for 4 h"}')

            def do_POST(self) -> None:  # noqa: N802
                FakeUpstream.seen.append({"path": self.path})
                self.reply(200, b"{}")
        arb = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Desk)
        threading.Thread(target=arb.serve_forever, daemon=True).start()
        self.addCleanup(arb.shutdown)
        FakeUpstream.seen = []
        cfg = self.cfg(LOCAL_MODEL_SERVERS=f"code={self.dead}",
                       LOCAL_MODEL_ARBITER_URL=f"http://127.0.0.1:{arb.server_address[1]}")
        found, why = model_shim.wait_for_model(cfg, 600, sleep=lambda s: None)
        self.assertIsNone(found)
        self.assertIn("held for the owner", why)
        self.assertEqual(FakeUpstream.seen, [])  # no POST /request

    def test_a_desk_model_that_is_loading_is_waited_for_not_refused(self) -> None:
        class Loading(FakeUpstream):
            def do_GET(self) -> None:  # noqa: N802
                self.reply(200, b'{"state": "desk", "reason": "a desk server is starting"}')

            def do_POST(self) -> None:  # noqa: N802
                FakeUpstream.seen.append({"path": self.path})
                self.reply(200, b"{}")
        arb = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Loading)
        threading.Thread(target=arb.serve_forever, daemon=True).start()
        self.addCleanup(arb.shutdown)
        FakeUpstream.seen = []
        cfg = self.cfg(LOCAL_MODEL_SERVERS=f"code={self.dead}",
                       LOCAL_MODEL_ARBITER_URL=f"http://127.0.0.1:{arb.server_address[1]}")
        t = [0.0]
        ticks = [0]

        def sleep(s: float) -> None:
            t[0] += s
            ticks[0] += 1
            if ticks[0] == 4:  # `llm code` finished loading
                cfg["LOCAL_MODEL_SERVERS"] = f"code={self.live}"
        found, why = model_shim.wait_for_model(cfg, 600, sleep=sleep, clock=lambda: t[0])
        self.assertIsNotNone(found, why)
        self.assertEqual(FakeUpstream.seen, [])  # it never asked the arbiter for its own model

    def test_check_prints_server_and_name(self) -> None:
        env = {"LOCAL_MODEL_SERVERS": f"code={self.live}", "HOME": self.tmp.name}
        out = subprocess.run([sys.executable, "-I", str(ROOT / "scripts" / "model_shim.py"), "check"],
                             capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.stdout.strip(), "ok code qwen3.6-35b-a3b")


if __name__ == "__main__":
    unittest.main()


class FastEquivalentTests(unittest.TestCase):
    def test_tokens_are_priced_at_the_fast_profiles_rates(self) -> None:
        import dispatch
        usage = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_input_tokens": 2_000_000,
                 "cache_creation_input_tokens": 0}
        self.assertAlmostEqual(dispatch.fast_equivalent(usage), 2.0 + 1.0 + 0.2)
