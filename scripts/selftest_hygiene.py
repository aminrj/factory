#!/usr/bin/env python3
"""Live self-test of image_watch.py and template_sync.py against the real Gitea (the unit tests use a fake one).

Creates a private throwaway repo under the owner's account, runs the weekly jobs against it with an injected scan result
(so no image is built), checks every outcome through the API (branch, commit author, PR body, labels, deduplication,
mergeability), then deletes the repo, also when a check fails. Run it after changing either job:

    python3 -I scripts/selftest_hygiene.py        # FACTORY_SELFTEST_TEMPLATE_REF=<branch> if the template manifest is not on main yet

Exit 0 when every check passed. The owner gets no push notification; nothing outside the throwaway repo is touched.
"""

import base64
import contextlib
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import image_watch
import quality_lib
import template_sync
from gitea import Gitea, GiteaError

g = Gitea()
NAME = "scratch-image-watch-" + time.strftime("%H%M%S")
full = f"{g.user}/{NAME}"
OLD, NEW = "a" * 64, "b" * 64
DOCKERFILE = f"""FROM python:3.12-slim@sha256:{OLD}
RUN apt-get update \\
    && apt-get install -y --no-install-recommends tesseract-ocr zlib1g \\
    && rm -rf /var/lib/apt/lists/*
USER nobody
"""
image_watch.notify = lambda *a, **k: print("   (notify suppressed)")
results = {}

def check(label, ok, detail=""):
    results[label] = ok
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))

def scan_seq(*fixed_counts):
    q = list(fixed_counts)
    def scan(root, tag):
        n = q.pop(0) if q else q[-1] if q else 0
        return {"fixed": n, "unfixed": 93, "critical": 0, "packages": ["libkrb5-3 1.21.3-5+deb13u1 -> 1.21.3-5+deb13u2"] if n else []}
    return scan

try:
    g.call("POST", "/user/repos", {"name": NAME, "private": True, "auto_init": True, "default_branch": "main",
                                   "description": "throwaway: live test of image_watch and template_sync, deleted right after"})
    print("created", full)
    g.call("POST", f"/repos/{full}/contents/Dockerfile", {"content": base64.b64encode(DOCKERFILE.encode()).decode(), "branch": "main", "message": "add Dockerfile"})
    time.sleep(1)

    # --- A: apt upgrade clears it -> a real PR
    rec = image_watch.watch(g, NAME, False, scan=scan_seq(56, 0), resolve=lambda r: None)
    print("A:", {k: v for k, v in rec.items() if k != "packages"})
    check("A action is pr_opened", rec["action"].startswith("pr_opened"), rec["action"])
    prs = g.get(f"/repos/{full}/pulls?state=open")
    check("A one open PR on a chore/image-security branch", len(prs) == 1 and prs[0]["head"]["ref"].startswith("chore/image-security"))
    pr = prs[0]
    branch_df = base64.b64decode(g.get(f"/repos/{full}/contents/Dockerfile?ref={pr['head']['ref']}")["content"]).decode()
    check("A Dockerfile on the branch has apt-get upgrade", "apt-get upgrade -y --no-install-recommends" in branch_df)
    main_df = base64.b64decode(g.get(f"/repos/{full}/contents/Dockerfile?ref=main")["content"]).decode()
    check("A main is untouched", main_df == DOCKERFILE)
    commits = g.get(f"/repos/{full}/commits?sha={pr['head']['ref']}&limit=2")
    check("A commit authored as factory-orchestrator", commits[0]["commit"]["author"]["name"] == "factory-orchestrator", commits[0]["commit"]["author"]["name"])
    check("A PR body names the package and the result", "libkrb5-3" in pr["body"] and "**0** fixable" in pr["body"])
    for _ in range(10):  # Gitea computes mergeability a moment after the PR exists
        pr = g.get(f"/repos/{full}/pulls/{pr['number']}")
        if pr["mergeable"]:
            break
        time.sleep(1)
    check("A PR is mergeable", pr["mergeable"] is True)

    # --- B: same day again -> no second PR
    rec = image_watch.watch(g, NAME, False, scan=scan_seq(56, 0), resolve=lambda r: None)
    check("B second run opens nothing new", rec["action"] == "pr_already_open", rec["action"])
    check("B still one PR", len(g.get(f"/repos/{full}/pulls?state=open")) == 1)

    # close the PR and its branch so C starts clean
    g.call("PATCH", f"/repos/{full}/pulls/{pr['number']}", {"state": "closed"})
    g.call("DELETE", f"/repos/{full}/branches/{pr['head']['ref']}")

    # --- C: nothing helps -> one issue with labels, then deduplicated
    rec = image_watch.watch(g, NAME, False, scan=scan_seq(56, 56, 56, 56), resolve=lambda r: NEW)
    print("C:", {k: v for k, v in rec.items() if k != "packages"})
    check("C action is issue_opened", rec["action"].startswith("issue_opened"), rec["action"])
    issues = g.get(f"/repos/{full}/issues?state=open&type=issues")
    check("C one issue with labels bug and security", len(issues) == 1 and {lab["name"] for lab in issues[0]["labels"]} == {"bug", "security"}, str([lab["name"] for lab in issues[0]["labels"]]) if issues else "none")
    check("C issue carries the dedupe marker and the package", issues and image_watch.MARKER in issues[0]["body"] and "libkrb5-3" in issues[0]["body"])
    check("C no PR was opened", not g.get(f"/repos/{full}/pulls?state=open"))
    rec = image_watch.watch(g, NAME, False, scan=scan_seq(56, 56, 56, 56), resolve=lambda r: NEW)
    check("C rerun does not file twice", rec["action"] == "issue_already_open", rec["action"])
    check("C still one issue", len(g.get(f"/repos/{full}/issues?state=open&type=issues")) == 1)

    # --- D: template sync against the template's feature branch (it holds the manifest)
    real_clone = quality_lib.read_only_clone
    @contextlib.contextmanager
    def clone(gg, repo, ref="main"):
        with real_clone(gg, repo, os.environ.get("FACTORY_SELFTEST_TEMPLATE_REF", ref) if repo == "factory-template" else ref) as p:
            yield p
    template_sync.read_only_clone = clone
    rec = template_sync.sync(g, NAME, False)
    print("D:", rec["action"], len(rec["differs"]), "files differ")
    check("D sync opened a PR", rec["action"].startswith("pr_opened"), rec["action"])
    sprs = [p for p in g.get(f"/repos/{full}/pulls?state=open") if p["head"]["ref"].startswith("chore/template-sync")]
    check("D one sync PR", len(sprs) == 1)
    files = g.get(f"/repos/{full}/pulls/{sprs[0]['number']}/files") if sprs else []
    check("D the PR carries the differing files", len(files) == len(rec["differs"]) and len(files) > 5, f"{len(files)} files")
    check("D includes the manifest-owned agent commands", any(f["filename"] == ".claude/commands/validate.md" for f in files))
    rec2 = template_sync.sync(g, NAME, False)
    check("D rerun opens nothing", rec2["action"] == "pr_already_open", rec2["action"])
finally:
    try:
        g.call("DELETE", f"/repos/{full}")
        print("deleted", full)
    except GiteaError as e:
        print("!! could not delete", full, str(e)[:100])
bad = [k for k, v in results.items() if not v]
print(f"\n{len(results) - len(bad)}/{len(results)} checks passed", "" if not bad else f"FAILED: {bad}")
sys.exit(1 if bad else 0)
