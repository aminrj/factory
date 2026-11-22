# Running the foreman on the devbox VM

Why: the GPU workstation sleeps, reboots and goes off at night, and while it is off nothing answers a command. The devbox is
always on. The GPU, the local model and (for now) the workstation itself stay where they are; the devbox reaches the model
over the mutual-TLS route.

What runs where after the move:

| | devbox | GPU workstation |
|---|---|---|
| foreman tick, quality timers | yes | off (rollback: `cutover.sh --back`) |
| agent steps (`claude -p`) | yes, cloud profiles direct, `model=local` through `model_shim.py` over mTLS | no |
| model gateway for staged apps (`factory-model-gateway.service`, loopback :8090) | yes | no (llama.cpp itself is on :8090) |
| staged builds | yes, on the devbox's own LAB address (`STAGING_BIND_IP`), opened over the tailnet through the subnet router | builds staged before the move stay until `stage.py down <app>` |
| the model | reached over mTLS | yes |

## Owner steps (once)

No Tailscale on the VM: OPNsense is the tailnet's subnet router for LAB, and the firewall passes tailnet admins to every
LAB port, so a phone opens a staged build at the devbox's name and port (homelab-foundation `docs/runbooks/devbox.md`).

1. **A Gitea token for the devbox** (its own, so it can be revoked alone): Gitea, Settings, Applications, scopes
   repository read/write and issue read/write. Save it on the devbox as `~/.config/git/gitea-token` (mode 600).
2. **ntfy and mirror settings**: copy `~/.config/factory/ntfy.env` and `~/.config/factory/mirror-deny.txt` from the
   GPU workstation to the same paths on the devbox (mode 600).
3. **staging.env on the devbox** (`~/.config/factory/staging.env`, mode 600):

       LLM_PROVIDER=openai_compat
       LLM_BASE_URL=http://127.0.0.1:8090/v1
       LLM_MODEL=qwen3.8-27b-agent
       LLM_API_KEY=gateway
       STAGING_HOST=<the devbox's lab name>
       STAGING_BIND_IP=<the devbox's LAB address>

   `LLM_API_KEY` can be any value: the gateway replaces it with the real key.
4. **GitHub mirror**: add the mirrored repos (factory, brevkoll, factory-template) to the devbox's fine-grained GitHub
   token (homelab-foundation runbook step D3/D4; `gh` is git's credential helper there).
5. Already in place on 2026-11-21: Claude Code logged in, `uv`, Docker, `~/.config/factory/llm/` (Lookout client identity,
   factory#37 option A), `~/.config/factory/local-model.env`, firewall rule `lab-devbox-to-llm`.

## The move

On the GPU workstation, after this change is merged and the runtime clone has it:

    bash ~/git/software-factory/factory/ops/devbox/cutover.sh <devbox ssh target>

It runs the bootstrap on the devbox (which stops before changing anything if a step above is missing), stops the timers
on the GPU workstation and waits for a running tick, copies `state/`, enables the timers on the devbox and waits up to three
minutes for a tick to finish there. If none does, it rolls back by itself. Rollback by hand: `cutover.sh <target> --back`.

## The local model: which one, and waking its host

`/go` runs on the local model unless you type `model=smart|fast|cheap`. "Local" is whichever model already serves; the
factory never loads or switches one. In `~/.config/factory/local-model.env` on the devbox:

```
LOCAL_MODEL_SERVERS=code=https://<model host>:8743/code,38=https://<model host>:8743/38,agent=https://<model host>:8743
LOCAL_MODEL_ARBITER_URL=https://<model host>:8743/arbiter
LOCAL_MODEL_WAKE_CMD=~/git/software-factory/factory/ops/devbox/wake-model-host.sh
```

Order matters only if two answer (one GPU fits one model, so normally one does): `llm code` first (fast, the night
default), then `llm 38`, then the agent server the arbiter starts when the GPU is free. When nothing answers a step
waits up to 10 minutes (`FACTORY_MODEL_WAIT_S`): it asks the arbiter for its model, or, when the host does not answer at
all, runs the wake command once. While the GPU is yours (`llm desk`, a hold) it does not ask and the step stops as
`blocked:model_unavailable` without spending a retry.

### Wake

Needs the router rule that lets the devbox reach the router's API (homelab-foundation `lab-devbox-to-wake`) and the
wake-only key in `~/.config/factory/wake.env` (mode 600): `WAKE_API_URL`, `WAKE_KEY`, `WAKE_SECRET`, `WAKE_IF`, `WAKE_MAC`.
Test: `ops/devbox/wake-model-host.sh` prints `magic packet sent`.

## Standby foreman on the GPU workstation

The devbox is the primary. The GPU workstation runs the same tick and status timers as a **standby**: it acts only
when the devbox has stopped renewing the foreman lease for about 5 minutes (`scripts/lease.py`, a Gitea Actions variable
`FOREMAN_LEASE` in factory-ops), pushes "the standby took over", and hands back between two ticks once the devbox is seen
again. Exactly one host acts at a time. The workstation is on whenever the local model is, so when it is off the
factory cannot run local steps anyway.

On the workstation, once:

```
printf 'FACTORY_ROLE=standby\nFACTORY_HOST=workstation\n' > ~/.config/factory/host.env
bash ~/git/software-factory/factory/ops/install-timers.sh     # links and enables factory-tick and factory-status
```

and `~/.config/factory/local-model.env` with the loopback servers (same machine as the model):

```
LOCAL_MODEL_SERVERS=code=http://127.0.0.1:8081,38=http://127.0.0.1:8082,agent=http://127.0.0.1:8090
LOCAL_MODEL_ARBITER_URL=http://127.0.0.1:8091
```

On the devbox: `printf 'FACTORY_ROLE=primary\nFACTORY_HOST=devbox\n' > ~/.config/factory/host.env`.

Limits: per-item state files (`state/<app>-<n>.json`: a rework note not yet used, staging details) stay on the host
that wrote them. A takeover in the middle of a step resumes from the last checkpoint on the issue, like after a crash.
Staged builds run on the host that staged them. `FACTORY_LEASE=off` turns the lease off (one host, as before).

## Work lanes

The tick never runs a step itself any more (`scripts/lanes.py`). When an item has a command to run, the tick starts
`orchestrate.py run <app> <n>` as a transient systemd user unit (`factory-<app>-<n>-<time>`) in the item's lane and
goes back to watching:

- **local** (`FACTORY_LANE_LOCAL`, default 1): every `/go` without `model=`, every night-shift step and pick-up.
- **cloud** (`FACTORY_LANE_CLOUD`, default 3): `model=smart|fast|cheap` only.

A command whose lane is full waits; the status job tells you which item holds the lane. See what runs:
`systemctl --user list-units 'factory-*'`, the lane records in `state/lanes/`, and the board. `FACTORY_LANES=off`
in `host.env` runs every step inside the tick, as before.
