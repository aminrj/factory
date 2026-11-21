# Retro 2026-W47 (second half): staging, promotion and the mirror

- Shipped: a staging step at the review checkpoint (`scripts/stage.py`), a mirror guard (`scripts/mirror.py`), `scripts/doctor.py`, the `docs/deploy.md` and `acceptance.md` contract in the template, and PLAYBOOK section 4a. brevkoll main was staged and its fixture letter decoded from a phone.
- Human minutes by gate: not measured for this work (it was factory work, not an app gate).
- Rework PRs / agent PRs: none, this was done in sessions.
- What the security gates caught: the new mirror guard found internal hostnames in three mirrored docs of `factory` before any new push. Three more are in brevkoll's human-owned spec, threat model and research.
- Best friction to fix next (one item only): run a real change through the new loop (`/rework` and the staged checkpoint have unit tests but have never run live).
- Talk material (one observation with a number): the first live stage failed because it built a branch head that was already merged and 22 commits old; the model answered 401 and the app showed 502. One preflight call that checks the key would have caught it before the build. My first preflight did not: it asked for the model list, which the server leaves open, so a wrong key still looked fine. It now probes the chat route with an empty body.

## Rework this week → which rule or check would have prevented it → where it now lives

| What went wrong | Rule or check that prevents it | Where it lives |
|---|---|---|
| Staged a stale, already merged branch head; the old build had no key support and showed 502 on the phone | Stage only the head of an open PR; anything else needs an explicit `--ref`; probe the chat route with an empty body and the key before building (the model list is open and proves nothing) | `scripts/stage.py` (`resolve_target`, `preflight_model`), tests in `tests/test_stage.py` |
| CI runs on pull requests only, so the merge commit that gets released has no checks | Run the four jobs on pushes to `main` and on tags. Interim: a merge commit counts as verified when its tree equals its green PR head | factory#29 (open), `stage.require_green` |
| Internal hostnames sat in files that go to the public mirror, and the orchestrator pushed without any scan | Every push to a mirror goes through one guard that scans the added lines; private names live in an untracked deny list | `scripts/mirror.py`, used by `orchestrate.py` and `bootstrap.py` |
| The kit (source) fell behind the runtime copy three times in one day; a fix existed only in the runtime, and `bootstrap.py` would have overwritten the live scripts with the stale copy | One home for the code: the factory and factory-template repos. The kit holds no copy; the tick units live in `ops/systemd` like the other timers; `doctor.py` warns on a second copy or a unit linked from elsewhere | `scripts/doctor.py` (`check_kit`, `check_unit_sources`), `ops/install-timers.sh` |
| The first preflight asked for the model list, which is open, so a wrong key passed; found only by trying a deliberately wrong key in the doctor demo | Test a check against a known-bad input, not only a good one | `stage.preflight_model`, `tests/test_stage.py` |
| A unit test wrote lines into the real RUNLOG | Tests that exercise code which logs must mock the logger | `tests/test_orchestrate.py`; consider an env override for the log path |
| The model key was copied into a second file | Reference a secret by its source file, never copy it | `LLM_API_KEY_FILE` in `stage.py`; `doctor.py` warns on a copy |
| Nothing told an agent or a person how the app runs; the real setup lived in another repo and in an untracked file | Every app repo has `docs/deploy.md` with the contract the factory relies on; `validate` checks it and writes the phone checklist | factory-template `docs/deploy.md`, `.claude/commands/validate.md` |
| Config and secrets were loose files with no check | `doctor.py` reports ok, WARN or FAIL with a fix hint and never prints a value | `scripts/doctor.py`; the rest of the plan is factory#30 |

## What worked

- Staging from the open PR with a fixed sandbox policy made "try it on my phone" a click, with nothing to type.
- Reading the model route from the homelab repo instead of guessing showed that adding a client certificate means rotating the whole PKI, which changed the plan (one shared identity, loopback as a stopgap).
- Writing the guard first and running it on all three repos found real leaks in minutes.

## What did not

- I staged a branch without checking it was the code about to be merged. The check existed in my head, not in the script.
- A fix reached the runtime copy and not the kit, and I found it by chance.
- The first design put a link in a file; the guard and the rule "links live only in Gitea comments" came from the owner's constraint, not from the process.

## Not done

Promotion automation (factory#27), reaping stale staged builds (#26), the bring-your-own-model contract (#25), the host-network stopgap (#28), mutual TLS in brevkoll (brevkoll#14, needs the owner to rotate the model PKI), CI on `main` (#29).
