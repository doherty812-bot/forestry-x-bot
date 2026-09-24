#!/usr/bin/env python3
"""実投稿を伴わないガード・整形・承認フローのユニットテスト。"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import draft_store
import forestry_bot as bot
from mislead_guard import check_mislead_risk


class TestEnforceLinebreaks(unittest.TestCase):
    def test_inserts_newline_after_period(self):
        text = "一文です。二文です。"
        out = bot.enforce_linebreaks(text)
        self.assertIn("。\n", out)


class TestBuildTweetPayload(unittest.TestCase):
    def test_url_and_hashtags(self):
        body = "山を経営資源として見る視点が大切です。\n現場の感覚も忘れません。"
        url = "https://news.google.com/articles/example"
        full, _ = bot.build_tweet_payload(body, url)
        self.assertIn(bot.HASHTAGS, full)
        self.assertIn(url, full)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            bot.build_tweet_payload("", "https://example.com")


class TestMisleadGuard(unittest.TestCase):
    def test_flags_price_hype(self):
        ok, flags = check_mislead_risk("木材価格が高騰しています。")
        self.assertFalse(ok)
        self.assertTrue(any("price_hype" in f for f in flags))

    def test_flags_absolute(self):
        ok, flags = check_mislead_risk("必ず上がります。")
        self.assertFalse(ok)
        self.assertTrue(any("absolute" in f for f in flags))

    def test_allows_hedged_comment(self):
        ok, flags = check_mislead_risk(
            "工場との値段交渉は、現場ごとに温度差があります。\n焦らず様子を見ています。"
        )
        self.assertTrue(ok)
        self.assertEqual(flags, [])


class TestLivePostGuard(unittest.TestCase):
    def test_requires_confirmation(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIRM_LIVE_POST", None)
            with self.assertRaises(RuntimeError):
                bot.require_live_post_confirmation()


class TestDraftOnlyJobs(unittest.TestCase):
    def test_noon_does_not_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(draft_store, "DRAFTS_DIR", Path(tmp)), \
                 patch.object(bot, "collect_noon_article", return_value=("題", "概要", "https://ex.com/a")), \
                 patch.object(bot, "generate_buzz_insight_tweet", side_effect=lambda t, s, provider="openai": f"{provider}-本文です。"), \
                 patch.object(bot, "post_to_x") as mock_post:
                draft = bot.noon_job()
                mock_post.assert_not_called()
                self.assertEqual(draft["status"], "pending")
                self.assertIn("openai", draft["candidates"])
                self.assertIn("grok", draft["candidates"])
                self.assertTrue(Path(tmp, f"{draft['id']}.json").exists())

    def test_evening_uses_industry_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(draft_store, "DRAFTS_DIR", Path(tmp)), \
                 patch.object(bot, "collect_evening_article", return_value=("DX", "生産性", "https://ex.com/b")), \
                 patch.object(bot, "generate_industry_trend_tweet", side_effect=lambda t, s, provider="openai": f"{provider}-示唆です。") as mock_gen, \
                 patch.object(bot, "generate_buzz_insight_tweet") as mock_domestic, \
                 patch.object(bot, "post_to_x") as mock_post:
                draft = bot.pre_evening_job()
                mock_post.assert_not_called()
                mock_domestic.assert_not_called()
                self.assertEqual(mock_gen.call_count, 2)
                self.assertEqual(draft["slot"], "20:00")


class TestApproveFlow(unittest.TestCase):
    def test_approve_without_confirm_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(draft_store, "DRAFTS_DIR", Path(tmp)):
                draft = {
                    "id": "testdraft",
                    "status": "pending",
                    "article": {"url": "https://ex.com"},
                    "candidates": {"openai": {"text": "本文です。", "guard_ok": True, "flags": []}},
                }
                draft_store.save_draft(draft)
                os.environ.pop("CONFIRM_LIVE_POST", None)
                with self.assertRaises(RuntimeError):
                    bot.approve_and_post("testdraft", "openai")

    def test_approve_posts_selected_provider_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(draft_store, "DRAFTS_DIR", Path(tmp)), \
                 patch.dict(os.environ, {"CONFIRM_LIVE_POST": "1"}), \
                 patch.object(bot, "post_to_x", return_value=True) as mock_post:
                draft = {
                    "id": "testdraft2",
                    "status": "pending",
                    "article": {"url": "https://ex.com/n"},
                    "candidates": {
                        "openai": {"text": "OpenAI案です。", "guard_ok": True, "flags": []},
                        "grok": {"text": "Grok案です。", "guard_ok": True, "flags": []},
                    },
                }
                draft_store.save_draft(draft)
                bot.approve_and_post("testdraft2", "grok")
                mock_post.assert_called_once_with("Grok案です。", "https://ex.com/n")
                saved = draft_store.load_draft("testdraft2")
                self.assertEqual(saved["status"], "posted")
                self.assertEqual(saved["posted_provider"], "grok")


class TestIndustryTrendPolicy(unittest.TestCase):
    def test_primary_queries_are_not_forestry_only(self):
        banned = ("林業", "林野庁", "国産材", "里山", "山林", "木材", "森林")
        for q in bot.INDUSTRY_TREND_QUERIES:
            for marker in banned:
                self.assertNotIn(marker, q)

    def test_system_prompt_has_mislead_guard(self):
        self.assertIn("ミスリード防止", bot.INDUSTRY_TREND_SYSTEM_PROMPT)


class TestCredentials(unittest.TestCase):
    def test_require_env_fails_without_defaults(self):
        env = os.environ.copy()
        for key in bot.REQUIRED_X_ENV_VARS:
            env.pop(key, None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                bot.get_x_credentials()


if __name__ == "__main__":
    unittest.main()
