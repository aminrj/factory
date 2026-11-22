# From a spec to production: the factory's lifecycle

This is the whole path of an app: you paste a spec, the factory builds it, you try it in a container on the devbox,
you accept the release there, and only then does it go to Kubernetes. Every arrow is the foreman's work; every 🛑 is
yours. Where the factory is not there yet, the **Status** column says so.

```
spec pasted ─▶ CP1 spec ─▶ CP2 threat model ─▶ CP3 plan ─▶ build (phases + CI) ─▶ CP4 try the PR, merge
   ─▶ release candidate (image, scans, registry) ─▶ CP5 try the candidate on the devbox, accept
   ─▶ GitOps PR (you merge = deploy) ─▶ rollout + smoke test ─▶ CP6 live ─▶ lookback after 7 and 14 days
```

## The stages

| # | Stage | What happens | Your part | Status |
|---|---|---|---|---|
| 0 | **Start** | Open an issue in the factory repo with the label `new-app`, the app name as the title and your spec as the body. The foreman creates the repo from `factory-template` (or fills an empty repo you created), commits your spec as `Status: draft`, and an agent drafts the holdout tests from it. | paste the spec | planned (today: `scripts/new_app.py <app> "<description>" --seed <folder>` from a session) |
| 1 | **CP1 spec** | Spec and holdout tests on the branch, WIP pull request. | approve the spec (`Status: approved` in your own commit), `/go` | works |
| 2 | **CP2 threat model** | An agent drafts the threat model. | decide every row, `/go` | works |
| 2b | **Questions** | Research may raise questions. | answer, `/go` | works |
| 3 | **CP3 plan** | An agent writes the plan in phases. | approve or `/rework` | works |
| 4 | **Build and test** | One agent run per phase, CI after each on the runner, retries; then a validation report. | nothing | works |
| 5 | **CP4 review on staging** | The pull request's head runs **as a container on the devbox**; the checkpoint links it (phone and PC). | try it, read the diff, merge, or `/rework` | works |
| 6 | **Release candidate** | After your merge: the foreman builds the image from `main` on the devbox, scans it (vulnerabilities, SBOM), tags it `v<date>.<n>` and pushes it to the Gitea container registry. It then runs **that exact image** as a container on the devbox, the same image that would go to production. | nothing | planned |
| 7 | **CP5 accept the release** | The checkpoint links the candidate on the devbox, the scan summary and what changed since the last release. | try it; `/go` accepts it, `/rework <what>` sends it back | planned |
| 8 | **Deploy** | Only after CP5: the foreman opens a pull request in the GitOps repo. The first time, it is the app's overlay (namespace, host name, NetworkPolicy, ExternalSecret, the Argo CD application); later releases only change the image tag. The app's secrets are written to Key Vault by the factory. | merge the GitOps PR: **that merge is the deploy** | planned |
| 9 | **CP6 live** | Argo CD syncs. The foreman watches the rollout (pods ready, no restarts for 10 minutes) and runs a smoke test against `https://<app>.<apps domain>/`. If it fails, a revert PR is ready. | nothing, or merge the revert | planned |
| 10 | **Assess** | After 7 and 14 days a read-only review of the shipped code opens issues for what it finds (`scripts/lookback.py`); the board shows lead time from spec to production. | read the findings | review exists, timers not installed |

Every stage runs on the local model unless you write `model=smart|fast|cheap`. Label an issue `night/ok` and the
night shift passes CP1 to CP3 for you (see [night-shift.md](night-shift.md)); CP4, CP5 and the deploy are always yours.

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

Releases and deploys are manual until stages 6 to 9 land: a session builds and pushes the image, and you merge the
GitOps PR it opens.
