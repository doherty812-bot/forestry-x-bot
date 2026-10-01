# 林業X投稿ボット（人間承認制）

岸本一夫さんのX向け。**自動でXに投げません。** OpenAI と Grok で二系統の**スレッド案**を作り、所有者が選んだ案だけを投稿します。

## 設計（要約）

1. Actions「林業X下書き生成」または `draft 12:00|20:00` … **複数ソース** → 二系統スレッド案（投稿しない）
2. 各候補に **anti-AI 校正レビュー**（第二 LLM）を通し、AIっぽい表現を削ってから artifact 化
3. 下書き成功後、Repo Secret `CURSOR_API_KEY` があれば **Cloud Agent が自動起動**し、スマホの Agents にレビューが届く
4. Cursor（**スマホアプリ可**）で両案のビートを確認し、`openai` / `grok` / `却下` と送る
5. エージェントが Actions「**林業X承認投稿**」を起動 → Secrets の `X_*` で **reply 連鎖スレッド**投稿（PC 操作不要）
6. 記事画像 URL が取れた場合は先頭ポストへ添付を試行（失敗しても本文は投稿）

下書き schedule には **X Secrets を渡さない**。承認 workflow だけが `confirm_live_post=true` 必須で投稿する。  
代替（上級者）: ローカル `CONFIRM_LIVE_POST=1 python forestry_bot.py approve ...`  
**623字一発の壁テキストは作らない。** 各ポストは短め（既定 `MAX_CHARS_PER_POST=260`）。

## 必要な環境変数

| 変数名 | 必須 | 用途 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 下書き | ChatGPT 系生成 |
| `XAI_API_KEY` または `GROK_API_KEY` | 下書き | Grok 生成（`XAI_API_KEY` 優先） |
| `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | 投稿時 | X API（投稿 + media upload） |
| `OPENAI_BASE_URL` / `XAI_BASE_URL` | 任意 | 互換エンドポイント |
| `OPENAI_MODEL` / `GROK_MODEL` | 任意 | モデル上書き |
| `MAX_CHARS_PER_POST` | 任意 | 各ポスト上限（既定 260） |
| `MAX_THREAD_POSTS` / `MIN_THREAD_POSTS` | 任意 | スレッド件数（既定 6 / 3） |
| `MAX_POST_CHARS` | 任意 | スレッド合計ソフト上限（既定 8000・後方互換） |
| `MAX_SOURCES` | 任意 | 下書きに付けるソース数 1〜5（既定 3） |
| `MEDIA_SOFT_FAIL` | 任意 | 画像失敗で本文続行（既定 1） |
| `MEDIA_ENRICH` | 任意 | 下書き時の og:image 取得（既定 1） |
| `ANTI_AI_COPYEDIT` | 任意 | AIっぽさ校正レビュー（既定 1）。失敗時は原文 + soft-fail flags |
| `ANTI_AI_COPYEDIT_PROVIDER` | 任意 | `same`（候補と同じ・既定） / `openai` / `grok` |
| `ANTI_AI_COPYEDIT_ON_FLAGS_ONLY` | 任意 | `1` なら検知時のみ LLM 校正（既定 0=毎回） |
| `ANTI_AI_COPYEDIT_TEMPERATURE` | 任意 | 校正 LLM 温度（既定 0.35） |
| `OBSIDIAN_VAULT_PATH` | 任意 | ローカル Obsidian vault（Windows 既定あり）。Actions は repo `obsidian/`（公開可ノートのみコミット）を優先 |
| `OBSIDIAN_MISSING_POLICY` | 任意 | `warn`（既定・空で続行）または `fail` |
| `CONFIRM_LIVE_POST` | 投稿時 | `1` のときのみ `approve` 可 |

## ローカル

```bash
pip install -r requirements.txt
cp .env.example .env   # 値はローカルのみ

python forestry_bot.py draft 12:00
python forestry_bot.py list-drafts
python forestry_bot.py show-draft <draft_id>

# 人間が選んだ案だけ投稿（スレッド）
export CONFIRM_LIVE_POST=1
python forestry_bot.py approve <draft_id> openai   # または grok
unset CONFIRM_LIVE_POST
```

詳細:
- 定刻のスマホ通知: Project の `docs/cursor-mobile-draft-notify.md`（`CURSOR_API_KEY`）
- スマホ承認: Project の `docs/mobile-cursor-approve.md`
- 二系統運用: Project の `docs/dual-ai-approval-flow.md`
- スレッド投稿: Project の `docs/thread-style-posts.md`
- 文体・Obsidian: Project の `docs/voice-and-obsidian.md`（Actions は方法1: 公開可ノートを `obsidian/` へ）
- AIっぽさ校正: Project の `docs/anti-ai-copyedit.md`
- 公開ノート同期: `obsidian/README.md` / `scripts/sync_obsidian_notes.md`

エージェントからの承認起動（PAT 必須・値は Secrets）:

```bash
# 権限確認のみ（投稿しない）
scripts/dispatch_approve.sh --check-auth
# ユーザーが openai|grok を選んだあと（本番）
scripts/dispatch_approve.sh --draft-id <ID> --provider grok --confirm
```

## コンテンツ枠

- **12:00**: 国内農林業・木材系の**複数ソース** × 現場コメント（スレッド）
- **20:00**: 産業・経営トレンドの**複数ソース** × 林業への示唆（スレッド）

固定タグ: `#林業 #forest`（最終ポストのみ）
