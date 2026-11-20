# Factory regression bench

One fixed, small task run through the real `scripts/dispatch.py` against a **scratch copy of the template**
(a throwaway private Gitea repo, deleted afterwards). Never run in the factory's own repos or in an app.

Task (`bench/fixtures/`): "add a `/version` endpoint with a test". The spec, threat model, research and an approved plan are
fixed, so the run measures the implement step: how long, how much it costs, how many turns, whether CI passes first time,
and whether a rework run was needed. Each run appends a line to `data/bench.jsonl` with the delta against the previous run.

Run by hand when the template, the commands or the model change:

    python3 bench/run_bench.py                         # template main
    python3 bench/run_bench.py --template-ref feat/x   # a template branch
    python3 bench/run_bench.py --keep                  # keep the scratch repo for inspection

Cost cap per run: `FACTORY_MAX_BUDGET_USD` (default 5 here). The bench copies `dispatch.py` and `gitea.py` into a temp dir,
so it never writes to `data/agent_runs.jsonl` of this repo.
