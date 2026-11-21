#!/usr/bin/env python3
"""Staging on the GPU workstation: build the PR head of an app, run it in a fixed sandbox, print the tailnet URL.

Usage:
  stage.py up <app> <issue>     build the head of the OPEN PR for agent/issue-<n> and (re)start it
  stage.py up <app> --ref <r>   build a branch, tag or commit that is not a PR (release candidate, current main)
  stage.py down <app>           stop and remove the staging container
  stage.py status               what runs, which commit, how old

Rules (the human only clicks the link):
  - Only a commit whose four required checks are green on Gitea is staged, and a PR build only while its PR is open
    (a merged or stale branch is refused: it would not be the code the owner is about to merge).
  - Before the build, the chat route is probed with an empty body and the configured key (refused before any inference,
    so nothing is loaded; the model list is open and proves nothing). A rejected key or an unreachable endpoint stops
    staging; a busy or loading model is reported and staging goes on.
  - The sandbox policy is fixed here, not chosen per app: read-only root, no capabilities, no new privileges,
    memory and pid limits, tmpfs for /tmp and /data, one container per app, restarts with the GPU workstation.
  - The model comes from ~/.config/factory/staging.env (bring your own model: any OpenAI-compatible endpoint;
    LLM_PROVIDER, LLM_BASE_URL, LLM_MODEL, optional LLM_API_KEY and LLM_TLS_*_FILE). Keys starting with STAGING_ are
    for this script and are not passed on: STAGING_HOST (name shown in the link; default: the tailnet IP), STAGING_BIND_IP
    (the address the staged build listens on; default: the tailnet IP). On the devbox VM, which has no Tailscale of its own
    and is reached through the tailnet's subnet router, STAGING_BIND_IP is its LAB address.
    ~/.config/factory/apps/<app>.env adds app-specific settings. Both files are mode 0600 and never tracked.
    Do not copy a secret into them: `LLM_API_KEY_FILE=<path>` makes stage.py read the key from its source file.
    `scripts/doctor.py` checks all of this.
  - The URL is only ever written to Gitea comments and the ignored state/ folder, never to a tracked file.
  - Two containers per app, same image and policy: one on the tailnet address (phones and other devices) and a twin on
    127.0.0.1 for a browser on this machine (the Windows host cannot reach the tailnet address of its own WSL service).
Contract for an app image: starts with a CMD that has `--host` and `--port`, serves /healthz, writes only to /tmp
and /data, runs as a non-root user.

Stopgap: when the model URL is a loopback address, the container shares the host network (the model listens on
127.0.0.1 only). That goes away when the model is reached over the mTLS route with a bridge network.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gitea import Gitea  # noqa: E402

WORK = Path(os.environ.get("WORK", Path.home() / "git" / "software-factory"))
STATE = WORK / "state"
REGISTRY = STATE / "staging.json"
CONF = Path(os.environ.get("FACTORY_CONF", Path.home() / ".config" / "factory"))
PORTS = range(8800, 8900)
REQUIRED = ("checks", "gates", "security", "holdout")
INTERNAL_PORT = 8000
POLICY = ["--read-only", "--tmpfs", "/tmp", "--tmpfs", "/data:uid=10001,gid=10001", "--cap-drop", "ALL",
          "--security-opt", "no-new-privileges", "--memory", "1g", "--pids-limit", "200",
          "--restart", "unless-stopped"]


class StageError(RuntimeError):
    pass


def sh(*args: str, cwd: Path | None = None, stdin: bytes | None = None, check: bool = True) -> str:
    res = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, check=False)
    if check and res.returncode != 0:
        raise StageError(f"{' '.join(args[:3])} failed: {res.stderr.decode(errors='replace')[-600:]}")
    return res.stdout.decode(errors="replace").strip()


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("\"'")
    return out


SECRET_FILES = {"LLM_API_KEY_FILE": "LLM_API_KEY"}  # a secret may be referenced by file instead of copied into the env file


def load_model_env(app: str) -> dict[str, str]:
    """staging.env plus apps/<app>.env, with referenced secret files read in. The values stay in memory."""
    env = read_env(CONF / "staging.env")
    env.update(read_env(CONF / "apps" / f"{app}.env"))
    for ref, name in SECRET_FILES.items():
        if ref in env:
            path = Path(env.pop(ref)).expanduser()
            if not path.is_file():
                raise StageError(f"{ref} points at {path}, which does not exist. Run scripts/doctor.py.")
            env[name] = path.read_text().strip()
    return env


def tailnet_ip() -> str:
    for line in sh("ip", "-4", "-o", "addr").splitlines():
        m = re.search(r"inet (100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+)/", line)
        if m:
            return m.group(1)
    raise StageError("No tailnet address on this machine. Is the Windows host's Tailscale connected? (On a host reached "
                     "through a subnet router, set STAGING_BIND_IP in staging.env.)")


def bind_ip(env: dict[str, str]) -> str:
    """STAGING_BIND_IP when set, else the tailnet address. Only an address this machine has, never every interface."""
    want = env.get("STAGING_BIND_IP", "").strip()
    if not want:
        return tailnet_ip()
    if want in ("0.0.0.0", "*") or not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", want):
        raise StageError(f"STAGING_BIND_IP={want} is not allowed: name one address of this machine")
    if not re.search(rf"inet {re.escape(want)}/", sh("ip", "-4", "-o", "addr")):
        raise StageError(f"STAGING_BIND_IP={want} is not an address of this machine")
    return want


def load_registry() -> dict[str, dict]:
    return json.loads(REGISTRY.read_text()) if REGISTRY.exists() else {}


def save_registry(reg: dict[str, dict]) -> None:
    STATE.mkdir(exist_ok=True)
    REGISTRY.write_text(json.dumps(reg, indent=2))


def pick_port(reg: dict[str, dict], app: str) -> int:
    if app in reg:
        return int(reg[app]["port"])
    used = {int(v["port"]) for v in reg.values()}
    return next(p for p in PORTS if p not in used)


def is_loopback(url: str) -> bool:
    m = re.match(r"https?://([^/:]+)", url)
    return bool(m) and m.group(1) in ("127.0.0.1", "localhost", "::1")


def check_status(g: Gitea, repo: str, sha: str) -> dict[str, str]:
    seen: dict[str, str] = {}
    for s in g.get(f"/repos/{repo}/commits/{sha}/status").get("statuses") or []:
        for name in REQUIRED:
            if f"/ {name} " in s["context"] or s["context"].endswith(f"/ {name}"):
                seen[name] = s["status"]
    return {n: seen.get(n, "missing") for n in REQUIRED if seen.get(n) != "success"}


def require_green(g: Gitea, repo: str, sha: str, clone: Path | None = None) -> None:
    """CI runs on pull requests only, so a merge commit on main has no checks of its own. It counts as verified when
    its tree is identical to the tree of its green PR head (second parent)."""
    bad = check_status(g, repo, sha)
    if bad and clone is not None:
        parents = sh("git", "rev-list", "--parents", "-n", "1", sha, cwd=clone).split()[1:]
        if len(parents) == 2 and sh("git", "rev-parse", f"{sha}^{{tree}}", cwd=clone) == \
                sh("git", "rev-parse", f"{parents[1]}^{{tree}}", cwd=clone) and not check_status(g, repo, parents[1]):
            return
    if bad:
        raise StageError(f"Not staging {sha[:7]}: required checks are not all green: {bad}")


def command_for(image: str, host: str, port: int) -> list[str]:
    cmd = json.loads(sh("docker", "inspect", "--format", "{{json .Config.Cmd}}", image))
    if not cmd or "--host" not in cmd or "--port" not in cmd:
        raise StageError("The image CMD needs --host and --port so staging can bind the tailnet address.")
    cmd = list(cmd)
    cmd[cmd.index("--host") + 1] = host
    cmd[cmd.index("--port") + 1] = str(port)
    return cmd


def wait_healthy(url: str, timeout: int = 90) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:  # noqa: S310
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    raise StageError(f"{url} did not answer 200 within {timeout}s")


def preflight_model(model: dict[str, str]) -> str:
    """Probe the endpoint's chat route with an empty body and the configured key. The model list is often open, so
    it proves nothing about the key. An empty body is refused before any inference, so nothing is loaded:
    401/403 = key rejected, 400/422 = key accepted, 502/503 = busy or loading. Returns a status for the review comment."""
    url = model["LLM_BASE_URL"].rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json"}
    if model.get("LLM_API_KEY"):
        headers["Authorization"] = f"Bearer {model['LLM_API_KEY']}"
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310
            return "reachable, key accepted" if r.status == 200 else f"answered {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise StageError(f"The model endpoint rejected the configured key ({e.code}). Fix LLM_API_KEY in staging.env "
                             "or the file LLM_API_KEY_FILE points at.") from e
        if e.code in (400, 422):
            return "reachable, key accepted"
        if e.code in (502, 503):
            return "busy or loading (the first decode may answer 503)"
        return f"answered {e.code}"
    except (urllib.error.URLError, OSError) as e:
        raise StageError(f"The model endpoint is unreachable: {getattr(e, 'reason', e)}") from e


def resolve_target(g: Gitea, repo: str, clone: Path, issue: int | None, ref: str | None) -> tuple[str, str]:
    """Return (commit sha, label). A PR build needs an open PR; anything else must be named with --ref."""
    sh("git", "fetch", "-q", "--tags", "origin", cwd=clone)
    if ref:
        return sh("git", "rev-parse", f"{ref}^{{commit}}", cwd=clone), ref
    branch = f"agent/issue-{issue}"
    pr = next((p for p in g.get(f"/repos/{repo}/pulls?state=open") if p["head"]["ref"] == branch), None)
    if not pr:
        raise StageError(f"No open PR for {branch}. Only the head of an open PR is staged; use --ref for anything else.")
    return pr["head"]["sha"], branch


def run_args(name: str, app: str, image: str, bind_ip: str, port: int, env_file: str, host_net: bool) -> list[str]:
    """The docker run command for one staged container: the fixed sandbox policy, the model env file, and either the host
    network with the app told to bind `bind_ip` (the model is on loopback) or a published port on `bind_ip`."""
    args = ["docker", "run", "-d", "--name", name, "--label", f"factory.stage={app}", *POLICY, "--env-file", env_file]
    if host_net:
        return [*args, "--network", "host", image, *command_for(image, bind_ip, port)]
    return [*args, "-p", f"{bind_ip}:{port}:{INTERNAL_PORT}", image]


LOOPBACK = "127.0.0.1"


def twin_args(app: str, image: str, port: int, env_file: str, host_net: bool) -> list[str]:
    """The twin for a browser on this machine: the same image and sandbox, reachable on loopback only. A Windows host cannot
    reach the tailnet address of the WSL service it hosts (the request hangs), but it reaches localhost."""
    return run_args(f"stage-{app}-local", app, image, LOOPBACK, port, env_file, host_net)


def up(app: str, issue: int | None = None, ref: str | None = None) -> dict:
    g = Gitea()
    repo = f"{g.user}/{app}"
    clone = WORK / app
    sha, label = resolve_target(g, repo, clone, issue, ref)
    require_green(g, repo, sha, clone)

    model = load_model_env(app)
    if not model.get("LLM_BASE_URL"):
        raise StageError(f"No model configured: put LLM_PROVIDER, LLM_BASE_URL, LLM_MODEL in {CONF / 'staging.env'}")
    ip = bind_ip(model)
    host_name = model.pop("STAGING_HOST", "") or ip
    model = {k: v for k, v in model.items() if not k.startswith("STAGING_")}
    model_status = preflight_model(model)

    reg = load_registry()
    port = pick_port(reg, app)
    image = f"{app}-stage:{sha[:7]}"
    if not sh("docker", "images", "-q", image):
        archive = subprocess.run(["git", "archive", sha], cwd=clone, capture_output=True, check=True).stdout
        sh("docker", "build", "-q", "-t", image, "-", stdin=archive)

    name = f"stage-{app}"
    for old in (name, f"{name}-local"):
        sh("docker", "rm", "-f", old, check=False)
    host_net = is_loopback(model["LLM_BASE_URL"])
    with tempfile.NamedTemporaryFile("w", delete=True, dir=STATE if STATE.exists() else None) as envf:
        os.chmod(envf.name, 0o600)
        envf.write("".join(f"{k}={v}\n" for k, v in model.items()))
        envf.flush()
        sh(*run_args(name, app, image, ip, port, envf.name, host_net))
        sh(*twin_args(app, image, port, envf.name, host_net))
    wait_healthy(f"http://{ip}:{port}/healthz")
    wait_healthy(f"http://{LOOPBACK}:{port}/healthz")

    info = {"app": app, "issue": issue, "ref": label, "model": model_status, "sha": sha, "port": port, "network": "host" if host_net else "bridge",
            "ip": ip, "url": f"http://{host_name}:{port}/", "local_url": f"http://localhost:{port}/", "started": datetime.now(UTC).isoformat(timespec="seconds")}
    reg[app] = info
    save_registry(reg)
    return info


def down(app: str) -> bool:
    for name in (f"stage-{app}", f"stage-{app}-local"):
        sh("docker", "rm", "-f", name, check=False)
    reg = load_registry()
    gone = reg.pop(app, None) is not None
    save_registry(reg)
    return gone


def status() -> list[dict]:
    reg = load_registry()
    running = set(sh("docker", "ps", "--filter", "label=factory.stage", "--format", "{{.Names}}").split())
    return [{**v, "running": f"stage-{k}" in running} for k, v in reg.items()]


def main(argv: list[str]) -> int:
    try:
        if len(argv) >= 4 and argv[1] == "up" and argv[3] == "--ref" and len(argv) >= 5:
            print(json.dumps(up(argv[2], ref=argv[4]), indent=2))
        elif len(argv) >= 4 and argv[1] == "up":
            print(json.dumps(up(argv[2], int(argv[3])), indent=2))
        elif len(argv) >= 3 and argv[1] == "down":
            print("removed" if down(argv[2]) else "nothing staged")
        elif argv[1:2] == ["status"]:
            print(json.dumps(status(), indent=2))
        else:
            print(__doc__)
            return 2
    except StageError as e:
        print(f"STAGING FAILED: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
