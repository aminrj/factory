# factory

I am building small AI apps for real people through a gated software factory, in public, and measuring where the human time goes.

- **Live board:** [BOARD.md](BOARD.md): every idea, its named user, its timebox, and whether it shipped or got killed.
- **Template:** [aminrj/factory-template](https://github.com/aminrj/factory-template), the repo every app starts from.
- **Metrics:** [data/SUMMARY.md](data/SUMMARY.md), collected from the app repos by [scripts/collect_metrics.py](scripts/collect_metrics.py) at every checkpoint.
- **Log:** [log/](log/), one entry per week and one ship note per app. Retros in [retros/](retros/), release records in [docs/releases/](docs/releases/).
- **Playbook:** [PLAYBOOK.md](PLAYBOOK.md), how the factory runs.

## How it works

Agents write the code. I own the decisions, at the points where they are still cheap to change:

| Gate | What I decide | Artifact |
|---|---|---|
| 1. Product | The problem, the named user, acceptance criteria | `thoughts/<n>/spec.md` + holdout tests the agent cannot see |
| 2. Threat model | Every AI and web risk, and the control that handles it | `thoughts/<n>/threat-model.md` |
| 3. Plan | Types, signatures, vertical slices | `thoughts/<n>/plan.md` |
| 4. Slices | Agent implements one phase per session, checks between phases | commits |
| 5. Try it and review | I use the staged build on my phone and read the diff; CI runs security scans and the holdout tests | staged build, pull request |
| 6. Promote | Merge the GitOps PR | homelab |
| 7. Evaluate | Keep, iterate or kill after 14 days | board |

I answer each gate with a comment on the Gitea work issue (`/go`, `/rework <note>`, `/stop`), and a small process, the foreman, runs the next agent step, on the local model unless I ask for Claude (`/go model=smart`). Every gate records my minutes, typed (`minutes=N`) or the gate's default, marked as such. That is the data this project exists to collect.

At night, issues I label `night/ok` move through gates 1 to 3 on their own, on the local model, and stop at the review for me: [docs/night-shift.md](docs/night-shift.md) (also: what each checkpoint is).

The whole path of an app, from a pasted spec to production on Kubernetes, and what is still missing: [docs/lifecycle.md](docs/lifecycle.md).

## Rules

1. No real user, no build.
2. Hard timeboxes: S = 8h, M = 16h. When it runs out, I ship or kill.
3. One app in Building at a time.
4. Shipped means someone other than me used it.
5. Killed apps stay on the board.

## Apps

| App | User | Status | Repo |
|---|---|---|---|
| brevkoll: Swedish letter decoder for parents | My household, then school parents | v0.1.0 released, not yet used by its named user; getting ready for the cluster | [aminrj/brevkoll](https://github.com/aminrj/brevkoll) |

## Where this runs

The primary home is a private Gitea instance on my homelab: repos, issues, pull requests and CI. This GitHub repository is a read-only public mirror, with Actions, Issues, Projects and Wiki turned off. The foreman ([scripts/orchestrate.py](scripts/orchestrate.py)) runs every minute on my machine, reads my commands on the work issues and dispatches agents with [scripts/dispatch.py](scripts/dispatch.py), one step at a time, inside a git worktree. Before a review it stages the pull request in a sandboxed container I open from my phone. The board is the `stage/*` labels, rendered to [BOARD.md](BOARD.md).
