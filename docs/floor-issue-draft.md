# New data sources from the quality guards

(Draft. To be posted as an issue in the `factory-floor` repo as soon as that repo exists on Gitea; at the time of writing the API returns 404.)

The quality-guards build adds five data sources. Field lists, units, one example line each and the writer are in
[`docs/data-contracts.md`](https://gitea.lab.aminrj.com/amine/factory/src/branch/feat/quality-jobs/docs/data-contracts.md) (branch `feat/quality-jobs` until merged):

- `data/health.jsonl` (nightly, per app)
- `data/lookbacks.jsonl` (daily check, a line per 14 and 30 day review)
- `data/rework.jsonl` (weekly, per rework PR)
- `data/bench.jsonl` (by hand, per bench run)
- the mutation JSON (`mutation.json`, an artifact of each PR's `quality` job)

Proposed panels:

1. **Health trend per metric per app**: lines of `max_complexity`, `mean_complexity`, `line_coverage_percent`,
   `test_to_source_ratio`, and a stacked bar of the weakened-check counts, from `health.jsonl`. Variable: `repo`.
2. **Mutation score per PR**: bar per changed file with a line at the policy target (60 %), from the artifact of each PR run.
3. **Lookback findings by age and severity**: table or stacked bar of `by_severity` over `window_days` (14/30) and `repo`;
   plus a stat "open `lookback` issues per app" from Gitea.
4. **Rework causes**: table of `friction` per week with a count per week, from `rework.jsonl`.
5. **Bench trend**: `minutes`, `cost_usd`, `turns` per run with `first_pass` as a marker, from `bench.jsonl`; annotate runs with `template_sha`.
6. **The number to watch first**: escaped bugs and lookback findings per app (stat panel, last 30 days).

Do not edit the quality scripts from the floor side; if a field is missing, ask here and the writer will add it.
