# Operating the factory

One page for everything you do or see while the factory runs. The path of an app is in
[lifecycle.md](lifecycle.md); the checkpoints and the night shift in [night-shift.md](night-shift.md); host setup in
[../ops/devbox/README.md](../ops/devbox/README.md).

## Starting work

Write the issue (a bug, a feature, a question), then comment **`/go`** under it. That is all. The **autopilot** takes it
from there: spec and acceptance tests, threat model, research, plan, build, checks, staging, one step after the other,
until **Checkpoint 4: a staged build you can try**, with links to every document it wrote and a list of what it decided
for you (provisional threat-model rows, deferred questions). Each step leaves a one-line `▶ Autopilot:` comment, so the
issue shows where it is. It stops earlier only when the plan touches risky files (`deploy/`, `.gitea/`, a Dockerfile,
the guard scripts, the acceptance tests), when a step fails, or where you asked: `/go stop=spec` (pause after the spec,
the old Checkpoint 1) or `/go stop=plan` (pause at the plan). `/go model=fast` does the same on Claude. Any time of day; no labels needed. (A `/go` older than 24 hours on an issue that never started is
treated as history, not as a request.)

For tonight instead of now: put the label `night/ok` on the issue (see below).

## Commands

On a work issue (only your login counts; the factory's own login is never obeyed). Every command gets an answer: if
nothing can happen, the factory says why.

| Command | Where | Effect |
|---|---|---|
| `/go` | an issue not started yet | start it; the autopilot runs to Checkpoint 4 (a staged build) |
| `/go stop=spec\|plan` | an issue not started yet, or any checkpoint | the same, but pause at the spec (CP1) or at the plan (CP3) |
| `/go` | CP1, CP3 (and CP2, 2b on items that were already waiting there) | **approve** what is shown and continue. The factory records your approval in the file (`Status: approved`, or the remaining `PROPOSED` rows become `ACCEPTED`) in a commit that names your comment. To change something first, edit the file, then `/go` |
| `/go` | any other checkpoint | continue (on the autopilot, up to Checkpoint 4); runs on the **local model**, records the gate's typical minutes |
| `/go model=smart\|fast\|cheap` | any checkpoint | the same step on Claude (Opus, Sonnet, Haiku): spends cloud tokens |
| `/go minutes=N` | any checkpoint | record your real time on the gate instead of the default |
| `/rework <what is wrong>` | CP3 plan, CP4 review, CP5 release | send it back with a note |
| `/stop` | anywhere | stop the item; one sent while a step runs is carried out at the next checkpoint |
| label `night/ok` | the issue | the night shift may pass CP1 to CP3 for you (22:00 to 07:00) and stops at CP4 |

On the pinned **Factory status** issue:

| Command | Effect |
|---|---|
| `/llm status` | what the GPU workstation serves now |
| `/llm code` / `/llm 38` | switch the local model (it waits until no local step runs; add `now` to switch at once) |
| `/llm agent` / `/llm off` | give the GPU back to the assistant / stop everything |

A model is never loaded on top of another: the only way the factory loads one is your own `llm` script, which stops
every model server first.

## Who does the work: lanes

The foreman never runs a step itself. Each step runs in its own process, in a lane:

| Lane | Capacity | What goes there |
|---|---|---|
| local | 1 (one GPU) | every `/go` without `model=`, the night shift, the spec drafts of picked-up issues |
| cloud | 3 | `model=smart|fast|cheap` only |

A command whose lane is full waits, and the status job tells you at once which item holds the lane. `/go model=fast`
on another item runs in parallel with a local step.

## The local model

- "local" is whichever model is serving (`llm code`, then `llm 38`, then the assistant's agent server). The factory
  waits up to 10 minutes for one, wakes the GPU workstation when it is off, and never asks for a model while the GPU is
  held for you.
- Steps reuse llama.cpp's prompt cache from turn to turn; a turn costs about a second.
- Each local run is priced as if the `fast` profile had done it (its tokens at Sonnet rates) and gets an energy
  estimate (`FACTORY_LOCAL_WATTS`, default 400 W; the cost appears once `FACTORY_KWH_PRICE` is set). The board shows what
  local work saved; cloud spend counts real cloud runs only.

## How a step runs

- **Your latest command wins.** Several commands while an item waits: the last one runs (a `/stop` beats them all),
  and the earlier ones are marked as replaced. `/go model=fast` after a queued `/go` really moves it to the cloud.
- **From the spec to the plan in one stop.** After your `/go` at CP1 the agents draft the threat model, research and
  the plan back to back; CP3 shows all three. Threat-model rows are accepted provisionally (edit a row before `/go` to
  decide it differently); research questions are listed and deferred unless you answer and `/rework`.
- **The agent checks its own work before anything is pushed.** After an implement or validate run the factory runs the
  app's checks locally (`.factory/checks` in the app repo, one command per line; otherwise what its CI runs: ruff,
  pyright, pytest, the holdout tests, `scripts/check-tests-fail-before-patch.sh`, `scripts/quality.sh`). A check that
  fails goes back to **the same agent session** at once, up to 2 rounds. A check that was
  already red before the run is reported, not handed to the agent. CI then confirms instead of debugging: a phase
  whose checks passed locally starts the next phase at once, and the foreman waits for CI only after the last phase.
- **Retrospectives.** Every 25 turns (40 on Claude) a run pauses and the agent writes five lines: goal, done, stuck,
  next, files. The factory checks them against the worktree (commits, changed files, files no agent may touch) and
  answers: continue, with the facts; undo a change to a protected file now (instead of a refusal after the run); or,
  after two pauses in a row with no change at all, stop as `stalled`. A local run also stops at 80 turns. The notes are
  on the board and folded into the step's comment.
- **A failed local step goes to Claude once, by itself.** When a local step fails (stalled, refused by a guard, tests
  that do not load), the foreman runs the same step on `fast` and says so in a comment, with what the local run reported.
  At most 3 switches per item and only while the item's cloud spend is under $5. Never at night, and never for a
  problem the cloud does not fix (credit, login, model down). Then the next step is local again.
- **One answer is at most 16k tokens on the local model.** A model caught in a loop used to write for half an hour
  until its context was full (brevkoll#7); the model shim now cuts one answer at 16k tokens (about 4 minutes).
- **The spec step cleans up after itself.** Files outside `thoughts/N/` and `tests/holdout/` are put back, and the
  acceptance tests must load before Checkpoint 1 is posted (otherwise the draft is written again).
- **A broken acceptance test stops the item at once.** Before a CI-rework or validate run, the factory runs the
  acceptance tests on the branch: a failure raised inside a library that never reaches the app's code (brevkoll#7:
  `Headers([...])` with a list) means no code can pass, so no agent is started and a `-fix` checkpoint names the tests
  and the library line. At the spec step, acceptance tests that already pass on the current code are drafted again.
- **Staged builds are checked.** The review link names the machine that runs the build, and the factory checks that
  its container runs exactly that build before posting the link. A `localhost` link appears only where the staging
  machine is also where you browse (`STAGING_LOCAL_LINK=on` in its `staging.env`).
- **Nothing private reaches a mirrored file.** Comments, CI notes and job logs copied into `thoughts/N/issue.md` have
  internal links and host names replaced (`<internal link>`), so a merged item can always be mirrored.

## Where to look

- **Working now** (top of the board): each running agent's step, model, minutes, turns, what it is doing this second,
  its task list, the files it touched, the last 80 entries of its output (what it thinks, every command with the first lines of what it returned, its messages) and a link to the full log. Refreshed every 4 seconds; panels
  you open or close stay that way. **dock ⇥** keeps it beside the board on a wide screen and as a bottom sheet on a
  phone (remembered per device; a bookmark ending in `#dock` always docks).

- **The board** (the devbox, port 8801): a kanban with the columns Your turn, Review, Blocked, Agent working, Inbox, Done
  and Archived; a filter bar; the commands waiting and why; the failures by kind; 7-day numbers; what the checkpoints
  mean. Done cards can be archived on your device. The tile **agent time wasted** counts the minutes of runs that failed
  or that a guard refused after they finished, against all agent minutes of the week: the number that should fall.
- **The Factory status issue**: the same facts as text, rewritten every minute; also where `/llm` goes.
- **Pushes** (ntfy): checkpoints, failures, a takeover by the standby, the morning summary of the night. Night
  checkpoints are silent.

## Identity and protection

- The factory works as its own Gitea user, `factory-bot` (`scripts/identity.py`): it posts, labels and pushes as that
  user, on the hosts that run the foreman only (your own git stays yours).
- Only you merge to `main`; the bot pushes directly only metrics (factory) and the run log (factory-ops).
- `identity.py apply --rotate` replaces the bot's token everywhere; `identity.py protect` re-checks the protection.

## Two hosts

The devbox is the primary foreman; the GPU workstation runs the same timers as a standby. A lease (a Gitea variable in
the bot's own repo) decides which acts: the standby takes over after about 5 minutes without renewals, pushes that it
did, and hands back when the devbox returns. The `/llm` switcher runs on the workstation only.

## When something goes wrong

| You see | Meaning | Do |
|---|---|---|
| `-retry` checkpoint | a step failed, budget 2 | `/go` (or wait for the night shift on `night/ok` items) |
| `-blocked` checkpoint | model, credit or login; no retry spent | `/go` later, or `/go model=fast` |
| `-fix` checkpoint | something only you can fix | do what it says, then `/go` |
| `-fix`, "hiding tests from a gate" | an agent tried to get a gate green by moving or skipping tests; nothing was pushed | usually the plan asks for something the gate cannot accept: change the plan (or the gate, yourself), then `/go`; or `/stop` |
| `-failed` checkpoint | retries used up | `/go` tries once more (`model=fast` for Claude), or `/stop` |
| "Not acted on" | a `/go` arrived while a step of that item ran | answer the new checkpoint |
| card in Blocked, "stopped without a checkpoint" | a step's process ended without a checkpoint | the foreman recovers it on the next tick, or a session |
