"""What an agent is doing right now, for the board: its current action, the files it touched, its todo list, its words.

2026-11-22: the owner could not see what an agent was working on while a step ran; only a summary arrived at the end.
dispatch.py now runs Claude Code with `--output-format stream-json`, feeds every event to a Live object, and the board
polls the files this writes (every few seconds) under $WORK/state/board/live/:

  index.json         the items running now (the board reads only this)
  <app>-<n>.json     one item: step, model, started, turns, action, files, todos, recent lines
  <app>-<n>.log      the whole run as readable lines (kept after the step as <app>-<n>.last.log)

The board is served only on the foreman host's LAN/tailnet address, never mirrored, so file names and agent words are
fine here (they are not in data/, which is mirrored).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

RECENT = 80  # entries kept for the board: the agent's words, its thinking, its actions and what they returned
RESULT_LINES = 6  # first lines of each tool result shown on the board (the .log file keeps it all)
WRITE_EVERY_S = 2.0
LOOP_WINDOW, LOOP_SAME, LOOP_RUN, LOOP_REFUSED = 12, 5, 10, 6
BOOKKEEPING = {"TodoWrite", "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "ToolSearch"}


def short(text: str, limit: int = 160) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def clip(text: str, lines: int = RESULT_LINES, width: int = 200) -> str:
    """The first lines of a block of text, each cut to `width`: enough to see what came back."""
    rows = [r.rstrip() for r in str(text).strip().splitlines() if r.strip()]
    out = [r if len(r) <= width else r[: width - 1] + "…" for r in rows[:lines]]
    if len(rows) > lines:
        out.append(f"… {len(rows) - lines} more lines")
    return "\n    ".join(out)


def result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def describe_tool(name: str, inp: dict) -> tuple[str, str | None]:
    """A readable line for a tool call, and the file it touches when it writes one."""
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path")
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"{'writes' if name == 'Write' else 'edits'} {path}", path
    if name == "Read":
        return f"reads {path}", None
    if name == "Bash":
        return f"runs `{short(inp.get('command', ''), 120)}`", None
    if name in ("Grep", "Glob"):
        return f"searches {short(inp.get('pattern', ''), 80)}", None
    if name in ("TodoWrite", "TaskCreate", "TaskUpdate", "TaskList"):
        return "updates its task list", None
    if name == "ToolSearch":
        return "loads a tool", None
    return f"uses {name}", None


class Live:
    def __init__(self, root: Path, app: str, n: int, step: str, model: str, wt: Path | None = None, url: str = "") -> None:
        self.dir = root / "state" / "board" / "live"
        self.prefix = f"{wt}/" if wt else ""
        self.key = f"{app}-{n}"
        self.state = {"app": app, "n": n, "step": step, "model": model, "started": time.time(), "updated": time.time(),
                      "turns": 0, "action": "starting", "files": [], "todos": [], "recent": [], "round": 1,
                      "url": url, "warning": "", "warning_id": 0}
        self._calls: list[str] = []  # the last tool calls, for the loop warning
        self._refused = 0  # tool calls refused in a row
        self._last_write = 0.0
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.log = (self.dir / f"{self.key}.log").open("w", buffering=1)
        except OSError:  # the board is a convenience: never fail a step over it
            self.log = None
        self.write(force=True)

    def line(self, text: str, board: str | None = None) -> None:
        """One entry: in full in the .log file, as `board` (default: one line of up to 400 characters) on the board."""
        stamp = time.strftime("%H:%M:%S")
        if self.log:
            self.log.write(f"{stamp} {str(text)[:20000]}\n")
        shown = board if board is not None else short(text, 400)
        self.state["recent"] = (self.state["recent"] + [f"{stamp} {shown}"])[-RECENT:]

    def note(self, text: str) -> None:
        """A line from the factory itself (checks, self-fix rounds)."""
        self.state["action"] = short(text)
        self.line(f"[factory] {text}")
        self.write(force=True)

    def event(self, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "assistant":
            self.state["turns"] += 1
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "text" and block.get("text", "").strip():
                    self.line(block["text"].strip(), clip(block["text"], 10, 300))
                elif block.get("type") == "thinking" and block.get("thinking", "").strip():
                    thought = block["thinking"].strip()
                    self.line(f"💭 {thought}", f"💭 ({len(thought)} chars) {short(thought, 300)}")
                elif block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    if self.prefix:  # paths relative to the worktree read better on a phone
                        inp = {k: (v.removeprefix(self.prefix) if isinstance(v, str) else v) for k, v in inp.items()}
                    what, path = describe_tool(block.get("name", ""), inp)
                    self.state["action"] = what
                    self.watch_loop(block.get("name", ""), what)
                    full = f"→ runs `{inp.get('command', '')}`" if block.get("name") == "Bash" else f"→ {what}"
                    self.line(full, short(full, 400))
                    if path and path not in self.state["files"]:
                        self.state["files"].append(path)
                    if block.get("name") == "TodoWrite":  # older Claude Code: the whole list each time
                        self.state["todos"] = [{"id": str(i), "text": short(t.get("content") or t.get("activeForm") or "", 120),
                                                "status": t.get("status", "pending")} for i, t in enumerate(inp.get("todos") or [])]
                    elif block.get("name") == "TaskUpdate":  # 2.1.x: one task at a time, by id
                        for t in self.state["todos"]:
                            if t["id"] == str(inp.get("taskId")):
                                t["status"] = inp.get("status") or t["status"]
                                t["text"] = short(inp.get("subject") or t["text"], 120)
        elif kind == "user":
            task = (ev.get("tool_use_result") or {}).get("task") if isinstance(ev.get("tool_use_result"), dict) else None
            if isinstance(task, dict) and task.get("id"):  # TaskCreate's answer carries the new task's id
                self.state["todos"].append({"id": str(task["id"]), "text": short(task.get("subject", ""), 120),
                                            "status": "pending"})
            for block in (ev.get("message") or {}).get("content") or []:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                text = result_text(block.get("content"))
                if not text.strip():
                    continue
                mark = "✗" if block.get("is_error") else "←"
                self._refused = self._refused + 1 if block.get("is_error") else 0
                if self._refused == LOOP_REFUSED:
                    self.warn(f"{LOOP_REFUSED} of its tool calls in a row failed or were refused")
                self.line(f"  {mark} {text}", f"  {mark} {clip(text)}")
        elif kind == "result":
            self.state["action"] = "finished" if not ev.get("is_error") else "ended with an error"
        self.write()

    def watch_loop(self, name: str, what: str) -> None:
        """brevkoll#7 (2026-11-22): 62 `ListAgents` calls in a row, and before that the same denied `cat` again and
        again. The same call LOOP_SAME times among the last LOOP_WINDOW, or one tool LOOP_RUN times in a row, raises a
        warning: the board shows it and dispatch.py interrupts the run for an early retrospective."""
        if name in BOOKKEEPING:  # task list updates and tool loading repeat by nature (brevkoll#32: false warnings)
            return
        self._calls = (self._calls + [what])[-max(LOOP_WINDOW, LOOP_RUN):]
        recent = self._calls[-LOOP_WINDOW:]
        same = max((recent.count(c) for c in set(recent)), default=0)
        tools = [c.split(" ", 1)[0] for c in self._calls[-LOOP_RUN:]]
        if same >= LOOP_SAME:
            self.warn(f"going in circles: `{what}` {same} times in its last {len(recent)} actions")
        elif len(tools) >= LOOP_RUN and len(set(tools)) == 1 and name not in ("Read", "Edit", "Write"):
            self.warn(f"going in circles: `{name}` {LOOP_RUN} times in a row")

    def warn(self, text: str) -> None:
        if text == self.state.get("warning"):
            return
        self.state["warning"] = text
        self.state["warning_id"] = int(self.state.get("warning_id", 0)) + 1
        self._calls.clear()
        self.line(f"[factory] ⚠️ {text}")
        self.write(force=True)

    def write(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_write < WRITE_EVERY_S:
            return
        self._last_write = now
        self.state["updated"] = now
        try:
            tmp = self.dir / f".{self.key}.json.tmp"
            tmp.write_text(json.dumps(self.state))
            os.replace(tmp, self.dir / f"{self.key}.json")
            self._index()
        except OSError:
            pass

    def _index(self) -> None:
        items = []
        for f in sorted(self.dir.glob("*-*.json")):
            try:
                rec = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if time.time() - float(rec.get("updated", 0)) < 6 * 3600:  # a crashed step's file fades away
                items.append(rec)
        tmp = self.dir / ".index.json.tmp"
        tmp.write_text(json.dumps({"updated": time.time(), "items": items}))
        os.replace(tmp, self.dir / "index.json")

    def close(self) -> None:
        try:
            (self.dir / f"{self.key}.json").unlink(missing_ok=True)
            if self.log:
                self.log.close()
                os.replace(self.dir / f"{self.key}.log", self.dir / f"{self.key}.last.log")
            self._index()
        except OSError:
            pass
