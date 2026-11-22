#!/usr/bin/env python3
"""Release candidates (docs/lifecycle.md, stages 6 and 7): build the image of `main`, scan it, push it to the Gitea
container registry, and run THAT image on the devbox for the owner to accept at Checkpoint 5.

  release.py candidate <app>   build, scan, push and stage a candidate of origin/main; prints JSON
  release.py accept <app> <version>   tag the source commit with the version (after the owner's /go at CP5)

Rules:
- Only a `main` whose required checks are green is released (the same check staging uses).
- The image is built once from `git archive` of that commit and tagged twice: `v<YYYY.MM.DD>.<n>` and `sha-<short>`.
  The candidate on the devbox and, later, production run that image: same tag, same digest.
- Scan with trivy from its pinned container image, fed `docker save` output read-only (never the Docker socket, which
  would give the scanner root on the host). Fixed HIGH or CRITICAL findings refuse the release; unfixed ones are counted
  and shown. A CycloneDX SBOM is written next to the record (state/releases/<app>/<version>.cdx.json).
- Push with the registry token (~/.config/factory/registry-token, package write) as the owner (FACTORY_OWNER), through
  `docker login --password-stdin`: the token never appears on a command line.
- Every candidate is recorded in data/releases.jsonl (no URLs or host names: data/ is mirrored).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stage  # noqa: E402
from gitea import Gitea  # noqa: E402

WORK = Path(os.environ.get("WORK", Path.home() / "git" / "software-factory"))
HQ = Path(__file__).resolve().parent.parent
DATA = HQ / "data"
STATE = WORK / "state"
TOKEN_FILE = Path(os.environ.get("FACTORY_REGISTRY_TOKEN_FILE", Path.home() / ".config" / "factory" / "registry-token"))
TRIVY = os.environ.get("FACTORY_TRIVY_IMAGE", "aquasec/trivy:0.76.2")


class ReleaseRefused(Exception):
    """The candidate must not be released; the message says why, for the checkpoint."""


def sh(*args: str, cwd: Path | None = None, stdin: bytes | None = None, check: bool = True) -> str:
    res = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, check=False)
    if check and res.returncode != 0:
        raise ReleaseRefused(f"`{' '.join(args[:3])}` failed: {res.stderr.decode(errors='replace')[-400:]}")
    return res.stdout.decode(errors="replace").strip()


def registry_host(g: Gitea) -> str:
    return g.base.split("://", 1)[1].split("/", 1)[0]


def next_version(tags: list[str], today: datetime) -> str:
    prefix = f"v{today.strftime('%Y.%m.%d')}."
    used = [int(t[len(prefix):]) for t in tags if t.startswith(prefix) and t[len(prefix):].isdigit()]
    return f"{prefix}{max(used, default=0) + 1}"


def scan_counts(report: dict) -> dict[str, int]:
    """Counts of HIGH/CRITICAL findings in a trivy JSON report: fixed (a newer package exists) and unfixed."""
    out = {"fixed": 0, "unfixed": 0, "critical": 0}
    for result in report.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            if v.get("Severity") not in ("HIGH", "CRITICAL"):
                continue
            out["fixed" if v.get("FixedVersion") else "unfixed"] += 1
            out["critical"] += v.get("Severity") == "CRITICAL"
    return out


def scan(image: str, sbom_path: Path) -> dict[str, int]:
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp) / "image.tar"
        sh("docker", "save", "-o", str(tar), image)
        os.chmod(tmp, 0o755)
        os.chmod(tar, 0o644)
        base = ["docker", "run", "--rm", "--network", "bridge", "-v", f"{tar}:/image.tar:ro", TRIVY, "image", "--quiet",
                "--input", "/image.tar"]
        report = json.loads(sh(*base, "--severity", "HIGH,CRITICAL", "--format", "json") or "{}")
        sbom_path.parent.mkdir(parents=True, exist_ok=True)
        sbom_path.write_text(sh(*base, "--format", "cyclonedx"))
    return scan_counts(report)


def push(g: Gitea, refs: list[str]) -> str:
    """Log in with the token on stdin, push every tag, return the digest."""
    host = registry_host(g)
    sh("docker", "login", host, "-u", g.user, "--password-stdin", stdin=TOKEN_FILE.read_text().strip().encode())
    for ref in refs:
        sh("docker", "push", "-q", ref)
    digests = json.loads(sh("docker", "inspect", "--format", "{{json .RepoDigests}}", refs[0]) or "[]")
    return next((d.split("@", 1)[1] for d in digests if d.startswith(f"{host}/")), "")


def registry_versions(g: Gitea, app: str) -> list[str]:
    """Versions of the app already in the registry (read with the registry token: the bot's token has no package scope).
    A candidate pushed by hand, or one whose record was lost, must still never be overwritten."""
    import urllib.request  # noqa: PLC0415
    try:
        req = urllib.request.Request(f"{g.base}/api/v1/packages/{g.user}?type=container&q={app}",
                                     headers={"Authorization": f"token {TOKEN_FILE.read_text().strip()}"})
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - the Gitea URL from the config
            return [p["version"] for p in json.loads(r.read()) if p.get("name") == app]
    except Exception:  # noqa: BLE001 - unknown: fall back on the git tags and the records
        return []


def candidate(g: Gitea, app: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    repo = f"{g.user}/{app}"
    clone = WORK / app
    sh("git", "fetch", "-q", "--tags", "origin", cwd=clone)
    sha = sh("git", "rev-parse", "origin/main", cwd=clone)
    try:
        stage.require_green(g, repo, sha, clone)
    except stage.StageError as e:
        raise ReleaseRefused(f"`main` ({sha[:7]}) is not green: {e}") from e
    recorded = [json.loads(x).get("version", "") for x in (DATA / "releases.jsonl").read_text().splitlines()
                if x.strip() and json.loads(x).get("app") == app] if (DATA / "releases.jsonl").exists() else []
    # refused or rejected candidates count too: a version is never pushed twice with different contents
    version = next_version(sh("git", "tag", "--list", "v*", cwd=clone).splitlines() + recorded
                           + registry_versions(g, app), now)
    name = f"{registry_host(g)}/{g.user}/{app}"
    refs = [f"{name}:{version}", f"{name}:sha-{sha[:7]}"]
    archive = subprocess.run(["git", "archive", sha], cwd=clone, capture_output=True, check=True).stdout
    sh("docker", "build", "-q", "-t", refs[0], "-t", refs[1], "-", stdin=archive)
    sbom = STATE / "releases" / app / f"{version}.cdx.json"
    findings = scan(refs[0], sbom)
    if findings["fixed"]:
        raise ReleaseRefused(f"{findings['fixed']} HIGH/CRITICAL findings have a fix available: update the base image or "
                             "the packages, then release again. Nothing was pushed.")
    digest = push(g, refs)
    staged = stage.up(app, ref=sha, image=refs[0])
    previous = sh("git", "describe", "--tags", "--abbrev=0", "--match", "v*", f"{sha}^", cwd=clone, check=False)
    changes = sh("git", "log", "--format=%s", f"{previous}..{sha}" if previous else sha, "-n", "30", cwd=clone)
    record = {"ts": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "app": app, "version": version, "sha": sha, "digest": digest,
              "findings": findings, "state": "candidate"}
    DATA.mkdir(exist_ok=True)
    with (DATA / "releases.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return {**record, "image": refs[0], "url": staged["url"], "local_url": staged.get("local_url", ""), "previous": previous,
            "changes": changes.splitlines(), "sbom": str(sbom)}


def accept(app: str, version: str, sha: str) -> None:
    """Tag the released commit (the owner accepted it at CP5) and push the tag."""
    if not re.fullmatch(r"v\d{4}\.\d{2}\.\d{2}\.\d+", version):
        raise ReleaseRefused(f"not a release version: {version}")
    clone = WORK / app
    sh("git", "tag", "-a", version, sha, "-m", f"{app} {version}: accepted at Checkpoint 5", cwd=clone)
    sh("git", "push", "-q", "origin", version, cwd=clone)
    with (DATA / "releases.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "app": app, "version": version,
                             "sha": sha, "state": "accepted"}) + "\n")


def main(argv: list[str]) -> int:
    try:
        if argv[1:2] == ["candidate"] and len(argv) == 3:
            print(json.dumps(candidate(Gitea(), argv[2]), indent=2))
        elif argv[1:2] == ["accept"] and len(argv) == 5:
            accept(argv[2], argv[3], argv[4])
            print(f"tagged {argv[3]}")
        else:
            print(__doc__)
            return 2
    except ReleaseRefused as e:
        print(f"RELEASE REFUSED: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 - one readable line for the checkpoint, never a traceback
        print(f"RELEASE FAILED: {type(e).__name__}: {str(e)[:400]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
