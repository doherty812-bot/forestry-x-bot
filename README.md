# 林業X自動投稿ボット

岸本一夫さんのXアカウント向けに、林業関連の投稿を自動生成・投稿するボットです。

## 現状（引継ぎキックオフ後）

- GitHub Actions の **「林業X自動投稿」は Disable 済み**（定期実行なし）
- ワークフロー定義から `schedule` を外し、`workflow_dispatch` のみ
- ソースコードに X 認証情報の既定値は **置かない**（環境変数必須）
- 記事URL未取得・投稿失敗時は **非ゼロ終了**（Actions を赤くする）
- 実投稿CLIは `CONFIRM_LIVE_POST=1` が必要

再開前チェックリストは `CURSOR_HANDOVER.md` を参照してください。

## スタック

| 項目 | 内容 |
| --- | --- |
| 実行 | GitHub Actions（Ubuntu / Python 3.11） |
| 投稿 | Tweepy / X API v2 / OAuth 1.0a |
| 生成 | OpenAI SDK（`gpt-4.1-mini`） |
| 記事 | Google News RSS |
| 本番枠 | JST 12:00 / 20:00（再開時に cron を戻す） |

## 必要な環境変数

`.env.example` と同名を GitHub Secrets に設定します。

| 変数名 | 必須 | 説明 |
| --- | --- | --- |
| `X_API_KEY` | Yes | X API Key |
| `X_API_SECRET` | Yes | X API Secret |
| `X_ACCESS_TOKEN` | Yes | Access Token |
| `X_ACCESS_TOKEN_SECRET` | Yes | Access Token Secret |
| `OPENAI_API_KEY` | Yes | OpenAI API Key |
| `OPENAI_BASE_URL` | No | 互換エンドポイント用 |
| `CONFIRM_LIVE_POST` | 実投稿時 | `1` のときのみ実投稿CLIを許可 |

漏えいの可能性がある旧資格情報は **失効・再発行** してから Secrets を更新してください。値をコード・issue・チャットに書かないでください。

## ローカル

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # 値はローカルのみに記入

# 実投稿なし（本文組み立て）
python forestry_bot.py dry-run-format

# ユニットテスト（外部APIなし）
python -m unittest discover -s tests -v
```

実投稿は所有者承認後のみ:

```bash
export CONFIRM_LIVE_POST=1
# 必要な X_* / OPENAI_* を export したうえで
python forestry_bot.py 12:00
```

## コンテンツ枠

- **12:00 JST**: 国内農林業ニュース × 現場実務コメント
- **20:00 JST**: 産業・経営トレンド × 林業経営への示唆（国内農林業固定ではない）

固定タグ: `#林業 #forest`
