"""Unit tests for the quality job helpers. Run: python3 -m unittest discover -s tests -v"""

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import lookback  # noqa: E402
import rework_digest  # noqa: E402


class LookbackTests(unittest.TestCase):
    def test_due_windows(self) -> None:
        now = datetime(2026, 12, 20, tzinfo=UTC)
        self.assertEqual(lookback.due_windows(now - timedelta(days=13), now, set()), [])
        self.assertEqual(lookback.due_windows(now - timedelta(days=14), now, set()), [14])
        self.assertEqual(lookback.due_windows(now - timedelta(days=31), now, {14}), [30])
        self.assertEqual(lookback.due_windows(now - timedelta(days=31), now, {14, 30}), [])

    def test_extract_findings_validates_and_caps(self) -> None:
        good = {"severity": "high", "category": "missing-test", "file": "src/a.py", "line": 3,
                "title": "AC2 has no test", "detail": "d", "reference": "spec AC2"}
        bad = {"severity": "catastrophic", "category": "drift", "title": "x"}
        text = "notes\n```json\n" + json.dumps({"findings": [good, bad] + [good] * 30}) + "\n```"
        found = lookback.extract_findings(text)
        self.assertEqual(len(found), lookback.FINDING_CAP - 1)  # `bad` dropped, cap applied before filtering
        self.assertTrue(all(f["severity"] == "high" for f in found))

    def test_extract_findings_requires_block(self) -> None:
        with self.assertRaises(ValueError):
            lookback.extract_findings("no json here")

    def test_prompt_substitutes_issue(self) -> None:
        self.assertIn("thoughts/7/spec.md", lookback.command_prompt(7))
        self.assertNotIn("$ARGUMENTS", lookback.command_prompt(7))


class ReworkTests(unittest.TestCase):
    def test_friction_section(self) -> None:
        body = "Closes #4\n\n## Friction\nThe OCR path had no timeout.\n\n## Review guide\nlook at x"
        self.assertEqual(rework_digest.friction_section(body), "The OCR path had no timeout.")
        self.assertEqual(rework_digest.friction_section("nothing"), "")

    def test_iso_week(self) -> None:
        self.assertEqual(rework_digest.iso_week(datetime(2026, 11, 20, tzinfo=UTC)), "2026-W47")


class DataFilesTests(unittest.TestCase):
    def test_append_and_read(self) -> None:
        import quality_lib

        with tempfile.TemporaryDirectory() as tmp:
            quality_lib.DATA = Path(tmp)
            quality_lib.append_jsonl("x.jsonl", {"a": 1})
            self.assertEqual(quality_lib.read_jsonl("x.jsonl"), [{"a": 1}])


if __name__ == "__main__":
    unittest.main()
