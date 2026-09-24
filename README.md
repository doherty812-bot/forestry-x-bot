# 林業X投稿ボット（人間承認制）

岸本一夫さんのX向け。**自動でXに投げません。** OpenAI と Grok で二系統の案を作り、所有者が選んだ案だけを投稿します。

## 設計（要約）

1. Actions「林業X下書き生成」または `draft 12:00|20:00` … **複数ソース** → 二系統案（投稿しない）
2. Cursor（**スマホアプリ可**）で両案を確認し、`openai` / `grok` / `却下` と送る
3. エージェントが Actions「**林業X承認投稿**」を起動 → Secrets の `X_*` で投稿（PC 操作不要）

下書き schedule には **X Secrets を渡さない**。承認 workflow だけが `confirm_live_post=true` 必須で投稿する。  
代替（上級者）: ローカル `CONFIRM_LIVE_POST=1 python forestry_bot.py approve ...`  
X Premium 前提の長文可（既定ソフト上限 **8000** 文字、`MAX_POST_CHARS` で変更）。

## 必要な環境変数

| 変数名 | 必須 | 用途 |
| --- | --- | --- |
| `OPENAI_API_KEY` | 下書き | ChatGPT 系生成 |
| `XAI_API_KEY` または `GROK_API_KEY` | 下書き | Grok 生成（`XAI_API_KEY` 優先） |
| `X_API_KEY` / `X_API_SECRET` / `X_ACCESS_TOKEN` / `X_ACCESS_TOKEN_SECRET` | 投稿時 | X API |
| `OPENAI_BASE_URL` / `XAI_BASE_URL` | 任意 | 互換エンドポイント |
| `OPENAI_MODEL` / `GROK_MODEL` | 任意 | モデル上書き |
| `MAX_POST_CHARS` | 任意 | 投稿ソフト上限（既定 8000） |
| `MAX_SOURCES` | 任意 | 下書きに付けるソース数 1〜5（既定 3） |
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

詳細:
- スマホ承認: Project の `docs/mobile-cursor-approve.md`
- 二系統運用: Project の `docs/dual-ai-approval-flow.md`

エージェントからの承認起動（PAT 必須・値は Secrets）:

```bash
# 権限確認のみ（投稿しない）
scripts/dispatch_approve.sh --check-auth
# ユーザーが openai|grok を選んだあと（本番）
scripts/dispatch_approve.sh --draft-id <ID> --provider grok --confirm
```

## コンテンツ枠

- **12:00**: 国内農林業・木材系の**複数ソース** × 現場コメント
- **20:00**: 産業・経営トレンドの**複数ソース** × 林業への示唆

固定タグ: `#林業 #forest`
