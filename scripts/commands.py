"""The options a command accepts, in one place. The foreman refuses an unknown option or a bad value by name, so a typo
never becomes silence (`/go agent=local` was read as a note on 2026-11-21 and nothing said so).

`/go [minutes=N] [model=P] [note]`: options may appear anywhere on the first line; any `word=value` there is an option.
`/rework [model=P] <what is wrong>`: options only right after the command; the rest is the note (it may contain `=`
after its first word).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

# Model profiles. The owner picks one per command; the foreman never switches on its own (no silent fallback).
MODELS: dict[str, str] = {
    "local": "the local model on the GPU workstation (no cloud credit used)",
    "smart": "Claude Opus (best results, highest cost)",
    "fast": "Claude Sonnet (good and quicker)",
    "cheap": "Claude Haiku (lowest cost)",
}
# `/go` alone runs on the local model: the cloud is used only when the owner types `model=smart|fast|cheap`.
DEFAULT_MODEL = os.environ.get("FACTORY_DEFAULT_MODEL", "local")
# The owner's own time on a gate (reading, trying, deciding), in minutes. Typing `minutes=N` records N; without it the
# gate's typical time below is recorded and marked `default`, so the metrics can tell measured time from a guess.
GATE_MINUTES: dict[str, int] = {"spec": 10, "threat_model": 5, "plan_review": 10, "code_review": 15, "rework": 5}
OPTIONS: dict[str, str] = {"minutes": "N (your own time on this gate, optional)", "model": "|".join(MODELS)}
OPTION = re.compile(r"^([A-Za-z_][\w-]*)=(\S*)$")
USAGE = "`/go [model=" + "|".join(MODELS) + "] [minutes=N]` (default: local model), `/stop`, `/rework [model=...] <what is wrong>`"


@dataclass
class Parsed:
    minutes: int | None = None
    model: str | None = None
    errors: list[str] = field(default_factory=list)


def _apply(p: Parsed, key: str, value: str) -> None:
    k = key.lower()
    if k == "minutes":
        digits = re.match(r"\d+", value)  # `minutes=5min` is read as 5, as before
        if digits:
            p.minutes = int(digits.group(0))
        else:
            p.errors.append(f"`minutes={value}` is not a number, write for example `minutes=10`")
    elif k == "model":
        if value.lower() in MODELS:
            p.model = value.lower()
        else:
            p.errors.append(f"`model={value}` is not a profile I know, use one of: " + ", ".join(f"`{m}`" for m in MODELS))
    else:
        p.errors.append(f"`{key}=` is not an option I know, the options are: "
                        + ", ".join(f"`{name}={v}`" for name, v in OPTIONS.items()))


def parse_go(first_line: str) -> Parsed:
    """The text after `/go` on its first line."""
    p = Parsed()
    for tok in first_line.split():
        if m := OPTION.match(tok):
            _apply(p, m.group(1), m.group(2))
    return p


def parse_rework(text: str) -> tuple[Parsed, str]:
    """The text after `/rework`: leading options, then the note."""
    p = Parsed()
    rest = text.lstrip()
    while m := re.match(r"([A-Za-z_][\w-]*)=(\S*)(?:[ \t]+|$)", rest):
        _apply(p, m.group(1), m.group(2))  # strict: `/rework agent=local ...` is refused, not read as a note
        rest = rest[m.end():]
    return p, rest.strip()
