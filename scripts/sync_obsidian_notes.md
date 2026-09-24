# 公開可 Obsidian ノートを `obsidian/` へ同期する（方法1）

Actions（hosted Ubuntu）は PC 上の vault を読めません。  
**公開してよい `.md` だけ**をリポジトリの `obsidian/` にコピーしてコミットします。

vault 実体（コピー元）:

`C:\Users\info\Obsidian Vault`

## 事前チェック

1. コピーするノートに API キー・パスワード・顧客の未公開情報がないこと（`CLAUDE.md` / `profile.md` の氏名・メール・事務所はユーザー方針で repo 可。Public 可）
2. `.obsidian/` や vault 全体をコピーしないこと
3. リポジトリをクローン済みであること
4. ノート内容は下書きの背景参照用。X 投稿本文への氏名フル・メール・事務所連絡先の転記は bot 側プロンプト＋ガードで禁止

## PowerShell（推奨）

```powershell
# clone したリポジトリ直下へ
cd C:\path\to\forestry-x-bot

# --- 例A: vault 内の「公開用」フォルダの .md をすべて ---
Copy-Item "C:\Users\info\Obsidian Vault\公開用\*.md" .\obsidian\ -Force

# --- 例B: ファイルを個別に選ぶ ---
# Copy-Item "C:\Users\info\Obsidian Vault\メモ\方針.md" .\obsidian\ -Force
# Copy-Item "C:\Users\info\Obsidian Vault\メモ\現場メモ.md" .\obsidian\ -Force

# 確認（秘密らしき名前が無いか目視）
Get-ChildItem .\obsidian\ -Filter *.md | Select-Object Name, Length, LastWriteTime

git add obsidian/
git status
git commit -m "chore: sync public Obsidian notes for Actions"
git push
```

補助スクリプト例: 同ディレクトリの `sync_obsidian_notes.ps1`。

## コミット後

次回の下書き workflow で、優先Aとして `obsidian/` 内の `.md` が参照されます（`.gitkeep` と README 以外に `.md` が1件以上あるとき）。

## 入れないもの

| 禁止 | 理由 |
| --- | --- |
| API キー・パスワード・トークン | 公開リポジトリ／ログ漏洩 |
| 顧客名・未公開契約 | 個人情報・営業秘密 |
| `.obsidian/` 全体 | 設定キャッシュ。bot も読まない |
| vault 丸ごと | 秘密混入リスク |

ルート `.gitignore` が `.obsidian/`・`.trash/`・危険なファイル名パターンを除外しますが、**目視確認を省略しない**でください。
