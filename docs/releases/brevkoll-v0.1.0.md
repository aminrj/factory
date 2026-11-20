# Release plan: brevkoll v0.1.0 (devbox)

Status: draft for the owner. Nothing here has been run except the checks marked done.
Decision already made: run on the devbox, not on the cluster (the cluster hosts other workloads).

## State today (2026-11-20)

- `brevkoll` `main` is at `622afe4` (v1 code plus the real README). CI green. No tags, no registry image.
- A container from `249c09b` runs on `127.0.0.1:8765` with two overrides that are not in the code (timeout 180 s, tokens 3000)
  and a localhost relay (`/tmp/bk-relay`, port 18091) that adds the model's API key. Both are stopgaps.
- Gitea's container registry is enabled (`/v2/` answers 401). `trivy` and `syft` are not installed on the devbox.
- `Dockerfile` pulls `python:3.12-slim` and `ghcr.io/astral-sh/uv:latest` by tag. `latest` is not pinned.

## What must be true before the tag

1. **brevkoll#4 (timeout and token cap from env) and #5 (`LLM_API_KEY`) are merged.** Without them the release depends on the
   overrides and the relay above. Build them through the factory (label the issue `stage/spec-ready`) or by hand.
2. **Pin both base images by digest** in the `Dockerfile` (`FROM python:3.12-slim@sha256:...`, `uv` copied from a pinned tag
   with digest). Human PR; the agent must not touch it without a plan.
3. Decision on the model (see "Decisions").

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
