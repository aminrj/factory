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
