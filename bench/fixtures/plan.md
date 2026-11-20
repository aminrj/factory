# Plan: #1
Status: approved

## Phase 1: GET /version
- [ ] Files: `src/app/routes/version.py` (new), `src/app/main.py` (include the router), `tests/unit/test_version.py` (new).
- [ ] Types and signatures: `def version() -> dict[str, str]` returning `{"version": <value of [project].version>}`, read once with `tomllib` from the repo-root `pyproject.toml` (`Path(__file__).resolve().parents[3]`). The project is not installed as a package, so do not use `importlib.metadata`.
- [ ] Tests to add (and why each fails today): `test_version_returns_package_version` (AC1, T1) fails because the route does not exist (404);
      `test_version_rejects_post` (AC2) fails because 404 is returned, not 405.
- [ ] Automated verification: `uv run ruff check . && uv run pyright && uv run pytest`
- [ ] Threat-model controls implemented: T1 (exact body).

## Architecture contracts
None beyond the template's.

## Dependencies added (Python and system)
None.

## What I am unsure about
Nothing.
