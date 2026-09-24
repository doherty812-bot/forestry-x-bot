#!/usr/bin/env python3
"""Actions 承認経路向けのユニットテスト（ネットワーク・実投稿なし）。"""

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts import fetch_draft_from_artifacts as fetch


class TestConfirmAndProvider(unittest.TestCase):
    def test_confirm_rejects_false(self):
        with self.assertRaises(SystemExit):
            fetch.require_confirm_flag("false")
        with self.assertRaises(SystemExit):
            fetch.require_confirm_flag("")

    def test_confirm_accepts_true(self):
        fetch.require_confirm_flag("true")
        fetch.require_confirm_flag("1")

    def test_provider_validation(self):
        self.assertEqual(fetch.validate_provider("Grok"), "grok")
        with self.assertRaises(SystemExit):
            fetch.validate_provider("claude")


class TestFindDraftJson(unittest.TestCase):
    def test_finds_nested(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "run-1"
            nested.mkdir()
            path = nested / "20260924T062759Z-1200.json"
            path.write_text('{"id":"20260924T062759Z-1200"}', encoding="utf-8")
            found = fetch.find_draft_json(root, "20260924T062759Z-1200")
            self.assertEqual(found, path)

    def test_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(fetch.find_draft_json(Path(tmp), "nope"))


class TestAlreadyApproved(unittest.TestCase):
    def test_detects_draft_in_run_name(self):
        runs = [
            {
                "name": "Approve 20260924T062759Z-1200 (grok)",
                "conclusion": "success",
            }
        ]
        self.assertTrue(
            fetch.already_approved_in_runs(runs, "20260924T062759Z-1200")
        )

    def test_ignores_other_drafts(self):
        runs = [
            {
                "name": "Approve 20260924T000000Z-1200 (openai)",
                "conclusion": "success",
            }
        ]
        self.assertFalse(
            fetch.already_approved_in_runs(runs, "20260924T062759Z-1200")
        )


class TestExtractZip(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "abc.json").write_text('{"id":"abc"}', encoding="utf-8")
            zip_path = Path(tmp) / "a.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                zf.write(src / "abc.json", arcname="abc.json")
            dest = Path(tmp) / "out"
            fetch.extract_zip_to(zip_path.read_bytes(), dest)
            self.assertTrue((dest / "abc.json").exists())


if __name__ == "__main__":
    unittest.main()
