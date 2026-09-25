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
from privacy_guard import check_privacy_risk, KNOWN_FULL_NAME_TOKENS
from prose_guard import check_prose_risk, normalize_prose_breaks, MAX_SENTENCE_CHARS


class TestNormalizeProseBreaks(unittest.TestCase):
    def test_does_not_force_newline_after_period(self):
        text = "一文です。二文です。"
        out = bot.enforce_linebreaks(text)
        self.assertEqual(out, "一文です。二文です。")
        self.assertNotIn("。\n", out)

    def test_joins_one_sentence_per_line(self):
        text = "一文です。\n二文です。\n三文です。"
        out = normalize_prose_breaks(text)
        self.assertEqual(out, "一文です。二文です。三文です。")

    def test_keeps_paragraph_break_between_dense_blocks(self):
        text = "一段落の一文です。続く二文です。\n\n二段落の一文です。続く二文です。"
        out = normalize_prose_breaks(text)
        self.assertIn("\n\n", out)
        self.assertEqual(out.count("\n\n"), 1)

    def test_collapses_extra_blank_lines(self):
        # 連続空行は圧縮され、短い一文段落どうしは密度のため結合される
        text = "前です。\n\n\n\n後です。"
        out = normalize_prose_breaks(text)
        self.assertEqual(out, "前です。後です。")
        self.assertNotIn("\n\n\n", out)

    def test_merges_sparse_single_sentence_paragraphs(self):
        text = "一文です。\n\n二文です。\n\n三文です。"
        out = normalize_prose_breaks(text)
        self.assertNotIn("\n\n", out)
        self.assertEqual(out, "一文です。二文です。三文です。")

    def test_does_not_join_hashtag_line(self):
        text = "本文です。次です。\n#林業 #森林 #forest"
        out = normalize_prose_breaks(text)
        self.assertIn("#林業", out)
        self.assertTrue(out.endswith("#林業 #森林 #forest"))


class TestBuildTweetPayload(unittest.TestCase):
    def test_url_and_hashtags(self):
        body = "山を経営資源として見る視点が大切です。\n現場の感覚も忘れません。"
        url = "https://news.google.com/articles/example"
        full, clean = bot.build_tweet_payload(body, url)
        self.assertIn(bot.HASHTAGS, full)
        self.assertIn(url, full)
        # 1文1行は結合される
        self.assertEqual(clean, "山を経営資源として見る視点が大切です。現場の感覚も忘れません。")

    def test_multiple_urls(self):
        body = "複数ソースを踏まえたコメントです。"
        urls = ["https://ex.com/a", "https://ex.com/b"]
        full, _ = bot.build_tweet_payload(body, urls)
        self.assertIn("https://ex.com/a", full)
        self.assertIn("https://ex.com/b", full)

    def test_long_body_not_cut_at_140(self):
        body = "あ" * 500
        full, _ = bot.build_tweet_payload(body, "https://ex.com/x")
        self.assertGreater(len(full), 140)
        self.assertIn("あ" * 100, full)

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


class TestPrivacyGuard(unittest.TestCase):
    def test_flags_full_name(self):
        name = KNOWN_FULL_NAME_TOKENS[0]
        ok, flags = check_privacy_risk(f"私は{name}です。現場を見ています。")
        self.assertFalse(ok)
        self.assertTrue(any("pii_full_name" in f for f in flags))

    def test_flags_email_and_gmail(self):
        ok, flags = check_privacy_risk("連絡は example.user@gmail.com まで。")
        self.assertFalse(ok)
        self.assertIn("pii_email", flags)

    def test_flags_phone(self):
        ok, flags = check_privacy_risk("電話は 025-123-4567 です。")
        self.assertFalse(ok)
        self.assertTrue(any("pii_phone" in f for f in flags))

    def test_flags_office_token(self):
        ok, flags = check_privacy_risk("事務所に問い合わせください。")
        self.assertFalse(ok)
        self.assertTrue(any("pii_office_contact" in f for f in flags))

    def test_allows_business_name_without_office(self):
        ok, flags = check_privacy_risk(
            "有限会社丸実として工場向け販売を続けます。\n私は現場でこのように考えます。"
        )
        self.assertTrue(ok)
        self.assertEqual(flags, [])

    def test_allows_normal_field_comment(self):
        ok, flags = check_privacy_risk(
            "1,500haの計画を軸に、人手不足へは機械化で応えます。\n私はこのように進めます。"
        )
        self.assertTrue(ok)
        self.assertEqual(flags, [])


