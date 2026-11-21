"""Who counts as the owner when a gate asks for a commit of the human."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from gitea import is_owner_email  # noqa: E402


class OwnerEmailTests(unittest.TestCase):
    def test_configured_address(self) -> None:
        self.assertTrue(is_owner_email("me@example.com", "me", "me@example.com"))

    def test_web_editor_address_for_the_same_login(self) -> None:
        self.assertTrue(is_owner_email("1+me@noreply.gitea.example.com", "me", "other@example.com"))

    def test_other_users_and_the_factory_never_count(self) -> None:
        self.assertFalse(is_owner_email("2+you@noreply.gitea.example.com", "me", "me@example.com"))
        self.assertFalse(is_owner_email("factory-agent@users.noreply.local", "me", "me@example.com"))
        self.assertFalse(is_owner_email("factory-orchestrator@users.noreply.local", "me", "me@example.com"))
        self.assertFalse(is_owner_email("1+me@evil.example.com", "me", "me@example.com"))


if __name__ == "__main__":
    unittest.main()
