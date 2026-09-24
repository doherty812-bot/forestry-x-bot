# 林業X投稿ボット（人間承認制）

岸本一夫さんのX向け。**自動でXに投げません。** OpenAI と Grok で二系統の案を作り、所有者が選んだ案だけを投稿します。

## 設計（要約）

1. `draft 12:00|20:00` … 記事取得 → ChatGPT（OpenAI）案 + Grok 案を `drafts/` に保存（投稿しない）
2. Cursor 等で両案を確認（ミスリード警告フラグも表示）
3. `CONFIRM_LIVE_POST=1 python forestry_bot.py approve <id> openai|grok` … 選択した案のみ投稿

GitHub Actions の schedule も **下書き生成のみ**（X Secrets を渡さない）。

## 必要な環境変数

| 変数名 | 必須 | 用途 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 下書き | ChatGPT 系生成 |
| `XAI_API_KEY` または `GROK_API_KEY` | 下書き | Grok 生成（`XAI_API_KEY` 優先） |
| `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | 投稿時 | X API |
| `OPENAI_BASE_URL` / `XAI_BASE_URL` | 任意 | 互換エンドポイント |
| `OPENAI_MODEL` / `GROK_MODEL` | 任意 | モデル上書き |
| `CONFIRM_LIVE_POST` | 投稿時 | `1` のときのみ `approve` 可 |

## ローカル

```bash
pip install -r requirements.txt
cp .env.example .env   # 値はローカルのみ

python forestry_bot.py draft 12:00
python forestry_bot.py list-drafts
python forestry_bot.py show-draft <draft_id>

# 人間が選んだ案だけ投稿
export CONFIRM_LIVE_POST=1
python forestry_bot.py approve <draft_id> openai   # または grok
unset CONFIRM_LIVE_POST
```

詳細: Project の `docs/dual-ai-approval-flow.md`

## コンテンツ枠

- **12:00**: 国内農林業ニュース × 現場コメント
- **20:00**: 産業・経営トレンド × 林業への示唆

固定タグ: `#林業 #forest`