class TestProseGuard(unittest.TestCase):
    def test_flags_long_sentence(self):
        long = "あ" * (MAX_SENTENCE_CHARS + 5) + "。"
        ok, flags = check_prose_risk(long)
        self.assertTrue(ok)  # 承認はブロックしない
        self.assertTrue(any(f.startswith("long_sentence:") for f in flags))

    def test_flags_excessive_linebreaks(self):
        text = "一文です。\n二文です。\n三文です。\n四文です。"
        ok, flags = check_prose_risk(text)
        self.assertTrue(ok)
        self.assertIn("excessive_linebreaks", flags)

    def test_allows_dense_short_prose(self):
        text = "現場の感覚を大事にします。私は機械化で人手不足に応えます。"
        ok, flags = check_prose_risk(text)
        self.assertTrue(ok)
        self.assertEqual(flags, [])

    def test_dual_candidates_keeps_prose_flags_without_failing_guard(self):
        sparse = "一文です。\n二文です。\n三文です。\n四文です。"

        def gen(sources, provider="openai"):
            return sparse

        sources = [{"title": "t", "snippet": "s", "url": "https://ex.com"}]
        cands = bot.build_dual_candidates(sources, gen)
        for p in ("openai", "grok"):
            self.assertTrue(cands[p]["guard_ok"])
            self.assertIn("excessive_linebreaks", cands[p]["flags"])
            # 保存本文は正規化済み
            self.assertNotIn("\n", cands[p]["text"])


