#!/usr/bin/env python3
"""A small Anthropic-compatible pass-through that lets `claude -p` run on the local model (`/go model=local`).

llama.cpp already speaks the Anthropic Messages API, but Claude Code sends a `role: system` message in the middle of
`messages` and the Qwen chat template refuses that ("System message must be at the beginning"). This shim moves such
messages into the top-level `system`, adds the real key (the agent never sees it) and, when configured, the client
certificate for the mutual-TLS route. Responses are streamed through unchanged.

dispatch.py starts it on a free loopback port for one agent step and stops it afterwards: nothing runs between steps.
On a host where staged apps reach the model through it (the devbox), `serve --port N` runs it as a fixed gateway
(ops/systemd/factory-model-gateway.service): the app calls plain HTTP on loopback, the shim adds the key and the client
certificate, so no secret enters the container. Only `/v1/messages` bodies are reshaped; OpenAI-style calls
(`/v1/chat/completions`) pass through unchanged.

"local" means whichever local model is serving now. The factory never loads or switches a model: one GPU fits one
model and the owner chooses it (`llm code`, `llm 38`, or the agent server the arbiter starts when the GPU is free).
With LOCAL_MODEL_SERVERS set, the shim asks each server in order and uses the first that answers; the name it serves
under is read from its /v1/models. When none answers, `check --wait S` asks the arbiter for its model and, when the
host does not answer at all, runs LOCAL_MODEL_WAKE_CMD once, then waits up to S seconds.

Config: ~/.config/factory/local-model.env (or the environment), all optional:
  LOCAL_MODEL_SERVERS    name=url,name=url,...  servers in order of preference (default: LOCAL_MODEL_URL alone)
  LOCAL_MODEL_URL        upstream base URL                default http://127.0.0.1:8090
  LOCAL_MODEL_NAME       model name when /v1/models names none   default qwen3.8-27b-agent
  LOCAL_MODEL_ARBITER_URL  GPU arbiter (GET /status, POST /request), for example <front end>/arbiter
  LOCAL_MODEL_WAKE_CMD   command that wakes the model host (for example ops/devbox/wake-desktop.sh)
  LOCAL_MODEL_KEY_FILE   bearer key file                  default ~/.config/llm-agent/api-key
  LOCAL_MODEL_TLS_CERT_FILE, LOCAL_MODEL_TLS_KEY_FILE, LOCAL_MODEL_TLS_CA_FILE   client cert, key and CA for mTLS
Secrets stay in their files; this file only names them.

Usage: model_shim.py serve [--port N]   prints `PORT <n>` on the first line, then serves until killed (default: a free port)
       model_shim.py check [--wait S]   prints `ok <server> <model>` and exits 0 when a server answers, else why, exit 1
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

CONFIG = Path.home() / ".config" / "factory" / "local-model.env"
DEFAULTS = {"LOCAL_MODEL_URL": "http://127.0.0.1:8090", "LOCAL_MODEL_NAME": "qwen3.8-27b-agent",
            "LOCAL_MODEL_KEY_FILE": str(Path.home() / ".config" / "llm-agent" / "api-key")}
HOP = {"host", "content-length", "accept-encoding", "connection", "x-api-key", "authorization", "transfer-encoding"}
UPSTREAM_TIMEOUT_S = 900


def load_config(path: Path = CONFIG, env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ) if env is None else env
    cfg = dict(DEFAULTS)
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                cfg[k.strip()] = os.path.expanduser(v.strip().strip("'\""))
    cfg.update({k: v for k, v in env.items() if k.startswith("LOCAL_MODEL_")})
    return cfg


def normalize(body: dict[str, Any]) -> dict[str, Any]:
    """Move every `role: system` message into the top-level system prompt, in order. Nothing else changes."""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not any(isinstance(m, dict) and m.get("role") == "system" for m in msgs):
        return body
    system = body.get("system") or []
    blocks: list[Any] = [{"type": "text", "text": system}] if isinstance(system, str) else list(system)
    keep = []
    for m in msgs:
        if isinstance(m, dict) and m.get("role") == "system":
            c = m.get("content", "")
            blocks += [{"type": "text", "text": c}] if isinstance(c, str) else list(c)
        else:
            keep.append(m)
    return {**body, "system": blocks, "messages": keep}


def connection(cfg: dict[str, str], timeout: float = UPSTREAM_TIMEOUT_S) -> tuple[http.client.HTTPConnection, str]:
    u = urllib.parse.urlsplit(cfg["LOCAL_MODEL_URL"])
    prefix = u.path.rstrip("/").removesuffix("/v1")
    if u.scheme == "https":
        ctx = ssl.create_default_context(cafile=cfg.get("LOCAL_MODEL_TLS_CA_FILE") or None)
        if cfg.get("LOCAL_MODEL_TLS_CERT_FILE"):
            ctx.load_cert_chain(cfg["LOCAL_MODEL_TLS_CERT_FILE"], cfg.get("LOCAL_MODEL_TLS_KEY_FILE") or None)
        return http.client.HTTPSConnection(u.hostname or "", u.port or 443, timeout=timeout, context=ctx), prefix
    return http.client.HTTPConnection(u.hostname or "", u.port or 80, timeout=timeout), prefix


def key(cfg: dict[str, str]) -> str:
    path = Path(cfg["LOCAL_MODEL_KEY_FILE"])
    return path.read_text().strip() if path.exists() else ""  # the desk servers take no key


def servers(cfg: dict[str, str]) -> list[tuple[str, str]]:
    """(name, base URL) in order of preference."""
    raw = cfg.get("LOCAL_MODEL_SERVERS", "").strip()
    if not raw:
        return [("local", cfg["LOCAL_MODEL_URL"])]
    out = []
    for item in raw.split(","):
        name, _, url = item.strip().partition("=")
        if name and url:
            out.append((name.strip(), url.strip()))
    return out


def served_name(body: str) -> str | None:
    """The model name in a /v1/models answer (OpenAI `data[].id`, or llama.cpp's `models[].name`)."""
    try:
        doc = json.loads(body)
    except json.JSONDecodeError:
        return None
    for k, field in (("data", "id"), ("models", "name")):
        items = doc.get(k) if isinstance(doc, dict) else None
        if isinstance(items, list) and items and isinstance(items[0], dict) and items[0].get(field):
            return str(items[0][field])
    return None


def probe(cfg: dict[str, str], url: str, timeout: float = 15) -> tuple[str | None, str]:
    """(model name, '') when the server at `url` lists a model, else (None, reason)."""
    one = {**cfg, "LOCAL_MODEL_URL": url}
    try:
        conn, prefix = connection(one, timeout=timeout)
        conn.request("GET", prefix + "/v1/models", headers={"authorization": f"Bearer {key(cfg)}"})
        resp = conn.getresponse()
        body = resp.read(4000).decode(errors="replace")
    except (OSError, ssl.SSLError) as e:
        return None, f"unreachable ({type(e).__name__}: {str(e)[:100]})"
    if resp.status != 200:
        return None, f"HTTP {resp.status} ({body[:120]})"
    return served_name(body) or cfg["LOCAL_MODEL_NAME"], ""


def pick(cfg: dict[str, str]) -> tuple[dict[str, str] | None, str]:
    """The config for the first server that answers, with its URL and served name, else (None, why each failed)."""
    why = []
    for name, url in servers(cfg):
        model, reason = probe(cfg, url)
        if model:
            return {**cfg, "LOCAL_MODEL_URL": url, "LOCAL_MODEL_NAME": model, "LOCAL_MODEL_SERVER": name}, ""
        why.append(f"{name}: {reason}")
    return None, "; ".join(why)


def arbiter(cfg: dict[str, str], method: str = "GET") -> dict[str, Any] | None:
    """The arbiter's status (GET /status) or the answer to POST /request; None when it does not answer."""
    base = cfg.get("LOCAL_MODEL_ARBITER_URL", "").rstrip("/")
    if not base:
        return None
    try:
        conn, prefix = connection({**cfg, "LOCAL_MODEL_URL": base}, timeout=10)
        conn.request(method, prefix + ("/status" if method == "GET" else "/request"), b"" if method == "POST" else None)
        resp = conn.getresponse()
        body = resp.read(4000).decode(errors="replace")
        return json.loads(body) if resp.status == 200 and body.strip().startswith("{") else {}
    except (OSError, ssl.SSLError, json.JSONDecodeError):
        return None


def wait_for_model(cfg: dict[str, str], wait_s: float, poll_s: float = 15,
                   sleep: Any = time.sleep, clock: Any = time.monotonic) -> tuple[dict[str, str] | None, str]:
    """pick(), and when nothing answers: ask the arbiter for its model, or wake the host (once), then poll until
    `wait_s` runs out. Never loads a model itself and never asks for one while the GPU is the owner's (desk/held)."""
    found, why = pick(cfg)
    if found or wait_s <= 0:
        return found, why
    deadline, woke, asked, notes = clock() + wait_s, False, False, []
    while clock() < deadline:
        st = arbiter(cfg)
        if st is None and not woke and cfg.get("LOCAL_MODEL_WAKE_CMD"):
            woke = True
            res = subprocess.run(cfg["LOCAL_MODEL_WAKE_CMD"], shell=True, capture_output=True, text=True, timeout=60)
            notes.append("wake sent" if res.returncode == 0 else f"wake failed: {(res.stdout + res.stderr).strip()[-120:]}")
        elif st is not None and st.get("state") in ("desk", "held"):
            return None, f"{why}; the GPU is the owner's ({st.get('state')}: {st.get('reason', '')})"
        elif st is not None and not asked:
            asked = True
            arbiter(cfg, "POST")
            notes.append("asked the arbiter for its model")
        sleep(poll_s)
        found, why = pick(cfg)
        if found:
            return found, ""
    return None, "; ".join([why, *notes, f"still nothing after {wait_s:.0f} s"])


def make_handler(cfg: dict[str, str], refresh_s: float = 30) -> type[http.server.BaseHTTPRequestHandler]:
    secret = key(cfg)
    current: dict[str, Any] = {"at": 0.0, "cfg": cfg}

    def upstream() -> dict[str, str]:
        if len(servers(cfg)) > 1 and time.monotonic() - current["at"] > refresh_s:
            found, _ = pick(cfg)
            current.update(at=time.monotonic(), cfg=found or cfg)
        return current["cfg"]

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: object) -> None:  # no request log: prompts may hold personal data
            pass

        def forward(self, method: str) -> None:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            if raw and method == "POST" and self.path.split("?")[0].rstrip("/").endswith("/v1/messages"):
                try:
                    raw = json.dumps(normalize(json.loads(raw))).encode()
                except json.JSONDecodeError:
                    pass  # not JSON: pass it on, the upstream answers with its own error
            conn, prefix = connection(upstream())
            headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            headers.update({"x-api-key": secret, "authorization": f"Bearer {secret}", "content-length": str(len(raw))})
            try:
                conn.request(method, prefix + self.path, raw or None, headers)
                resp = conn.getresponse()
            except OSError as e:
                msg = json.dumps({"type": "error", "error": {"type": "api_error",
                                                             "message": f"local model unreachable: {type(e).__name__}"}})
                self.send_response(502)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg.encode())
                return
            self.send_response(resp.status)
            for k, v in resp.getheaders():
                if k.lower() not in ("transfer-encoding", "content-length", "connection"):
                    self.send_header(k, v)
            self.send_header("connection", "close")
            self.end_headers()
            self.close_connection = True
            while chunk := resp.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
            conn.close()

        def do_POST(self) -> None:  # noqa: N802
            self.forward("POST")

        def do_GET(self) -> None:  # noqa: N802
            self.forward("GET")

    return Handler


def check(cfg: dict[str, str]) -> str | None:
    """None when a server lists models, else a short reason for the owner."""
    found, why = pick(cfg)
    return None if found else f"no local model is serving ({why}): it may be loading, the GPU may be in use, or the host is off"


def main() -> None:
    cfg = load_config()
    args = sys.argv[1:]
    if args[:1] == ["check"]:
        wait = float(args[2]) if args[1:2] == ["--wait"] and len(args) == 3 else 0
        found, why = wait_for_model(cfg, wait)
        if found:
            print(f"ok {found['LOCAL_MODEL_SERVER']} {found['LOCAL_MODEL_NAME']}")
            sys.exit(0)
        print(f"no local model is serving ({why})")
        sys.exit(1)
    if args[:1] != ["serve"] or len(args) not in (1, 3) or (len(args) == 3 and args[1] != "--port"):
        sys.exit(__doc__)
    port = int(args[2]) if len(args) == 3 else 0
    found, _ = pick(cfg)  # a step uses the server that answered when it started (a gateway re-checks every 30 s)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(found or cfg))
    print(f"PORT {srv.server_address[1]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
