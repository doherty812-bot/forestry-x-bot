#!/usr/bin/env python3
"""dispatch_token / 引数ゲートのユニットテスト（秘密・ネットワークなし）。"""

import os
import unittest
from unittest.mock import patch

from scripts.dispatch_token import resolve_dispatch_token, require_dispatch_token


class TestResolveDispatchToken(unittest.TestCase):
    def test_prefers_workflow_dispatch_pat(self):
        env = {
            "WORKFLOW_DISPATCH_PAT": "pat-primary",
            "GH_PAT": "pat-secondary",
        }
        name, token = resolve_dispatch_token(env)
        self.assertEqual(name, "WORKFLOW_DISPATCH_PAT")
        self.assertEqual(token, "pat-primary")

    def test_falls_back_to_gh_pat(self):
        name, token = resolve_dispatch_token({"GH_PAT": " only-gh "})
        self.assertEqual(name, "GH_PAT")
        self.assertEqual(token, "only-gh")

    def test_ignores_empty(self):
        name, token = resolve_dispatch_token(
            {"WORKFLOW_DISPATCH_PAT": "  ", "GH_PAT": ""}
        )
        self.assertIsNone(name)
        self.assertIsNone(token)

    def test_require_raises_when_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit):
                require_dispatch_token({})


if __name__ == "__main__":
    unittest.main()
