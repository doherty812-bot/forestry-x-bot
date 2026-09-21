# 林業X自動投稿ボット：Cursor引継ぎ資料

**作成日:** 2026-09-21（JST）  
**対象リポジトリ:** `doherty812-bot/forestry-x-bot`  
**確認済みの `main` HEAD（引継ぎ時点）:** `dc02f521b0765aee0a369d37647d746031ea65ce`（`dc02f52`）

## キックオフ進捗（Cursor / 2026-09-21）

| 項目 | 状態 |
| --- | --- |
| GitHub Actions「林業X自動投稿」 | **Disable 済み**（API確認: `disabled_manually`） |
| `schedule` cron | ワークフロー定義から削除（`workflow_dispatch` のみ） |
| ソース内 X 認証の既定値 | **削除済み**（環境変数必須） |
| URL未取得 / 投稿失敗 | `RuntimeError` で非ゼロ終了 |
| 実投稿CLIガード | `CONFIRM_LIVE_POST=1` 必須 |
| 夜20時コンテンツ方針 | **決定済み: 産業・経営トレンド**（国内農林業固定ではない） |
| 資格情報の失効・再発行 | **所有者作業待ち**（コード修正だけでは不十分） |

再開用 cron（参考）: `0 3 * * *` = JST 12:00、`0 11 * * *` = JST 20:00。再開時に Secrets 更新・方針確定・受入条件クリア後に戻す。

---

## 結論と最優先事項

このボットはGitHub Actionsにより、毎日 **JST 12:00** と **JST 20:00** にXへ投稿する構成です。引継ぎに伴う**配信停止が最優先**です。引継ぎ時点の確認では、ワークフローは有効で、最新表示は **Run #630** まで進んでいました。**その後ワークフローは Disable 済み**です。再開しない限り定期実行は発生しません。[1]

> **安全上の注意:** 過去の `forestry_bot.py` には、環境変数の既定値としてXの認証情報が直接記述されていました。リポジトリはPublic表示です。この情報は漏えい済みとして扱い、X・OpenAI・GitHubの認証情報を失効・再発行してください。引継ぎ文書、issue、コミット、チャットには実際の値を残さないでください。コード上の既定値削除は完了していますが、**ローテーションは別途必須**です。

## 1. まず実施する停止操作

GitHubの `Actions` タブを開き、左側の **「林業X自動投稿」** を選択します。画面右上の **「…」または Workflow options** から **「Disable workflow」** を実行してください。これにより、定期実行と手動実行の両方が止まります。停止後は同じ画面で、ワークフローが無効状態であることを確認します。[1]

UI操作が使えない場合は、`main` 上の `.github/workflows/post.yml` から `schedule:` ブロックを削除し、`workflow_dispatch:` のみを残して通常のコミットとしてpushします。これは定期実行だけを止め、再開後の手動テストの入口は残す方法です。ただし、引継ぎ中は誤投稿を避けるため、**ワークフロー全体を無効化する方法を優先**してください。[2]

停止後に確認すべき点は、次の二つです。第一に、次のJST 12:00または20:00の時刻をまたいでも新しい `Scheduled` run が作られないことです。第二に、Cursor側で `test_all`、`test`、`test_quote`、`12:00`、`20:00` の各引数を付けてスクリプトを実行しないことです。これらは実際の投稿処理へ進みます。[3]

## 2. 現在の構成

| 項目 | 現在の実装 | 補足 |
| --- | --- | --- |
| 実行基盤 | GitHub Actions | `main` の `.github/workflows/post.yml` |
| 定期時刻 | UTC 03:00 / 11:00 | JSTでは12:00 / 20:00 |
| 実行環境 | Ubuntu runner、Python 3.11 | 実行時に `tweepy`、`openai`、`requests`、`beautifulsoup4`、`schedule` をインストール |
| X投稿 | Tweepy `Client.create_tweet`、X API v2、OAuth 1.0a | X APIの利用可能残高不足時には過去にHTTP 402が発生 |
| 文章生成 | OpenAI Python SDK、`gpt-4.1-mini` | `OPENAI_API_KEY` と任意の `OPENAI_BASE_URL` を使用 |
| 記事取得 | Google News RSS | 日本語・国内向けRSSのリダイレクトURLを投稿末尾へ添付 |
| 固定タグ | `#林業 #forest` | `post_to_x()` が生成文中のタグを除き、末尾に強制付与 |
| 文体 | 日本語、です・ます調、句点後改行 | 「〜だろう」「〜だな」「〜かな」「〜ですな」は禁止 |

