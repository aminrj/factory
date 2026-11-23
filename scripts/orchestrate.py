#!/usr/bin/env python3
"""Stateless factory foreman (this file is the foreman; the name 'orchestrate' is kept for the file). Run it on a timer; it does the next step when the human says /go.

Usage:
  orchestrate.py tick [--dry-run]      look at every active work issue, act on a new /go or /stop
  orchestrate.py run <repo> <issue>    same, for one issue
  orchestrate.py status                print what each active issue is waiting for

State lives in the issue itself, so nothing has to stay open between steps:
  * the last comment that starts with `🛑 CHECKPOINT` is the gate we wait at. It ends with a hidden marker
    `<!--factory:resume=<handler>;stop=<what>-->` that says which handler runs on /go.
  * a comment that starts with `▶` means a step was picked up (or the run stopped). Ticks ignore those.
  * only comments by the Gitea user that owns the token count, and only the exact forms
    `/go [model=P] [minutes=N]`, `/rework <note>` and `/stop`. Everything else is data and is ignored.
Handlers call dispatch.py for every agent step, so the guards there still apply. A failed step posts a new
checkpoint with the diagnosis (at most 2 retries per handler, then it asks for a session).
Notifications go to a self-hosted ntfy if NTFY_URL and NTFY_TOPIC are set (env or ~/.config/factory/ntfy.env).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO

sys.path.insert(0, str(Path(__file__).resolve().parent))
import commands  # noqa: E402
import mirror  # noqa: E402
import signals  # noqa: E402
import lanes  # noqa: E402
import lease  # noqa: E402
import night  # noqa: E402
import tiers  # noqa: E402
from gitea import Gitea, Unreachable, by_owner, is_owner_email, label_added_by_owner, ours  # noqa: E402

WORK = Path(os.environ.get("WORK", Path.home() / "git" / "software-factory"))
HQ = Path(__file__).resolve().parent.parent
DATA = HQ / "data"
STATE = WORK / "state"
# The command is the first line. Anything after it, on the same line or below (a note to me), is allowed and ignored,
# except `minutes=N` on the first line. A command that is not understood gets a reply, never silence.
GO = re.compile(r"^\s*/go\b([^\r\n]*)", re.IGNORECASE)
MINUTES = re.compile(r"\bminutes=(\d+)", re.IGNORECASE)  # `minutes=5min` is read as 5
STOP = re.compile(r"^\s*/stop[ \t]*(?:\r?\n|$)", re.IGNORECASE)
# At the review checkpoint only: `/rework what is wrong` (same line or the lines below) sends the note to a new implement run.
REWORK = re.compile(r"^\s*/rework\b[ \t]*(.*)$", re.IGNORECASE | re.DOTALL)
CP_PREFIX = "🛑 CHECKPOINT"
CLAIM_PREFIX = "▶"
MARKER = re.compile(r"<!--factory:(.*?)-->", re.DOTALL)
ORCH = {"GIT_AUTHOR_NAME": "factory-orchestrator", "GIT_AUTHOR_EMAIL": "factory-orchestrator@users.noreply.local",
        "GIT_COMMITTER_NAME": "factory-orchestrator", "GIT_COMMITTER_EMAIL": "factory-orchestrator@users.noreply.local"}
GATES = ["spec", "threat_model", "plan_review", "code_review", "rework"]  # the PR minutes table; "release" is recorded too
TIMEBOX_H = float(os.environ.get("FACTORY_TIMEBOX_HOURS", "8"))
DISPATCH_TIMEOUT = 3300
CI_TIMEOUT = 1800
MAX_RETRIES = 2


class StepFailed(Exception):
    """An agent step or a check failed. Retried by the human with /go."""


class NeedsHuman(Exception):
    """The human must change something (for example a PROPOSED row is left). Not counted as a retry."""


class AgentDeclined(NeedsHuman):
    """The agent changed nothing and explained why. Asking the owner is the only useful next step: a retry repeats it."""

    def __init__(self, step: str, said: str):
        super().__init__(f"{said}\n\nRunning `{step}` again would give the same answer, so I did not retry it. Decide what the "
                         "agent is waiting for (change `plan.md`, answer in a comment, or `/stop`), then reply `/go`.")
        self.step = step


class GateEvasion(NeedsHuman):
    """dispatch.py refused to push a run that hid tests from a gate. A retry would try the same trick."""


class Stop(Exception):
    pass


# ---------------------------------------------------------------- small helpers
def now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def sh(*args: str, cwd: Path, check: bool = True) -> str:
    out = subprocess.run(args, cwd=cwd, check=check, capture_output=True, text=True, env={**os.environ, **ORCH})
    return out.stdout.strip()


def log(msg: str) -> None:
    with (WORK / "RUNLOG.md").open("a") as fh:
        fh.write(f"{now()} orchestrate: {msg}\n")


def commit_ops(msg: str) -> None:
    """Commit and push RUNLOG.md (only that file) in factory-ops, as factory-orchestrator."""
    try:
        sh("git", "add", "RUNLOG.md", cwd=WORK)
        if sh("git", "diff", "--cached", "--name-only", cwd=WORK):
            sh("git", "commit", "-q", "-m", msg, "--", "RUNLOG.md", cwd=WORK)
        if sh("git", "log", "--oneline", "@{u}..HEAD", cwd=WORK):
            # the owner merges docs PRs in factory-ops and a standby host writes here too: put ours on top, then push
            if subprocess.run(["git", "push", "-q", "origin", "HEAD"], cwd=WORK, capture_output=True).returncode:
                sh("git", "pull", "-q", "--rebase", "--autostash", "origin", "main", cwd=WORK)
                sh("git", "push", "-q", "origin", "HEAD", cwd=WORK)
    except subprocess.CalledProcessError as e:
        alarm("commit-ops", "run log not pushed", f"could not commit or push factory-ops: {(e.stderr or '')[-200:]}")


def ntfy_config() -> dict[str, str]:
    cfg = {k: os.environ[k] for k in ("NTFY_URL", "NTFY_TOPIC", "NTFY_TOKEN") if k in os.environ}
    path = Path.home() / ".config" / "factory" / "ntfy.env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                cfg.setdefault(k.strip(), v.strip().strip("'\""))
    return cfg


def notify(title: str, message: str, click: str | None = None, priority: int = 3) -> None:
    cfg = ntfy_config()
    if not cfg.get("NTFY_URL") or not cfg.get("NTFY_TOPIC"):
        log("ntfy not configured, no push sent")
        return
    req = urllib.request.Request(f"{cfg['NTFY_URL'].rstrip('/')}/{cfg['NTFY_TOPIC']}", data=message.encode(),
                                 method="POST")
    req.add_header("Title", title.encode("utf-8").decode("latin-1", "ignore") or "factory")
    req.add_header("Priority", str(priority))
    req.add_header("Tags", "factory")
    if click:
        req.add_header("Click", click)
    if cfg.get("NTFY_TOKEN"):
        req.add_header("Authorization", f"Bearer {cfg['NTFY_TOKEN']}")
    try:
        urllib.request.urlopen(req, timeout=10).read()
    except Exception as e:  # a failed push must never stop the run
        log(f"ntfy failed: {type(e).__name__}")


ALARM_EVERY_H = 6


def alarm(key: str, title: str, message: str) -> None:
    """Something odd that is not a work item's checkpoint (a crashed tick, a failed push of the metrics or the run log):
    log it and push it, at most once per key every ALARM_EVERY_H hours so a failure that repeats every minute is one push."""
    log(f"alarm {key}: {message[:200]}")
    path = STATE / "alarms.json"  # read at call time: tests point STATE elsewhere
    try:
        sent = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        sent = {}
    now_s = time.time()
    if now_s - sent.get(key, 0) < ALARM_EVERY_H * 3600:
        return
    notify(f"Factory: {title}", message[:400], priority=4)
    sent[key] = now_s
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sent))
    except OSError:
        pass  # the push went out; at worst the next one is not throttled


def jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()] if path.exists() else []


class Ctx:
    def __init__(self, g: Gitea, repo: str, n: int, minutes: int | None, go_id: int, marker: dict[str, str],
                 model: str | None = None):
        self.g, self.repo, self.n, self.minutes, self.go_id, self.marker = g, repo, n, minutes, go_id, marker
        self.model = model  # the owner's `model=` for this run; None is the account default
        self.auto = False  # True when the night policy passed this gate, not the owner
        self.unattended = False  # True when the autopilot passes a gate the owner's starting /go already approved
        self.app = repo.split("/")[1]
        self.branch = f"agent/issue-{n}"
        self.clone = WORK / self.app
        self.state_path = STATE / f"{self.app}-{n}.json"

    # -- state
    def state(self) -> dict:
        return json.loads(self.state_path.read_text()) if self.state_path.exists() else {}

    def save(self, st: dict) -> None:
        STATE.mkdir(exist_ok=True)
        self.state_path.write_text(json.dumps(st, indent=1))

    # -- links and files
    def url(self, path: str = "") -> str:
        return f"{self.g.repo_url(self.repo).removesuffix('.git')}{path}"

    def file_link(self, name: str) -> str:
        return self.url(f"/src/branch/{self.branch}/thoughts/{self.n}/{name}")

    def pr(self) -> dict:
        prs = self.g.get(f"/repos/{self.repo}/pulls?state=open")
        pr = next((p for p in prs if p["head"]["ref"] == self.branch), None)
        if not pr:
            raise StepFailed(f"no open PR for {self.branch}")
        return pr

    def branch_file(self, name: str) -> str:
        sh("git", "fetch", "-q", "origin", cwd=self.clone)
        res = subprocess.run(["git", "show", f"origin/{self.branch}:thoughts/{self.n}/{name}"], cwd=self.clone,
                             capture_output=True, text=True)
        if res.returncode != 0:
            raise StepFailed(f"thoughts/{self.n}/{name} is not on {self.branch}")
        return res.stdout

    # -- gitea
    def comment(self, body: str) -> dict:
        return self.g.call("POST", f"/repos/{self.repo}/issues/{self.n}/comments", {"body": body})

    def record_minutes(self, gate: str) -> None:
        """The owner's time on this gate: typed with `minutes=N`, else the gate's typical time marked `default`, or
        0 marked `auto` when the night policy passed the gate (nobody looked)."""
        if self.auto or self.unattended:
            minutes, source = 0, "auto"
        elif self.minutes is not None:
            minutes, source = self.minutes, "typed"
        else:
            minutes, source = commands.GATE_MINUTES.get(gate, 5), "default"
        DATA.mkdir(exist_ok=True)
        with (DATA / "human_minutes.jsonl").open("a") as fh:
            fh.write(json.dumps({"ts": now(), "repo": self.app, "issue": self.n, "gate": gate,
                                 "minutes": minutes, "source": source, "comment_id": self.go_id}) + "\n")
        log(f"{self.app}#{self.n}: recorded {minutes} human minutes ({source}) for gate {gate}")

    def status_line(self) -> str:
        runs = [r for r in jsonl(DATA / "agent_runs.jsonl") if r.get("repo") == self.app and r.get("issue") == self.n]
        mins = [m for m in jsonl(DATA / "human_minutes.jsonl") if m["repo"] == self.app and m["issue"] == self.n]
        labels = [label["name"] for label in self.g.get(f"/repos/{self.repo}/issues/{self.n}/labels")]
        stage = next((label for label in labels if label.startswith("stage/")), "stage/?")
        return (f"_Board: {self.app}#{self.n} is `{stage}`. Agent so far: {len(runs)} runs, "
                f"{sum(r.get('duration_s', 0) for r in runs) / 60:.1f} min, "
                f"${sum(0 if r.get('model') == 'local' else (r.get('cost_usd') or 0) for r in runs):.2f}. Your time so far: "
                f"{sum(m['minutes'] for m in mins)} min._")


# ---------------------------------------------------------------- checkpoints and agent steps
def push_metrics(root: Path = HQ) -> None:
    """Push the metrics commit. If origin/main moved meanwhile (the owner merged a PR), put the commit on top of it and
    push again; a rejected push must never leave a diverged runtime behind (it paused the foreman once)."""
    try:
        sh("git", "push", "-q", "origin", "HEAD", cwd=root)
    except subprocess.CalledProcessError:
        if not sync_metrics_commits(root):
            raise


def refresh_board() -> None:
    """Run collect_metrics.py and commit BOARD.md and data/ in the factory clone (Gitea only)."""
    res = subprocess.run([sys.executable, "-I", str(HQ / "scripts" / "collect_metrics.py")], cwd=HQ,
                         capture_output=True, text=True, env={**os.environ, "WORK": str(WORK)})
    if res.returncode != 0:
        alarm("collect-metrics", "metrics failed", f"collect_metrics failed: {res.stderr[-200:]}")
        return
    try:
        sh("git", "add", "BOARD.md", "data", cwd=HQ)
        if sh("git", "diff", "--cached", "--name-only", cwd=HQ):
            sh("git", "commit", "-q", "-m", "Metrics refresh", cwd=HQ)
            push_metrics(HQ)
    except subprocess.CalledProcessError as e:
        alarm("push-metrics", "metrics not pushed", f"could not commit or push the metrics: {(e.stderr or '')[-200:]}")


# What the owner has to look at to answer each checkpoint, so they never go looking for it. {n} is the issue number.
# A trailing slash is a folder. Only what exists on the branch is linked.
FILES_TO_CHECK: dict[str, list[tuple[str, str]]] = {
    "after_gate1": [("thoughts/{n}/spec.md", "the spec (approve it with `Status: approved`)"),
                    ("tests/holdout/", "the acceptance tests that will judge the build")],
    "after_gate2": [("thoughts/{n}/threat-model.md", "the threat model: decide every `PROPOSED` row")],
    "after_research_questions": [("thoughts/{n}/research.md", "the research and its open questions"),
                                 ("thoughts/{n}/threat-model.md", "the threat model you decided")],
    "after_gate3": [("thoughts/{n}/plan.md", "the plan (`Status: approved`, or send it back)"),
                    ("docs/decisions/", "the decisions (ADRs) the plan proposes")],
    "after_plan_rework": [("thoughts/{n}/plan.md", "the plan")],
    "after_gate4": [("thoughts/{n}/validation.md", "the validation report"),
                    ("thoughts/{n}/acceptance.md", "the checklist for your phone")],
    "after_staging_rework": [("thoughts/{n}/validation.md", "the validation report"),
                             ("thoughts/{n}/acceptance.md", "the checklist for your phone")],
    "after_timebox": [("thoughts/{n}/plan.md", "the plan and its checkboxes")],
    "manual": [("thoughts/{n}/plan.md", "the plan and its checkboxes"), ("thoughts/{n}/validation.md", "the validation report")],
}


def files_section(ctx: Ctx, resume: str) -> str:
    """The links for a checkpoint: each file the owner must read (only if it exists on the branch) and the pull request with
    every change. Best effort: a Gitea or git hiccup returns what it has, never an exception."""
    rows: list[str] = []
    try:
        sh("git", "fetch", "-q", "origin", cwd=ctx.clone)
    except Exception:  # noqa: BLE001
        pass
    for path, why in FILES_TO_CHECK.get(resume, []):
        path = path.format(n=ctx.n)
        if subprocess.run(["git", "cat-file", "-e", f"origin/{ctx.branch}:{path.rstrip('/')}"], cwd=ctx.clone,
                          capture_output=True, check=False).returncode == 0:
            rows.append(f"- [{path}]({ctx.url('/src/branch/' + ctx.branch + '/' + path.rstrip('/'))}): {why}")
    try:
        rows.append(f"- [Pull request: every change on this branch]({ctx.pr()['html_url']}/files)")
    except Exception:  # noqa: BLE001 - no open PR yet
        pass
    return ("### Files to check\n\n" + "\n".join(rows)) if rows else ""


def checkpoint(ctx: Ctx, label: str, title: str, body: str, resume: str, stop: str = "", extra: str = "",
               rework: bool = False, reply: str = "") -> None:
    refresh_board()
    extra = ";".join(x for x in (extra, "rework=ok" if rework else "") if x)
    marker = f"<!--factory:resume={resume};stop={stop}{';' + extra if extra else ''}-->"
    # The last line says what each command does HERE: a `/go` that can only answer "a session is needed" was offered
    # at checkpoint 4b, and the owner sent it five times (brevkoll#19, 2026-11-22).
    reply = reply or ("Reply `/go` to accept, `/rework <what is wrong>` to send it back, or `/stop` to stop." if rework
                      else "Nothing here for `/go` to run: this needs a session. `/stop` ends the item."
                      if resume == "manual" else "Reply `/go` to continue or `/stop` to stop.")
    files = files_section(ctx, resume)
    text = (f"{CP_PREFIX} {label}: {title}\n\n{body}\n\n{files + chr(10) * 2 if files else ''}{ctx.status_line()}\n\n"
            f"{reply}\n{marker}")
    c = ctx.comment(text)
    signals.clear(STATE, ctx.app, ctx.n)  # a /stop or /steer meant for the step that just ended must not reach the next
    log(f"{ctx.app}#{ctx.n}: CHECKPOINT {label} posted (comment {c['id']}), resume={resume}")
    commit_ops(f"Checkpoint {label}: {ctx.app}#{ctx.n}")
    notify(f"{ctx.app}: checkpoint {label}", f"{title}. Reply /go on {ctx.app}#{ctx.n}.",
           click=f"{ctx.url('/issues/' + str(ctx.n))}#issuecomment-{c['id']}",
           priority=1 if ctx.auto else 3)  # night shift: silent; the morning push sums the night up


# A failed local step goes to Claude once, by itself, while the item's cloud spend is under the budget (2026-11-22, the
# owner: "allow it automatically within the item's budget, and say so clearly"). Before that the owner had to read the
# failure and type `/go model=fast` (brevkoll#7, #14, #26, #29 the same day). Never for the night shift (no credit at
# night), never for an infrastructure problem (credit, login, model down: the cloud is not the fix), never for a
# refusal that needs the owner (evasion, declined: those are not StepFailed).
ESCALATE_TO = "fast"
ITEM_CLOUD_BUDGET_USD = 5.0
MAX_ESCALATIONS = 3
INFRA_CLASSES = {"credit_exhausted", "auth", "rate_limit", "model_unavailable"}


def item_cloud_spend(app: str, n: int) -> float:
    return sum(float(r.get("cost_usd") or 0) for r in jsonl(DATA / "agent_runs.jsonl")
               if r.get("repo") == app and r.get("issue") == n and r.get("model") not in ("local", None))


def dispatch(ctx: Ctx, step: str, note: str | None = None, note_kind: str = "ci") -> str:
    try:
        return dispatch_once(ctx, step, note, note_kind)
    except StepFailed as e:
        cls = FAILURE_CLASS.search(str(e))
        st = ctx.state()
        spent = item_cloud_spend(ctx.app, ctx.n)
        if (ctx.auto or ctx.model != "local" or (cls and cls.group(1) in INFRA_CLASSES)
                or st.get("escalations", 0) >= MAX_ESCALATIONS or spent >= ITEM_CLOUD_BUDGET_USD):
            raise
        st["escalations"] = st.get("escalations", 0) + 1
        ctx.save(st)
        why = str(e).strip().splitlines()
        reason = next((ln for ln in why if ln.startswith(("REFUSED", "stalled", "claude run failed"))), why[-1] if why else "")
        ctx.comment(f"{NOTICE_PREFIX} `{step}` failed on the local model: {reason[:300]}\n\nI am running it again on "
                    f"`{ESCALATE_TO}` (Claude) by myself. This item has spent ${spent:.2f} of its "
                    f"${ITEM_CLOUD_BUDGET_USD:.0f} cloud budget; switch {st['escalations']} of {MAX_ESCALATIONS}. "
                    "`/stop` stops it.\n\n<details><summary>What the local run reported</summary>\n\n```\n"
                    f"{str(e)[-1500:]}\n```\n\n</details>")
        log(f"{ctx.app}#{ctx.n}: {step} failed on local, escalated to {ESCALATE_TO} ({st['escalations']}/{MAX_ESCALATIONS})")
        local, ctx.model = ctx.model, ESCALATE_TO
        try:
            return dispatch_once(ctx, step, note, note_kind)
        finally:
            ctx.model = local


def dispatch_once(ctx: Ctx, step: str, note: str | None = None, note_kind: str = "ci") -> str:
    cmd = [sys.executable, "-I", str(HQ / "scripts" / "dispatch.py"), ctx.app, str(ctx.n), step]
    if note:
        cmd += ["--note", note, "--note-kind", note_kind]
    if getattr(ctx, "model", None):
        cmd += ["--model", ctx.model]
    log(f"{ctx.app}#{ctx.n}: dispatch {step}")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "WORK": str(WORK)},
                             timeout=DISPATCH_TIMEOUT + 300)
    except subprocess.TimeoutExpired as e:
        raise StepFailed(f"`{step}` timed out") from e
    out = (res.stdout + res.stderr).strip()
    if res.returncode == DECLINED_EXIT:
        raise AgentDeclined(step, out)
    if res.returncode == EVASION_EXIT:
        raise GateEvasion(out[-3000:])
    if res.returncode == STOPPED_EXIT:  # the owner's /stop ended the step at once (scripts/signals.py)
        m = re.search(r"/stop \(comment (\d+)\)", out)
        log(f"{ctx.app}#{ctx.n}: {step} ended by the owner's /stop")
        raise StepStopped(int(m.group(1)) if m else 0, out.strip().splitlines()[-1][:600] if out.strip() else "")
    if res.returncode == BROKEN_TEST_EXIT:
        raise NeedsHuman(out[-3000:])  # not a retry and not a reason to switch models: the test is wrong
    if res.returncode != 0:
        raise StepFailed(f"`{step}` failed:\n{out[-1500:]}")
    log(f"{ctx.app}#{ctx.n}: dispatch {step}: {out.splitlines()[-1] if out else 'ok'}")
    return out


DECLINED_EXIT = 3  # keep in step with dispatch.py
EVASION_EXIT = 4  # keep in step with dispatch.py
BROKEN_TEST_EXIT = 5  # keep in step with dispatch.py
STOPPED_EXIT = 6  # keep in step with dispatch.py
STEER = re.compile(r"^\s*/steer\b[ \t]*(.*)$", re.IGNORECASE | re.DOTALL)
FAILURE_CLASS = re.compile(r"\[class: (\w+)\]")  # written by dispatch.py
CONTEXTS = ("checks", "gates", "security", "holdout")


def ci_wait(ctx: Ctx, want: tuple[str, ...]) -> dict[str, str]:
    """Wait until each wanted check has a final status on the PR head. Returns {check: status}."""
    sha = ctx.pr()["head"]["sha"]
    deadline = time.time() + CI_TIMEOUT
    out: dict[str, str] = {}
    while time.time() < deadline:
        st = ctx.g.get(f"/repos/{ctx.repo}/commits/{sha}/status")
        out = {}
        for s in st.get("statuses") or []:
            for name in CONTEXTS:
                if f"/ {name} " in s["context"] or s["context"].endswith(f"/ {name}"):
                    out[name] = s["status"]
        if all(out.get(w) not in (None, "pending") for w in want):
            return out
        time.sleep(30)
    raise StepFailed(f"CI did not finish for {sha[:7]} within {CI_TIMEOUT // 60} min: {out or 'no statuses'}")


def failing(result: dict[str, str], want: tuple[str, ...]) -> list[str]:
    return [w for w in want if result.get(w) != "success"]


EMPTY_PHASE = re.compile(r"^\W*(none|n/?a|tbd|-+|nothing|empty)\W*$", re.IGNORECASE)


def phases(plan: str) -> int:
    """The phases to build: `## Phase N: <title>` headings before any `## Deferred`, without placeholders such as
    `## Phase 2: (none)` (brevkoll#32, 2026-11-22: that heading started a second implement run that had nothing to do,
    and the item stopped for the owner; #29's one-phase plan was announced as "2 phases" the same way)."""
    body = re.split(r"^##\s+Deferred\b", plan, maxsplit=1, flags=re.MULTILINE | re.IGNORECASE)[0]
    titles = re.findall(r"^##\s+Phase\s+\d+\b[ \t]*:?[ \t]*(.*)$", body, re.MULTILINE)
    return sum(1 for t in titles if not EMPTY_PHASE.match(t))


def open_questions(research: str) -> list[str]:
    m = re.search(r"^##\s*Open questions for the human\s*\n(.*?)(?=^##\s|\Z)", research, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    items = [re.sub(r"^\d+[.)]\s*", "", x.strip().lstrip("-*• \t")) for x in m.group(1).splitlines()
             if x.strip().startswith(("-", "*", "•")) or re.match(r"^\s*\d+[.)]", x)]
    return [i for i in items if i and not re.match(r"^(none|n/a|no open questions|nothing)\b", i, re.IGNORECASE)]


# ---------------------------------------------------------------- handlers (one per gate)
STATUS_LINE = re.compile(r"^\**Status:\**\s*(draft|approved)\b", re.IGNORECASE | re.MULTILINE)


def spec_approval_problem(clone: Path, branch: str, n: int, spec: str, human_email: str, login: str = "") -> str | None:
    """A spec with a `Status:` line needs `Status: approved`, introduced by a commit of the human (like the plan gate).
    Specs without the line (older apps) keep the old behaviour. Returns the problem to show, or None."""
    m = STATUS_LINE.search(spec)
    if not m:
        return None
    if m.group(1).lower() != "approved":
        return ("`spec.md` says `Status: draft`. Review the spec and `tests/holdout/`, change the line to `Status: approved` "
                "in a commit of your own (the pencil icon in Gitea works) and reply `/go` again.")
    out = sh("git", "log", "-G", r"Status:.*approved", "--format=%ae", f"origin/{branch}", "--", f"thoughts/{n}/spec.md",
             cwd=clone)
    authors = out.splitlines()
    if not authors:
        return "`Status: approved` is not in any commit yet."
    if not is_owner_email(authors[0], login, human_email):
        return ("`Status: approved` was committed by someone other than you. Approving is yours: make the change in a "
                "commit of your own.")
    return None


OWNER_TRAILER = "Approved-by-owner: comment {id}"  # keep in step with dispatch.py


def approve(ctx: Ctx, name: str, edit: Callable[[str], str], what: str) -> None:
    """Pass a gate. With the night label: the policy's approval. Otherwise the owner's `/go` is the approval (2026-11-22:
    editing `Status:` lines and `PROPOSED` rows on the phone made every gate a chore, and forgetting it cost 6 `-fix`
    checkpoints). The foreman commits the edit as factory-orchestrator with a trailer naming the owner's comment; the
    guards accept it only when that comment is the owner's `/go` on this issue."""
    if ctx.auto:
        return approve_by_policy(ctx, name, edit, what)
    trailer = OWNER_TRAILER.format(id=ctx.go_id)
    approval_commit(ctx, name, edit, f"#{ctx.n} {what}: approved by the owner's /go\n\n{trailer}", record=True)
    log(f"{ctx.app}#{ctx.n}: {what} approved by the owner's /go (comment {ctx.go_id})")


def approve_by_policy(ctx: Ctx, name: str, edit: Callable[[str], str], what: str) -> None:
    """The night shift passes a gate: one commit by factory-orchestrator with the policy trailer, pushed to the branch.
    The owner sees it in the branch history and on Checkpoint 4; the guards accept it only while the label is on."""
    approval_commit(ctx, name, edit, f"#{ctx.n} {what}: approved by the night policy\n\nThe issue carries the label "
                    f"{night.LABEL}; the owner reviews this at Checkpoint 4.\n\n{night.TRAILER}")
    log(f"{ctx.app}#{ctx.n}: {what} approved by the night policy")


def approval_commit(ctx: Ctx, name: str, edit: Callable[[str], str], message: str, record: bool = False) -> None:
    """One commit by factory-orchestrator that edits thoughts/N/<name>, pushed to the item's branch. record: commit
    even when the file needs no edit, so every gate the owner passed is in the branch history."""
    # A short-lived detached worktree: the branch may be checked out in the agent's worktree, which fast-forwards to
    # this commit on its next step.
    wt = WORK / "worktrees" / f"{ctx.app}-issue-{ctx.n}-policy"
    sh("git", "fetch", "-q", "origin", cwd=ctx.clone)
    if wt.exists():
        sh("git", "worktree", "remove", "--force", str(wt), cwd=ctx.clone)
    sh("git", "worktree", "add", "-q", "--detach", str(wt), f"origin/{ctx.branch}", cwd=ctx.clone)
    try:
        path = wt / "thoughts" / str(ctx.n) / name
        text = path.read_text()
        new = edit(text)
        if new == text and not record:
            return
        path.write_text(new)
        sh("git", "add", str(path.relative_to(wt)), cwd=wt)
        sh("git", "commit", "-q", "--allow-empty", "-m", message, cwd=wt)
        sh("git", "push", "-q", "origin", f"HEAD:refs/heads/{ctx.branch}", cwd=wt)
    finally:
        sh("git", "worktree", "remove", "--force", str(wt), cwd=ctx.clone, check=False)


def set_status(text: str, value: str = "approved") -> str:
    return re.sub(r"^(\**Status:\**\s*)\w+", lambda m: m.group(1) + value, text, count=1, flags=re.IGNORECASE | re.MULTILINE)


PLACEHOLDER = re.compile(r"<(?:[a-z]+[ _-]){1,6}[a-z]+>")  # `<one line summary>`, not `<script>` or `a < b`


# ---------------------------------------------------------------- the autopilot
# 2026-11-22, the owner: "the work should go ahead until it delivers a link to a running app with clear builds and
# clear messaging". The owner's `/go` that starts an issue approves the spec, the threat model and the plan as they come
# (each approval is still a commit naming that comment); the item stops only at Checkpoint 4 (try it, merge), when the
# plan touches risky files, when a step fails, or where `/go stop=spec|plan` says. Each hop posts a one-line claim
# (`Running X`), so a crash in between is recovered like any interrupted step and the owner sees where it is.
RISKY_PATHS = re.compile(r"(^|[\s`/(])(deploy/|\.gitea/|\.github/|Dockerfile|docker-compose|scripts/check-|"
                         r"secrets?/|\.env\b|tests/holdout/)", re.IGNORECASE)


def risky_plan(plan: str) -> list[str]:
    seen: list[str] = []
    for m in RISKY_PATHS.finditer(plan):
        if m.group(2) not in seen:
            seen.append(m.group(2))
    return seen[:8]


def autopilot_on(ctx: Ctx) -> bool:
    return not ctx.auto and bool(ctx.state().get("autopilot"))


def note_for_review(ctx: Ctx, text: str) -> None:
    """What the autopilot decided without the owner (provisional threat model rows, deferred questions): shown at CP4."""
    if not text.strip():
        return
    st = ctx.state()
    st["autopilot_notes"] = [*st.get("autopilot_notes", []), text.strip()][-6:]
    ctx.save(st)


def autopilot_summary(ctx: Ctx) -> str:
    st = ctx.state()
    if not st.get("autopilot"):
        return ""
    notes = "\n\n".join(st.get("autopilot_notes", []))
    return ("### What the autopilot decided for you\n\nYour `/go` approved the "
            f"[spec]({ctx.file_link('spec.md')}), the [threat model]({ctx.file_link('threat-model.md')}) and the "
            f"[plan]({ctx.file_link('plan.md')}) as they came.\n\n" + (notes + "\n\n" if notes else ""))


def hop(ctx: Ctx, handler: str, done: str) -> None:
    """Run the next handler in this process, as if the owner had answered its checkpoint with `/go`."""
    check_stop(ctx)
    ctx.unattended = True
    ctx.comment(f"{CLAIM_PREFIX} Autopilot: {done}. Running `{handler}` on model `{ctx.model or 'default'}`. "
                "`/stop` stops it.")
    log(f"{ctx.app}#{ctx.n}: autopilot -> {handler}")
    run_handler(ctx.g, ctx.repo, ctx.n, ctx, ctx.marker, handler)


def set_autopilot(ctx: Ctx, stop: str | None, tier: str | None = None) -> None:
    st = ctx.state()
    st["autopilot"], st["stop_at"], st["tier_forced"] = True, stop or "", tier or ""
    ctx.save(st)


def issue_kind_of(ctx: Ctx) -> str:
    """`bug`, `decision` or `feature` (scripts/tiers.py), remembered so the later hops do not ask Gitea again."""
    st = ctx.state()
    if st.get("kind"):
        return str(st["kind"])
    issue = ctx.g.get(f"/repos/{ctx.repo}/issues/{ctx.n}")
    labels = {x["name"] for x in issue.get("labels", [])} if isinstance(issue, dict) else set()
    kind = tiers.issue_kind(str(issue.get("title", "")), str(issue.get("body", "")), labels) if isinstance(issue, dict) \
        else "feature"
    st["kind"] = kind
    ctx.save(st)
    return kind


DECISION_NEEDED = re.compile(r"Decision needed[^:\n]*:\s*`?yes\b", re.IGNORECASE)


def after_gate1(ctx: Ctx) -> None:
    spec = ctx.branch_file("spec.md")
    st = ctx.state()
    if DECISION_NEEDED.search(spec) and not st.get("decision_asked"):
        # The issue itself says a choice is open (brevkoll#6: "Needs a decision before any code"). The autopilot used to
        # pick an option for the owner. Ask once; the owner's reply is read by the later steps like any comment.
        st["decision_asked"] = True
        ctx.save(st)
        raise NeedsHuman(
            f"The [spec]({ctx.file_link('spec.md')}) says a decision is needed before any code, and lists the options. "
            "Answer with `/go <your choice, in a sentence>`: your sentence is kept on the issue and every later step "
            "reads it. `/go` alone builds what the spec assumes. `/stop` drops it.")
    holes = sorted(set(PLACEHOLDER.findall(spec)))
    if holes:
        raise NeedsHuman("`spec.md` still has template placeholders: " + ", ".join(f"`{h}`" for h in holes[:6])
                         + f". Fill them in ({ctx.file_link('spec.md')}), or `/stop`.")
    approve(ctx, "spec.md", set_status, "spec")
    ctx.record_minutes("spec")
    dispatch(ctx, "threat-model")
    ctx.g.set_stage(ctx.repo, ctx.n, "spec-ready")
    return through_to_plan(ctx)


# One stop between the spec and the plan instead of three (2026-11-22: "it only produces artefacts that are not code").
# After the owner's CP1 `/go`, threat model, research and plan run back to back and Checkpoint 3 shows all three.


def through_to_plan(ctx: Ctx) -> None:
    tm = ctx.branch_file("threat-model.md")
    rows = len(re.findall(r"\bPROPOSED\b", tm))
    if rows:
        word = "ACCEPTED (night policy)" if ctx.auto else "ACCEPTED (provisional, shown at Checkpoint 3)"
        approve(ctx, "threat-model.md", lambda t: re.sub(r"\bPROPOSED\b", word, t), f"threat model ({rows} rows, provisional)")
    dispatch(ctx, "research")
    qs = open_questions(ctx.branch_file("research.md"))
    extra = (f"Threat model ({rows} rows accepted provisionally): {ctx.file_link('threat-model.md')}. To decide a row "
             "differently, edit it before `/go`.\n\n")
    if qs:
        extra += ("Research raised questions; the plan defers them unless you answer in a comment and `/rework`:\n"
                  + "\n".join(f"- {q}" for q in qs[:8]) + "\n\n")
    dispatch(ctx, "plan")
    if autopilot_on(ctx) and ctx.state().get("stop_at") != "plan":
        plan = ctx.branch_file("plan.md")
        verdict = tiers.after_plan(issue_kind_of(ctx), ctx.branch_file("spec.md"), plan, risky_plan(plan),
                                   str(ctx.state().get("tier_forced") or ""))
        st = ctx.state()
        st["tier"], st["tier_reasons"] = verdict.tier, verdict.reasons
        ctx.save(st)
        if verdict.tier == "auto":
            note_for_review(ctx, extra)
            return hop(ctx, "after_gate3", "the plan is written; building it")
        extra += ("**The autopilot stopped here** (a labelled bug fix with a small plan runs on; this one does not):\n"
                  + "\n".join(f"- {r}" for r in verdict.reasons) + "\n\nRead the plan; `/go` builds it, `/rework <note>` "
                  "sends it back, `/go tier=auto` builds it without this stop.\n\n")
    plan_checkpoint(ctx, extra)


def after_gate2(ctx: Ctx) -> None:
    tm = ctx.branch_file("threat-model.md")
    hits = [(i, line) for i, line in enumerate(tm.splitlines(), 1) if re.search(r"\bPROPOSED\b", line)]
    if hits:
        word = "ACCEPTED (night policy)" if ctx.auto else "ACCEPTED"
        approve(ctx, "threat-model.md", lambda t: re.sub(r"\bPROPOSED\b", word, t), f"threat model ({len(hits)} rows)")
    ctx.record_minutes("threat_model")
    dispatch(ctx, "research")
    qs = open_questions(ctx.branch_file("research.md"))
    if qs:
        checkpoint(ctx, "2b", "research raised questions",
                   f"Research: {ctx.file_link('research.md')}\n\nOpen questions:\n" + "\n".join(f"- {q}" for q in qs[:10]) +
                   "\n\nAnswer them in a comment if you want to (your comments go to the planner), then `/go`. "
                   "`/go` without answers is fine: the planner defers what is still open.", "after_research_questions")
        return
    plan_and_checkpoint(ctx)


def after_research_questions(ctx: Ctx) -> None:
    # `/go` without answers continues: the planner keeps an unanswered question out of the plan or under `Deferred`.
    # (It used to ask "are you sure?" first: one more checkpoint for nothing.)
    ctx.record_minutes("plan_review")
    plan_and_checkpoint(ctx)


def plan_and_checkpoint(ctx: Ctx, note: str | None = None) -> None:
    dispatch(ctx, "plan", note=note)
    plan_checkpoint(ctx)


def plan_checkpoint(ctx: Ctx, extra: str = "") -> None:
    n_ph = phases(ctx.branch_file("plan.md"))
    checkpoint(ctx, "3", "approve the plan or send it back",
               f"{extra}Plan with {n_ph} phases: {ctx.file_link('plan.md')}\n\n"
               "- **Build it:** reply `/go` (I record your approval in the plan).\n"
               "- **Change it:** reply `/rework <what to change>`. The planner gets your note, your comments and any edits "
               "you made to `plan.md`, and writes a new draft.",
               "after_gate3", rework=True)


PLAN_STATUS = re.compile(r"^\**Status:\**\s*(\w+)", re.IGNORECASE | re.MULTILINE)


def plan_status(plan: str) -> str:
    m = PLAN_STATUS.search(plan)
    return m.group(1).lower() if m else ""


def owner_feedback(ctx: Ctx, typed: str = "") -> str:
    """What the owner told the planner: the text of the command, the comments they wrote since the last checkpoint, and
    their edits to plan.md (a diff against the agent's last draft). All of it is data for the planner, not instructions."""
    parts = []
    if typed.strip():
        parts.append(f"The owner's note:\n{typed.strip()[:2000]}")
    comments = ctx.g.paged(f"/repos/{ctx.repo}/issues/{ctx.n}/comments")
    # Since the planner last ran. Later "retry" or "fix" checkpoints must not hide what the owner said in between.
    last_plan_run = max((c["id"] for c in comments if c["body"].startswith("Agent step `plan`")), default=0)
    said = [c["body"].strip() for c in comments if c["id"] > last_plan_run and by_owner(ctx.g, c)
            and not c["body"].startswith((CP_PREFIX, CLAIM_PREFIX, NOTICE_PREFIX, "Agent step")) and not is_command(c["body"])]
    if said:
        parts.append("The owner's comments on this issue since the planner last ran:\n" + "\n---\n".join(s[:1500] for s in said))
    agent_commit = sh("git", "log", "--author=factory-agent", "-1", "--format=%H", f"origin/{ctx.branch}", "--",
                      f"thoughts/{ctx.n}/plan.md", cwd=ctx.clone)
    if agent_commit:
        diff = sh("git", "diff", agent_commit, f"origin/{ctx.branch}", "--", f"thoughts/{ctx.n}/plan.md", cwd=ctx.clone)
        if diff:
            parts.append("The owner's edits to thoughts/N/plan.md since the agent's last draft (a diff):\n" + diff[:6000])
    return "\n\n".join(parts) or "The owner asked for a rework without a note; use their comments and edits in plan.md."


def after_gate3(ctx: Ctx) -> None:
    plan = ctx.branch_file("plan.md")
    status = plan_status(plan)
    if status == "rework":
        return after_plan_rework(ctx)
    if status == "draft" and ctx.auto:
        risky = night.risky_mentions(plan)
        if risky:
            raise NeedsHuman("The night shift does not approve a plan that touches risky areas (" + ", ".join(
                f"`{r}`" for r in risky) + "). Read it; to build it reply `/go` (your approval), or "
                "`/rework <what to change>`.")
    if status != "approved":
        approve(ctx, "plan.md", set_status, "plan")
    ctx.record_minutes("plan_review")
    build(ctx)


def after_plan_rework(ctx: Ctx) -> None:
    """The owner sent the plan back. One new planner run with their feedback, then the plan checkpoint again."""
    st = ctx.state()
    note = owner_feedback(ctx, st.get("rework_note", ""))
    ctx.record_minutes("plan_review")
    plan_and_checkpoint(ctx, note=note)
    st = ctx.state()  # forget the typed note only now: after a crash the retry still has it
    st.pop("rework_note", None)
    ctx.save(st)


def after_timebox(ctx: Ctx) -> None:
    ctx.record_minutes("rework")
    st = ctx.state()
    st["shipping_early"] = True
    ctx.save(st)
    build(ctx)


def build(ctx: Ctx) -> None:
    st = ctx.state()
    if "build_start" not in st:
        ctx.g.set_stage(ctx.repo, ctx.n, "building")
        st["build_start"] = time.time()
        ctx.save(st)
    n_ph = phases(ctx.branch_file("plan.md"))
    while st.get("phases_done", 0) < n_ph and not st.get("shipping_early"):
        check_stop(ctx)
        hours = (time.time() - st["build_start"]) / 3600
        if hours >= TIMEBOX_H:
            checkpoint(ctx, "T", f"{TIMEBOX_H:g} h timebox reached: ship or kill?",
                       f"Phases done: {st.get('phases_done', 0)} of {n_ph}. `/go` ships what exists (validation next, "
                       "unfinished phases are dropped). `/stop` kills the app (`stage/killed`).",
                       "after_timebox", stop="kill")
            return
        if hours >= 0.8 * TIMEBOX_H and not st.get("warned"):
            st["warned"] = True
            ctx.save(st)
            ctx.comment(f"{CLAIM_PREFIX} Timebox warning: {hours:.1f} h of {TIMEBOX_H:g} h used, "
                        f"{st.get('phases_done', 0)} of {n_ph} phases done.")
            notify(f"{ctx.app}: timebox 80%", f"{hours:.1f} h of {TIMEBOX_H:g} h used", priority=4)
        try:
            out = dispatch(ctx, "implement")
        except AgentDeclined as e:
            if st.get("phases_done", 0) < 1:
                raise
            # a later phase found nothing left to build: the earlier runs did it (brevkoll#32). Validation decides.
            log(f"{ctx.app}#{ctx.n}: implement found nothing left after {st['phases_done']} phase(s); on to validation")
            note_for_review(ctx, f"The plan listed {n_ph} phases; after phase {st['phases_done']} the agent found "
                                 f"nothing left to build and said: {str(e)[-400:].strip()}")
            st = ctx.state()
            st["phases_done"] = n_ph
            ctx.save(st)
            break
        # Count the phase as soon as its commit exists. If CI is red and the rework run fails, a retry must not
        # dispatch another implement run for a phase that is already built (it would make no commit and fail).
        st["phases_done"] = st.get("phases_done", 0) + 1
        ctx.save(st)
        if "LOCAL_CHECKS=pass" in (out or "") and st["phases_done"] < n_ph:
            # The same checks just passed on this commit locally: start the next phase now. CI still runs on every
            # push, and the foreman waits for it after the last phase, where a red result gets the usual rework.
            log(f"{ctx.app}#{ctx.n}: phase {st['phases_done']} passed its checks locally; next phase without waiting for CI")
            continue
        want = ("checks", "gates", "security")
        res = ci_wait(ctx, want)
        if failing(res, want):
            rework(ctx, res, want)
    validate_and_checkpoint(ctx)


ANSI = re.compile(r"\x1b\[[0-9;]*m|\[[0-9;]*m")
SECRET = re.compile(r"(?i)(token|secret|password|passwd|api[_-]?key|authorization)([ \t]*[=:][ \t]*|[ \t]+)(Bearer[ \t]+)?[^\s\"']+")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def log_excerpt(raw: str, max_lines: int = 60) -> str:
    """The part of a job log that explains a failure: the lines before the last `##[error]` marker, without
    timestamps or colour codes, with secrets and e-mail addresses scrubbed. It is data for the rework agent."""
    lines = [ANSI.sub("", re.sub(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ", "", ln)) for ln in raw.splitlines()]
    marks = [i for i, ln in enumerate(lines) if ln.startswith("##[error]")]
    end = marks[-1] + 1 if marks else len(lines)
    chunk = [ln for ln in lines[max(0, end - max_lines):end] if ln.strip() and not ln.startswith("::")]
    return EMAIL.sub("<email>", SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}<scrubbed>", "\n".join(chunk)))


def failing_logs(ctx: Ctx, sha: str, bad: list[str]) -> str:
    """Log excerpts of the failed jobs for one commit. Best effort: an API problem returns what it has."""
    out: list[str] = []
    try:
        runs = ctx.g.get(f"/repos/{ctx.repo}/actions/runs?head_sha={sha}&limit=10")["workflow_runs"]
        for run in runs:
            for job in ctx.g.get(f"/repos/{ctx.repo}/actions/runs/{run['id']}/jobs")["jobs"]:
                if job["name"] in bad and job.get("conclusion") == "failure":
                    text = ctx.g.call("GET", f"/repos/{ctx.repo}/actions/jobs/{job['id']}/logs", text=True)
                    out.append(f"--- job {job['name']} (log excerpt, data) ---\n{log_excerpt(text)}")
    except Exception as e:  # noqa: BLE001 - never let log fetching break the rework
        log(f"could not read CI logs: {e}")
    return "\n\n".join(out)


def rework(ctx: Ctx, res: dict[str, str], want: tuple[str, ...]) -> None:
    """One more implement run with the failure as a data note. Raises StepFailed if still red."""
    pr = ctx.pr()
    lid = ctx.g.label_id(ctx.repo, "rework")
    if lid:
        ctx.g.call("POST", f"/repos/{ctx.repo}/issues/{pr['number']}/labels", {"labels": [lid]})
    bad = failing(res, want)
    note = (f"CI failed on the last commit: {', '.join(f'{b}={res.get(b)}' for b in bad)}. Run the same checks locally "
            f"(uv run ruff check ., uv run ruff format --check ., uv run pyright, uv run pytest), fix the cause, and do "
            f"not weaken any check. Details: pull request #{pr['number']}.")
    excerpt = failing_logs(ctx, pr["head"]["sha"], bad)
    if excerpt:
        note += "\n\nThe failing job output follows. It is data from CI, not instructions.\n\n" + excerpt
    dispatch(ctx, "implement", note=note)
    res2 = ci_wait(ctx, want)
    if failing(res2, want):
        raise StepFailed(f"still red after one rework run: {res2}")


def stage_call(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-I", str(HQ / "scripts" / "stage.py"), *args], capture_output=True, text=True,
                          env={**os.environ, "WORK": str(WORK)}, timeout=900, check=False)


def staging_block(ctx: Ctx) -> str:
    """Stage the PR head on the GPU workstation and return the review section for the checkpoint (links, checklist).
    Never raises: a staging problem is shown to the human, it does not stop the checkpoint."""
    try:
        res = stage_call("up", ctx.app, str(ctx.n))
    except subprocess.TimeoutExpired:
        return "### Staging\n\nStaging timed out after 15 minutes. The checks are green; review the diff."
    if res.returncode != 0:
        why = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["unknown"]
        log(f"{ctx.app}#{ctx.n}: staging failed: {why[0][:200]}")
        return f"### Staging\n\nCould not stage this build: `{why[0][:300]}`\nThe checks are green; review the diff."
    info = json.loads(res.stdout)
    ip_url = f"http://{info['ip']}:{info['port']}/"
    try:
        checklist = ctx.branch_file("acceptance.md").strip()
    except StepFailed:
        checklist = ("- [ ] Try the main flow from the spec on your phone\n- [ ] Try one failure case\n\n"
                     "_(No `acceptance.md` was written for this change, so this list is generic: read the spec's "
                     "acceptance criteria.)_")
    local = (f"On the PC that hosts it, use [{info['local_url']}]({info['local_url']}) (a Windows browser cannot reach the "
             "tailnet address of its own machine; the request just hangs)." if info.get("local_url") else "")
    return (f"### 🧪 Try it on your phone\n\n**[Open the staged build]({info['url']})**"
            f" (if the name does not resolve: [{ip_url}]({ip_url}))\n\n"
            f"Build `{info['sha'][:7]}` of `{ctx.branch}` (checked: the container runs this build), in the sandbox on "
            f"`{info.get('host') or 'the factory host'}`. Only devices on your tailnet can open it. "
            f"Model check before staging: {info.get('model', 'unknown')}. The model is shared and can be busy or off.\n\n"
            f"{local + chr(10) * 2 if local else ''}{checklist}")


def drift_summary(report: str, link: str) -> str:
    """The checkpoint's view of thoughts/N/drift.md (written by the app's scripts/drift.py after validate): the headline,
    what was asked and built, and each finding. The owner reads this first: it is what the code says about the
    distance between the intent and the diff, not what a model says about its own work."""
    head = re.search(r"(?m)^Drift:\s*(.+)$", report)
    card = re.search(r"(?ms)^## Card\s*\n(.*?)(?=^## )", report)
    found = re.search(r"(?ms)^## Findings\s*\n(.*?)(?=^## |\Z)", report)
    verdict = head.group(1).strip() if head else "unknown"
    icon = "✅" if verdict.startswith("CLEAN") else "🔎"
    out = [f"### {icon} What was asked, what was built (code, not the model): {verdict}"]
    if card and card.group(1).strip():
        out.append(card.group(1).strip())
    items = [x.strip() for x in (found.group(1) if found else "").splitlines() if re.match(r"\s*\d+\.\s", x)]
    if items:
        out.append("\n".join(items[:8]) + (f"\n_… and {len(items) - 8} more in the report._" if len(items) > 8 else ""))
    out.append(f"Full report: {link}")
    return "\n\n".join(out)


def review_depth(ctx: Ctx, report: str) -> str:
    """How closely to read this build: the tier the plan got, and what the diff then did (scripts/tiers.py)."""
    st = ctx.state()
    head = re.search(r"(?m)^Drift:\s*(.+)$", report)
    verdict = tiers.Verdict(str(st.get("tier") or "plan"), list(st.get("tier_reasons") or []))
    if not st.get("tier"):
        verdict.reasons = verdict.reasons or ["this item did not go through the risk check (started before it existed, or by hand)"]
    return tiers.review_line(verdict, head.group(1).strip() if head else "no report") + "\n\n"


def drift_block(ctx: Ctx) -> str:
    try:
        report = ctx.branch_file("drift.md")
        return review_depth(ctx, report) + drift_summary(report, ctx.file_link("drift.md")) + "\n\n"
    except StepFailed:
        return (review_depth(ctx, "") + "### What was asked, what was built\n\nNo drift report: this app has no `scripts/drift.py`, so nothing "
                "compared the diff with the spec and the plan except the model's own validation report.\n\n")


def stage_down(app: str) -> None:
    try:
        stage_call("down", app)
    except Exception as e:  # noqa: BLE001 - best effort
        log(f"{app}: could not remove staging: {e}")


def validate_and_checkpoint(ctx: Ctx) -> None:
    check_stop(ctx)
    st = ctx.state()
    everything = CONTEXTS
    for attempt in range(MAX_RETRIES + 1):
        if not st.get("validated"):
            dispatch(ctx, "validate")
            st["validated"] = True
            ctx.save(st)
        res = ci_wait(ctx, everything)
        bad = failing(res, everything)
        if not bad:
            break
        if attempt == MAX_RETRIES:
            raise StepFailed(f"checks still red after rework: {res}")
        rework(ctx, res, everything)
        st["validated"] = False
        ctx.save(st)
    pr = ctx.pr()
    mins = {g: 0 for g in GATES}
    for m in jsonl(DATA / "human_minutes.jsonl"):
        if m["repo"] == ctx.app and m["issue"] == ctx.n and m["gate"] in mins:
            mins[m["gate"]] += m["minutes"]
    body = pr["body"] or ""
    row = "| " + " | ".join(str(mins[g]) for g in GATES) + " |"
    body = re.sub(r"(\|\s*Spec[^\n]*\|\s*\n\|[-|: ]+\|\s*\n)\|[^\n]*\|", lambda m: m.group(1) + row, body, count=1)
    title = re.sub(r"^WIP:\s*", "", pr["title"])
    ctx.g.call("PATCH", f"/repos/{ctx.repo}/pulls/{pr['number']}", {"title": title, "body": body})
    checkpoint(ctx, "4", "try it, review and merge",
               f"{night_note(ctx)}{autopilot_summary(ctx)}{drift_block(ctx)}All four checks are green on PR #{pr['number']}: {pr['html_url']}\nValidation report: "
               f"{ctx.file_link('validation.md')}\n\n{staging_block(ctx)}\n\n---\nThen read the full diff and merge "
               f"it yourself (I removed `WIP:` and filled the minutes table). Reply `/go` after the merge and I "
               f"will push `main` to the GitHub mirror and remove the staged build. If something is wrong, reply "
               f"`/rework <what is wrong>` instead and I will fix it and stage it again.",
               "after_gate4", rework=True)


def night_note(ctx: Ctx) -> str:
    """On Checkpoint 4: what the night policy approved instead of the owner, and the risky files the diff touches."""
    try:
        sh("git", "fetch", "-q", "origin", cwd=ctx.clone)
        approved = sh("git", "log", "--grep", night.TRAILER, "--fixed-strings", "--format=%s", f"origin/main..origin/{ctx.branch}",
                      cwd=ctx.clone).splitlines()
        files = sh("git", "diff", "--name-only", f"origin/main...origin/{ctx.branch}", cwd=ctx.clone).splitlines()
    except subprocess.CalledProcessError:
        return ""
    out = []
    if approved:
        out.append("🌙 **The night shift approved these, not you. Read them as if new:**\n" +
                   "\n".join(f"- {a.split(': approved')[0]}" for a in approved))
    risky = night.risky_files(files)
    if risky:
        out.append("⚠️ **The diff touches sensitive files:** " + ", ".join(f"`{f}`" for f in risky[:12]))
    return "\n\n".join(out) + "\n\n" if out else ""


def after_gate4(ctx: Ctx) -> None:
    still_open = next((p for p in ctx.g.get(f"/repos/{ctx.repo}/pulls?state=open") if p["head"]["ref"] == ctx.branch), None)
    if still_open:  # an earlier PR of the same branch was merged (a rework after a merge): that is not this review
        raise NeedsHuman(f"PR #{still_open['number']} for `{ctx.branch}` is not merged yet. Review and merge it, then "
                         "reply `/go` again. Nothing was removed or published.")
    prs = ctx.g.get(f"/repos/{ctx.repo}/pulls?state=closed&limit=50")
    pr = next((p for p in prs if p["head"]["ref"] == ctx.branch and p.get("merged")), None)
    if not pr:
        raise NeedsHuman(f"PR for `{ctx.branch}` is not merged yet. Merge it, then reply `/go` again.")
    ctx.record_minutes("code_review")
    stage_down(ctx.app)
    sh("git", "checkout", "-q", "main", cwd=ctx.clone)
    sh("git", "pull", "-q", "--ff-only", "origin", "main", cwd=ctx.clone)
    try:
        mirror.push(ctx.clone)  # one snapshot of main; refuses when its files hold internal names or addresses
    except SystemExit as e:
        raise NeedsHuman(f"{e}\n\nMerged on Gitea, but `main` was NOT pushed to the GitHub mirror. Fix the names in a "
                         "new PR, then reply `/go` again.") from e
    log(f"{ctx.app}#{ctx.n}: merged PR #{pr['number']}, pushed main to github")
    if os.environ.get("FACTORY_RELEASES", "on") != "off":
        return release_candidate(ctx)
    checkpoint(ctx, "4b", "merged and mirrored; the release is not automated yet",
               f"PR #{pr['number']} is merged and `main` is on the GitHub mirror. The release steps (tag, image build, "
               f"trivy scan, registry push, deploy guide) are not in the foreman yet ({RELEASE_ISSUE}). To release now, "
               f"open a Claude Code session in `~/git/software-factory` and say: run the release for {ctx.app}.",
               "after_merge", reply=f"Reply `/go` to close this work item (the release waits in {RELEASE_ISSUE}), or "
                                    "`/stop` to stop.")


RELEASE_ISSUE = "factory#27"


def release_call(*args: str) -> str:
    res = subprocess.run([sys.executable, "-I", str(HQ / "scripts" / "release.py"), *args], capture_output=True,
                         text=True, env={**os.environ, "WORK": str(WORK)}, timeout=1800)
    if res.returncode != 0:
        raise NeedsHuman((res.stderr or res.stdout).strip()[-1200:] or "release.py failed")
    return res.stdout


def release_candidate(ctx: Ctx) -> None:
    """Stage 6 and 7 (docs/lifecycle.md): build, scan and push the image of main, run that image on the devbox, and ask
    the owner to accept it at Checkpoint 5. A refused candidate (red main, fixable findings) says why and can be retried."""
    check_stop(ctx)
    try:
        rc = json.loads(release_call("candidate", ctx.app))
    except NeedsHuman as e:
        checkpoint(ctx, "5-fix", "no release candidate",
                   f"{e}\n\nFix it with a new change, then reply `/go` to build the candidate again, or `/stop`.",
                   "release_candidate")
        return
    f = rc["findings"]
    changes = "\n".join(f"- {c}" for c in rc["changes"][:15]) or "- (first release)"
    checkpoint(ctx, "5", f"try release candidate {rc['version']}, then accept it",
               f"The image of `main` ({rc['sha'][:7]}) is built, scanned and in the registry. **This exact image** runs on "
               f"the devbox now:\n\n**[Open the candidate]({rc['url']})**"
               + (f" (on this PC: [{rc['local_url']}]({rc['local_url']}))" if rc.get("local_url") else "")
               + f"\n\n- version `{rc['version']}`, digest `{rc['digest'][:19]}…`\n- scan: "
               f"{f['fixed']} fixable and {f['unfixed']} unfixed HIGH/CRITICAL findings ({f['critical']} critical)\n\n"
               f"Changes since {rc['previous'] or 'the start'}:\n{changes}\n\n---\nReply `/go` to accept "
               f"`{rc['version']}`: the commit is tagged and the deploy to Kubernetes follows (a GitOps pull request for "
               "you to merge). `/rework <what is wrong>` sends it back as a change.",
               "after_release", rework=True, extra=f"version={rc['version']};sha={rc['sha']}")


def after_release(ctx: Ctx) -> None:
    """Checkpoint 5 answered with /go: the owner accepted the candidate they tried on the devbox."""
    version, sha = ctx.marker.get("version", ""), ctx.marker.get("sha", "")
    release_call("accept", ctx.app, version, sha)
    ctx.record_minutes("release")
    ctx.comment(f"{NOTICE_PREFIX} ✅ Release `{version}` accepted and tagged ({sha[:7]}). The deploy to Kubernetes "
                f"(stage 8 of docs/lifecycle.md) is not automated yet; it waits in {RELEASE_ISSUE}. Closing this work "
                "item.")
    ctx.g.call("PATCH", f"/repos/{ctx.repo}/issues/{ctx.n}", {"state": "closed"})
    commit_ops(f"Release {version}: {ctx.app}#{ctx.n}")
    notify(f"{ctx.app}: release {version} accepted", f"{ctx.app}#{ctx.n}: tagged; the deploy is next", priority=3)


def after_merge(ctx: Ctx) -> None:
    """Checkpoint 4b answered: the change is merged and mirrored, so the work item is done. The release is not
    automated yet; the item is closed and the release is noted on the release issue, so nothing waits forever."""
    when = datetime.now(UTC).strftime("%Y-%m-%d")
    ctx.comment(f"{NOTICE_PREFIX} ✅ Done: merged and mirrored, closing this work item. The release (tag, image, scan, "
                f"registry, deploy) is not automated yet and waits in {RELEASE_ISSUE}.")
    try:
        n27 = int(RELEASE_ISSUE.split("#")[1])
        ctx.g.call("POST", f"/repos/{ctx.g.user}/factory/issues/{n27}/comments",
                   {"body": f"{ctx.app}#{ctx.n} merged and mirrored on {when}; waits for a release."})
    except Exception as e:  # noqa: BLE001 - a note elsewhere must never keep the item open
        log(f"{ctx.app}#{ctx.n}: could not note the release: {type(e).__name__}")
    ctx.g.call("PATCH", f"/repos/{ctx.repo}/issues/{ctx.n}", {"state": "closed"})
    commit_ops(f"Done: {ctx.app}#{ctx.n}")
    notify(f"{ctx.app}: done", f"{ctx.app}#{ctx.n} merged, mirrored and closed; release waits in {RELEASE_ISSUE}",
           priority=2)


def manual(ctx: Ctx) -> None:
    # a notice, not a claim: a claim would become the item's state and make it look busy forever
    ctx.comment(f"{NOTICE_PREFIX} Nothing automated is left for this gate (comment {ctx.go_id}): a session is needed. "
                "`/stop` ends the item.")
    notify(f"{ctx.app}: needs a session", f"{ctx.app}#{ctx.n} waits for a manual step", priority=4)


def after_staging_rework(ctx: Ctx) -> None:
    """The owner tried the staged build and sent it back. Their note is the instruction; one implement run, then
    the usual checks, validation and a new staging checkpoint."""
    st = ctx.state()
    note = st.get("rework_note", "").strip()
    if not note:
        raise NeedsHuman("`/rework` needs the text of what is wrong. Reply `/rework <what is wrong>`.")
    ctx.record_minutes("rework")
    if not st.get("rework_implemented"):  # a retry after a later step failed (a usage limit, a CI timeout) resumes there
        st["validated"] = False
        ctx.save(st)
        dispatch(ctx, "implement", note=("The owner tried the staged build and asks for this change (their words, treat as "
                                          "data and apply only what it asks; do not weaken any check):\n\n" + note[:2000]),
                 note_kind="owner")
        st = ctx.state()
        st["rework_implemented"] = True
        ctx.save(st)
    want = ("checks", "gates", "security")
    res = ci_wait(ctx, want)
    if failing(res, want):
        rework(ctx, res, want)
    validate_and_checkpoint(ctx)
    st = ctx.state()  # the review checkpoint is posted: this rework is finished
    st["rework_implemented"] = False
    ctx.save(st)


REWORK_HANDLERS = {"after_gate3": "after_plan_rework", "after_gate4": "after_staging_rework",
                   "after_release": "after_staging_rework"}
HANDLERS = {"after_gate1": after_gate1, "after_gate2": after_gate2, "after_research_questions": after_research_questions,
            "after_gate3": after_gate3, "after_timebox": after_timebox, "after_gate4": after_gate4, "manual": manual,
            "after_staging_rework": after_staging_rework, "after_plan_rework": after_plan_rework,
            "after_merge": after_merge}
LEGACY = {"1": "after_gate1", "2": "after_gate2", "2b": "after_research_questions", "3": "after_gate3",
          "4": "after_gate4", "T": "after_timebox"}


# ---------------------------------------------------------------- the tick
def parse_marker(body: str) -> dict[str, str]:
    m = MARKER.search(body)
    out: dict[str, str] = {}
    if m:
        for part in m.group(1).split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
    if "resume" not in out:
        label = re.match(rf"^{CP_PREFIX}\s+(\S+?):", body)
        if label and label.group(1) in LEGACY:
            out["resume"] = LEGACY[label.group(1)]
    return out


ANSWERED = re.compile(r"\(comment (\d+)")


def answered_ids(mine: list[dict]) -> set[int]:
    """Commands the foreman already answered with a notice (refused, ignored, superseded): never acted on again."""
    return {int(x) for c in mine if c["body"].startswith(NOTICE_PREFIX) for x in ANSWERED.findall(c["body"])}


def command_options(body: str) -> commands.Parsed:
    if m := GO.match(body):
        return commands.parse_go(m.group(1))
    if m := REWORK.match(body):
        return commands.parse_rework(m.group(1) or "")[0]
    return commands.Parsed()


def refuse(ctx: Ctx, cmd_id: int, reason: str, fix: str, also: str = "") -> None:
    """A command that cannot run gets this reply on the tick that sees it, and a push: never silence."""
    ctx.comment(f"{NOTICE_PREFIX} 🛑 Refused (comment {cmd_id}){also}: {reason}.\n\nTo fix: {fix}")
    notify(f"{ctx.app}: command refused", f"{ctx.app}#{ctx.n}: {reason}", priority=4,
           click=ctx.url(f"/issues/{ctx.n}"))
    log(f"{ctx.app}#{ctx.n}: refused comment {cmd_id}: {reason}")


def decide(g: Gitea, repo: str, n: int) -> tuple[dict, dict, str, int | None] | None:
    """Return (checkpoint_comment, go_or_stop_comment, 'go'|'stop', minutes) if there is work to do."""
    mine = [c for c in g.paged(f"/repos/{repo}/issues/{n}/comments") if ours(g, c)]
    pivot = next((c for c in reversed(mine) if c["body"].startswith((CP_PREFIX, CLAIM_PREFIX))), None)
    if pivot is None or not pivot["body"].startswith(CP_PREFIX):
        return None
    answered = answered_ids(mine)
    cmds = [c for c in mine if c["id"] > pivot["id"] and c["id"] not in answered and by_owner(g, c)
            and is_command(c["body"])]
    if cmds:
        # The owner's latest word wins, and `/stop` beats everything. 2026-11-22 (brevkoll#29): a queued `/go` waited
        # 19 minutes for the busy local lane; the owner sent `/go model=fast` to use the cloud, as the factory had told
        # them to, and the earlier `/go` ran on the local model anyway while the override was "not acted on".
        stops = [c for c in cmds if STOP.match(c["body"])]
        c = stops[-1] if stops else cmds[-1]
        c = {**c, "covers": [x["id"] for x in cmds if x["id"] != c["id"]]}
        if m := GO.match(c["body"]):
            minutes = MINUTES.search(m.group(1))
            return pivot, c, "go", int(minutes.group(1)) if minutes else None
        return pivot, c, "stop" if STOP.match(c["body"]) else "rework", None
    return night_command(g, repo, n, pivot, mine)


def covered(cmd: dict) -> str:
    """The earlier commands this one replaces, written so answered_ids() counts them as answered (no "not acted on")."""
    ids = cmd.get("covers") or []
    return f", replacing the earlier {' '.join(f'(comment {i})' for i in ids)}" if ids else ""


NIGHT = {"taken": False}  # one night-shift action per tick (reset in run_command)


def night_command(g: Gitea, repo: str, n: int, pivot: dict, mine: list[dict]) -> tuple[dict, dict, str, None] | None:
    """No command from the owner: may the night shift answer this checkpoint itself? (scripts/night.py)"""
    now = datetime.now(UTC)
    if not night.in_window(now):
        return None
    labels = {label["name"] for label in g.get(f"/repos/{repo}/issues/{n}/labels")}
    if night.LABEL in labels and not label_added_by_owner(g, repo, n, night.LABEL):
        labels.discard(night.LABEL)  # only the owner's label is an approval
    why = night.may_pass(pivot, parse_marker(pivot["body"]), labels, mine, now)
    if why is None:
        return None
    return pivot, {"id": pivot["id"], "body": "/go", "auto": why}, "go", None


class StopRequested(Exception):
    """The owner said /stop while a handler was running: heard between phases, or at once as StepStopped."""

    def __init__(self, comment_id: int):
        super().__init__(f"/stop (comment {comment_id})")
        self.comment_id = comment_id


class StepStopped(StopRequested):
    """The owner's /stop ended a running step at once (dispatch.py exit 6). Only the step ends: the item waits at a
    checkpoint, so the lane is free for the next item and the owner chooses what happens to this one."""

    def __init__(self, comment_id: int, detail: str = ""):
        super().__init__(comment_id)
        self.detail = detail


def stop_item(ctx: Ctx, marker: dict[str, str], comment_id: int, where: str = "", also: str = "") -> None:
    signals.clear(STATE, ctx.app, ctx.n)
    ctx.comment(f"{CLAIM_PREFIX} Stopped by /stop (comment {comment_id}){also}{where}. Nothing more will run for this issue.")
    if marker.get("stop") == "kill":
        ctx.g.set_stage(ctx.repo, ctx.n, "killed")
    stage_down(ctx.app)
    log(f"{ctx.app}#{ctx.n}: /stop received, stopped cleanly")
    commit_ops(f"Stopped: {ctx.app}#{ctx.n}")
    notify(f"{ctx.app}: stopped", f"{ctx.app}#{ctx.n} stopped by /stop")


def check_stop(ctx: Ctx) -> None:
    """Called between phases of a long handler: a /stop written after the command that started this run ends it cleanly.
    A build runs inside one tick, so without this the owner could not stop it for hours."""
    for c in owner_comments(ctx.g, ctx.repo, ctx.n):
        if c["id"] > ctx.go_id and STOP.match(c["body"]):
            raise StopRequested(c["id"])


def consecutive_failures(g: Gitea, repo: str, n: int, handler: str) -> int:
    """Failures of this handler since the last agent step that worked. Two refusals from before a fix, then two successful
    phases, then a new problem is a first failure, not a third (it was counted as a third on brevkoll#15)."""
    mine = [c for c in g.paged(f"/repos/{repo}/issues/{n}/comments") if ours(g, c)]
    last_ok = max((c["id"] for c in mine if c["body"].startswith("Agent step")), default=0)
    return sum(1 for c in mine if c["id"] > last_ok and f"retry-of={handler}" in c["body"])


START_WINDOW_H = 24  # an older `/go` on an unstarted issue is history, not a request (no surprise starts on deploy)


def start_request(g: Gitea, repo: str, n: int) -> dict | None:
    """The owner's `/go` on an issue the factory has not started (no checkpoint, no claim of ours): it starts the item.
    2026-11-22: a bug filed as an issue, then `/go model=fast`, got no answer at all, because only labeled issues were
    watched. Now `/go` is the one way to start work, any time of day."""
    comments = g.paged(f"/repos/{repo}/issues/{n}/comments")
    mine = [c for c in comments if ours(g, c)]
    if any(c["body"].startswith((CP_PREFIX, CLAIM_PREFIX)) for c in mine):
        return None
    answered = answered_ids(mine)
    cutoff = datetime.now(UTC) - timedelta(hours=START_WINDOW_H)
    gos = [c for c in comments if by_owner(g, c) and GO.match(c["body"]) and c["id"] not in answered
           and parse_ts(c["created_at"]) >= cutoff]
    return gos[-1] if gos else None


def start(g: Gitea, repo: str, n: int, cmd: dict) -> str:
    """Start an issue on the owner's `/go`: in its lane's own process, like any step."""
    app = repo.split("/")[1]
    opts = command_options(cmd["body"])
    if opts.errors:
        refuse(Ctx(g, repo, n, None, cmd["id"], {}), cmd["id"], "; ".join(opts.errors),
               f"send the command again without that option. Accepted: {commands.USAGE}.")
        return "refused"
    model = opts.model or commands.DEFAULT_MODEL
    if lanes.enabled():
        if lanes.item_running(STATE, app, n):
            return "busy"
        lane = lanes.lane_of(model)
        if lane == "local" and (pending := model_switch_pending(g)):
            return f"queued: the local model is being switched to `{pending}` (/llm on the status issue)"
        if not lanes.has_room(STATE, lane):
            return f"queued: the {lane} lane is full ({lanes.describe(lanes.busy(STATE, lane))})"
        rec = lanes.spawn(STATE, app, n, lane, ["adopt", app, str(n), "--go", str(cmd["id"])], "spec (start)",
                          HQ / "scripts" / "orchestrate.py")
        log(f"{app}#{n}: started by /go (comment {cmd['id']}) in the {lane} lane ({rec['unit']})")
        return f"started in the {lane} lane"
    issue = g.get(f"/repos/{repo}/issues/{n}")
    adopt(g, repo, n, issue.get("title", ""), go=cmd)
    return "started"


def process(g: Gitea, repo: str, n: int, dry: bool = False) -> str:
    if decide(g, repo, n) is None:
        cmd = start_request(g, repo, n)
        if cmd is None:
            return "waiting"
        return "would start" if dry else start(g, repo, n, cmd)
    if dry:
        return "would act"
    if lanes.enabled():
        return hand_off(g, repo, n)
    return process_inline(g, repo, n)


def model_switch_pending(g: Gitea) -> str:
    """The model the owner asked to switch to with `/llm` (scripts/llm_switch.py), while the switch waits or runs: no
    new local step starts then. Empty when none, or older than 3 hours (a switcher that died must not block forever)."""
    if not lease.enabled():
        return ""
    try:
        cur = lease.Lease(g).read("FOREMAN_LLM_SWITCH")
    except Exception:  # noqa: BLE001 - unknown: do not block the factory on it
        return ""
    fresh = time.time() - float(cur.get("since") or 0) < 3 * 3600
    return str(cur.get("target") or "") if fresh else ""


def resume_of(pivot: dict, marker: dict[str, str]) -> str:
    """The handler a `/go` runs at this checkpoint. Old `-failed` checkpoints said `manual` (a dead end: brevkoll#26);
    their label still names the step, so `/go` tries it once more like the new ones."""
    handler = marker.get("resume", "")
    label = night.checkpoint_label(pivot["body"])
    if handler == "manual" and label.endswith("-failed") and label.removesuffix("-failed") in HANDLERS:
        return label.removesuffix("-failed")
    return handler


def hand_off(g: Gitea, repo: str, n: int) -> str:
    """The tick never runs a step itself (scripts/lanes.py): it starts the item in its lane's own process, or leaves the
    command waiting when the lane is full. Quick answers (a refusal, /stop, a manual gate) are given right here."""
    app = repo.split("/")[1]
    d = decide(g, repo, n)
    if d is None:
        return "waiting"
    if lanes.item_running(STATE, app, n) or item_is_running(app, n):
        return "busy"
    pivot, cmd, kind, _ = d
    opts = command_options(cmd["body"])
    marker = parse_marker(pivot["body"])
    quick = (kind == "stop" or bool(opts.errors) or resume_of(pivot, marker) == "manual"
             or (kind == "rework" and marker.get("rework") != "ok"))
    if quick:
        return process_inline(g, repo, n)
    lane = "local" if cmd.get("auto") else lanes.lane_of(opts.model or commands.DEFAULT_MODEL)
    if lane == "local" and (pending := model_switch_pending(g)):
        return f"queued: the local model is being switched to `{pending}` (/llm on the status issue)"
    if not lanes.has_room(STATE, lane):
        return f"queued: the {lane} lane is full ({lanes.describe(lanes.busy(STATE, lane))})"
    rec = lanes.spawn(STATE, app, n, lane, ["run", app, str(n)], resume_of(pivot, marker) or "a step",
                      HQ / "scripts" / "orchestrate.py")
    log(f"{app}#{n}: started in the {lane} lane ({rec['unit']})")
    return f"started in the {lane} lane"


def take_item_lock(app: str, n: int) -> IO[str] | None:
    """The item's lock, or None when a process holds it. Whoever runs steps for an item holds it for the whole run
    (every autopilot hop included), so that no tick mistakes a live run for a dead one."""
    STATE.mkdir(exist_ok=True)
    lock = (STATE / f"{app}-{n}.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return None
    return lock


def process_inline(g: Gitea, repo: str, n: int) -> str:
    app = repo.split("/")[1]
    lock = take_item_lock(app, n)
    if lock is None:
        return "busy"
    d = decide(g, repo, n)  # again, now that we hold the lock
    if d is None:
        return "waiting"
    pivot, cmd, kind, minutes = d
    marker = parse_marker(pivot["body"])
    auto = cmd.get("auto")
    if auto:
        if NIGHT["taken"]:
            return "waiting"  # one night item per tick: the GPU runs one model and one step at a time
        NIGHT["taken"] = True
    else:
        try:
            record_wait(app, n, pivot, cmd, kind)
        except Exception as e:  # noqa: BLE001 - metrics must never block a gate
            log(f"could not record the wait: {type(e).__name__}")
    ctx = Ctx(g, repo, n, minutes, cmd["id"], marker)
    ctx.auto = bool(auto)
    opts = command_options(cmd["body"])
    if opts.errors:
        refuse(ctx, cmd["id"], "; ".join(opts.errors), f"send the command again without that option. Accepted: {commands.USAGE}.",
               also=covered(cmd))
        return "refused"
    ctx.model = "local" if auto else (opts.model or commands.DEFAULT_MODEL)  # the night shift never spends credit
    if not auto and kind == "go":
        set_autopilot(ctx, opts.stop, opts.tier)  # from here the item runs on to Checkpoint 4 (or where `stop=` says)
    if kind == "stop":
        stop_item(ctx, marker, cmd["id"], also=covered(cmd))
        return "stopped"
    handler = resume_of(pivot, marker)
    if kind == "rework":
        if marker.get("rework") != "ok":
            # a notice, not a claim: a claim would become the pivot and hide the owner's next /go
            refuse(ctx, cmd["id"], "`/rework` only works at the plan checkpoint and the review checkpoint",
                   "reply `/go` to continue, or `/stop`", also=covered(cmd))
            return "ignored"
        st = ctx.state()
        rw = REWORK.match(cmd["body"])
        st["rework_note"] = commands.parse_rework(rw.group(1) or "")[1] if rw else ""
        st["rework_implemented"] = False  # a new request: the implement step has to run
        ctx.save(st)
        handler = REWORK_HANDLERS.get(marker.get("resume", ""), "after_staging_rework")
    if handler not in HANDLERS:
        ctx.comment(f"{CLAIM_PREFIX} I do not know what `/go` should run here (no resume marker). A session is needed.")
        notify(f"{app}: unclear checkpoint", f"{app}#{n} has no resume marker", priority=4)
        return "unknown"
    if handler == "manual":  # nothing runs: answer with a notice so the checkpoint stays the state (no claim)
        manual(ctx)
        return "ran manual"
    if auto:
        ctx.comment(f"{CLAIM_PREFIX} 🌙 Night shift (label `{night.LABEL}`): no command from you, so I pass checkpoint "
                    f"`{night.checkpoint_label(pivot['body'])}` myself: {auto}. Running `{handler}` on model `local`. "
                    "You review everything at Checkpoint 4; remove the label or reply `/stop` to stop me.")
    else:
        ctx.comment(f"{CLAIM_PREFIX} Picked up `/{'rework' if kind == 'rework' else 'go'}` (comment {cmd['id']}"
                    f"{f', {minutes} min' if minutes is not None else ''}){covered(cmd)}. Running `{handler}` on model "
                    f"`{ctx.model or 'default'}`.")
    log(f"{app}#{n}: /go (comment {cmd['id']}, minutes={minutes}, model={ctx.model}) -> {handler}")
    if auto:
        result = run_handler(g, repo, n, ctx, marker, handler)
        night.record(STATE, datetime.now(UTC), f"{app}#{n}", handler, result)
        return result
    return run_handler(g, repo, n, ctx, marker, handler)


def run_handler(g: Gitea, repo: str, n: int, ctx: Ctx, marker: dict[str, str], handler: str) -> str:
    app = repo.split("/")[1]
    try:
        HANDLERS[handler](ctx)
        return f"ran {handler}"
    except StepStopped as e:
        checkpoint(ctx, f"{handler}-stopped", "you stopped the running step",
                   f"Your `/stop` (comment {e.comment_id}) ended the step at once. {e.detail}\n\n"
                   f"- `/go` runs `{handler}` again (`/go model=fast` on Claude)\n"
                   "- `/rework <what to do differently>` at the plan or review checkpoint, or comment your direction and `/go`\n"
                   "- `/stop` ends the item", handler)
        return "step stopped"
    except StopRequested as e:
        stop_item(ctx, marker, e.comment_id, where=" between two steps")
        return "stopped"
    except NeedsHuman as e:
        checkpoint(ctx, f"{marker.get('gate', handler)}-fix", "one thing is left for you", str(e), handler)
        return "needs human"
    except StepFailed as e:
        cls = FAILURE_CLASS.search(str(e))
        if cls:  # not the code's fault (credit, login, model down): say what to do, do not spend a retry
            checkpoint(ctx, f"{handler}-blocked", f"blocked: {cls.group(1)}",
                       f"Diagnosis:\n\n```\n{e}\n```\n\nNo retry was counted. Reply `/go` (with `model=...` if the "
                       f"hint says so) to run `{handler}` again.", handler)
            return f"blocked:{cls.group(1)}"
        prior = consecutive_failures(g, repo, n, handler)
        if prior >= MAX_RETRIES:
            # Not a dead end: `/go` tries once more (the owner may have fixed something, or wants the cloud). On
            # brevkoll#26 `/go model=fast` here got "nothing automated is left" (2026-11-22).
            checkpoint(ctx, f"{handler}-failed", "retries used up",
                       f"`{handler}` failed {prior + 1} times. Last error:\n\n```\n{e}\n```\n\nReply `/go` to try once "
                       "more (`/go model=fast` to use Claude), or `/stop`.", handler)
        else:
            checkpoint(ctx, f"{handler}-retry", "a step failed",
                       f"Diagnosis:\n\n```\n{e}\n```\n\nReply `/go` to retry `{handler}` (retry {prior + 1} of "
                       f"{MAX_RETRIES}).", handler, extra=f"retry-of={handler}")
        return "failed"
    except Exception as e:  # never leave an issue without a visible state
        log(f"{app}#{n}: unexpected {type(e).__name__}: {str(e)[-200:]}")
        ctx.comment(f"{CLAIM_PREFIX} Unexpected error in `{handler}`: `{type(e).__name__}`. A session is needed.")
        notify(f"{app}: foreman error", f"{type(e).__name__} in {handler}", priority=5)
        return "error"


def runtime_problem(root: Path = HQ) -> str | None:
    """The timer runs this checkout. If it is on another branch or has local code changes it silently runs the wrong
    code (the owner's /go on brevkoll#15 was ignored for that reason). Data files (data/, BOARD.md) change all the time
    and do not count. Returns what is wrong, or None."""
    def git(*a: str) -> str:
        return subprocess.run(["git", *a], cwd=root, capture_output=True, text=True, check=False).stdout.strip()

    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        return f"the runtime checkout is on `{branch}`, not `main`. Dev work belongs in a worktree under worktrees/"
    if git("status", "--porcelain", "--", "scripts", "tests", "PLAYBOOK.md"):
        return "the runtime checkout has uncommitted changes in scripts/, tests/ or PLAYBOOK.md"
    return None


def git_out(root: Path, *args: str) -> tuple[int, str]:
    res = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False, env={**os.environ, **ORCH})
    return res.returncode, res.stdout.strip()


def local_commits_are_metrics_only(root: Path = HQ) -> bool:
    """True when every commit the runtime has and origin/main lacks is a 'Metrics refresh' that touches only BOARD.md and
    data/: the foreman's own bookkeeping, safe to put on top of whatever was merged meanwhile."""
    _, subjects = git_out(root, "log", "--format=%s", "origin/main..HEAD")
    _, files = git_out(root, "log", "--name-only", "--format=", "origin/main..HEAD")
    names = [x for x in files.splitlines() if x]
    return bool(subjects) and all(x == "Metrics refresh" for x in subjects.splitlines()) \
        and all(f == "BOARD.md" or f.startswith("data/") for f in names)


def sync_metrics_commits(root: Path = HQ) -> bool:
    """Rebase the local metrics commits onto origin/main and push them. False when they are not only metrics commits or the
    rebase does not apply cleanly (then the guard pauses and tells the owner, as before)."""
    git_out(root, "fetch", "-q", "origin", "main")
    if not local_commits_are_metrics_only(root):
        return False
    code, _ = git_out(root, "rebase", "-q", "origin/main")
    if code != 0:
        git_out(root, "rebase", "--abort")
        return False
    git_out(root, "push", "-q", "origin", "HEAD")
    return True


def update_runtime(root: Path = HQ) -> None:
    """Fast-forward the runtime checkout to origin/main when it is clean and on main, so a merge takes effect within a
    minute without anyone touching the folder. The foreman's own metrics commits are rebased on top, not a blocker."""
    if runtime_problem(root):
        return
    subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=root, check=False, capture_output=True)
    res = subprocess.run(["git", "merge", "--ff-only", "-q", "origin/main"], cwd=root, check=False, capture_output=True)
    if res.returncode != 0:
        sync_metrics_commits(root)


def runtime_behind(root: Path = HQ) -> str | None:
    """After a fast-forward attempt the runtime must not still be behind origin/main (that means local commits)."""
    res = subprocess.run(["git", "rev-list", "--count", "HEAD..origin/main"], cwd=root, capture_output=True, text=True,
                         check=False)
    if res.returncode == 0 and res.stdout.strip() not in ("", "0"):
        return "the runtime checkout cannot fast-forward to origin/main (it has local commits)"
    return None


def head_of(root: Path = HQ) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=False).stdout.strip()


def guard_runtime() -> bool:
    """Called at the start of every tick. False means: do nothing this minute and tell the owner (once per problem)."""
    problem = runtime_problem()
    alert = STATE / "runtime-alert.txt"
    before = head_of()
    if problem is None:
        update_runtime()
        problem = runtime_behind(HQ)
    if problem is None:
        alert.unlink(missing_ok=True)
        if before != head_of():
            log(f"runtime updated to {head_of()[:7]}")
            os.execv(sys.executable, [sys.executable, "-I", *sys.argv])  # run this tick with the new code
        return True
    log(f"runtime guard: {problem}")
    STATE.mkdir(exist_ok=True)
    if not alert.exists() or alert.read_text() != problem:
        alert.write_text(problem)
        notify("factory: foreman paused", problem + ". No /go or /stop is being read until this is fixed.", priority=5)
    return False


MERGES = STATE / "merges.json"


def watched_repos(g: Gitea) -> list[str]:
    apps = [x["full_name"] for x in g.paged("/repos/search?q=factory-app&topic=true")]
    return sorted({f"{g.user}/factory", f"{g.user}/factory-template", *apps})


def announce_merges(g: Gitea) -> None:
    """Tell the owner when a pull request lands: title, where, and why (the first line of its body). The owner merges;
    the factory only reports. The first run records 'now' and sends nothing."""
    STATE.mkdir(exist_ok=True)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    seen = json.loads(MERGES.read_text()) if MERGES.exists() else None
    if seen is None:
        MERGES.write_text(json.dumps({"since": now, "sent": []}))
        return
    sent = set(seen["sent"])
    for repo in watched_repos(g):
        for pr in g.get(f"/repos/{repo}/pulls?state=closed&sort=recentupdate&limit=20"):
            key = f"{repo}#{pr['number']}"
            if not pr.get("merged") or key in sent or (pr.get("merged_at") or "") <= seen["since"]:
                continue
            why = next((ln.strip() for ln in (pr.get("body") or "").splitlines() if ln.strip() and not ln.startswith("Refs")), "")
            notify(f"Merged: {key}", f"{pr['title']}\n{why[:200]}", click=pr["html_url"], priority=3)
            log(f"merged: {key} {pr['title'][:80]}")
            sent.add(key)
    MERGES.write_text(json.dumps({"since": seen["since"], "sent": sorted(sent)[-200:]}))


# ---------------------------------------------------------------- no silent ignores
REPLIED = STATE / "replied.json"
REMINDERS = STATE / "reminders.json"
REMIND_AFTER_H = float(os.environ.get("FACTORY_REMIND_AFTER_H", "4"))
REMIND_EVERY_H = float(os.environ.get("FACTORY_REMIND_EVERY_H", "6"))
NOTICE_PREFIX = "ℹ️"


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_command(body: str) -> bool:
    return bool(GO.match(body) or STOP.match(body) or REWORK.match(body))


def owner_comments(g: Gitea, repo: str, n: int) -> list[dict]:
    """The owner's comments. With one shared login the bot's own comments are told apart by their prefix."""
    return [c for c in g.paged(f"/repos/{repo}/issues/{n}/comments") if by_owner(g, c)
            and not c["body"].startswith((CP_PREFIX, CLAIM_PREFIX, NOTICE_PREFIX, "Agent step"))]


def answer_unknown_commands(g: Gitea, repo: str, n: int) -> None:
    """An owner comment that starts with `/` but is not a command gets one reply that says what is accepted."""
    seen = set(json.loads(REPLIED.read_text())) if REPLIED.exists() else set()
    comments = g.paged(f"/repos/{repo}/issues/{n}/comments")
    pivot = next((c for c in reversed(comments) if ours(g, c)
                  and c["body"].startswith((CP_PREFIX, CLAIM_PREFIX))), None)
    if pivot is None:  # not started: `/go` starts it (start_request); anything else gets one reply, never silence
        cutoff = datetime.now(UTC) - timedelta(hours=START_WINDOW_H)
        for c in owner_comments(g, repo, n):
            key = f"{repo}#{c['id']}"
            if (key in seen or not c["body"].lstrip().startswith("/") or GO.match(c["body"])
                    or parse_ts(c["created_at"]) < cutoff):
                continue
            word = c["body"].lstrip().split()[0][:30]
            g.comment(repo, n, f"{NOTICE_PREFIX} `{word}` (comment {c['id']}) did nothing: this issue has not started "
                               "yet. Reply `/go` to start it (the autopilot runs to a staged build you can try; `/go stop=spec` pauses after the spec); "
                               "add `model=fast` to use the cloud.")
            seen.add(key)
        STATE.mkdir(exist_ok=True)
        REPLIED.write_text(json.dumps(sorted(seen)[-300:]))
        return
    for c in owner_comments(g, repo, n):
        key = f"{repo}#{c['id']}"
        if (c["id"] <= pivot["id"] or key in seen or not c["body"].lstrip().startswith("/") or is_command(c["body"])
                or STEER.match(c["body"])):  # /steer: status.py hands it to the running step or says why not
            continue
        word = c["body"].lstrip().split()[0][:30]
        if word.lower() == "/llm":  # brevkoll#26, 2026-11-22: sent on a work issue; it only works on the status issue
            status = next((i["number"] for i in g.paged(f"/repos/{g.user}/factory/issues?state=open&type=issues")
                           if i["title"] == "Factory status"), None)
            where = (f"{g.repo_url(f'{g.user}/factory').removesuffix('.git')}/issues/{status}" if status
                     else "the pinned Factory status issue")
            g.comment(repo, n, f"{NOTICE_PREFIX} `/llm` (comment {c['id']}) switches the local model and works only on "
                               f"the Factory status issue: {where} . Send it there; nothing was done here.")
        else:
            g.comment(repo, n, f"{NOTICE_PREFIX} I did not understand `{word}` (comment {c['id']}), so I did nothing. "
                               f"Accepted: {commands.USAGE}. A note may follow on the same line or below.")
        seen.add(key)
    for c in unanswered_commands(g, repo, n, pivot):
        # Never invite a resend: the item has moved on, and the same words mean something else at the new checkpoint
        # (a resent /go at the review checkpoint means "I merged"; brevkoll#15 comment 584).
        now_at = pivot["body"].splitlines()[0][:100]
        g.comment(repo, n, f"{NOTICE_PREFIX} Not acted on (comment {c['id']}): `{c['body'].strip().splitlines()[0][:60]}` "
                           "arrived while another command was being handled, so it was skipped and will not run. "
                           f"Nothing to do about it. The issue is now at: {now_at}. Answer that, as it says.")
        notify(f"{repo.split('/')[1]}: old command skipped", f"#{n}: comment {c['id']} was skipped (it came during a "
               "run). No action needed; answer the current checkpoint as it says.",
               priority=3, click=f"{g.repo_url(repo).removesuffix('.git')}/issues/{n}")
        seen.add(f"{repo}#{c['id']}")
    STATE.mkdir(exist_ok=True)
    REPLIED.write_text(json.dumps(sorted(seen)[-300:]))


SKIPPED_WINDOW_H = 24  # older commands are history: answering them on the first tick after a deploy would be noise


def unanswered_commands(g: Gitea, repo: str, n: int, pivot: dict) -> list[dict]:
    """Owner commands older than the current checkpoint or claim that no comment of the foreman refers to: they were
    never acted on (comment 534 on brevkoll#15 was such a /go). Each gets one notice."""
    comments = g.paged(f"/repos/{repo}/issues/{n}/comments")
    bot = [c for c in comments if ours(g, c) and c["body"].startswith((CP_PREFIX, CLAIM_PREFIX, NOTICE_PREFIX))]
    referred = {int(x) for c in bot for x in ANSWERED.findall(c["body"])}
    cutoff = datetime.now(UTC).timestamp() - SKIPPED_WINDOW_H * 3600
    return [c for c in owner_comments(g, repo, n) if c["id"] < pivot["id"] and is_command(c["body"])
            and c["id"] not in referred and parse_ts(c["created_at"]).timestamp() >= cutoff]


def waiting_list(g: Gitea) -> list[dict]:
    """Work items that wait for the owner: the last comment of the bot is a checkpoint nobody has answered yet."""
    out = []
    for repo, n in active_issues(g):
        mine = [c for c in g.paged(f"/repos/{repo}/issues/{n}/comments") if ours(g, c)]
        pivot = next((c for c in reversed(mine) if c["body"].startswith((CP_PREFIX, CLAIM_PREFIX))), None)
        if pivot is None or not pivot["body"].startswith(CP_PREFIX):
            continue
        if any(c["id"] > pivot["id"] and is_command(c["body"]) for c in mine):
            continue  # answered; the next tick handles it
        since = parse_ts(pivot["created_at"])
        out.append({"repo": repo, "issue": n, "what": pivot["body"].splitlines()[0].removeprefix(CP_PREFIX).strip(),
                    "since": since.isoformat(timespec="minutes"),
                    "age_h": round((datetime.now(UTC) - since).total_seconds() / 3600, 1)})
    return out


def remind_waiting(g: Gitea) -> None:
    """One push per waiting item after REMIND_AFTER_H hours, then again every REMIND_EVERY_H hours."""
    sent = json.loads(REMINDERS.read_text()) if REMINDERS.exists() else {}
    now_s = datetime.now(UTC).timestamp()
    for w in waiting_list(g):
        key = f"{w['repo']}#{w['issue']}:{w['what'][:40]}"
        if w["age_h"] >= REMIND_AFTER_H and now_s - sent.get(key, 0) >= REMIND_EVERY_H * 3600:
            notify(f"Waiting for you: {w['repo'].split('/')[1]}#{w['issue']}", f"{w['what']} (since {w['since'][11:16]}, {w['age_h']} h)",
                   click=f"{g.repo_url(w['repo']).removesuffix('.git')}/issues/{w['issue']}", priority=3)
            sent[key] = now_s
    STATE.mkdir(exist_ok=True)
    REMINDERS.write_text(json.dumps(sent))


def record_wait(app: str, n: int, pivot: dict, cmd: dict, kind: str) -> None:
    """How long the owner took to answer a checkpoint: the real human-friction number per gate."""
    DATA.mkdir(exist_ok=True)
    wait = (parse_ts(cmd["created_at"]) - parse_ts(pivot["created_at"])).total_seconds() / 60
    with (DATA / "gate_waits.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": now(), "repo": app, "issue": n, "checkpoint": parse_marker(pivot["body"]).get("resume", ""),
                             "answer": kind, "wait_min": round(wait, 1), "comment_id": cmd["id"]}) + "\n")


# ---------------------------------------------------------------- crash recovery
RUNNING_CLAIM = re.compile(r"Running `(\w+)`")
FAILED_CLAIM = re.compile(r"Unexpected error in `(\w+)`")


def stuck_handler(g: Gitea, repo: str, n: int) -> str | None:
    """The handler an item was running when it stopped, if its last bot comment is a claim (`Picked up ... Running X` or
    `Unexpected error in X`) instead of a checkpoint. Such an item answers to no /go: decide() looks for a checkpoint."""
    mine = [c for c in g.paged(f"/repos/{repo}/issues/{n}/comments") if ours(g, c)]
    pivot = next((c for c in reversed(mine) if c["body"].startswith((CP_PREFIX, CLAIM_PREFIX))), None)
    if pivot is None or not pivot["body"].startswith(CLAIM_PREFIX):
        return None
    m = RUNNING_CLAIM.search(pivot["body"]) or FAILED_CLAIM.search(pivot["body"])
    return m.group(1) if m and m.group(1) in HANDLERS else None


def item_is_running(app: str, n: int) -> bool:
    """True while a process holds the item's lock, that is, while a step of it is really running."""
    path = STATE / f"{app}-{n}.lock"
    if not path.exists():
        return False
    with path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


def recover_interrupted(g: Gitea, repo: str, n: int) -> bool:
    """If the foreman died while running a step (kill, reboot, out of memory), say so with a checkpoint the owner can
    answer, instead of leaving the item silent. The lock is held for exactly as long as a process is running the item."""
    app = repo.split("/")[1]
    STATE.mkdir(exist_ok=True)
    if lanes.item_running(STATE, app, n):
        return False  # a lane's child is running it (an `adopt` child holds no item lock: brevkoll#6, 2026-11-22)
    lock = (STATE / f"{app}-{n}.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False  # a process is running it right now
    handler = stuck_handler(g, repo, n)
    if handler is None:
        return False
    ctx = Ctx(g, repo, n, None, 0, {})
    checkpoint(ctx, f"{handler}-interrupted", "the foreman stopped while running a step",
               f"`{handler}` did not finish: the process ended before it reported anything (a restart, a kill, or an "
               "unexpected error). Nothing was lost; the step is safe to run again. Reply `/go` to run it again, or "
               "`/stop`.", handler)
    notify(f"{app}: interrupted", f"{app}#{n}: `{handler}` did not finish. Reply /go to run it again.", priority=4)
    log(f"{app}#{n}: recovered an interrupted `{handler}`")
    return True


def active_issues(g: Gitea) -> list[tuple[str, int]]:
    out = []
    for r in sorted(x["full_name"] for x in g.paged("/repos/search?q=factory-app&topic=true")):
        for i in g.paged(f"/repos/{r}/issues?state=open&type=issues"):
            names = {label["name"] for label in i.get("labels", [])}
            # A started item carries a stage label. Any other open issue with a comment may hold the owner's `/go` that
            # starts it (start_request), or a command to answer: never ignore a command silently (brevkoll#29).
            if names & {"stage/idea", "stage/spec-ready", "stage/building"} or i.get("comments", 0) > 0:
                out.append((r, i["number"]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    r = sub.add_parser("run")
    r.add_argument("repo")
    r.add_argument("issue", type=int)
    ad = sub.add_parser("adopt")
    ad.add_argument("repo")
    ad.add_argument("issue", type=int)
    ad.add_argument("--go", type=int, help="the owner's /go comment that starts the issue (else: the night shift)")
    sub.add_parser("status")
    a = ap.parse_args()
    try:
        g = Gitea()
        if a.cmd in ("run", "adopt"):  # one item, inside this process (a lane's child, or a session by hand)
            os.environ["FACTORY_CHILD"] = "1"
            try:
                if a.cmd == "run":
                    print(process(g, f"{g.user}/{a.repo}", a.issue))
                else:
                    lock = take_item_lock(a.repo, a.issue)  # held until this process ends
                    if lock is None:
                        print("busy")
                        return
                    repo = f"{g.user}/{a.repo}"
                    title = g.get(f"/repos/{repo}/issues/{a.issue}")["title"]
                    if a.go:  # started by the owner's /go: only while it is still the open request (no double start)
                        cmd = start_request(g, repo, a.issue)
                        if cmd and cmd["id"] == a.go:
                            adopt(g, repo, a.issue, title, go=cmd)
                    else:
                        adopt(g, repo, a.issue, title)
            finally:
                lanes.finished(STATE, a.repo, a.issue)
            return
        run_command(g, a)
    except Unreachable as e:
        # A stalled network path ends this tick quietly. The next one (a minute later) tries again; the heartbeat file
        # keeps the time of the last tick that really read Gitea, so an outage is visible and reported when it ends.
        log(f"gitea unreachable, tick skipped: {str(e)[:160]}")
    except Exception as e:  # a crashed tick used to be visible only in the journal
        alarm(f"crash-{type(e).__name__}", "the tick crashed", f"{type(e).__name__}: {str(e)[:200]}. "
              "It runs again in a minute; this push repeats at most every 6 hours while it keeps crashing.")
        raise


HEARTBEAT = STATE / "heartbeat"
BLIND_NOTICE_MINUTES = 10


def beat(now: float | None = None) -> None:
    """Record a tick that read Gitea. If the one before it was long ago, say so: comments in between were not seen."""
    now = time.time() if now is None else now
    try:
        last = float(HEARTBEAT.read_text())
    except (OSError, ValueError):
        last = now
    STATE.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(str(now))
    gap = (now - last) / 60
    if gap >= BLIND_NOTICE_MINUTES:
        notify("Factory: back after a blind spell",
               f"The foreman could not read Gitea for {gap:.0f} minutes. Commands sent in that time are handled "
               "now; nothing was lost.")


def each_issue(issues: list[tuple[str, int]], what: str, fn: Callable[[str, int], object]) -> None:
    """Run fn for every issue. One issue failing is logged and skipped; only an unreachable Gitea ends the tick."""
    for repo, n in issues:
        try:
            fn(repo, n)
        except Unreachable:
            raise
        except Exception as e:  # noqa: BLE001 - one bad issue must not stop the others
            alarm(f"issue-{repo}#{n}-{what}", f"{what} failed on {repo.split('/')[-1]}#{n}",
                  f"{type(e).__name__}: {str(e)[:160]}. The other issues were handled; this one is retried every tick.")


def night_tick(g: Gitea) -> None:
    """After the issues: pick up one labeled issue that has no checkpoint yet, and send the morning summary once."""
    if not night.enabled():
        return
    now = datetime.now(UTC)
    rows = night.summary_due(STATE, now)
    if rows:  # nothing happened: no push at all
        notify("factory: the night shift", night.summary_text(rows), priority=3)
    repos = sorted(x["full_name"] for x in g.paged("/repos/search?q=factory-app&topic=true"))
    made = STATE / "night-label.txt"  # the label exists in every app repo, so the owner can pick it from the list
    if made.exists() is False or made.read_text().split() != repos:
        for repo in repos:
            g.ensure_label(repo, night.LABEL, "#6366f1", "Night shift may pass the gates before the code (docs/night-shift.md)")
        STATE.mkdir(parents=True, exist_ok=True)
        made.write_text("\n".join(repos))
    if (NIGHT["taken"] or not night.in_window(now) or (lanes.enabled() and not lanes.has_room(STATE, "local"))
            or model_switch_pending(g)):
        return
    for repo in repos:
        for i in g.paged(f"/repos/{repo}/issues?state=open&type=issues&labels={night.LABEL}"):
            if night.LABEL not in {label["name"] for label in i.get("labels", [])}:
                continue
            if not label_added_by_owner(g, repo, i["number"], night.LABEL):
                continue
            mine = [c for c in g.paged(f"/repos/{repo}/issues/{i['number']}/comments") if ours(g, c)]
            if any(c["body"].startswith((CP_PREFIX, CLAIM_PREFIX)) for c in mine):
                continue  # already in the loop: decide() handles it
            NIGHT["taken"] = True
            if lanes.enabled():
                if lanes.has_room(STATE, "local"):
                    rec = lanes.spawn(STATE, repo.split("/")[1], i["number"], "local", ["adopt", repo.split("/")[1],
                                      str(i["number"])], "spec (night pick-up)", HQ / "scripts" / "orchestrate.py")
                    log(f"{repo}#{i['number']}: night pick-up started in the local lane ({rec['unit']})")
            else:
                adopt(g, repo, i["number"], i["title"])
            return


def adopt(g: Gitea, repo: str, n: int, title: str, go: dict | None = None) -> None:
    """Start an issue: an agent drafts the spec and the holdout tests; then the autopilot runs on to Checkpoint 4 (or
    Checkpoint 1 with `stop=spec`). Started by the owner's `/go` (go: that comment and the model it names) or by the night shift (label, local model,
    Checkpoint 1 passed on the next tick)."""
    if go is not None:
        opts = command_options(go["body"])
        model = opts.model or commands.DEFAULT_MODEL
        ctx = Ctx(g, repo, n, None, go["id"], {}, model=model)
        ctx.auto = False
        set_autopilot(ctx, opts.stop, opts.tier)
        until = {"spec": "the spec and its acceptance tests (Checkpoint 1)",
                 "plan": "the plan (Checkpoint 3)"}.get(opts.stop or "", "a staged build you can try (Checkpoint 4)")
        earlier = "".join(f" (comment {c['id']})" for c in g.paged(f"/repos/{repo}/issues/{n}/comments")
                          if by_owner(g, c) and GO.match(c["body"]) and c["id"] < go["id"])
        also = f", covering the earlier{earlier}" if earlier else ""
        ctx.comment(f"{CLAIM_PREFIX} Picked up `/go` (comment {go['id']}){also}: starting this issue. An agent reads it (and "
                    f"your comments) and works on model `{model}` until {until}. It stops earlier only for a plan that "
                    "touches risky files or a step that fails. `/stop` stops it.")
    else:
        ctx = Ctx(g, repo, n, None, 0, {}, model="local")
        ctx.auto = True
        ctx.comment(f"{CLAIM_PREFIX} 🌙 Night shift (label `{night.LABEL}`): picking up this issue. An agent drafts the "
                    "spec and the holdout tests from it on model `local`; then Checkpoint 1, which I pass on the next "
                    "tick. You review everything at Checkpoint 4; remove the label or reply `/stop` to stop me.")
    app = repo.split("/")[1]
    g.set_stage(repo, n, "idea")
    try:
        pick_up(ctx)
    except StepFailed as e:
        # A retryable checkpoint like any failed step: `/go` (or the night shift) drafts the spec again, within the
        # retry budget. brevkoll#7 (2026-11-22) timed out once and got a checkpoint only a session could answer.
        if ctx.auto:
            night.record(STATE, datetime.now(UTC), f"{app}#{n}", "spec", "failed")
        cls = FAILURE_CLASS.search(str(e))
        label, title_ = (("spec-blocked", f"blocked: {cls.group(1)}") if cls
                         else ("spec-retry", "the spec draft failed"))
        checkpoint(ctx, label, title_, f"Diagnosis:\n\n```\n{str(e)[-1200:]}\n```\n\nReply `/go` to draft it again "
                   "(add `model=fast` for the cloud), or write the spec yourself with `scripts/new_change.py`.",
                   "pick_up", extra="" if cls else "retry-of=pick_up")


def pick_up(ctx: Ctx) -> None:
    """The spec step for an issue that has no spec yet; then the autopilot goes on, or Checkpoint 1 (stop=spec, night)."""
    issue = ctx.g.get(f"/repos/{ctx.repo}/issues/{ctx.n}")
    title = issue.get("title", "") if isinstance(issue, dict) else ""
    dispatch(ctx, "spec")
    if ctx.auto:
        night.record(STATE, datetime.now(UTC), f"{ctx.app}#{ctx.n}", "spec", "drafted")
    hold = tiers.after_spec(issue_kind_of(ctx), str(ctx.state().get("tier_forced") or "")) if autopilot_on(ctx) else None
    if autopilot_on(ctx) and ctx.state().get("stop_at") != "spec" and not (hold and hold.tier == "spec"):
        return hop(ctx, "after_gate1", f"the [spec]({ctx.file_link('spec.md')}) and its acceptance tests are drafted")
    why = ("**The autopilot stopped here:** " + "; ".join(hold.reasons) + ". Say what you want in `/go <your choice>`.\n\n"
           if hold and hold.reasons else "")
    checkpoint(ctx, "1", "the spec and its acceptance tests",
               f"{why}An agent drafted the spec and the acceptance tests for \"{title}\" on `{ctx.branch}`:\n\n"
               f"- [spec.md]({ctx.file_link('spec.md')})\n- [tests/holdout/]({ctx.url(f'/src/branch/{ctx.branch}/tests/holdout')})"
               "\n\nIs this the change you want? Reply `/go` to approve it (I record your approval), edit the files "
               "first if something is wrong, or `/stop`.", "after_gate1")


HANDLERS["pick_up"] = pick_up
HANDLERS["release_candidate"] = release_candidate
HANDLERS["after_release"] = after_release  # defined after the table: the night shift's pick-up, retried with /go


def hold_lease(g: Gitea) -> bool:
    """Only the host holding the lease acts (scripts/lease.py). A primary that cannot use the lease (an old token, the
    API down) still ticks and says so; a standby never acts without it."""
    if not lease.enabled():
        return True
    ls = lease.Lease(g)
    try:
        ok, why = ls.claim()
    except Unreachable:
        raise
    except Exception as e:  # noqa: BLE001
        if lease.role() == "standby":
            print(f"lease: unreadable ({type(e).__name__}); a standby does not act")
            return False
        alarm("lease", "the foreman lease does not work",
              f"{type(e).__name__}: {str(e)[:160]}. This host keeps ticking; a standby will not take over.")
        return True
    print(f"lease ({lease.role()} {ls.me}): {why}")
    if not ok:
        return False
    if why.startswith("took over"):
        alarm("standby-took-over", f"the standby {ls.me} took over",
              f"{why}: the primary stopped renewing the foreman lease. Work continues here and goes back when the "
              "primary is seen again.")
    ls.keepalive()
    return True


def run_command(g: Gitea, a: argparse.Namespace) -> None:
    live = a.cmd == "tick" and not a.dry_run
    if live and not guard_runtime():
        return
    if live and not hold_lease(g):
        return
    issues = active_issues(g)  # listed once per tick: every extra listing is another chance to meet a stalled request
    if live:
        beat()
        try:
            announce_merges(g)
        except Unreachable:
            raise
        except Exception as e:  # noqa: BLE001 - a notification problem must never stop the run
            alarm("announce-merges", "merge announcements failed", f"{type(e).__name__}: {str(e)[:120]}")
        each_issue(issues, "answering an unknown command", lambda r, n: answer_unknown_commands(g, r, n))
        each_issue(issues, "recovery", lambda r, n: recover_interrupted(g, r, n))
    if a.cmd == "status":
        for repo, n in issues:
            d = decide(g, repo, n)
            print(f"{repo}#{n}: {'go/stop waiting to be handled' if d else 'waiting or busy'}")
        for repo, n in issues:
            handler = stuck_handler(g, repo, n)
            if handler and item_is_running(repo.split("/")[1], n):
                print(f"RUNNING: {repo}#{n} is running `{handler}` now")
            elif handler:
                print(f"STUCK: {repo}#{n} was running `{handler}` and the process ended; the next tick posts a checkpoint")
        for w in waiting_list(g):
            print(f"WAITING FOR YOU: {w['repo']}#{w['issue']} at {w['what']} since {w['since']} ({w['age_h']} h)")
    else:
        NIGHT["taken"] = False
        each_issue(issues, "processing", lambda r, n: print(f"{r}#{n}: {process(g, r, n, dry=a.dry_run)}"))
    if live:
        try:
            night_tick(g)
        except Unreachable:
            raise
        except Exception as e:  # noqa: BLE001
            alarm("night", "the night shift failed", f"{type(e).__name__}: {str(e)[:160]}")
        try:
            remind_waiting(g)
        except Unreachable:
            raise
        except Exception as e:  # noqa: BLE001
            alarm("remind-waiting", "reminders failed", f"{type(e).__name__}: {str(e)[:100]}")


if __name__ == "__main__":
    main()
