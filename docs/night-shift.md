# Checkpoints and the night shift

## The checkpoints (CP1 to CP4)

A work item is one Gitea issue. The foreman moves it through four checkpoints. At each one it posts a
`🛑 CHECKPOINT` comment and waits for your answer: `/go`, `/rework <what is wrong>` (CP3 and CP4) or `/stop`.

| CP | What waits for you | What you decide | What runs after your `/go` |
|---|---|---|---|
| **1** spec | `thoughts/N/spec.md` and `tests/holdout/` on the branch | Is this the right change, are the acceptance criteria and the tests right? You set `Status: approved` in the spec in a commit of your own. | An agent drafts the threat model |
| **2** threat model | `thoughts/N/threat-model.md` | Every `PROPOSED` row becomes `ACCEPTED`, `CHANGED: ...` or `REJECTED` | An agent researches the code base |
| **2b** questions (only if research raised any) | the open questions in `research.md` | Answer them in a comment, or `/go` without answers (the planner defers them) | An agent writes the plan |
| **3** plan | `thoughts/N/plan.md` | Approve it (`Status: approved` in your own commit), or `/rework` | The build: one agent run per phase, CI after each, then validation and staging |
| **4** review | the pull request, all checks green, the staged build to try on the phone | Merge it yourself, or `/rework` (staged again) | After the merge, `/go`: push to the GitHub mirror, remove the staged build |

Checkpoints whose label ends in `-retry` (a step failed, retry budget 2), `-blocked` (the model, the credit or the login,
no retry spent), `-fix` (something only you can fix) or `-failed` (retries used up) are the same gates after a problem.

Defaults: `/go` alone runs on the **local model** and records the gate's typical time (`minutes=` is optional; see
`scripts/commands.py`). `/go model=smart|fast|cheap` uses Claude.

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

- One Gitea login is shared by you and the foreman, so Gitea cannot show who added the label. Anyone who can make the
  foreman's login add a label (a person with the token, or an agent that misuses it) can start night work. The damage
  is bounded: local model, no merge, no deploy, CP4 always yours. A separate bot login (see the operating guide) closes
  this.
- The same model family writes the holdout tests (spec step) and the code (implement step) of a night item. They run as
  separate agents with separate prompts, and the guard stops the implementer from editing the tests, but the tests are
  not independent of the model in the way your own CP1 is. Read the tests at CP4.
