# Checkpoints and the night shift

## The checkpoints (CP1 to CP4)

A work item is one Gitea issue. Comment `/go` on a new issue to start it. The **autopilot** then runs it straight to
CP4 (a staged build): your starting `/go` approves the spec, the threat model and the plan as they come, each approval
still a commit naming that comment. It stops at CP1 or CP3 only when you ask (`/go stop=spec`, `/go stop=plan`), when the
plan touches risky files, or when a step fails. Where it stops it posts a `🛑 CHECKPOINT` comment and waits for your
answer: `/go`, `/rework <what is wrong>` (CP3 and CP4) or `/stop`. At CP1 to CP3 your `/go` **is** the approval: the factory writes it into the file for
you (a commit by factory-orchestrator with `Approved-by-owner: comment N`), and the guards accept it only when comment N
is your `/go` on that issue. Edit a file first only when you want something different.

| CP | What waits for you | What `/go` means | What runs after your `/go` |
|---|---|---|---|
| **1** spec | `thoughts/N/spec.md` and `tests/holdout/` on the branch | this is the right change, and the tests check it | Threat model, research and plan, back to back |
| **3** plan | `thoughts/N/plan.md`, with the threat model (rows accepted provisionally) and the research questions | build this plan (`/rework <note>` to change it; edit a threat-model row first to decide it differently) | The build: one agent run per phase, local checks and self-fix, CI, then validation and staging |
| **4** review | the pull request, all checks green, the staged build to try on the phone | (merge it yourself first) the merge is done | Push to the GitHub mirror, remove the staged build |

Items that were already waiting at one of the old stops between CP1 and CP3 finish through them:

| CP | What waits for you | What `/go` means |
|---|---|---|
| **2** threat model | `thoughts/N/threat-model.md` | accept every row still `PROPOSED` (rows you changed stay as you wrote them) |
| **2b** questions (only if research raised any) | the open questions in `research.md` | go on; unanswered questions are deferred |

Checkpoints whose label ends in `-retry` (a step failed, retry budget 2), `-blocked` (the model, the credit or the login,
no retry spent), `-fix` (something only you can fix) or `-failed` (retries used up) are the same gates after a problem.

Defaults: `/go` alone runs on the **local model** and records the gate's typical time (`minutes=` is optional; see
`scripts/commands.py`). `/go model=smart|fast|cheap` uses Claude. `/go model=grok` and `/auto model=grok` use the Grok logged in on the foreman host. The night shift stays on the local model.

## The night shift: `night/ok`

Put the label `night/ok` on an issue of a factory app (from the phone: the issue, Labels). That is your standing
approval for **the gates before the code** (CP1, CP2, CP2b, CP3), under this policy:

1. **When:** 22:00 to 07:00 local time (`FACTORY_NIGHT_WINDOW`, `always` for daytime too). One item per tick, on the
   **local model only**: it never spends cloud credit. If no local model serves, the step waits up to 10 minutes and
   wakes the GPU workstation once (see `ops/devbox/README.md`).
2. **Picking up:** an issue with the label and no checkpoint yet is picked up. An agent drafts the spec and the holdout
   tests from the issue (the `spec` step: it may write only `thoughts/N/` and `tests/holdout/`; the commit is
   re-authored as factory-orchestrator, so later agent steps still cannot touch them). Then CP1 is posted.
3. **Passing a gate:** the next tick answers the checkpoint itself. Each approval is one commit by
   factory-orchestrator with the trailer `Approved-by-policy: night/ok`:
   - CP1: `Status: approved` in the spec;
   - CP2: every `PROPOSED` becomes `ACCEPTED (night policy)`;
   - CP2b: continues without answers (the planner defers them);
   - CP3: `Status: approved` in the plan, **unless the plan mentions risky areas** (deploy, CI workflows, Dockerfile,
     dependencies, auth, secrets, passwords, tokens, TLS, certificates, crypto). Then the item stops at a "needs you"
     checkpoint.
   The guards (`dispatch.check_entry`) accept such an approval only while the label is on the issue and only from
   factory-orchestrator with the trailer. An agent's commit never counts, trailer or not.
4. **Problems:** a failed step is retried within the normal budget. A step blocked because the model was busy or
   loading is tried again after 20 minutes, at most 3 times in a row. A `-fix` or `-failed` checkpoint waits for you.
5. **Always stops at CP4.** The pull request and the staged build wait for you. CP4 lists what the night shift
   approved instead of you ("read them as if new") and the sensitive files the diff touches. It never merges and never
   deploys.
6. **Quiet at night:** night checkpoints push at the lowest priority (no sound). At 07:00 one push sums up the night.

Stop it: remove the label (the next checkpoint waits for you as usual), or `/stop`. Turn it off for the whole factory:
`FACTORY_NIGHT=off` in `~/.config/factory/host.env`.

### What to label

Good night work: bugs with a clear symptom (brevkoll #6, #7, #8), small features, test gaps, a red quality ratchet,
dependency bumps without a major version. Not night work: anything deploy, auth, secrets or TLS (the policy stops it at
CP3 anyway), anything you have not decided yet.

### Trust and its limits

- **Who added the label.** With the factory's own Gitea user (`scripts/identity.py`), a label counts only if the
  owner added it (Gitea's timeline says who), and a command from the factory's login is never obeyed. With the old
  single shared login Gitea cannot tell, and anyone who can make that login add a label can start night work; the
  damage is still bounded (local model, no merge, no deploy, CP4 always yours).
- The same model family writes the holdout tests (spec step) and the code (implement step) of a night item. They run as
  separate agents with separate prompts, and the guard stops the implementer from editing the tests, but the tests are
  not independent of the model in the way your own CP1 is. Read the tests at CP4.

## Switching the local model from the phone: `/llm`

Comment on the pinned **Factory status** issue (factory, "Factory status"):

| Comment | What happens |
|---|---|
| `/llm status` | the answer shows what is loaded now (`llm status`) |
| `/llm code` | Qwen3.6-35B-A3B: fast, the factory's local model for now |
| `/llm 38` | Qwen3.8-27B: slower, stronger |
| `/llm agent` | stops everything; the arbiter brings back the assistant's model after 10 quiet minutes |
| `/llm off` | stops everything |
| add `now` | switch even while a local step runs (that step fails and is tried again later) |

The GPU workstation checks every minute (`factory-llm-switch.timer`, only where `FACTORY_LLM_SWITCH=on`). It obeys
only your login, answers at once, and switches only through your own `llm` script, which stops every model server
and waits for the GPU memory to be free before it starts the next one: a model is never loaded on top of another.
Without `now` it waits until the local lane is empty, and the foreman starts no new local step while a switch is
pending. It gives up after 3 hours.

## The board's Archived lane

Done items have an **archive** button: the card moves to the folded **Archived** lane on that device (browser
storage), and **restore** puts it back. Nothing changes on the issue; the board stays read-only. Done items older
than 7 days leave the board by themselves.
