# Renovate setup (needs a human, listed at checkpoint Q3)

Prepared: `renovate.json` (global defaults, no automerge) and `.gitea/workflows/renovate.yml` (Mondays 04:00 UTC,
manual dispatch too, autodiscovers repos with topic `factory-app`). Nothing runs until you do the steps below.

1. Create a Gitea user `renovate-bot` (Site administration, User accounts; a normal non-admin user).
2. Give it **Write** on each `factory-app` repo (as a collaborator) so it can push branches and open PRs.
   For read access to HQ only the workflow checkout uses the job token.
3. Create a token for `renovate-bot` with these scopes only: `write:repository`, `write:issue`, `read:user`, `read:organization`.
   (`write:repository` covers branches and PRs, `write:issue` covers labels and the PR comments Renovate makes.)
4. Store it as the repository secret `RENOVATE_TOKEN` in `amine/factory`, and the repository variable `GITEA_URL`
   (your Gitea URL).
5. Check the Renovate version pin in the workflow (`RENOVATE_VERSION`) against the current release, and that the runner can reach
   npm. The runner label is `ubuntu-latest` like the other workflows; Renovate needs Node, which the runner image must provide.
6. Run the workflow once with `workflow_dispatch` and read the log before relying on the schedule.

Apps keep their own `renovate.json` optional; PRs go through the normal gates (`checks`, `gates`, `security`, `holdout`, `quality`).