現行ワークフローは以下のcronを持ちます。`0 3 * * *` がJST 12:00、`0 11 * * *` がJST 20:00です。`workflow_dispatch` が残っているため、権限を持つ利用者はGitHub UIから任意のスロットを手動実行できます。[2]

## 3. コンテンツ仕様と実装の照合

昼12時の実装は、国内農林業ニュースをGoogle News RSSで取得し、林業経営者の現場目線のコメントを生成するものです。検索候補には「林業 国内 最新」「木材 市場 国産材」「森林 整備 政策」「林野庁 新しい林業」などが含まれます。[3]

夜20時は、所有者決定により **産業・経営トレンド × 林業経営への示唆** で固定しました。`fetch_todays_buzz_article()` は `INDUSTRY_TREND_QUERIES`、生成は `generate_industry_trend_tweet()` / `INDUSTRY_TREND_SYSTEM_PROMPT` を使います。昼12時の国内農林業枠（`generate_buzz_insight_tweet`）とは分離しています。[3]

投稿は記事URLを添える設計です。本文はURLを23文字換算して最大104文字程度に切り詰め、タグとURLを末尾に付けます。句点後の改行は `enforce_linebreaks()` で補正されます。[3]

## 4. 停止中に必ず解消する不具合とリスク

| 優先度 | 問題 | 現状の影響 | 再開前の対処 |
| --- | --- | --- | --- |
| 緊急 | X OAuth認証情報がソースコードの既定値に存在 | Publicリポジトリから取得され得る。アカウント不正利用の危険がある | XのAPI key/secret、access token/secretを失効・再発行し、コードの既定値を削除する。GitHub Secretsのみで供給する |
| 緊急 | 認証情報が過去の会話・作業履歴にも露出 | X、OpenAI、GitHubの各資格情報に漏えいリスクがある | OpenAI API keyとGitHubのPersonal Access Tokenも失効・再発行し、GitHub Secretsを更新する |
| 高 | X投稿失敗を例外にせず `False` で返す | X API 402などでもGitHub Actionsが成功（緑）に見える | 投稿失敗時には非ゼロ終了にして、runを失敗にする。投稿IDのログも確認する |
| 高 | URL取得が全再試行で失敗した時、URLなし投稿へ進める | 「必ず記事URLを添える」という要件を破る可能性が残る | `article_url` が空なら投稿せず、明示的にジョブを失敗させる |
| 中 | （解消）夜20時の要件と検索クエリの不整合 | 方針決定済み | 産業・経営トレンドで実装済み |
| 中 | 古い1日5回用の関数・定数・常駐スケジューラーが残存 | GitHub Actionsでは使わない処理が多く、誤テスト時に投稿する | 2枠に不要なコードを削除し、実行可能コマンドを最小化する |
| 中 | X API残高不足が過去に繰り返し発生 | 投稿が停止する。現行ではrun成功に見える場合がある | X Developer Consoleで自動チャージ設定と利用上限を確認し、402をジョブ失敗として通知可能にする |

### 認証情報の安全な修正例

`forestry_bot.py` の認証情報は、既定値を持たせず環境変数を必須にしてください。たとえば次の形式です。実際の値は絶対にコードへ書かず、GitHub Secretsにのみ登録します。

```python
X_API_KEY = os.environ["X_API_KEY"]
X_API_SECRET = os.environ["X_API_SECRET"]
X_ACCESS_TOKEN = os.environ["X_ACCESS_TOKEN"]
X_ACCESS_TOKEN_SECRET = os.environ["X_ACCESS_TOKEN_SECRET"]
OPENAI_API_KEY = os.environ["OPENAI_API_KEY"]
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")
```

コード修正後も、公開済みの履歴には古い値が残り得ます。値のローテーションを先に行えば、履歴消去の有無にかかわらず旧資格情報は無効です。履歴の削除やリポジトリの非公開化は、必要に応じて所有者が別途判断してください。

### 投稿失敗をワークフロー失敗にする修正方針

`post_to_x()` は成功時に `True`、例外時に `False` を返します。現状の `noon_job()` と `pre_evening_job()` は戻り値を検査しないため、例外が握りつぶされます。各ジョブでは、次の条件を満たさなければ `RuntimeError` を送出するよう統一してください。

