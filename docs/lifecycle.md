# From a spec to production: the factory's lifecycle

This is the whole path of an app: you paste a spec, the factory builds it, you try it in a container on the devbox,
you accept the release there, and only then does it go to Kubernetes. Every arrow is the foreman's work; every 🛑 is
yours. Where the factory is not there yet, the **Status** column says so.

```
spec pasted ─▶ spec ─▶ threat model ─▶ plan ─▶ build (phases + CI) ─▶ CP4 try the PR, merge   (one /go: the autopilot)
   ─▶ release candidate (image, scans, registry) ─▶ CP5 try the candidate on the devbox, accept
   ─▶ GitOps PR (you merge = deploy) ─▶ rollout + smoke test ─▶ CP6 live ─▶ lookback after 7 and 14 days
```

## The stages

| # | Stage | What happens | Your part | Status |
|---|---|---|---|---|
| 0 | **Start** | Open an issue in the factory repo with the label `new-app`, the app name as the title and your spec as the body. The foreman creates the repo from `factory-template` (or fills an empty repo you created), commits your spec as `Status: draft`, and an agent drafts the holdout tests from it. | paste the spec | planned (today: `scripts/new_app.py <app> "<description>" --seed <folder>` from a session) |
| 0b | **Start a change** | On an app that exists: write an issue, comment `/go`. An agent drafts the spec and the acceptance tests from it. | write the issue, `/go` | works |
| 1 | **Spec** | Spec and holdout tests on the branch, WIP pull request. Broken or already-passing tests are drafted again. | nothing (your starting `/go` approves it); `/go stop=spec` makes this CP1 | works |
| 2 | **Threat model** | An agent drafts it; rows are accepted provisionally and listed at CP4. | nothing | works |
| 2b | **Questions** | Research may raise questions; the planner defers them and CP4 lists them. | nothing | works |
| 3 | **Plan** | An agent writes the plan in phases. A plan that touches risky files stops here as CP3. | nothing; `/go stop=plan` makes this CP3 (`/go` to build, or `/rework <note>`) | works |
| 4 | **Build and test** | One agent run per phase with local checks and self-fix, retrospectives every 25 turns, a failed local step tried once on Claude, CI; then a validation report. | nothing; `/steer <note>` or `/stop` while a step runs | works |
| 5 | **CP4 review on staging** | The pull request's head runs **as a container on the devbox**; the checkpoint links it (phone and PC). | try it, read the diff, merge, or `/rework` | works |
| 6 | **Release candidate** | After your merge: the foreman builds the image from `main` on the devbox, scans it (vulnerabilities, SBOM), tags it `v<date>.<n>` and pushes it to the Gitea container registry. It then runs **that exact image** as a container on the devbox, the same image that would go to production. | nothing | built (factory#84) |
| 7 | **CP5 accept the release** | The checkpoint links the candidate on the devbox, the scan summary and what changed since the last release. | try it; `/go` accepts it, `/rework <what>` sends it back | built (factory#84) |
| 8 | **Deploy** | Only after CP5: the foreman opens a pull request in the GitOps repo. The first time, it is the app's overlay (namespace, host name, NetworkPolicy, ExternalSecret, the Argo CD application); later releases only change the image tag. The app's secrets are written to Key Vault by the factory. | merge the GitOps PR: **that merge is the deploy** | planned |
| 9 | **CP6 live** | Argo CD syncs. The foreman watches the rollout (pods ready, no restarts for 10 minutes) and runs a smoke test against `https://<app>.<apps domain>/`. If it fails, a revert PR is ready. | nothing, or merge the revert | planned |
| 10 | **Assess** | After 7 and 14 days a read-only review of the shipped code opens issues for what it finds (`scripts/lookback.py`); the board shows lead time from spec to production. | read the findings | review exists, timers not installed |

Every stage runs on the local model unless you write `model=smart|fast|cheap`. Label an issue `night/ok` and the
night shift does the same on the local model at night (see [night-shift.md](night-shift.md)); CP4, CP5 and the deploy are always yours.

## Two environments, one image

- **Staging is a container on the devbox.** At CP4 it runs the pull request's code; at CP5 it runs the release
  candidate image itself. Nothing reaches Kubernetes before you accept that image at CP5.
- **Production is Kubernetes**, deployed by Argo CD from the GitOps repo. The image there is byte for byte the one you
  accepted (same tag, same digest).

## Decisions recorded (2026-11-22)

| Decision | Choice | Where it is configured |
|---|---|---|
| Image registry | the Gitea container registry; a pull secret in the cluster | `~/.config/factory/registry-token` (package write) on each foreman host |
| App secrets | written to Key Vault by the factory, read by an ExternalSecret | the factory's `az` login on the foreman host |
| Host names | `<app>.<apps domain>`; a dedicated domain may replace it later | `FACTORY_APPS_DOMAIN` in `host.env` (one setting to change) |

## What you need to start an app today

1. A spec: `thoughts/1/spec.md` with `Status: draft`, acceptance criteria and a contract (`.factory/templates/spec.md`
   in `factory-template`).
2. Holdout tests, or let the spec step draft them.
3. `scripts/new_app.py <app> "<one-line description>" --seed <folder with thoughts/1/spec.md and tests/holdout/>`.
4. Answer the checkpoints on the issue from your phone, and follow the item on the board.

Until stages 8 and 9 land, the deploy is manual: after CP5 a session opens the GitOps PR and you merge it.

Day-to-day commands, lanes, the local model, the board and the two hosts: [operating.md](operating.md).
