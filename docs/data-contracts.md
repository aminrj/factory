# Data contracts for the quality data sources

Consumers (the floor dashboard exporter) should treat every file as append-only JSON Lines: one object per line, UTF-8,
timestamps in UTC ISO 8601 (`2026-11-21T02:30:04Z`). Unknown keys must be ignored; new keys may be added. Missing numbers are
`null`, never `0`. All files live in `data/` of the `factory` repo and are written only by the scripts named below
(commit and push happen with the normal metrics refresh). No file contains personal data or letter text.

## `data/health.jsonl`

Writer: `scripts/nightly_health.py` (systemd `nightly-health.timer`, daily 02:30). One line per `factory-app` repo per run.

| Field | Type | Unit / values |
|---|---|---|
| `ts` | string | UTC time of the measurement |
| `repo` | string | app repo name, e.g. `brevkoll` |
| `status` | string | `ok` or `error` (then `error` holds a message and `metrics` is `{}`) |
| `source` | string | `app` (the app's own `scripts/health.py`) or `template` (fallback while not adopted) |
| `metrics.max_complexity` | number | cyclomatic complexity of the worst function |
| `metrics.mean_complexity` | number | mean cyclomatic complexity over all functions |
| `metrics.longest_function_lines` | number | lines |
| `metrics.longest_file_lines` | number | non-blank lines in the longest file under `src/` |
| `metrics.type_ignore_count`, `cast_count`, `bare_except_count`, `except_exception_count`, `noqa_count` | number | occurrences in `src/` (weakened checks; lower is better) |
| `metrics.runtime_dependency_count` | number | entries in `[project].dependencies` |
| `metrics.test_to_source_ratio` | number | non-blank test lines / source lines (unit and integration tests) |
| `metrics.line_coverage_percent` | number or null | percent, from coverage.py |
| `worst_functions` | array | up to 5 of `{file, function, line, complexity}` |
| `improved` | array of string | metrics better than the baseline, `name: old -> new` |
| `tighten` | string | `none`, `not_adopted`, `dry_run`, `pr_opened:<n>`, `pr_already_open` |
| `duration_s` | number | seconds |

Example:
```json
{"duration_s": 41.3, "improved": [], "metrics": {"bare_except_count": 0, "cast_count": 0, "except_exception_count": 0, "line_coverage_percent": 100.0, "longest_file_lines": 8, "longest_function_lines": 8, "max_complexity": 1, "mean_complexity": 1.0, "noqa_count": 0, "runtime_dependency_count": 1, "test_to_source_ratio": 0.88, "type_ignore_count": 0}, "repo": "brevkoll", "source": "template", "status": "ok", "tighten": "not_adopted", "ts": "2026-11-21T02:30:41Z", "worst_functions": []}
```

## `data/lookbacks.jsonl`

Writer: `scripts/lookback.py` (`lookback.timer`, daily 03:00). One line per review run. A window is "done" once a line with
`status: ok` exists for `(repo, issue, window_days)`.

| Field | Type | Unit / values |
|---|---|---|
| `ts` | string | UTC time the run finished |
| `repo`, `issue` | string, number | app repo and its work issue |
| `window_days` | number | `14` or `30` days after `stage/shipped` |
| `ship_date` | string | `YYYY-MM-DD`, from the issue's label event |
| `status` | string | `ok` or `error` (`error` holds a message; not counted as done) |
| `findings` | number | findings accepted by the script's validation |
| `by_severity` | object | counts for `high`, `medium`, `low` |
| `by_category` | object | counts for `drift`, `weakened-check`, `dead-code`, `missing-test`, `contract` |
| `filed_issues` | array of number | issue numbers filed in the app repo with label `lookback` |
| `cost_usd`, `turns`, `duration_s` | number | USD, count, seconds |

Example:
```json
{"by_category": {"contract": 0, "dead-code": 1, "drift": 1, "missing-test": 1, "weakened-check": 0}, "by_severity": {"high": 1, "low": 1, "medium": 1}, "cost_usd": 0.74, "duration_s": 212.5, "filed_issues": [7, 8, 9], "findings": 3, "issue": 1, "repo": "brevkoll", "ship_date": "2026-12-01", "status": "ok", "ts": "2026-12-15T03:04:11Z", "turns": 31, "window_days": 14}
```
(illustrative values; no lookback has run yet)

## `data/rework.jsonl`

Writer: `scripts/rework_digest.py` (`rework-digest.timer`, Sundays 17:30). One line per merged PR labelled `rework`, written once.

| Field | Type | Unit / values |
|---|---|---|
| `ts` | string | when the digest saw it |
| `week` | string | ISO week of the merge, `2026-W47` |
| `repo`, `pr` | string, number | app repo and PR number |
| `issue` | string or null | first `#n` in the PR body (the work issue) |
| `title` | string | PR title |
| `merged_at` | string | UTC |
| `friction` | string | the PR's "Friction" section, max 2000 characters (a cause category is not extracted automatically) |
| `prevented_by` | null | filled by hand in the retro: where the lesson now lives (AGENTS.md, lint rule, ratchet, contract) |

Example:
```json
{"friction": "The OCR step had no timeout, CI hung.", "issue": "1", "merged_at": "2026-11-26T10:12:00Z", "pr": 4, "prevented_by": null, "repo": "brevkoll", "title": "#1 rework: add OCR timeout", "ts": "2026-11-29T17:30:02Z", "week": "2026-W48"}
```
(illustrative values)

## `data/bench.jsonl`

Writer: `bench/run_bench.py`, run by hand. One line per bench run.

| Field | Type | Unit / values |
|---|---|---|
| `ts` | string | UTC |
| `task` | string | the fixed task, `add a /version endpoint with a test` |
| `template_ref`, `template_sha` | string | template branch and short commit that was benchmarked |
| `status` | string | `ok` or `error` |
| `minutes` | number | agent time for the implement step, including a rework run if any |
| `cost_usd` | number | USD reported by `claude -p` |
| `turns` | number | agent turns |
| `first_pass` | boolean | CI success on the first agent push, no rework |
| `rework_runs` | number | extra implement runs needed (0 or 1) |
| `ci_state`, `ci_failed` | string, array | final CI state of the first push and the failing check names |
| `acceptance_ok` | boolean | an independent check of `/version` passed on the result |
| `vs_previous` | object | `minutes`, `cost_usd`, `turns` differences to the last `ok` run, `first_pass_was` |
| `scratch_repo` | string | `deleted`, or the repo name if it could not be deleted |

Example (first real run):
```json
{"acceptance_ok": true, "ci_failed": [], "ci_state": "success", "cost_usd": 0.14658870000000002, "dispatch_ok": true, "first_pass": true, "minutes": 0.64, "rework_runs": 0, "scratch_repo": "deleted", "status": "ok", "task": "add a /version endpoint with a test", "template_ref": "feat/quality-guards", "template_sha": "676984a", "ts": "2026-11-20T13:30:41Z", "turns": 19}
```

## Mutation JSON (`mutation.json`)

Writer: `scripts/mutation.py` in each app's `quality` CI job (artifact `mutation-report` on the PR run, not stored in this repo).
One object per run. Gitea exposes the artifact per workflow run, so the floor reads it per PR when it needs a panel.

| Field | Type | Unit / values |
|---|---|---|
| `mode` | string | `warn` or `enforce` (from `quality/policy.toml`) |
| `target_percent`, `time_limit_minutes` | number | policy values |
| `files[].file` | string | changed source file |
| `files[].status` | string | `complete`, `timed_out`, `skipped_time_limit` |
| `files[].score_percent` | number or null | killed / (killed + survived), percent; null if no mutants |
| `files[].killed`, `survived` | number | mutant counts |
| `files[].survivors[]` | array | `{line, code, status}` per surviving mutant (`bad_survived`, `bad_timeout`, `ok_suspicious`) |

Example:
```json
{"files": [{"file": "src/app/services/calc.py", "killed": 0, "score_percent": 0.0, "status": "complete", "survived": 3, "survivors": [{"code": "return a + b", "line": 4, "status": "bad_survived"}]}], "mode": "warn", "target_percent": 60, "time_limit_minutes": 10}
```

## `data/runs.jsonl`

Writer: written by hand (or by the session that ran the check), one line per manual check run on an app after it is built.
Not produced by a timer. Checks that were not run are not written; they are listed in the retro.

| Field | Type | Unit / values |
|---|---|---|
| `ts` | string | UTC time the check finished |
| `repo`, `ref` | string | app repo and the commit that was run |
| `env` | string | `devbox-container`, `phone`, `cluster` |
| `model`, `model_source` | string or null | model name and where it came from; null when no model is involved |
| `check` | string | short snake_case name of the check |
| `result` | string | `pass` or `fail` |
| `seconds` | number or null | wall time of the check |
| `note` | string | one or two sentences: what was seen, what was overridden. No letter text, no secrets |

Example:
```json
{"check": "decode_fixture_letter", "env": "devbox-container", "model": "qwen3.8-27b-agent", "note": "timeout 180 s set inside the container only", "ref": "249c09b", "repo": "brevkoll", "result": "pass", "seconds": 7.5, "ts": "2026-11-20T18:50:00Z"}
```