class TestLivePostGuard(unittest.TestCase):
    def test_requires_confirmation(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONFIRM_LIVE_POST", None)
            with self.assertRaises(RuntimeError):
                bot.require_live_post_confirmation()


class TestDraftOnlyJobs(unittest.TestCase):
    def test_noon_does_not_post(self):
        sources = [
            {"title": "題1", "snippet": "概要1", "url": "https://ex.com/a", "label": "木材新聞"},
            {"title": "題2", "snippet": "概要2", "url": "https://ex.com/b", "label": "林野庁"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(draft_store, "DRAFTS_DIR", Path(tmp)), \
                 patch.object(bot, "collect_noon_sources", return_value=sources), \
                 patch.object(bot, "generate_buzz_insight_tweet", side_effect=lambda src, provider="openai": f"{provider}-本文です。"), \
                 patch.object(bot, "post_to_x") as mock_post:
                draft = bot.noon_job()
                mock_post.assert_not_called()
                self.assertEqual(draft["status"], "pending")
                self.assertEqual(len(draft["sources"]), 2)
                self.assertEqual(len(draft["urls"]), 2)
                self.assertIn("openai", draft["candidates"])
                self.assertIn("grok", draft["candidates"])
                self.assertTrue(Path(tmp, f"{draft['id']}.json").exists())

    def test_evening_uses_industry_generator(self):
        sources = [{"title": "DX", "snippet": "生産性", "url": "https://ex.com/b", "label": "経営"}]
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(draft_store, "DRAFTS_DIR", Path(tmp)), \
                 patch.object(bot, "collect_evening_sources", return_value=sources), \
                 patch.object(bot, "generate_industry_trend_tweet", side_effect=lambda src, provider="openai": f"{provider}-示唆です。") as mock_gen, \
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
                    "urls": ["https://ex.com/n", "https://ex.com/m"],
                    "candidates": {
                        "openai": {"text": "OpenAI案です。", "guard_ok": True, "flags": []},
                        "grok": {"text": "Grok案です。", "guard_ok": True, "flags": []},
                    },
                }
                draft_store.save_draft(draft)
                bot.approve_and_post("testdraft2", "grok")
                mock_post.assert_called_once_with(
                    "Grok案です。", ["https://ex.com/n", "https://ex.com/m"]
                )
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
        prompt = bot._industry_system_prompt()
        self.assertIn("ミスリード防止", prompt)
        self.assertIn("Premium", prompt)

    def test_system_prompt_has_privacy_guard(self):
        prompt = bot._industry_system_prompt()
        self.assertIn("個人情報・連絡先", prompt)
        self.assertIn("氏名フル", prompt)
        self.assertIn("事務所", prompt)
        self.assertIn(bot.PRIVACY_GUARD_PROMPT.strip().splitlines()[0], prompt)

    def test_noon_system_prompt_has_privacy_guard(self):
        sources = [{"title": "題", "snippet": "概要", "url": "https://ex.com", "label": "x"}]
        captured = {}

        def fake_chat(provider, system_prompt, user_content, temperature=0.75):
            captured["system"] = system_prompt
            captured["user"] = user_content
            return "私は現場でこのように考えます。\n#林業 #森林 #forest"

        with patch.object(bot, "chat_complete", side_effect=fake_chat):
            bot.generate_buzz_insight_tweet(
                sources,
                provider="openai",
                obsidian_context={"text": "", "status": "missing", "path": None, "files_used": [], "warning": "x"},
            )
        self.assertIn("個人情報・連絡先", captured["system"])
        self.assertIn("氏名フル・メール・事務所", captured["user"])

    def test_system_prompt_first_person_not_reader_questions(self):
        prompt = bot._industry_system_prompt()
        self.assertIn("一人称", prompt)
        self.assertIn("問いかけは禁止", prompt)
        self.assertIn("私は〜と考えます", prompt)
        self.assertIn(bot.VOICE_FIRST_PERSON_PROMPT.strip().splitlines()[0], prompt)

    def test_system_prompt_mobile_prose_rules(self):
        prompt = bot._industry_system_prompt()
        self.assertIn("スマホ向け", prompt)
        self.assertIn("改行を取りすぎない", prompt)
        self.assertIn("1文ごとに行をバラさない", prompt)
        self.assertIn("短め", prompt)
        voice = bot.VOICE_FIRST_PERSON_PROMPT
        self.assertIn("改行を取りすぎない", voice)
        self.assertNotIn("1文ごとに改行", voice)

    def test_dual_candidates_merge_privacy_flags(self):
        name = KNOWN_FULL_NAME_TOKENS[0]

        def gen(sources, provider="openai"):
            return f"{provider}: 私は{name}です。現場を見ています。"

        sources = [{"title": "t", "snippet": "s", "url": "https://ex.com"}]
        cands = bot.build_dual_candidates(sources, gen)
        for p in ("openai", "grok"):
            self.assertFalse(cands[p]["guard_ok"])
            self.assertTrue(any("pii_full_name" in f for f in cands[p]["flags"]))


class TestObsidianContext(unittest.TestCase):
    def test_missing_vault_warns_and_continues(self):
        with patch.dict(
            os.environ,
            {"OBSIDIAN_MISSING_POLICY": "warn"},
            clear=False,
        ), patch.object(bot, "resolve_obsidian_vault_path", return_value=None):
            ctx = bot.load_obsidian_context()
            self.assertEqual(ctx["status"], "missing")
            self.assertEqual(ctx["text"], "")
            self.assertTrue(ctx["warning"])

    def test_missing_vault_fail_raises(self):
        with patch.dict(os.environ, {"OBSIDIAN_MISSING_POLICY": "fail"}, clear=False), \
             patch.object(bot, "resolve_obsidian_vault_path", return_value=None):
            with self.assertRaises(RuntimeError):
                bot.load_obsidian_context()

    def test_loads_recent_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp)
            (vault / ".obsidian").mkdir()
            (vault / ".obsidian" / "app.md").write_text("secret-config", encoding="utf-8")
            note = vault / "経営メモ.md"
            note.write_text("私は3,000ha拡大を見据えています。\n", encoding="utf-8")
            with patch.object(bot, "REPO_OBSIDIAN_DIRS", ()), patch.dict(
                os.environ,
                {"OBSIDIAN_VAULT_PATH": str(vault), "OBSIDIAN_MISSING_POLICY": "warn"},
                clear=False,
            ):
                ctx = bot.load_obsidian_context()
            self.assertEqual(ctx["status"], "ok")
            self.assertIn("3,000ha", ctx["text"])
            self.assertIn("経営メモ.md", ctx["files_used"])
            self.assertNotIn("secret-config", ctx["text"])

    def test_resolve_prefers_repo_sync_with_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sync = root / "obsidian"
            sync.mkdir()
            (sync / "note.md").write_text("repo sync note\n", encoding="utf-8")
            local = root / "local-vault"
            local.mkdir()
            (local / "other.md").write_text("local only\n", encoding="utf-8")
            old = os.getcwd()
            try:
                os.chdir(root)
                with patch.object(bot, "REPO_OBSIDIAN_DIRS", ("obsidian", "vault-sync")), \
                     patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(local)}, clear=False):
                    resolved = bot.resolve_obsidian_vault_path()
                self.assertEqual(resolved, sync.resolve())
            finally:
                os.chdir(old)

    def test_resolve_uses_env_when_repo_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            vault.mkdir()
            with patch.object(bot, "REPO_OBSIDIAN_DIRS", ()), patch.dict(
                os.environ, {"OBSIDIAN_VAULT_PATH": str(vault)}, clear=False
            ):
                resolved = bot.resolve_obsidian_vault_path()
            self.assertEqual(resolved, vault.resolve())

    def test_default_windows_path_constant(self):
        self.assertEqual(
            bot.DEFAULT_OBSIDIAN_VAULT_PATH,
            r"C:\Users\info\Obsidian Vault",
        )
        self.assertNotIn("OneDrive", bot.DEFAULT_OBSIDIAN_VAULT_PATH)

    def test_generator_includes_obsidian_in_user_prompt(self):
        sources = [{"title": "題", "snippet": "概要", "url": "https://ex.com", "label": "x"}]
        captured = {}

        def fake_chat(provider, system_prompt, user_content, temperature=0.75):
            captured["system"] = system_prompt
            captured["user"] = user_content
            return "私は現場でこのように考えます。\n#林業 #森林 #forest"

        obsidian = {
            "text": "### memo.md\n拡大計画のメモ",
            "status": "ok",
            "path": "/tmp/vault",
            "files_used": ["memo.md"],
            "warning": None,
        }
        with patch.object(bot, "chat_complete", side_effect=fake_chat):
            text = bot.generate_industry_trend_tweet(
                sources, provider="openai", obsidian_context=obsidian
            )
        self.assertIn("私は現場で", text)
        self.assertIn("一人称", captured["system"])
        self.assertIn("問いかけは禁止", captured["system"])
        self.assertIn("拡大計画のメモ", captured["user"])
        self.assertIn("Obsidian", captured["user"])

    def test_noon_generator_also_first_person(self):
        sources = [{"title": "題", "snippet": "概要", "url": "https://ex.com", "label": "x"}]
        captured = {}

        def fake_chat(provider, system_prompt, user_content, temperature=0.75):
            captured["system"] = system_prompt
            return "私は工場向け販売を軸に考えます。\n#林業 #森林 #forest"

        with patch.object(bot, "chat_complete", side_effect=fake_chat):
            bot.generate_buzz_insight_tweet(
                sources,
                provider="grok",
                obsidian_context={"text": "", "status": "missing", "path": None, "files_used": [], "warning": "x"},
            )
        self.assertIn("問いかけは禁止", captured["system"])
        self.assertIn("私は〜と考えます", captured["system"])

    def test_create_dual_draft_records_obsidian_meta(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(draft_store, "DRAFTS_DIR", Path(tmp)), \
                 patch.object(
                     bot,
                     "load_obsidian_context",
                     return_value={
                         "text": "",
                         "status": "missing",
                         "path": None,
                         "files_used": [],
                         "warning": "no vault",
                     },
                 ):
                sources = [{"title": "題", "snippet": "概要", "url": "https://ex.com", "label": "x"}]
                draft = bot.create_dual_draft(
                    "20:00",
                    sources,
                    lambda src, provider="openai": f"{provider} 私はこう考えます。",
                )
                self.assertEqual(draft["obsidian"]["status"], "missing")
                self.assertEqual(draft["status"], "pending")


class TestEnvModelDefaults(unittest.TestCase):
    def test_empty_openai_model_secret_uses_default(self):
        with patch.dict(os.environ, {"OPENAI_MODEL": "", "GROK_MODEL": "  "}):
            self.assertEqual(bot.env_or_default("OPENAI_MODEL", "gpt-4.1-mini"), "gpt-4.1-mini")
            self.assertEqual(bot.get_openai_model(), bot.DEFAULT_OPENAI_MODEL)
            self.assertEqual(bot.get_grok_model(), bot.DEFAULT_GROK_MODEL)

    def test_explicit_model_is_kept(self):
        with patch.dict(os.environ, {"GROK_MODEL": "grok-4-fast"}):
            self.assertEqual(bot.get_grok_model(), "grok-4-fast")

    def test_max_post_chars_default_is_relaxed(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MAX_POST_CHARS", None)
            self.assertGreaterEqual(bot.get_max_post_chars(), 2000)


class TestDualCandidateErrors(unittest.TestCase):
    def test_errors_are_recorded_not_empty_text_only(self):
        def boom(sources, provider="openai"):
            raise RuntimeError(f"{provider} API呼び出し失敗 (model=): missing")

        sources = [{"title": "t", "snippet": "s", "url": "https://ex.com"}]
        cands = bot.build_dual_candidates(sources, boom)
        for p in ("openai", "grok"):
            self.assertIsNone(cands[p]["text"])
            self.assertIn("generation_error", cands[p]["flags"])
            self.assertTrue(cands[p]["error"])
            self.assertNotIn("empty_text", cands[p]["flags"])

    def test_both_fail_marks_draft_failed_and_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(draft_store, "DRAFTS_DIR", Path(tmp)):
                def boom(sources, provider="openai"):
                    raise RuntimeError(f"{provider} down")

                sources = [{"title": "題", "snippet": "概要", "url": "https://ex.com", "label": "x"}]
                with self.assertRaises(RuntimeError) as ctx:
                    bot.create_dual_draft("12:00", sources, boom)
                self.assertIn("両方", str(ctx.exception))
                drafts = list(Path(tmp).glob("*.json"))
                self.assertEqual(len(drafts), 1)
                import json
                data = json.loads(drafts[0].read_text(encoding="utf-8"))
                self.assertEqual(data["status"], "failed")
                self.assertTrue(data["candidates"]["openai"]["error"])
                self.assertTrue(data["candidates"]["grok"]["error"])


class TestMultiSourceCatalog(unittest.TestCase):
    def test_noon_catalog_includes_mokuzai_shimbun(self):
        labels = [x[0] for x in bot.NOON_SOURCE_QUERIES]
        self.assertIn("木材新聞", labels)
        self.assertIn("林野庁", labels)

    def test_collect_sources_dedupes_and_caps(self):
        fake_catalog = [
            ("A", "query-a"),
            ("B", "query-b"),
            ("C", "query-c"),
        ]

        def fake_parse(query, limit=2):
            return [{
                "title": f"t-{query}",
                "url": f"https://ex.com/{query}",
                "source": "S",
                "snippet": "snip",
                "query": query,
            }]

        with patch.object(bot, "_parse_rss_items", side_effect=fake_parse), \
             patch.object(bot, "get_max_sources", return_value=2):
            sources = bot.collect_sources_from_catalog(fake_catalog, "test")
            self.assertEqual(len(sources), 2)
            urls = [s["url"] for s in sources]
            self.assertEqual(len(urls), len(set(urls)))


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
