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

Config: ~/.config/factory/local-model.env (or the environment), all optional:
  LOCAL_MODEL_URL        upstream base URL                default http://127.0.0.1:8090
  LOCAL_MODEL_NAME       model name                       default qwen3.8-27b-agent
  LOCAL_MODEL_KEY_FILE   bearer key file                  default ~/.config/llm-agent/api-key
  LOCAL_MODEL_TLS_CERT_FILE, LOCAL_MODEL_TLS_KEY_FILE, LOCAL_MODEL_TLS_CA_FILE   client cert, key and CA for mTLS
Secrets stay in their files; this file only names them.

Usage: model_shim.py serve [--port N]   prints `PORT <n>` on the first line, then serves until killed (default: a free port)
       model_shim.py check    exits 0 when the upstream answers /v1/models, else prints why and exits 1
"""

from __future__ import annotations

import http.client
import http.server
import json
import os
import ssl
import sys
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
    return Path(cfg["LOCAL_MODEL_KEY_FILE"]).read_text().strip()


def make_handler(cfg: dict[str, str]) -> type[http.server.BaseHTTPRequestHandler]:
    secret = key(cfg)

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
            conn, prefix = connection(cfg)
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
    """None when the upstream lists models, else a short reason for the owner."""
    try:
        conn, prefix = connection(cfg, timeout=15)
        conn.request("GET", prefix + "/v1/models", headers={"authorization": f"Bearer {key(cfg)}"})
        resp = conn.getresponse()
        body = resp.read(2000).decode(errors="replace")
    except (OSError, ssl.SSLError) as e:
        return f"the local model is unreachable ({type(e).__name__}: {str(e)[:120]})"
    if resp.status != 200:
        return f"the local model answered HTTP {resp.status} ({body[:160]}): it may be loading or the GPU is in use"
    return None


def main() -> None:
    cfg = load_config()
    if sys.argv[1:] == ["check"]:
        why = check(cfg)
        print(why or "ok")
        sys.exit(1 if why else 0)
    args = sys.argv[1:]
    if args[:1] != ["serve"] or len(args) not in (1, 3) or (len(args) == 3 and args[1] != "--port"):
        sys.exit(__doc__)
    port = int(args[2]) if len(args) == 3 else 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), make_handler(cfg))
    print(f"PORT {srv.server_address[1]}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
