# Release plan: brevkoll v0.1.0 (devbox)

Status: draft for the owner. Preconditions 1 and 2 are done and merged; steps 1 to 4 and 7 to 8 are not run.
Decision already made: run on the devbox, not on the cluster (the cluster hosts other workloads).

## State today (2026-11-20, evening)

- `brevkoll` `main` is at `1a53dde`: v1 code, the real README, configurable model timeout and token cap, optional `LLM_API_KEY`
  (brevkoll#9), base images pinned by digest (brevkoll#10). CI green. No tag, no registry image.
- A container `brevkoll` (image `brevkoll:0.1.0-rc1`, built from `1a53dde`) runs on `127.0.0.1:8765` with the real settings from
  `~/.config/brevkoll/env` (mode 0600). No overrides, no relay. Results: `data/runs.jsonl` (5 lines, all pass; fixture letter in 6.9 s).
- Gitea's container registry is enabled (`/v2/` answers 401). `trivy` and `syft` are not installed on the devbox.

## What had to be true before the tag

1. DONE: brevkoll#4 and #5 (merged in brevkoll#9).
2. DONE: base images pinned by digest (brevkoll#10).
3. OPEN: the owner's decisions below.

## Steps

1. **Tag** the merge commit: `git tag -a v0.1.0 <sha> -m "brevkoll v0.1.0"` and push the tag to Gitea only.
2. **Build:** `docker build -t brevkoll:0.1.0 .` (local image; add the registry name only if you push).
3. **Scan:** `trivy image --severity HIGH,CRITICAL --exit-code 1 brevkoll:0.1.0` with the pinned trivy binary from
   `.gitea/workflows/gates.yml` (`TRIVY_VERSION`, checksum in the same file). Download it to `/tmp`, verify the checksum, run it. Any
   HIGH or CRITICAL stops the release.
4. **SBOM:** `syft` with the version and checksum in `factory-template/docs/release.md`; keep `sbom.cdx.json` with the tag.
5. **Run:** one container, bound to `127.0.0.1:8000` unless you decide on LAN access:
   `docker run -d --name brevkoll --restart unless-stopped --read-only --tmpfs /tmp --tmpfs /data:uid=10001,gid=10001 --cap-drop ALL
   --security-opt no-new-privileges --memory 1g --pids-limit 200 --env-file ~/.config/brevkoll/env -p 127.0.0.1:8000:8000 brevkoll:0.1.0`
   The env file is mode 0600 and holds `LLM_PROVIDER`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`. It is never committed.
   Reaching a model on the host's `127.0.0.1` needs `--network host` (then bind uvicorn to 127.0.0.1) or the host's bridge IP.
6. **Verify and record** (each line goes to `factory/data/runs.jsonl`): decode the fixture letter and check both dates and the
   `.ics`; the personnummer is absent; `/data` and `/tmp` empty; 415 and 422 cases; no cookie or CORS headers; logs hold no
   letter text; restart the container and decode again.
7. **Phone check** (needs LAN access, see Decisions): open the page, photograph a real letter, import the `.ics`.
8. **Mark shipped:** add the label `stage/shipped` to brevkoll#1 and close it. This starts the clocks: the lookback runs 14 and 30 days
   later (`lookback.timer`), and brevkoll adopts the quality guards (`chore/adopt-quality-guards`) right after.

## Rollback

`docker stop brevkoll && docker rm brevkoll`. The previous image tag stays local. Nothing else is changed on the machine.

## Decisions for the owner

| # | Question | My recommendation |
|---|---|---|
| 1 | Model: the model on `:8090` belongs to the assistant and the GPU arbiter can unload it. | Use it for now. When it is gone the app answers 502 and nothing else breaks. A separate always-on model needs VRAM you do not have. |
| 2 | LAN access for the phone. | A reverse proxy rule with access control, or an SSH tunnel for the first test. Not a public port: the app has no login (accepted risk R3). |
| 3 | Push to the Gitea registry. | Skip for v0.1.0. It needs a package token (I will not create one) and nothing consumes the image yet. Do it with the cluster deploy. |
| 4 | Hostname `brevkoll.lab.aminrj.com`. | Only after decision 2. It is already visible in the mirrored spec, so it is not a secret. |

## Not in this plan

Cluster deploy (GitOps PR, ArgoCD, External Secrets), Cloudflare tunnel, Renovate, GlitchTip.
