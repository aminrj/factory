#!/usr/bin/env python3
"""Check that this machine is ready to run the factory. Prints ok / WARN / FAIL with a fix hint; never prints a value.

Usage: doctor.py [--app <name>] [--install-hooks]

Checks the loose files the factory depends on (modes, required keys, no copied secrets), the model endpoint with its key,
the tailnet address, Docker, the Gitea token, the mirror deny list and the timers. Exit 1 when something FAILs.
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage  # noqa: E402

CONF = stage.CONF
TIMERS = ("factory-tick.timer", "factory-status.timer", "nightly-health.timer", "lookback.timer", "rework-digest.timer")
results: list[tuple[str, str, str]] = []


def say(level: str, what: str, hint: str = "") -> None:
    results.append((level, what, hint))
    print(f"{level:4}  {what}" + (f"\n      -> {hint}" if hint and level != "ok" else ""))


def mode_ok(path: Path, want: int) -> bool:
    return stat.S_IMODE(path.stat().st_mode) & ~want == 0


def check_files(app: str | None) -> None:
    if not CONF.is_dir():
        say("FAIL", f"{CONF} is missing", "create it (mode 700) and add staging.env; see scripts/stage.py")
        return
    say("ok" if mode_ok(CONF, 0o700) else "WARN", f"{CONF} mode", f"chmod 700 {CONF}")
    files = [CONF / "staging.env", CONF / "mirror-deny.txt", CONF / "ntfy.env", *sorted((CONF / "apps").glob("*.env"))]
    for f in files:
        if not f.exists():
            level = "FAIL" if f.name == "staging.env" else "WARN"
            say(level, f"{f.name} is missing", {"staging.env": "needed for staging: LLM_PROVIDER, LLM_BASE_URL, LLM_MODEL, LLM_API_KEY_FILE",
                                                "mirror-deny.txt": "one regex per line for your private names; the built-in rules still apply",
                                                "ntfy.env": "optional, for push notifications"}.get(f.name, ""))
        elif not mode_ok(f, 0o600):
            say("FAIL", f"{f.name} is readable by others", f"chmod 600 {f}")
        else:
            say("ok", f"{f.name} present, mode 600")
    env = stage.read_env(CONF / "staging.env")
    missing = [k for k in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL") if not env.get(k)]
    if missing:
        say("FAIL", f"staging.env lacks {', '.join(missing)}")
    if "LLM_API_KEY" in env:
        say("WARN", "staging.env holds a copy of the model key", "replace it with LLM_API_KEY_FILE=<path of the key file>")
    if "STAGING_HOST" not in env:
        say("WARN", "STAGING_HOST is not set", "the link will use the tailnet IP instead of the machine name")


def check_model(app: str | None) -> None:
    try:
        model = stage.load_model_env(app or "")
        if not model.get("LLM_BASE_URL"):
            return
        say("ok", f"model endpoint {stage.preflight_model(model)}")
    except stage.StageError as e:
        say("FAIL", str(e))


def check_machine() -> None:
    try:
        ip = stage.bind_ip(stage.read_env(stage.CONF / "staging.env"))
        say("ok", f"staged builds listen on {ip}")
    except stage.StageError as e:
        say("FAIL", str(e))
    res = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True, check=False)
    say("ok" if res.returncode == 0 else "FAIL", "docker daemon reachable", "start Docker, or add your user to the docker group")
    for t in TIMERS:
        active = subprocess.run(["systemctl", "--user", "is-active", t], capture_output=True, text=True, check=False).stdout.strip()
        say("ok" if active == "active" else "WARN", f"{t} {active or 'unknown'}", f"systemctl --user enable --now {t}")


def check_runtime() -> None:
    """The timer runs the factory/ checkout. It must be on main with no local code changes, or it runs the wrong code."""
    import orchestrate

    problem = orchestrate.runtime_problem(stage.WORK / "factory")
    say("FAIL" if problem else "ok", problem or "the runtime checkout is on main and clean",
        "git -C factory switch main (keep dev work in a worktree: git worktree add ../worktrees/factory-<topic> -b <branch>)")


HOOK_MARK = "# factory runtime guard"


def hook_texts(runtime: Path) -> dict[str, str]:
    """Git hooks for the runtime checkout. Hooks live in the shared .git directory, so each one first checks that it is
    running in the runtime folder: worktrees of the same repo are not affected."""
    return {
        "pre-commit": f"""#!/bin/sh
{HOOK_MARK}
if [ "$(git rev-parse --show-toplevel)" = "{runtime}" ] && [ "$GIT_AUTHOR_NAME" != "factory-orchestrator" ]; then
  echo "REFUSED: {runtime} is the factory runtime (the timers run it). Do not commit here." >&2
  echo "Work in a worktree: git worktree add ../worktrees/factory-<topic> -b <branch> origin/main" >&2
  exit 1
