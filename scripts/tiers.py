"""Risk tiers: how far the autopilot may go before the owner reads something.

The autopilot used to run every issue to Checkpoint 4 whatever it was (brevkoll#6, a question, went through it like a
footer change). Now the foreman decides, in code, from facts it can read:

  auto  a bug fix whose plan is small, touches no invariant and adds nothing new: run to the staged build.
  plan  anything else that is a build: stop at the plan (Checkpoint 3) with the reasons, so the owner reads the cheapest
        artefact that can still redirect the work.
  spec  an issue that asks for a decision: stop at the spec (Checkpoint 1).

The owner can force a level (`/go tier=auto`, `/go stop=plan`, `/go stop=spec`); a forced `auto` skips the plan stop but
the reasons are still shown at Checkpoint 4. Nothing here is read by a model: a prompt can be ignored, this cannot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

BUG_LABELS = {"bug", "type/bug", "kind/bug", "fix"}
DECISION_LABELS = {"decision", "question", "type/decision", "kind/question"}
DECISION_TEXT = re.compile(
    r"needs?\s+a\s+decision|decision\s+(?:is\s+)?needed|before any code", re.IGNORECASE
)
MAX_AUTO_PHASES = 2
NO_DEPENDENCIES = ("", "none", "n/a", "no", "no new dependencies", "none.", "-")


@dataclass
class Verdict:
    tier: str  # "auto" | "plan" | "spec"
    reasons: list[str] = field(default_factory=list)


def issue_kind(title: str, body: str, labels: set[str]) -> str:
    """`bug`, `decision` or `feature`. Only labels (the owner's) and the issue's own words about a decision count: a
    guess from the title would let a feature through as a bug, and a bug is what the autopilot may run alone."""
    names = {x.lower() for x in labels}
    if names & DECISION_LABELS or DECISION_TEXT.search(f"{title}\n{body}"):
        return "decision"
    if names & BUG_LABELS:
        return "bug"
    return "feature"


def after_spec(kind: str, forced: str = "") -> Verdict:
    if kind == "decision" and forced != "auto":
        return Verdict(
            "spec",
            [
                "the issue asks for a decision before any code: the options are yours to choose"
            ],
        )
    return Verdict("auto")


def plan_phases(plan: str) -> int:
    return len(re.findall(r"(?m)^##\s+Phase\s+\d+", plan))


def new_dependencies(plan: str) -> str:
    m = re.search(r"(?ims)^##\s+Dependencies added[^\n]*\n(.*?)(?=^##\s|\Z)", plan)
    text = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S).strip() if m else ""
    first = text.splitlines()[0].strip(" -*`.").lower() if text else ""
    return "" if first in NO_DEPENDENCIES else text.splitlines()[0].strip()[:80]


def touches(spec: str) -> list[str]:
    """The N<n> and I<n> the spec says it comes near (its `Touches:` line, comments stripped)."""
    m = re.search(r"Touches:\s*(.*)", spec)
    return (
        re.findall(r"\b[NI]\d+\b", re.sub(r"<!--.*?-->", "", m.group(1))) if m else []
    )


def after_plan(
    kind: str, spec: str, plan: str, risky: list[str], forced: str = ""
) -> Verdict:
    """May the autopilot build this plan without the owner reading it? Reasons are given for every reason it may not."""
    reasons: list[str] = []
    if kind != "bug":
        reasons.append(
            f"this is a {kind}, not a labelled bug fix: label it `bug` to let the autopilot run it"
        )
    if risky:
        reasons.append(
            "the plan touches risky files (" + ", ".join(f"`{r}`" for r in risky) + ")"
        )
    if hit := touches(spec):
        reasons.append(
            "the spec touches "
            + ", ".join(hit)
            + " (a promise in docs/intent.md or docs/architecture.md)"
        )
    if dep := new_dependencies(plan):
        reasons.append(f"the plan adds a dependency ({dep})")
    if re.search(r"docs/decisions/\d{4}-", plan):
        reasons.append(
            "the plan proposes a decision record (ADR): a decision that outlives this change"
        )
    if (n := plan_phases(plan)) > MAX_AUTO_PHASES:
        reasons.append(
            f"the plan has {n} phases (more than {MAX_AUTO_PHASES} is not a small fix)"
        )
    if forced == "auto":
        return Verdict(
            "auto",
            [
                f"you forced the autopilot; without that it would have stopped: {r}"
                for r in reasons
            ],
        )
    return Verdict("plan" if reasons else "auto", reasons)


def review_line(verdict: Verdict, drift_headline: str) -> str:
    """The first line of Checkpoint 4: how closely to read, from the plan's tier and the drift report."""
    clean = drift_headline.upper().startswith("CLEAN")
    if verdict.tier == "auto" and not verdict.reasons and clean:
        return (
            "✅ **Quick review.** A labelled bug fix, a small plan, nothing new, and the diff matches the plan: "
            "try the build, then merge."
        )
    why = list(verdict.reasons)
    if not clean:
        why.append(f"the drift report says `{drift_headline or 'unknown'}`")
    head = (
        "You forced the autopilot; these would have stopped it at the plan:"
        if verdict.tier == "auto" and verdict.reasons
        else "Why:"
    )
    return "🔎 **Read closely.** " + head + "\n" + "\n".join(f"- {r}" for r in why)
