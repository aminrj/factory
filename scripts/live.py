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

RECENT = 12  # lines of the agent's own words and actions kept in the JSON
WRITE_EVERY_S = 2.0


def short(text: str, limit: int = 160) -> str:
    one = " ".join(str(text).split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


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
    def __init__(self, root: Path, app: str, n: int, step: str, model: str, wt: Path | None = None) -> None:
        self.dir = root / "state" / "board" / "live"
        self.prefix = f"{wt}/" if wt else ""
        self.key = f"{app}-{n}"
        self.state = {"app": app, "n": n, "step": step, "model": model, "started": time.time(), "updated": time.time(),
                      "turns": 0, "action": "starting", "files": [], "todos": [], "recent": [], "round": 1}
        self._last_write = 0.0
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.log = (self.dir / f"{self.key}.log").open("w", buffering=1)
        except OSError:  # the board is a convenience: never fail a step over it
            self.log = None
        self.write(force=True)

    def line(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        if self.log:
            self.log.write(f"{stamp} {text}\n")
        self.state["recent"] = (self.state["recent"] + [f"{stamp} {short(text, 220)}"])[-RECENT:]

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
                    self.line(block["text"].strip())
                elif block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    if self.prefix:  # paths relative to the worktree read better on a phone
                        inp = {k: (v.removeprefix(self.prefix) if isinstance(v, str) else v) for k, v in inp.items()}
                    what, path = describe_tool(block.get("name", ""), inp)
                    self.state["action"] = what
                    self.line(f"→ {what}")
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
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    content = block.get("content")
                    text = content if isinstance(content, str) else json.dumps(content)[:300]
                    self.line(f"  ✗ {short(text, 200)}")
        elif kind == "result":
            self.state["action"] = "finished" if not ev.get("is_error") else "ended with an error"
        self.write()

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
