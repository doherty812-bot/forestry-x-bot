#!/usr/bin/env python3
"""実投稿を伴わないガード・整形のユニットテスト。"""

import os
import unittest
from unittest.mock import MagicMock, patch

import forestry_bot as bot


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
        self.assertNotIn("#森林", full)  # GPT由来タグは除去され固定タグのみ

    def test_strips_generated_hashtags(self):
        body = "コメントです。 #林業 #forest #余計"
        url = "https://example.com/a"
        full, clean = bot.build_tweet_payload(body, url)
        self.assertEqual(full.count("#"), 2)
        self.assertNotIn("余計", full)

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            bot.build_tweet_payload("", "https://example.com")


class TestEnsurePostReady(unittest.TestCase):
    def test_missing_url_raises_without_posting(self):
        with patch.object(bot, "post_to_x") as mock_post:
            with self.assertRaises(RuntimeError) as ctx:
                bot.ensure_post_ready("本文です。", None)
            self.assertIn("記事URL", str(ctx.exception))
            mock_post.assert_not_called()

    def test_missing_tweet_raises(self):
        with patch.object(bot, "post_to_x") as mock_post:
            with self.assertRaises(RuntimeError) as ctx:
                bot.ensure_post_ready(None, "https://example.com")
            self.assertIn("投稿文", str(ctx.exception))
            mock_post.assert_not_called()

    def test_post_failure_raises(self):
        with patch.object(bot, "post_to_x", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                bot.ensure_post_ready("本文です。", "https://example.com")
            self.assertIn("投稿に失敗", str(ctx.exception))


class TestCredentials(unittest.TestCase):
    def test_require_env_fails_without_defaults(self):
        env = os.environ.copy()
        for key in bot.REQUIRED_X_ENV_VARS:
            env.pop(key, None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                bot.get_x_credentials()


class TestLivePostGuard(unittest.TestCase):
    def test_requires_confirmation(self):
        with patch.dict(os.environ, {"CONFIRM_LIVE_POST": ""}, clear=False):
            os.environ.pop("CONFIRM_LIVE_POST", None)
            with self.assertRaises(RuntimeError):
                bot.require_live_post_confirmation()

    def test_allows_when_set(self):
        with patch.dict(os.environ, {"CONFIRM_LIVE_POST": "1"}):
            bot.require_live_post_confirmation()


class TestNoonJobFailHard(unittest.TestCase):
    def test_no_url_does_not_call_create_tweet(self):
        with patch.object(bot, "fetch_forestry_news", return_value=("snippet", None)), \
             patch.object(bot, "generate_buzz_insight_tweet", return_value="本文です。"), \
             patch.object(bot, "post_to_x") as mock_post:
            with self.assertRaises(RuntimeError):
                bot.noon_job()
            mock_post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