```python
if not article_url:
    raise RuntimeError("記事URLが取得できないため投稿を中止しました")

if not tweet:
    raise RuntimeError("投稿文を生成できませんでした")

if not post_to_x(tweet, article_url):
    raise RuntimeError("Xへの投稿に失敗しました")
```

この形なら、X API 402、認証エラー、記事URL未取得、文章生成失敗がGitHub Actions上でも失敗として可視化されます。再開時はrunログに **`記事URL付き投稿:`** と **`投稿成功！ Tweet ID:`** が両方あることを成功基準にしてください。緑のrunだけで投稿成功と判断しないでください。[3]

## 5. ファイルと責務

| ファイル | 役割 | Cursorで見る箇所 |
| --- | --- | --- |
| `.github/workflows/post.yml` | GitHub Actionsのcron、Secrets注入、スロット判定 | `schedule`、`workflow_dispatch`、`python forestry_bot.py $SLOT` |
| `forestry_bot.py` | RSS取得、文章生成、X投稿、2枠のジョブ | `fetch_forestry_news()`、`fetch_todays_buzz_article()`、`generate_buzz_insight_tweet()`、`post_to_x()`、`noon_job()`、`pre_evening_job()` |
| `CURSOR_HANDOVER.md` | 本資料 | 停止後の改善・再開時のチェックリスト |

GitHub Secretsで必要な名前は `X_API_KEY`、`X_API_SECRET`、`X_ACCESS_TOKEN`、`X_ACCESS_TOKEN_SECRET`、`OPENAI_API_KEY`、`OPENAI_BASE_URL` です。最後の `OPENAI_BASE_URL` は任意ですが、ワークフローは常に渡します。[2]

## 6. Cursorでの安全な引継ぎ手順

まず、通常のcloneを行い、作業前に `main` の最新状態を取得します。履歴の書き換えやforce pushは使わず、変更ごとに通常のコミットとpushを使ってください。

```bash
git clone https://github.com/doherty812-bot/forestry-x-bot.git
cd forestry-x-bot
git checkout main
git pull --ff-only origin main
```

停止確認後、認証情報の削除と投稿失敗の非ゼロ終了化を1つの安全な修正としてコミットします。その後、実投稿を伴わないテストを先に整備してください。外部APIをモックして、URL未取得時に `create_tweet` が呼ばれないこと、X投稿の例外がプロセス失敗になること、URL・タグ・改行の組合せが文字数上限に収まることを確認します。

実投稿テストが必要な場合は、停止状態を解除する前に所有者の明示的な承認を得ます。テストに使うXアカウント、実投稿の文面、実行時刻を事前に合意してください。特に `test_all` は複数の投稿処理を呼ぶため、通常の検証には使用しません。[3]

## 7. 再開前の受入条件

再開するのは、以下をすべて満たした後です。

1. X、OpenAI、GitHubの漏えい可能性がある資格情報を失効・再発行し、コードから実値を削除していること。
2. GitHub Actionsが停止中であり、意図した再開日時までScheduled runが発生しないこと。
3. 記事URLが取れない場合は**投稿を中止してワークフローを失敗**にするテストが通ること。
4. X APIエラーがGitHub Actionsの失敗として表示されること。
5. 昼12時と夜20時のコンテンツ方針を確定し、実装・プロンプト・docstringが同じ仕様を表していること。
6. GitHub Secretsが新しい資格情報で更新され、X Developer Consoleの残高・自動チャージ・上限が確認されていること。
7. 手動検証でログに `記事URL付き投稿:` と `投稿成功！ Tweet ID:` が揃い、X上の実際の投稿にもURLと `#林業 #forest` が含まれること。

## 8. 連絡・判断が必要な事項

停止操作後、所有者へ「GitHub Actionsのワークフローを無効化済みで、次の定期実行は発生しない」と報告してください。その際、認証情報の再発行が未完了なら、再開できない状態であることを明確にします。

夜20時のテーマは **産業・経営トレンド** で決定済みです。第一案（国内農林業固定）は採用しません。

## References

[1]: https://github.com/doherty812-bot/forestry-x-bot/actions/workflows/post.yml "林業X自動投稿 workflow runs"
[2]: https://github.com/doherty812-bot/forestry-x-bot/blob/main/.github/workflows/post.yml "GitHub Actions workflow definition"
[3]: https://github.com/doherty812-bot/forestry-x-bot/blob/main/forestry_bot.py "Forestry X bot implementation"
