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
| 1 | **Spec** | Spec and holdout tests on the branch, WIP pull request. Broken or already-passing tests are drafted again. The spec names the goal it serves and the invariants it touches (`Serves:`, `Touches:` in its Intent section). If the issue itself says a choice is open, the spec says `Decision needed ... : yes` and the foreman stops once, before any code. | nothing (your starting `/go` approves it); `/go <your choice>` answers a held decision; `/go stop=spec` makes this CP1 | works |
| 2 | **Threat model** | An agent drafts it; rows are accepted provisionally and listed at CP4. | nothing | works |
| 2b | **Questions** | Research may raise questions; the planner defers them and CP4 lists them. | nothing | works |
| 3 | **Plan** | An agent writes the plan in phases. A plan that touches risky files stops here as CP3. | nothing; `/go stop=plan` makes this CP3 (`/go` to build, or `/rework <note>`) | works |
| 4 | **Build and test** | One agent run per phase with local checks and self-fix, retrospectives every 25 turns, a failed local step tried once on Claude, CI; then a validation report. | nothing; `/steer <note>` or `/stop` while a step runs | works |
| 5 | **CP4 review on staging** | The pull request's head runs **as a container on the devbox**; the checkpoint links it (phone and PC) and leads with the **drift report**: what was asked, what was built, and every place the diff leaves the spec or the plan (see below). | try it, read the diff, merge, or `/rework` | works |
| 6 | **Release candidate** | After your merge: the foreman builds the image from `main` on the devbox **from scratch (no layer cache, fresh base)**, scans it (vulnerabilities, SBOM), tags it `v<date>.<n>` and pushes it to the Gitea container registry. It then runs **that exact image** as a container on the devbox, the same image that would go to production. | nothing | built (factory#84) |
| 7 | **CP5 accept the release** | The checkpoint links the candidate on the devbox, the scan summary and what changed since the last release. | try it; `/go` accepts it, `/rework <what>` sends it back | built (factory#84) |
| 8 | **Deploy** | Only after CP5: the foreman opens a pull request in the GitOps repo. The first time, it is the app's overlay (namespace, host name, NetworkPolicy, ExternalSecret, the Argo CD application); later releases only change the image tag. The app's secrets are written to Key Vault by the factory. | merge the GitOps PR: **that merge is the deploy** | planned |
| 9 | **CP6 live** | Argo CD syncs. The foreman watches the rollout (pods ready, no restarts for 10 minutes) and runs a smoke test against `https://<app>.<apps domain>/`. If it fails, a revert PR is ready. | nothing, or merge the revert | planned |
| 10 | **Assess** | After 7 and 14 days a read-only review of the shipped code opens issues for what it finds (`scripts/lookback.py`); the board shows lead time from spec to production. | read the findings | review exists, timers not installed |

Every stage runs on the local model unless you write `model=smart|fast|cheap`. Label an issue `night/ok` and the
night shift does the same on the local model at night (see [night-shift.md](night-shift.md)); CP4, CP5 and the deploy are always yours.

## Intent, architecture and the drift report

The per-issue files (`thoughts/N/`) are disposable. Two files per app are not, and you write them: `docs/intent.md`
(who it is for, goals `G<n>`, what it must never do `N<n>`) and `docs/architecture.md` (the picture, where the data
goes, invariants `I<n>`, each enforced by a test carrying `# I<n>`; `scripts/trace.py` fails CI otherwise). Agents read
them and never edit `intent.md`. Spec and plan cite them (`Serves:`, `Touches:`), and a change that moves a box or an
invariant updates `architecture.md` in the same pull request.

After the validate step the foreman runs the app's `scripts/drift.py`, which writes `thoughts/N/drift.md` from the
spec, the plan and the diff, with no model involved: files changed but not in the plan, acceptance criteria whose only
test is a same-named one from an older issue, test files pytest never collects, weakened checks, dependencies the plan
did not name, a missing `acceptance.md`. CP4 quotes its headline (`Drift: CLEAN` or `REVIEW (k)`) and its findings
before anything else. The validation report is still written by a model and is only read second.

`doctor.py` warns when an app's agent commands or document templates are behind `factory-template`'s.

## How far the autopilot goes (risk tiers)

A `/go` no longer means the same thing for every issue. The foreman decides in code (`scripts/tiers.py`), from the issue's
labels, the spec and the plan:

| Issue | The foreman | Why |
|---|---|---|
| labelled `bug`, plan of at most two phases, spec touches no `N<n>`/`I<n>`, no new dependency, no ADR, no risky file | runs to Checkpoint 4 | a failing test reproduces the bug; the invariant tests guard the promises |
| anything else that builds (including an unlabelled issue) | stops at the plan (CP3) and lists why | the plan is the cheapest thing that can still redirect the work |
| labelled `decision`/`question`, or the issue says it needs a decision | stops at the spec (CP1) | the choice is yours |

`/go tier=auto` runs to the staged build whatever the risk check says (the reasons are still printed at CP4);
`/go stop=plan|spec` stops earlier. Checkpoint 4 opens with **Quick review** (a clean bug fix) or **Read closely** (and
why), from the tier and the drift report.

What a prompt only asks, code makes sure of: after the spec step the foreman writes the `# AC<n>` markers itself and
refuses a spec whose criteria have none; after validate it writes `acceptance.md` from the spec if the model did not;
and a run that adds a test file pytest would never collect (a name that does not start with `test_`, or a folder outside
the app's `testpaths`) is refused.

## Files you attach to an issue

A file attached to the issue or to one of your comments (a letter that fails, a log, a screenshot) is handed to a run on
the **local model** as `.attachments/<where>-<name>` in the agent's folder, kept out of git, up to five files of 5 MB. A run
on a cloud model is told the file exists and is not given it: the file is your private data, so send `/rework model=local`
when an agent must read it. When an agent says it could not reproduce a problem, its comment says so first ("its change is
a guess") instead of looking like any other green step.

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
