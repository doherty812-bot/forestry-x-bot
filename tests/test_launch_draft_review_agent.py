#!/usr/bin/env python3
"""launch_draft_review_agent のユニットテスト（ネットワークなし）。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import launch_draft_review_agent as launch


class TestHelpers(unittest.TestCase):
    def test_resolve_repo_from_github_env(self):
        with mock.patch.dict(
            "os.environ",
            {"GITHUB_REPOSITORY": "doherty812-bot/forestry-x-bot"},
            clear=False,
        ):
            # Clear override if present
            env = {
                "GITHUB_REPOSITORY": "doherty812-bot/forestry-x-bot",
            }
            with mock.patch.dict("os.environ", env, clear=True):
                self.assertEqual(
                    launch.resolve_repo_url(),
                    "https://github.com/doherty812-bot/forestry-x-bot",
                )

    def test_resolve_repo_https_passthrough(self):
        self.assertEqual(
            launch.resolve_repo_url("https://github.com/acme/r"),
            "https://github.com/acme/r",
        )

    def test_find_latest_draft_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "20260924T100000Z-1200.json").write_text("{}", encoding="utf-8")
            (root / "20260925T130256Z-2000.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                launch.find_latest_draft_id(root), "20260925T130256Z-2000"
            )

    def test_deterministic_agent_id_stable(self):
        a = launch.deterministic_agent_id("20260925T130256Z-2000")
        b = launch.deterministic_agent_id("20260925T130256Z-2000")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith("bc-"))
        # UUID form after bc-
        self.assertEqual(len(a), len("bc-") + 36)

    def test_prompt_contains_required_bits(self):
        text = launch.build_prompt(
            draft_id="ABC-1200",
            run_id="123",
            run_url="https://github.com/o/r/actions/runs/123",
            repository="https://github.com/o/r",
            slot="20:00",
        )
        self.assertIn("DRAFT_ID: `ABC-1200`", text)
        self.assertIn("openai", text)
        self.assertIn("grok", text)
        self.assertIn("却下", text)
        self.assertIn("ライブ投稿しない", text)
        self.assertIn(launch.PROJECT_ID, text)

    def test_v1_payload_no_pr(self):
        payload = launch.build_v1_payload(
            draft_id="ABC-1200",
            prompt_text="hello",
            repo_url="https://github.com/o/r",
            ref="main",
        )
        self.assertFalse(payload["autoCreatePR"])
        self.assertEqual(payload["repos"][0]["startingRef"], "main")
        self.assertIn("agentId", payload)
        self.assertEqual(payload["prompt"]["text"], "hello")

    def test_v0_payload_no_pr(self):
        payload = launch.build_v0_payload(
            draft_id="ABC-1200",
            prompt_text="hello",
            repo_url="https://github.com/o/r",
            ref="main",
        )
        self.assertFalse(payload["target"]["autoCreatePr"])

    def test_extract_agent_url_v1(self):
        url = launch.extract_agent_url(
            {
                "agent": {
                    "id": "bc-11111111-1111-1111-1111-111111111111",
                    "url": "https://cursor.com/agents/bc-11111111-1111-1111-1111-111111111111",
                }
            }
        )
        self.assertIn("cursor.com/agents", url)

    def test_dry_run_exit_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "20260925T130256Z-2000.json").write_text("{}", encoding="utf-8")
            code = launch.main(
                [
                    "--drafts-dir",
                    str(root),
                    "--dry-run",
                    "--run-id",
                    "99",
                ]
            )
            self.assertEqual(code, 0)

    def test_missing_key_allow(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True):
                code = launch.main(
                    ["--drafts-dir", str(root), "--draft-id", "x", "--soft-fail"]
                )
            self.assertEqual(code, 0)

    def test_missing_key_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.json").write_text("{}", encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True):
                code = launch.main(["--drafts-dir", str(root), "--draft-id", "x"])
            self.assertEqual(code, 1)

    def test_launch_409_treated_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "x.json").write_text("{}", encoding="utf-8")
            fake = mock.Mock(return_value=(409, {"error": "agent_id_conflict"}))
            with mock.patch.dict(
                "os.environ",
                {"CURSOR_API_KEY": "test-key-not-real"},
                clear=True,
            ):
                with mock.patch.object(launch, "launch_agent", fake):
                    code = launch.main(
                        ["--drafts-dir", str(root), "--draft-id", "x"]
                    )
            self.assertEqual(code, 0)
            fake.assert_called_once()


if __name__ == "__main__":
    unittest.main()
