# Spec: #1 Version endpoint

**Real user:** the bench   **Size:** S   **Timebox:** 1h   **Kill date:** n/a

## Problem
Operators cannot tell which version of the app is running.

## What v1 does (one sentence)
`GET /version` returns the installed package version as JSON.

## Acceptance criteria
- [ ] AC1: Given the app, when I GET `/version`, then I get 200 and JSON `{"version": "<string>"}` where the string is the package version from `pyproject.toml`.
- [ ] AC2: Given the app, when I POST `/version`, then I get 405.

## Contract
Route `GET /version` in `src/app/routes/`, no authentication, no parameters.

## Out of scope
Build metadata, git SHA, health details.
