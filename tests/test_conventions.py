"""Conventions that a single wrong line can break silently."""

import re
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


class OwnerIdentityTests(unittest.TestCase):
    def test_no_script_compares_an_author_email_with_the_git_config_address_directly(self) -> None:
        """The owner commits from Gitea's web editor with a noreply address. Always go through is_owner_email()."""
        bad = re.compile(r"[!=]=\s*human_email|human_email\s*[!=]=")
        hits = [f"{p.name}:{n}" for p in SCRIPTS.glob("*.py") for n, line in enumerate(p.read_text().splitlines(), 1)
                if bad.search(line) and not line.lstrip().startswith("#")]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
