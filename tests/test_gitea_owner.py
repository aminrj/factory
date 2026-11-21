"""Who counts as the owner when a gate asks for a commit of the human."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from gitea import is_owner_email, url_from_credential_config  # noqa: E402


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


# `git config --global --get-regexp` on the devbox after `gh auth setup-git`: GitHub comes first (2026-11-21, the
# first cutover rolled back because the foreman called github.com/api/v1/user and got 410).
DEVBOX = """credential.https://github.com.helper 
credential.https://github.com.helper !/usr/bin/gh auth git-credential
credential.https://gist.github.com.helper 
credential.https://gist.github.com.helper !/usr/bin/gh auth git-credential
credential.https://git.example.com.helper !f() { echo "password=$(cat ~/.config/git/gitea-token)"; }; f
"""


class ServerUrlTests(unittest.TestCase):
    def test_github_helpers_are_skipped(self) -> None:
        self.assertEqual(url_from_credential_config(DEVBOX), "https://git.example.com")

    def test_the_helper_reading_the_token_file_wins(self) -> None:
        config = "credential.https://other.example.com.helper store\n" + DEVBOX
        self.assertEqual(url_from_credential_config(config), "https://git.example.com")

    def test_a_single_helper_without_the_token_file_still_counts(self) -> None:
        self.assertEqual(url_from_credential_config("credential.https://git.example.com/.helper store\n"),
                         "https://git.example.com")

    def test_only_github_means_none(self) -> None:
        self.assertIsNone(url_from_credential_config(DEVBOX.rsplit("\n", 2)[0]))


if __name__ == "__main__":
    unittest.main()
