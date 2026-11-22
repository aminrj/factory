# Operating the factory

One page for everything you do or see while the factory runs. The path of an app is in
[lifecycle.md](lifecycle.md); the checkpoints and the night shift in [night-shift.md](night-shift.md); host setup in
[../ops/devbox/README.md](../ops/devbox/README.md).

## Starting work

Write the issue (a bug, a feature, a question), then comment **`/go`** under it. That is all. An agent reads the issue
and your comments, drafts the spec and the acceptance tests, and Checkpoint 1 waits for you. `/go model=fast` does the
same on Claude. Any time of day; no labels needed. (A `/go` older than 24 hours on an issue that never started is
treated as history, not as a request.)

For tonight instead of now: put the label `night/ok` on the issue (see below).

## Commands

On a work issue (only your login counts; the factory's own login is never obeyed). Every command gets an answer: if
nothing can happen, the factory says why.

| Command | Where | Effect |
|---|---|---|
| `/go` | an issue not started yet | start it: spec and acceptance tests are drafted, then Checkpoint 1 |
| `/go` | CP1, CP2, CP3 | **approve** what is shown and continue. The factory records your approval in the file (`Status: approved`, or the remaining `PROPOSED` rows become `ACCEPTED`) in a commit that names your comment. To change something first, edit the file, then `/go` |
| `/go` | any other checkpoint | continue; runs on the **local model**, records the gate's typical minutes |
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

## Where to look

- **The board** (the devbox, port 8801): a kanban with the columns Your turn, Review, Blocked, Agent working, Inbox, Done
  and Archived; a filter bar; the commands waiting and why; the failures by kind; 7-day numbers; what the checkpoints
  mean. Done cards can be archived on your device.
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
| `-failed` checkpoint | retries used up | a session (or `/stop`) |
| "Not acted on" | a `/go` arrived while a step of that item ran | answer the new checkpoint |
| card in Blocked, "stopped without a checkpoint" | a step's process ended without a checkpoint | the foreman recovers it on the next tick, or a session |
