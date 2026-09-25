# Actions 用 Obsidian ノート（方法1）

このフォルダには、**GitHub Actions の下書き生成が読んでよいノートだけ**を置きます。

## 入れてよいもの

- X 投稿の背景になる方針・現場メモ・事業文脈（`CLAUDE.md` / `profile.md` 等）
- プレーンな `.md`（Obsidian の通常ノート）
- 氏名・メール・事務所を含むノートも **repo 配置は可**（Public でも可、ユーザー方針）。用途は生成時の背景参照のみ

## 入れないもの（秘密禁止）

- API キー・パスワード・トークン・証明書
- 顧客名・未公開の契約・社内だけの数値（公開したくないもの）
- Obsidian の `.obsidian/` キャッシュや `.trash/`
- 実 vault 全体のコピー

## X 投稿本文では禁止（ノートにあっても転記しない）

- 氏名フル、個人メール、事務所の住所／電話／「事務所」での所在案内
- 屋号・事業者名は文脈上必要なら最小限（迷ったら載せない）
- ガード: `privacy_guard.py`（`pii_*` flags）＋両モデル共通プロンプト

実体の vault はユーザー PC 上です（クラウドからは読めません）:

`C:\Users\info\Obsidian Vault`

## Windows: 公開可ノートだけコピーする

リポジトリをクローンしたフォルダで、PowerShell 例:

```powershell
# リポジトリ直下へ移動（パスは自分の clone 先に合わせる）
cd C:\path\to\forestry-x-bot

# フォルダごと（例: vault 内の「公開用」）
Copy-Item "C:\Users\info\Obsidian Vault\公開用\*.md" .\obsidian\ -Force

# または個別に選ぶ
Copy-Item "C:\Users\info\Obsidian Vault\メモ\方針.md" .\obsidian\ -Force

git add obsidian/*.md
git status   # 秘密ファイルが混ざっていないか確認
git commit -m "chore: sync public Obsidian notes for Actions"
git push
```

詳細手順: リポジトリの `scripts/sync_obsidian_notes.md`、Project の `docs/voice-and-obsidian.md`。

## ダミー

`sample-public-note.md` は動作確認用の短いダミーです。実 vault の内容ではありません。公開ノートを置いたらこのファイルは削除して構いません。
