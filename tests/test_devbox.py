"""The devbox move: the bootstrap refuses before changing anything when a secret is missing, and the units it links exist."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOOT = ROOT / "ops" / "devbox" / "bootstrap.sh"


class BootstrapTests(unittest.TestCase):
    def test_missing_secrets_stop_it_before_any_change(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            env = {"HOME": home, "PATH": "/usr/bin:/bin", "GIT_SSH_COMMAND": "false"}  # no network call to GitHub
            res = subprocess.run(["bash", str(BOOT), "https://git.example/owner"], capture_output=True, text=True, env=env,
                                 timeout=60)
            self.assertEqual(res.returncode, 1, res.stdout + res.stderr)
            for what in ("gitea-token", "ntfy.env", "staging.env", "local-model.env"):
                self.assertIn(f"MISSING {home}", res.stdout)
                self.assertIn(what, res.stdout)
            self.assertFalse((Path(home) / "git").exists())  # nothing cloned
            self.assertFalse((Path(home) / ".gitconfig").exists())  # no credential helper written

    def test_a_secret_with_loose_permissions_is_tightened(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            token = Path(home) / ".config" / "git" / "gitea-token"
            token.parent.mkdir(parents=True)
            token.write_text("x")
            os.chmod(token, 0o644)
            subprocess.run(["bash", str(BOOT), "https://git.example/owner"], capture_output=True, text=True,
                           env={"HOME": home, "PATH": "/usr/bin:/bin"}, timeout=60)
            self.assertEqual(oct(token.stat().st_mode & 0o777), "0o600")

    def test_scripts_parse(self) -> None:
        for script in (BOOT, ROOT / "ops" / "devbox" / "cutover.sh"):
            subprocess.run(["bash", "-n", str(script)], check=True)


class GatewayUnitTests(unittest.TestCase):
    def test_the_gateway_runs_the_shim_on_loopback_from_the_runtime_clone(self) -> None:
        unit = (ROOT / "ops" / "systemd" / "factory-model-gateway.service").read_text()
        self.assertIn("%h/git/software-factory/factory/scripts/model_shim.py serve --port 8090", unit)
        self.assertIn("Restart=always", unit)
        self.assertTrue((ROOT / "scripts" / "model_shim.py").exists())


if __name__ == "__main__":
    unittest.main()
