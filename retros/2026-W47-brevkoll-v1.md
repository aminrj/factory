# Retro 2026-W47: brevkoll v1 (first app through the factory)

- Shipped: brevkoll v1 merged to Gitea `main` (249c09b) and mirrored to GitHub. Not released: no tag, no registry image. Runs on the devbox as a local container; not on the cluster.
- Hours on factory work vs app work: not measured. Most of this day went to factory work and quality guards, not to the app. The app itself took 13 agent runs.
- Human minutes by gate (from `data/human_minutes.jsonl`): spec 90, threat model 10, plan review 5, code review 1, total 106. Understates the real time: the manual checks, the merge and the devbox run are not in it, and three `/go minutes=1` were posted on the owner's behalf by a session.
- Agent: 13 runs, 18.0 min, $3.88, 305 turns (threat-model 1, research 1, plan 1, implement 9, validate 1).
- Rework PRs / agent PRs: 1 rework run on 1 PR (it could not fix the red job; a human fixed it).
- What the security gates caught: semgrep flagged the upload form for a missing Django CSRF token. A false positive (FastAPI, no cookies or session); suppressed on that one rule with the reason in the template. Nothing else.
- Best friction to fix next (one item only): the process has no owner for the app's own docs and no way for the rework run to see a CI log. Order: docs ownership first (visible on the public mirror), CI log in the rework note second.
- Talk material (one observation with a number): the same app, same letter, same code took 55 s on a thinking model running on CPU and 7.5 s on the model already loaded on the GPU. The 30 s timeout in the code was the real failure, not the model.
- Rework this week → which rule or check would have prevented it → where it now lives:
  - Agent `ruff format` rewrote the holdout file, guard refused the step → Gate 1 must leave the holdout file format-clean → factory#14, to be added to the Gate 1 checklist.
  - CI red on one semgrep finding the agent could not read → the rework note should carry the failing log tail → factory#15.
  - Phase counter stuck at 5/6 after a red CI → count the phase when its commit exists → factory#12, fixed on `fix/orchestrator-phase-counter` with a test.
  - PR body `Closes #n` closed the issue and `/go` was ignored → `Refs #n` → factory#13, same branch.
  - README and AGENTS.md left as template text → plan, implement and validate steps now cover them → factory#16, `factory-template` PR #2 and brevkoll PR #3.
- Quality: health trend and lookback findings per app: one health line for brevkoll (max complexity 1, coverage 100%, test ratio 0.88) from the nightly job. No lookback yet: first one is due 14 days after `stage/shipped`, and the app is not shipped. Escaped bugs: 0 known, but the app has run on exactly one synthetic letter. App defects found on the devbox: brevkoll#4 to #8.

## What worked

- Every gate stopped where it should. The holdout guard refused an agent change to a protected file; the unpushed commit made repair easy.
- Holdout acceptance tests passed in CI with real Tesseract on the first CI run after the format fix.
- Local checks (ruff, pyright, pytest, semgrep) were reproducible, so each CI failure was reproduced and fixed before pushing.
- The data files (`agent_runs`, `human_minutes`) made this retro a ten-minute job.

## What did not

- Four orchestrator and process bugs above, all found only by running a real app through.
- The first devbox run loaded models on a full GPU. Rule now: use the already-loaded model (`llm.sh status`), never load one.
- A localhost relay was needed because the app cannot send an API key (brevkoll#5).

## Not done

Phone `.ics` import (needs LAN access to the container), cluster deploy (owner chose the devbox), the release steps (tag, image, scan, registry), Renovate, GlitchTip, brevkoll adoption of the quality guards (waits for `stage/shipped`).
