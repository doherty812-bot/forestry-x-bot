#!/usr/bin/env python3
"""実投稿を伴わないガード・整形・コンテンツ方針のユニットテスト。"""

import os
import unittest
from unittest.mock import patch

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

    def test_uses_domestic_insight_generator(self):
        with patch.object(bot, "fetch_forestry_news", return_value=("国産材の動向", "https://example.com/noon")), \
             patch.object(bot, "generate_buzz_insight_tweet", return_value="昼の本文です。") as mock_gen, \
             patch.object(bot, "generate_industry_trend_tweet") as mock_industry, \
             patch.object(bot, "post_to_x", return_value=True):
            bot.noon_job()
            mock_gen.assert_called_once()
            mock_industry.assert_not_called()


class TestIndustryTrendPolicy(unittest.TestCase):
    """夜20時は産業・経営トレンド方針（国内農林業固定にしない）。"""

    def test_primary_queries_are_not_forestry_only(self):
        self.assertTrue(bot.INDUSTRY_TREND_QUERIES)
        banned = ("林業", "林野庁", "国産材", "里山", "山林", "木材", "森林")
        for q in bot.INDUSTRY_TREND_QUERIES:
            for marker in banned:
                self.assertNotIn(marker, q, msg=f"農林業固定クエリが混入: {q}")
            self.assertTrue(
                any(
                    k in q
                    for k in (
                        "経営",
                        "産業",
                        "企業",
                        "DX",
                        "AI",
                        "物流",
                        "ESG",
                        "人手不足",
                        "価格",
                        "投資",
                        "働き方",
                        "設備",
                        "サプライ",
                        "カーボン",
                        "製造",
                        "地方経済",
                    )
                ),
                msg=f"産業・経営トレンドに見えないクエリ: {q}",
            )

    def test_fallback_queries_are_not_forestry_news(self):
        for q in bot.INDUSTRY_TREND_FALLBACK_QUERIES:
            self.assertNotIn("林業", q)
            self.assertNotIn("国産材", q)
            self.assertNotIn("林野庁", q)

    def test_system_prompt_requires_forestry_insight_from_industry(self):
        prompt = bot.INDUSTRY_TREND_SYSTEM_PROMPT
        self.assertIn("産業・経営", prompt)
        self.assertIn("林業経営への示唆", prompt)
        self.assertIn("国内農林業ニュースの単なる紹介", prompt)

    def test_pre_evening_uses_industry_generator(self):
        with patch.object(
            bot,
            "fetch_todays_buzz_article",
            return_value=("中小企業のDXが進む", "生産性の話", "https://example.com/biz"),
        ), patch.object(
            bot, "generate_industry_trend_tweet", return_value="示唆付き本文です。"
        ) as mock_industry, patch.object(
            bot, "generate_buzz_insight_tweet"
        ) as mock_domestic, patch.object(bot, "post_to_x", return_value=True):
            bot.pre_evening_job()
            mock_industry.assert_called_once_with("中小企業のDXが進む", "生産性の話")
            mock_domestic.assert_not_called()

    def test_pre_evening_fallback_stays_on_industry_queries(self):
        with patch.object(bot, "fetch_todays_buzz_article", return_value=(None, None, None)), \
             patch.object(
                 bot,
                 "fetch_forestry_news",
                 side_effect=[
                     ("", None),
                     ("人手不足と自動化", "https://example.com/fb"),
                 ],
             ) as mock_fetch, \
             patch.object(bot, "generate_industry_trend_tweet", return_value="代替本文です。") as mock_gen, \
             patch.object(bot, "post_to_x", return_value=True):
            bot.pre_evening_job()
            used_queries = [call.args[0] for call in mock_fetch.call_args_list]
            for q in used_queries:
                self.assertIn(q, bot.INDUSTRY_TREND_FALLBACK_QUERIES)
            mock_gen.assert_called_once()


if __name__ == "__main__":
    unittest.main()