fi
""",
        "post-checkout": f"""#!/bin/sh
{HOOK_MARK}
if [ "$3" = "1" ] && [ "$(git rev-parse --show-toplevel)" = "{runtime}" ]; then
  b=$(git rev-parse --abbrev-ref HEAD)
  if [ "$b" != "main" ]; then
    echo "WARNING: you switched the factory runtime to '$b'. The timers now run that code and the foreman pauses." >&2
    echo "Switch back: git switch main. Work in a worktree: git worktree add ../worktrees/factory-<topic> -b <branch> origin/main" >&2
  fi
fi
""",
    }


def hooks_dir(runtime: Path) -> Path:
    common = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=runtime, capture_output=True, text=True,
                            check=False).stdout.strip()
    return (runtime / common).resolve() / "hooks"


def hooks_installed(runtime: Path) -> bool:
    d = hooks_dir(runtime)
    return all((d / name).exists() and (d / name).read_text() == text for name, text in hook_texts(runtime).items())


def install_hooks(runtime: Path) -> None:
    d = hooks_dir(runtime)
    d.mkdir(parents=True, exist_ok=True)
    for name, text in hook_texts(runtime).items():
        path = d / name
        if path.exists() and HOOK_MARK not in path.read_text():
            raise SystemExit(f"{path} exists and is not ours; merge it by hand.")
        path.write_text(text)
        path.chmod(0o755)


def check_hooks(install: bool) -> None:
    runtime = (stage.WORK / "factory").resolve()
    if not (runtime / ".git").exists():
        return
    if install:
        install_hooks(runtime)
    ok = hooks_installed(runtime)
    say("ok" if ok else "WARN", "runtime guard hooks installed" if ok else "runtime guard hooks are not installed",
        "python3 factory/scripts/doctor.py --install-hooks (stops a second session from committing in the runtime folder)")


def check_kit() -> None:
    """The factory repo is the one home of the code. A second copy in the kit gets overwritten by bootstrap.py or drifts
    behind the runtime (it did, three times in one day). The kit may only hold bootstrap.py, its README and the app seed."""
    kit = stage.WORK / "factory-kit"
    dupes = [str(p.relative_to(stage.WORK)) for p in (kit / "hq" / "scripts", kit / "hq" / "tests", kit / "template") if p.exists()]
    say("WARN" if dupes else "ok", f"the kit holds a second copy of the code: {', '.join(dupes)}" if dupes
        else "the kit holds no second copy of the code", "delete it; the factory and factory-template repos are the source")


def unit_problem(unit: str, runtime: Path, systemd_dir: Path) -> str | None:
    """A timer must be linked from the runtime repo's ops/systemd, not from a copy somewhere else."""
    link = systemd_dir / unit
    if not link.exists():
        return f"{unit} is not installed"
    target = Path(os.path.realpath(link)).parent
    if target != (runtime / "ops" / "systemd").resolve():
        return f"{unit} is linked from {target}, not from the factory repo's ops/systemd"
    return None


def check_unit_sources() -> None:
    runtime = stage.WORK / "factory"
    for t in TIMERS:
        for unit in (t, t.replace(".timer", ".service")):
            problem = unit_problem(unit, runtime, Path.home() / ".config" / "systemd" / "user")
            if problem:
                say("WARN", problem, "factory/ops/install-timers.sh")
                break
        else:
            continue
    if not any(lvl == "WARN" and "linked from" in what for lvl, what, _ in results):
        say("ok", "all timers are linked from the factory repo")


def check_gitea() -> None:
    try:
        g = stage.Gitea()
        g.get(f"/repos/{g.user}/factory/issues?limit=1")
        say("ok", f"Gitea token works for {g.user} (read and issues)")
    except Exception as e:  # noqa: BLE001
        say("FAIL", f"Gitea: {str(e)[:120]}", "check ~/.config/git/gitea-token and its scopes")


def main(argv: list[str]) -> int:
    app = argv[argv.index("--app") + 1] if "--app" in argv else None
    check_files(app)
    check_model(app)
    check_machine()
    check_runtime()
    check_hooks("--install-hooks" in argv)
    check_kit()
    check_unit_sources()
    check_gitea()
    fails = sum(1 for lvl, *_ in results if lvl == "FAIL")
    warns = sum(1 for lvl, *_ in results if lvl == "WARN")
    print(f"\n{fails} fail, {warns} warn")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
