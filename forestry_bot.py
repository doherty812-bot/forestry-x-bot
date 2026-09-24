#!/usr/bin/env python3
"""
林業Xアカウント投稿ボット（人間承認制）
岸本一夫さんのXアカウント向け

運用:
  1) draft 12:00 / 20:00 で OpenAI と Grok の二系統案を生成（投稿しない）
  2) Cursor 等で人間が確認・選択
  3) CONFIRM_LIVE_POST=1 付きで approve <draft_id> <openai|grok> のみ投稿

時間帯別コンテンツ:
  昼12時 : 国内農林業ニュース（複数ソース）× 実務コメント
  夜20時 : 産業・経営トレンド（複数ソース）× 林業経営への示唆

X Premium 前提で長文可（既定ソフト上限 8000 文字、MAX_POST_CHARS で変更）。
schedule は下書き生成のみ。即時ライブ投稿は行わない。
"""

import os
import random
import time
import logging
import inspect
from datetime import datetime, timezone
import tweepy
from openai import OpenAI
import requests
from bs4 import BeautifulSoup
import schedule
import json

from mislead_guard import MISLEAD_GUARD_PROMPT, check_mislead_risk
from privacy_guard import PRIVACY_GUARD_PROMPT, check_privacy_risk
from draft_store import (
    list_drafts,
    load_draft,
    mark_draft_posted,
    new_draft_id,
    save_draft,
)
# ログ設定（GitHub Actions対応：stdout のみ）
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
# API Keys（環境変数必須。既定値は持たない。GitHub Secrets / .env のみで供給）
REQUIRED_X_ENV_VARS = (
    "X_API_KEY",
    "X_API_SECRET",
    "X_ACCESS_TOKEN",
    "X_ACCESS_TOKEN_SECRET",
)


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"必須環境変数 {name} が未設定です")
    return value


def get_x_credentials():
    """X API OAuth 1.0a 認証情報を環境変数から取得する。"""
    return {
        "consumer_key": _require_env("X_API_KEY"),
        "consumer_secret": _require_env("X_API_SECRET"),
        "access_token": _require_env("X_ACCESS_TOKEN"),
        "access_token_secret": _require_env("X_ACCESS_TOKEN_SECRET"),
    }


_openai_client = None
_grok_client = None

# 未設定 Secrets が Actions で空文字 "" として渡ると get(..., default) が潰れるため、空は無視する
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_GROK_MODEL = "grok-3-mini"
DEFAULT_XAI_BASE_URL = "https://api.x.ai/v1"
# X Premium 前提。公式上限は大きいが、運用上のソフト上限（env で変更可）
DEFAULT_MAX_POST_CHARS = 8000
DEFAULT_MAX_SOURCES = 3
DEFAULT_GEN_MAX_TOKENS = 1200
VALID_PROVIDERS = ("openai", "grok")
# Obsidian: 無い／読めないときは警告して空コンテキストで続行（fail にすると下書き中止）
DEFAULT_OBSIDIAN_MISSING_POLICY = "warn"
DEFAULT_OBSIDIAN_MAX_FILES = 8
DEFAULT_OBSIDIAN_MAX_CHARS = 6000
# ローカル Windows 既定（Actions の Linux ランナーでは存在しない → 次候補／警告へ）
DEFAULT_OBSIDIAN_VAULT_PATH = r"C:\Users\info\Obsidian Vault"
# Actions 向け: リポジトリ内の同期先（方法1: 公開可 .md を obsidian/ にコミット。中身があれば最優先）
REPO_OBSIDIAN_DIRS = ("obsidian", "vault-sync")

# OpenAI / Grok 共通: 読者問いかけをやめ、一人称の意志を出す
VOICE_FIRST_PERSON_PROMPT = """
【文体・一人称の意志（最重要・両モデル共通）】
・読者への問いかけは禁止する（例: 「皆さんは〜？」「どう思いますか」「皆様の現場では」「どのようにお考えですか」）
・一人称の意志・見解を明示する（例: 「私は〜と考えます」「このように思います」「自分としては〜です」）
・締めは問いかけではなく、自分の考え・方針・これからやることの一文にする
・「です」「ます」調。禁止語尾: 「〜だろう」「〜だな」「〜かな」「〜ですな」
・絵文字なし。硬い「〜が重要です」「〜を推進します」は避ける
・1文ごとに改行（句点「。」の後は改行）
"""


def env_or_default(name: str, default: str) -> str:
    """環境変数が未設定・空白のときは default を使う。"""
    raw = os.environ.get(name)
    if raw is None:
        return default
    stripped = str(raw).strip()
    return stripped if stripped else default


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        return default


def get_max_post_chars() -> int:
    """投稿本文のソフト上限（ハッシュタグ・URL含む最終ペイロード）。"""
    return max(280, env_int("MAX_POST_CHARS", DEFAULT_MAX_POST_CHARS))


def get_max_sources() -> int:
    return max(1, min(5, env_int("MAX_SOURCES", DEFAULT_MAX_SOURCES)))


def get_gen_max_tokens() -> int:
    return max(200, env_int("GEN_MAX_TOKENS", DEFAULT_GEN_MAX_TOKENS))


def get_openai_model() -> str:
    return env_or_default("OPENAI_MODEL", DEFAULT_OPENAI_MODEL)


def get_grok_model() -> str:
    return env_or_default("GROK_MODEL", DEFAULT_GROK_MODEL)


def get_xai_base_url() -> str:
    return env_or_default("XAI_BASE_URL", DEFAULT_XAI_BASE_URL)


def get_obsidian_missing_policy() -> str:
    """vault が無い／読めないときの方針: warn（続行）または fail（中止）。"""
    raw = env_or_default("OBSIDIAN_MISSING_POLICY", DEFAULT_OBSIDIAN_MISSING_POLICY).lower()
    if raw not in ("warn", "fail"):
        logger.warning(
            f"OBSIDIAN_MISSING_POLICY={raw!r} は未対応のため {DEFAULT_OBSIDIAN_MISSING_POLICY} を使います"
        )
        return DEFAULT_OBSIDIAN_MISSING_POLICY
    return raw


def resolve_obsidian_vault_path():
    """
    Obsidian vault のルートを解決する。

    優先順（Actions＝Linux / ローカル Windows 両対応）:
      A. リポジトリ内 `obsidian/` または `vault-sync/`（.md が1件以上あるとき）
      B. OBSIDIAN_VAULT_PATH → OBSIDIAN_SYNC_PATH → コード既定の Windows ローカルパス
         （パスが実在するローカル／self-hosted 向け。Actions hosted では通常届かない）
    いずれも無ければ None（呼び出し側が warn で空コンテキスト続行、または fail）。
    """
    from pathlib import Path

    # A: repo 同期フォルダ（中身があるものだけ。空の .gitkeep 置き場はスキップ）
    for name in REPO_OBSIDIAN_DIRS:
        path = Path(name)
        try:
            if path.is_dir() and _iter_obsidian_markdown(path):
                resolved = path.resolve()
                logger.info(f"Obsidian vault: repo 同期を使用 ({name}/) → {resolved}")
                return resolved
            if path.is_dir():
                logger.info(f"Obsidian: {name}/ はあるが .md なし — 次候補へ")
        except OSError as e:
            logger.warning(f"Obsidian パス確認失敗: {path}: {e}")

    # B: 環境変数、なければ Windows ローカル既定（C:\Users\info\Obsidian Vault）
    candidates = []
    for name in ("OBSIDIAN_VAULT_PATH", "OBSIDIAN_SYNC_PATH"):
        raw = os.environ.get(name)
        if raw and str(raw).strip():
            candidates.append(Path(str(raw).strip()).expanduser())
    if not candidates:
        candidates.append(Path(DEFAULT_OBSIDIAN_VAULT_PATH))

    for path in candidates:
        try:
            if path.is_dir():
                resolved = path.resolve()
                logger.info(f"Obsidian vault: ローカル／指定パスを使用 → {resolved}")
                return resolved
        except OSError as e:
            logger.warning(f"Obsidian パス確認失敗: {path}: {e}")

    return None


def _iter_obsidian_markdown(vault_root):
    """vault 内の .md を新しい順で返す。.obsidian / README.md は除外。"""
    from pathlib import Path

    root = Path(vault_root)
    files = []
    for path in root.rglob("*.md"):
        parts = set(path.parts)
        if ".obsidian" in parts or ".trash" in parts:
            continue
        # 同期手順説明（obsidian/README.md 等）を下書きコンテキストに入れない
        if path.name.lower() == "readme.md":
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        files.append((mtime, path))
    files.sort(key=lambda x: x[0], reverse=True)
    return [p for _, p in files]


def load_obsidian_context(
    max_files: int | None = None,
    max_chars: int | None = None,
) -> dict:
    """
    下書き生成前に Obsidian ノートを読み、プロンプト用テキストを返す。

    戻り値 dict:
      text, status (ok|missing|empty|error), path, files_used, warning
    vault が無い場合は policy=warn なら空 text で続行、fail なら RuntimeError。
    """
    from pathlib import Path

    max_files = max_files if max_files is not None else env_int(
        "OBSIDIAN_MAX_FILES", DEFAULT_OBSIDIAN_MAX_FILES
    )
    max_chars = max_chars if max_chars is not None else env_int(
        "OBSIDIAN_MAX_CHARS", DEFAULT_OBSIDIAN_MAX_CHARS
    )
    max_files = max(1, max_files)
    max_chars = max(200, max_chars)
    policy = get_obsidian_missing_policy()
    vault = resolve_obsidian_vault_path()

    if vault is None:
        msg = (
            "Obsidian vault が見つかりません "
            "(repo obsidian|vault-sync / OBSIDIAN_VAULT_PATH / 既定 Windows パス)。"
            f" policy={policy}"
        )
        if policy == "fail":
            logger.error(msg)
            raise RuntimeError(msg)
        logger.warning(msg + " — 空コンテキストで続行します")
        return {
            "text": "",
            "status": "missing",
            "path": None,
            "files_used": [],
            "warning": msg,
        }

    try:
        md_files = _iter_obsidian_markdown(vault)[:max_files]
    except OSError as e:
        msg = f"Obsidian vault を読めません: {vault}: {e} (policy={policy})"
        if policy == "fail":
            logger.error(msg)
            raise RuntimeError(msg) from e
        logger.warning(msg + " — 空コンテキストで続行します")
        return {
            "text": "",
            "status": "error",
            "path": str(vault),
            "files_used": [],
            "warning": msg,
        }

    if not md_files:
        msg = f"Obsidian vault に .md がありません: {vault} (policy={policy})"
        if policy == "fail":
            logger.error(msg)
            raise RuntimeError(msg)
        logger.warning(msg + " — 空コンテキストで続行します")
        return {
            "text": "",
            "status": "empty",
            "path": str(vault),
            "files_used": [],
            "warning": msg,
        }

    chunks = []
    used = []
    total = 0
    for path in md_files:
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.warning(f"Obsidian ノート読込失敗: {path}: {e}")
            continue
        rel = path.relative_to(vault) if path.is_relative_to(vault) else Path(path.name)
        header = f"### {rel}\n"
        remain = max_chars - total - len(header)
        if remain <= 0:
            break
        excerpt = body.strip()
        if len(excerpt) > remain:
            excerpt = excerpt[: remain - 1] + "…"
        chunk = header + excerpt
        chunks.append(chunk)
        used.append(str(rel))
        total += len(chunk) + 2

    text = "\n\n".join(chunks).strip()
    logger.info(
        f"Obsidian コンテキスト読込: vault={vault} files={len(used)} chars={len(text)}"
    )
    return {
        "text": text,
        "status": "ok",
        "path": str(vault),
        "files_used": used,
        "warning": None,
    }


def format_obsidian_for_prompt(obsidian: dict | None) -> str:
    """生成 user プロンプト用の Obsidian ブロック。空なら短い注記のみ。"""
    if not obsidian or not (obsidian.get("text") or "").strip():
        status = (obsidian or {}).get("status") or "missing"
        return (
            "【Obsidian メモ】\n"
            f"（参照なし: status={status}。"
            "ノートが無い場合はソースと人物像だけで書いてください。）"
        )
    files = obsidian.get("files_used") or []
    return (
        "【Obsidian メモ（下書き前に参照したノート。口調・関心・方針の手がかり）】\n"
        f"vault={obsidian.get('path')} files={len(files)}\n"
        f"{obsidian['text']}\n"
        "上記メモの事実を捏造で広げず、自分の考えを述べるときの背景にしてください。\n"
        "メモ内の氏名フル・メール・事務所住所／電話／連絡先は投稿本文に転記しないでください。"
        "屋号は必要最小限のみ（迷ったら載せない）。"
    )


# 後方互換: モジュール属性（テストや表示用）。実行時は get_*_model() を使う。
OPENAI_MODEL = get_openai_model()
GROK_MODEL = get_grok_model()
XAI_BASE_URL = get_xai_base_url()


def get_openai_client():
    """OpenAI クライアントを遅延初期化する。"""
    global _openai_client
    if _openai_client is None:
        api_key = _require_env("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL")
        base_url = base_url.strip() if base_url and str(base_url).strip() else None
        if base_url:
            _openai_client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def get_xai_api_key():
    """xAI / Grok 用キー。XAI_API_KEY を優先、なければ GROK_API_KEY。"""
    for name in ("XAI_API_KEY", "GROK_API_KEY"):
        raw = os.environ.get(name)
        if raw and str(raw).strip():
            return str(raw).strip()
    return None


def get_grok_client():
    """Grok（xAI OpenAI互換）クライアントを遅延初期化する。"""
    global _grok_client
    if _grok_client is None:
        api_key = get_xai_api_key()
        if not api_key:
            raise RuntimeError("必須環境変数 XAI_API_KEY または GROK_API_KEY が未設定です")
        _grok_client = OpenAI(api_key=api_key, base_url=get_xai_base_url())
    return _grok_client


def get_client_and_model(provider: str):
    provider = (provider or "openai").lower()
    if provider == "openai":
        model = get_openai_model()
        if not model:
            raise RuntimeError("OPENAI_MODEL が空です")
        return get_openai_client(), model
    if provider == "grok":
        model = get_grok_model()
        if not model:
            raise RuntimeError("GROK_MODEL が空です")
        return get_grok_client(), model
    raise ValueError(f"未対応の provider: {provider}（openai / grok）")


def chat_complete(provider: str, system_prompt: str, user_content: str, temperature: float = 0.75):
    """指定プロバイダで chat completion を1回実行する。失敗時は例外を送出。"""
    client, model = get_client_and_model(provider)
    logger.info(f"chat_complete provider={provider} model={model}")
    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=get_gen_max_tokens(),
            temperature=temperature,
        )
    except Exception as e:
        raise RuntimeError(f"{provider} API呼び出し失敗 (model={model}): {e}") from e

    if not response.choices:
        raise RuntimeError(f"{provider} 応答に choices がありません (model={model})")
    content = response.choices[0].message.content
    if content is None or not str(content).strip():
        raise RuntimeError(f"{provider} 応答本文が空です (model={model})")
    return str(content).strip()


def require_live_post_confirmation():
    """
    実投稿CLIを誤実行しないためのガード。
    CONFIRM_LIVE_POST=1 のときのみ実投稿モードを許可する。
    """
    if os.environ.get("CONFIRM_LIVE_POST") != "1":
        raise RuntimeError(
            "実投稿モードは無効です。"
            "意図した実投稿の場合のみ CONFIRM_LIVE_POST=1 を設定してください。"
        )
# =========================================================
# 改行後処理：句点の後に必ず改行を入れる
# =========================================================
def enforce_linebreaks(text):
    """
    句点「。」の後に改行がない場合、強制的に改行を挿入する。
    また、行末の全角スペースや半角スペースを除去する。
    """
    import re
    # 句点の後に改行がない場合、改行を挿入（ハッシュタグ行の直前は除く）
    text = re.sub(r'。(?!\n)(?!$)', '。\n', text)
    # 行末の空白を除去
    lines = [line.rstrip() for line in text.split('\n')]
    # 空行が連続する場合は1つにまとめる
    result = []
    prev_empty = False
    for line in lines:
        if line == '':
            if not prev_empty:
                result.append(line)
            prev_empty = True
        else:
            result.append(line)
            prev_empty = False
    return '\n'.join(result).strip()


# =========================================================
# 投稿カテゴリ（時間帯別）
# =========================================================

# 朝7時: 国内政策・ニュース
MORNING_TOPICS = [
    ("国内政策", "林野庁が推進する「新しい林業」の実現に向けた取り組みと、スマート林業の最新動向"),
    ("国内政策", "森林経営管理制度（森林バンク）の活用状況と民有林の集積・集約化の現状"),
    ("国内政策", "脱炭素社会に向けた森林吸収源対策と、J-クレジット制度の林業活用"),
    ("国内政策", "木材自給率向上に向けた国産材利用促進政策と、CLT・木造建築の拡大動向"),
    ("国内政策", "林業の担い手確保・育成に向けた緑の雇用制度と、新規就業者の動向"),
]

# 昼12時: 木材市況・テクノロジー
NOON_TOPICS = [
    ("木材市況", "国産材の市場価格動向と、針葉樹・広葉樹の需給バランスの変化"),
    ("木材市況", "木材輸出の現状と、海外市場（中国・韓国・米国）への国産材販売戦略"),
    ("テクノロジー", "ドローンを活用した森林資源調査と、航空レーザー測量による立木材積推定の精度向上"),
    ("テクノロジー", "AIと機械学習を活用した樹木病害虫の早期発見システムの最新事例"),
    ("テクノロジー", "林業機械の自動化・遠隔操作技術の進展と、人手不足解消への貢献"),
    ("テクノロジー", "ICTを活用した作業道設計・施業計画の効率化と、GISデータの活用事例"),
]

# 夜21時: 海外トレンド・研究情報
EVENING_TOPICS = [
    ("海外トレンド", "欧州の持続可能な森林管理（SFM）認証の最新動向と、日本の林業への示唆"),
    ("海外トレンド", "北欧フィンランド・スウェーデンの高度機械化林業モデルと、日本の急峻地形への適用可能性"),
    ("海外トレンド", "カナダ・米国の大規模林業経営と、デジタルツインを活用した森林管理の最前線"),
    ("海外トレンド", "東南アジアの造林・植林プロジェクトと、カーボンクレジット市場の拡大"),
    ("研究情報", "森林総合研究所の最新研究：樹木の成長モデルと、精密な材積計算手法の開発"),
    ("研究情報", "気候変動が森林生態系に与える影響と、適応的森林管理の科学的根拠"),
    ("研究情報", "広葉樹林の資源量評価と、持続可能な利用に向けた施業指針の最新知見"),
]

# 夜20時: 有名経営者・心理学者の名言・引用
QUOTES = [
    {
        "person": "ピーター・ドラッカー",
        "role": "経営学者",
        "quote": "What gets measured gets managed.",
        "quote_ja": "測定できるものは管理できる。",
        "theme": "データに基づく森林経営・材積管理の重要性"
    },
    {
        "person": "ピーター・ドラッカー",
        "role": "経営学者",
        "quote": "The best way to predict the future is to create it.",
        "quote_ja": "未来を予測する最善の方法は、それを創ることだ。",
        "theme": "森林経営計画の長期ビジョン設計と先手の施業"
    },
    {
        "person": "スティーブ・ジョブズ",
        "role": "Apple創業者",
        "quote": "Innovation distinguishes between a leader and a follower.",
        "quote_ja": "イノベーションがリーダーとフォロワーを分ける。",
        "theme": "スマート林業・AI活用による差別化経営"
    },
    {
        "person": "ジェフ・ベゾス",
        "role": "Amazon創業者",
        "quote": "We are stubborn on vision. We are flexible on details.",
        "quote_ja": "ビジョンには頑固に、詳細には柔軟に。",
        "theme": "3,000ha拡大という長期ビジョンと、現場の柔軟な施業判断"
    },
    {
        "person": "ダニエル・カーネマン",
        "role": "心理学者・ノーベル賞受賞者",
        "quote": "Nothing in life is as important as you think it is, while you are thinking about it.",
        "quote_ja": "考えている最中は、物事の重要性を過大評価しがちだ。",
        "theme": "林業経営における判断バイアスと、データに基づく冷静な意思決定"
    },
    {
        "person": "チャーリー・マンガー",
        "role": "投資家・バークシャー・ハサウェイ副会長",
        "quote": "Invert, always invert.",
        "quote_ja": "逆から考えよ、常に逆から。",
        "theme": "林業経営の失敗要因を逆算して考えるリスク管理の発想"
    },
    {
        "person": "ジム・コリンズ",
        "role": "経営研究者・『ビジョナリー・カンパニー』著者",
        "quote": "Good is the enemy of great.",
        "quote_ja": "良いは偉大の敵だ。",
        "theme": "現状維持の林業から脱却し、経営規模拡大と高付加価値化を目指す姿勢"
    },
    {
        "person": "マルクス・アウレリウス",
        "role": "ローマ皇帝・哲学者",
        "quote": "You have power over your mind, not outside events. Realize this, and you will find strength.",
        "quote_ja": "あなたが支配できるのは自分の心だけで、外の出来事ではない。それを悟れば強さが生まれる。",
        "theme": "気候変動・木材価格変動など外部環境に左右されない林業経営の軸"
    },
    {
        "person": "アダム・グラント",
        "role": "組織心理学者・ペンシルバニア大学教授",
        "quote": "The hallmark of originality is rejecting the default and exploring whether a better option exists.",
        "quote_ja": "独創性の証は、デフォルトを疑い、より良い選択肢を探すことだ。",
        "theme": "従来の林業慣行を問い直し、新しい施業・経営モデルを模索する重要性"
    },
    {
        "person": "稲盛和夫",
        "role": "京セラ・KDDI創業者",
        "quote": "楽観的に構想し、悲観的に計画し、楽観的に実行する。",
        "quote_ja": "楽観的に構想し、悲観的に計画し、楽観的に実行する。",
        "theme": "森林経営計画の策定と現場施業における理想と現実のバランス"
    },
    {
        "person": "松下幸之助",
        "role": "パナソニック創業者",
        "quote": "失敗の原因を素直に認識し、それを改める勇気を持つことが大切だ。",
        "quote_ja": "失敗の原因を素直に認識し、それを改める勇気を持つことが大切だ。",
        "theme": "林業現場での施業ミスや経営判断の失敗から学ぶPDCAサイクル"
    },
    {
        "person": "カール・ユング",
        "role": "心理学者",
        "quote": "Until you make the unconscious conscious, it will direct your life and you will call it fate.",
        "quote_ja": "無意識を意識化しない限り、それが人生を支配し、あなたはそれを運命と呼ぶだろう。",
        "theme": "林業経営における暗黙知・経験則を可視化・データ化することの重要性"
    },
]

# =========================================================
# ニュース収集（Web検索）／複数ソース
# =========================================================

# 昼12時: 国内農林業・木材系メディア寄り（Google News クエリ）
NOON_SOURCE_QUERIES = [
    ("木材新聞", "木材新聞"),
    ("日本農業新聞", "日本農業新聞 林業 OR 木材"),
    ("林野庁", "林野庁"),
    ("農林水産", "農林水産省 林業"),
    ("国産材市場", "国産材 市場 OR 市況"),
    ("森林整備", "森林 整備 政策"),
    ("スマート林業", "スマート林業 OR 林業 DX"),
    ("木材建築", "木材利用 建築 国産材"),
]

# 夜20時: 産業・経営（農林業固定にしない）
EVENING_SOURCE_QUERIES = [
    ("日経・経営", "経営 戦略 デジタル化"),
    ("人手不足", "人手不足 自動化 産業"),
    ("地方経済", "地方経済 産業再生"),
    ("サプライチェーン", "サプライチェーン リスク管理"),
    ("カーボン経営", "カーボンニュートラル 企業経営"),
    ("中小DX", "中小企業 DX 生産性"),
    ("事業承継", "中小企業 事業承継"),
    ("ESG", "ESG 投資 経営"),
]


def _parse_rss_items(query: str, limit: int = 3):
    """Google News RSS から複数 item を返す。各要素: title, url, source, snippet, query。"""
    import xml.etree.ElementTree as ET
    import urllib.parse

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=ja&gl=JP&ceid=JP:ja"
    response = requests.get(rss_url, headers=headers, timeout=15)
    if response.status_code != 200:
        return []
    root = ET.fromstring(response.content)
    items = root.findall(".//item")
    out = []
    for item in items[: max(limit, 1)]:
        title_el = item.find("title")
        link_el = item.find("link")
        desc_el = item.find("description")
        raw_title = (title_el.text or "").strip() if title_el is not None else ""
        source = ""
        title = raw_title
        if " - " in raw_title:
            title, source = raw_title.rsplit(" - ", 1)
            title, source = title.strip(), source.strip()
        url = link_el.text.strip() if link_el is not None and link_el.text else None
        if not url:
            continue
        snippet = ""
        if desc_el is not None and desc_el.text:
            snippet = desc_el.text.strip()[:300]
        out.append(
            {
                "title": title or raw_title,
                "url": url,
                "source": source or "Google News",
                "snippet": snippet,
                "query": query,
            }
        )
    return out


def fetch_forestry_news(query, retry=True):
    """
    Google News RSSで林業関連ニュースを検索して取得する。
    (snippet_text, article_url) のタプルを返す（後方互換）。
    """
    try:
        items = _parse_rss_items(query, limit=3)
        if not items and retry:
            items = _parse_rss_items("林業 国内 最新", limit=3)
        if not items:
            return "", None
        snippet = " / ".join(i["title"] for i in items[:3])
        return snippet, items[0]["url"]
    except Exception as e:
        logger.warning(f"ニュース取得エラー: {e}")
        return "", None


def collect_sources_from_catalog(catalog, label: str):
    """
    カタログから複数クエリを選び、重複URLを除いて最大 get_max_sources() 件集める。
    戻り値: sources (list[dict]) — 1件以上必須、なければ RuntimeError。
    """
    need = get_max_sources()
    shuffled = list(catalog)
    random.shuffle(shuffled)
    collected = []
    seen_urls = set()
    for source_label, query in shuffled:
        if len(collected) >= need:
            break
        logger.info(f"{label} ソース取得: [{source_label}] q={query}")
        try:
            items = _parse_rss_items(query, limit=2)
        except Exception as e:
            logger.warning(f"RSS失敗 [{source_label}]: {e}")
            continue
        for item in items:
            url = item.get("url")
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            # ラベルを優先表示用に付与
            item = dict(item)
            item["label"] = source_label
            if not item.get("source"):
                item["source"] = source_label
            collected.append(item)
            break  # 1クエリあたり原則1件
    if len(collected) < 1:
        raise RuntimeError(f"{label}: 記事URLを1件も取得できませんでした")
    logger.info(f"{label}: {len(collected)} 件のソースを取得")
    return collected[:need]


def format_sources_for_prompt(sources) -> str:
    lines = []
    for i, s in enumerate(sources, 1):
        lines.append(
            f"{i}. [{s.get('label') or s.get('source')}] {s.get('title')}\n"
            f"   概要: {(s.get('snippet') or '（なし）')[:180]}\n"
            f"   URL: {s.get('url')}"
        )
    return "\n".join(lines)


def sources_as_urls(sources) -> list:
    return [s["url"] for s in sources if s.get("url")]


def normalize_article_urls(article_url_or_urls):
    """単一URLまたはURLリストを list[str] に正規化する。"""
    if not article_url_or_urls:
        return []
    if isinstance(article_url_or_urls, str):
        return [article_url_or_urls] if article_url_or_urls.strip() else []
    return [u for u in article_url_or_urls if u and str(u).strip()]


def fetch_global_forest_buzz():
    """
    海外の森林・林業関連のバズ記事・トレンドトピックをGoogle News RSSで取得する。
    複数の英語キーワードで検索し、最も関連性の高い情報を返す。
    """
    import xml.etree.ElementTree as ET
    import urllib.parse
    
    search_queries = [
        "forest management innovation",
        "forestry technology AI drones",
        "sustainable forest carbon credits",
        "deforestation reforestation news",
        "timber market wood price trend",
        "smart forestry digital",
    ]
    query = random.choice(search_queries)
    logger.info(f"海外バズ記事検索クエリ: {query}")
    
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=en&gl=US&ceid=US:en"
        response = requests.get(rss_url, headers=headers, timeout=10)
        
        if response.status_code != 200:
            return query, []
        
        root = ET.fromstring(response.content)
        items = root.findall('.//item')
        
        articles = []
        for item in items[:5]:
            title = item.find('title')
            link = item.find('link')
            description = item.find('description')
            if title is not None:
                article_url = link.text if link is not None else None
                articles.append({
                    "title": title.text or '',
                    "snippet": (description.text or '')[:200] if description is not None else '',
                    "url": article_url
                })
        
        if articles:
            return query, articles
        return query, []
    except Exception as e:
        logger.warning(f"海外バズ記事取得エラー: {e}")
        return query, []


# =========================================================
# その日の産業・経営トレンド記事取得（夜20時枠）
# =========================================================

# 所有者決定: 夜20時は「産業・経営トレンド」固定（国内農林業ニュースにはしない）
INDUSTRY_TREND_QUERIES = [
    "経営 戦略 デジタル化",
    "人手不足 自動化 産業",
    "地方経済 産業再生",
    "サプライチェーン リスク管理",
    "カーボンニュートラル 企業経営",
    "AI 生産性 経営",
    "中小企業 事業承継",
    "物流 コスト 値上げ",
    "エネルギー価格 産業",
    "ESG 投資 経営",
    "製造業 DX 事例",
    "価格転嫁 中小企業",
    "働き方改革 地方企業",
    "設備投資 金利 企業",
]

# 取得失敗時のフォールバックも農林業固定にしない
INDUSTRY_TREND_FALLBACK_QUERIES = [
    "経営 トレンド 最新",
    "産業 人手不足 対策",
    "企業 DX 生産性",
]


def fetch_todays_buzz_article():
    """
    夜20時枠: 産業・経営・経済・テクノロジー分野のトレンド記事を
    Google News RSSから取得する（国内農林業固定クエリは使わない）。
    (title, snippet, url) のタプルを返す。
    """
    import xml.etree.ElementTree as ET
    import urllib.parse

    query = random.choice(INDUSTRY_TREND_QUERIES)
    logger.info(f"夜20時 産業・経営トレンド検索クエリ: {query}")

    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(query)}&hl=ja&gl=JP&ceid=JP:ja"
        response = requests.get(rss_url, headers=headers, timeout=10)

        if response.status_code != 200:
            return None, None, None

        root = ET.fromstring(response.content)
        items = root.findall('.//item')

        if not items:
            return None, None, None

        selected = random.choice(items[:3])
        title = selected.find('title')
        link = selected.find('link')
        description = selected.find('description')

        title_text = title.text if title is not None else ''
        url_text = link.text if link is not None else None
        snippet_text = (description.text or '')[:300] if description is not None else ''

        return title_text, snippet_text, url_text
    except Exception as e:
        logger.warning(f"産業・経営トレンド記事取得エラー: {e}")
        return None, None, None


# =========================================================
# ツイート生成（通常）
# =========================================================
def generate_tweet(category, topic, news_context=""):
    """OpenAI APIを使って通常のツイートを生成する"""
    
    system_prompt = """
あなたは新潟で1,500ha規模の森林経営計画を管理し、将来的に3,000haへの拡大を見据える林業経営者「岸本一夫」として投稿文を作成します。

【人物像】
・山を「所有」ではなく「経営資源」として捉える実務家
・針葉樹・広葉樹の販売先を工場中心に置く現実的な判断力
・AIやロボット活用を地域と産業が生き残るための必然的手段として捉える
・森林総合研究所などのエビデンスに基づいた判断を重視
・地方の人口減少・人手不足を冷静に見据えている

【文体の特徴（最重要）】
・1文ごとに必ず改行する。句点「。」の後は必ず改行すること
・短文・中文中心（1文あたり20〜40文字程度）
・「です」「ます」調を基本とする。語尾は「〜です。」「〜ます。」「〜ですね。」「〜でしょうか。」「〜かもしれません。」など
・体言止めを適度に混ぜる
・「...」で余韻・沈黙を表現することがある
・絵文字は使わない
・「〜だろう」「〜だな」「〜かな」「〜ですな」などの語尾は使わない
・スマートで知性的な口調を保ちつつ、押しつけがましくない

【実際の投稿例（この文体を参考にすること）】
例1：
「今日も生産森林組合さんとの山歩き。
エリートツリーの成長も実感出来たようで良かったです。
週末に山主さんとの山歩きをしていると、清々しいような、時間が無くなるような微妙な心境で新年度も精進していきます。」

例2：
「学校や公共施設への木材活用が進まないと、行政はなかなか動きません。
里山整備と災害対策、同時に進める必要があります。」

例3：
「森林が侵食され、山にはゴミが残ります。
原子力発電の廃棄物問題よりも、ずっと身近な問題になるかもしれません。」

【投稿の構成】
1. 事実・問題提起（短く）
2. 背景・理由・自分の見方
3. 一言コメントまたは問いかけ（押しつけがましくない）

【厳守事項】
・文字数は全体で140文字以内（ハッシュタグ・改行含む）
・必ず最後に「#林業 #森林 #forest」を付ける
・URLは含めない
・140文字を超えた場合は必ず短縮すること
・AIが書いたような「〜が重要です」「〜を推進します」「〜が期待されています」などの硬い表現は避ける
"""
    
    user_content = f"カテゴリ: {category}\nトピック: {topic}"
    if news_context:
        user_content += f"\n参考情報: {news_context[:300]}"
    
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            max_tokens=200,
            temperature=0.75
        )
        tweet_text = response.choices[0].message.content.strip()
        
        # 140文字チェック
        if len(tweet_text) > 140:
            retry_response = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": tweet_text},
                    {"role": "user", "content": f"文字数が{len(tweet_text)}文字で140文字を超えています。140文字以内に収めて書き直してください。"}
                ],
                max_tokens=200,
                temperature=0.5
            )
            tweet_text = retry_response.choices[0].message.content.strip()
        
        return enforce_linebreaks(tweet_text)
    except Exception as e:
        logger.error(f"ツイート生成エラー: {e}")
        return None


# =========================================================
# ツイート生成（海外バズ記事紹介）
# =========================================================
def generate_global_buzz_tweet(query, articles):
    """
    海外の森林関連バズ記事を日本語で紹介するツイートを生成する（朝6時枠）
    """
    system_prompt = """
あなたは新潟で1,500ha規模の森林経営計画を管理する林業経営者「岸本一夫」として、
海外の森林・林業関連の最新情報を日本語で紹介するX（旧Twitter）投稿を作成します。

【投稿の目的】
海外の森林関連トレンドを日本の林業経営者・関係者にわかりやすく伝え、
日本の林業への示唆や自分の視点を一言添える。

【文体の特徴（最重要）】
・1文ごとに必ず改行する。句点「。」の後は必ず改行すること
・短文・中文中心（1文あたり20〜40文字程度）
・「です」「ます」調を基本とする。語尾は「〜です。」「〜ます。」「〜ですね。」「〜でしょうか。」「〜かもしれません。」など
・「海外では〜」「世界では〜」などの書き出しで海外情報であることを明示する
・最後に日本の林業経営への示唠や自分のコメントを一言添える
・「〜だろう」「〜だな」「〜かな」「〜ですな」などの語尾は使わない
・スマートで知性的な口調を保ちつつ、押しつけがましくない
・絵文字は使わない
・AIが書いたような「〜が期待されています」「〜を推進します」などの硬い表現は避ける

【実際の投稿例（この文体を参考にすること）】
「森林が侵され、そして山にはゴミが残る。
原子力発電のゴミ問題よりも身近になるだろう。」

「学校とか公共施設に出始めないと、行政は動かない。
里山整備もやりながら、同時に災害対策を進めていかないと状況が悪化する。」

【厳守事項】
・文字数は全体で140文字以内（ハッシュタグ・改行含む）
・必ず最後に「#林業 #森林 #forest」を付ける
・URLは含めない
・140文字を超えた場合は必ず短縮すること
"""
    
    # 記事情報を整形
    articles_text = "\n".join([
        f"- タイトル: {a['title']}\n  内容: {a['snippet']}"
        for a in articles[:3]
    ]) if articles else "（記事取得なし）"
    
    user_content = f"""
検索クエリ: {query}

取得した海外記事:
{articles_text}

上記の情報を参考に、海外の森林・林業トレンドを日本語で紹介する投稿を作成してください。
記事が取得できていない場合は、クエリのテーマに関する一般的な海外トレンドを紹介してください。
"""
    
    try:
        client = get_openai_client()
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            max_tokens=200,
            temperature=0.75
        )
        tweet_text = response.choices[0].message.content.strip()
        
        # 140文字チェック
        if len(tweet_text) > 140:
            retry_response = client.chat.completions.create(
                model="gpt-4.1-mini",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                    {"role": "assistant", "content": tweet_text},
                    {"role": "user", "content": f"文字数が{len(tweet_text)}文字で140文字を超えています。140文字以内に収めて書き直してください。"}
                ],
                max_tokens=200,
                temperature=0.5
            )
            tweet_text = retry_response.choices[0].message.content.strip()
        
        return enforce_linebreaks(tweet_text)
    except Exception as e:
        logger.error(f"海外バズ記事ツイート生成エラー: {e}")
        return None


# =========================================================
# ツイート生成（昼12時: 国内農林業ニュース × 実務コメント）
# =========================================================
def generate_buzz_insight_tweet(
    sources,
    provider="openai",
    article_title=None,
    article_snippet=None,
    obsidian_context=None,
):
    """
    昼12時枠: 複数の国内農林業ソースを踏まえ、現場実務コメント付き投稿を生成する。
    sources: list[dict]（推奨）。旧引数 title/snippet のみの呼び出しも互換。
    """
    if not sources and (article_title or article_snippet):
        sources = [{"title": article_title or "", "snippet": article_snippet or "", "url": "", "source": ""}]
    if not sources:
        raise ValueError("sources が空です")

    max_chars = get_max_post_chars()
    n = len(sources)
    system_prompt = f"""
あなたは新潟で1,500ha規模の森林経営計画を管理し、将来的に3,000haへの拡大を見据える林業経営者「岸本一夫」として、
国内の農林業・木材関連の複数ニュースを読んで、現場目線の実務的コメントを含むX投稿を作成します。

【人物像】
・山を「所有」ではなく「経営資源」として捉える実務家
・針葉樹・広葉樹の販売先を工場中心に置く現実的な判断力
・地方の人口減少・人手不足を冷静に見据え、AI・ロボット活用を必然的手段として捉える

【投稿の目的】
複数ソース（最大{n}件）に触れつつ、第一人称の現場感覚と自分の意志を語る。
1記事の要約だけで終わらない。出典の違いや共通点にも軽く触れてよい。

{VOICE_FIRST_PERSON_PROMPT}

{MISLEAD_GUARD_PROMPT}

{PRIVACY_GUARD_PROMPT}

【構成】
1. 複数ソースのテーマを自分の言葉で（短く）
2. 現場目線のコメント（2〜4文程度でも可）
3. 「私は〜と考えます」など一人称の意志・方針（問いかけで締めない）
4. 末尾に #林業 #森林 #forest（URLは付けない。システムが後付けする）

【文字数】
・X Premium 前提。本文は目安 {max_chars} 文字以内（ハッシュタグ含む）。無理に短くしない。
・URLは含めない。
"""

    if obsidian_context is None:
        obsidian_context = load_obsidian_context()

    user_content = f"""
以下の国内農林業・木材関連ソース（{n}件）を踏まえて投稿を作成してください。
価格・相場は根拠が無い限り断定しないでください。
読者への問いかけはせず、一人称の意志で締めてください。
氏名フル・メール・事務所住所／電話は本文に書かないでください。

{format_sources_for_prompt(sources)}

{format_obsidian_for_prompt(obsidian_context)}
"""

    try:
        tweet_text = chat_complete(provider, system_prompt, user_content, temperature=0.75)
        soft = get_max_post_chars()
        if len(tweet_text) > soft:
            tweet_text = chat_complete(
                provider,
                system_prompt,
                user_content + f"\n\n（再生成）直前案は{len(tweet_text)}文字でした。{soft}文字以内に収めてください。",
                temperature=0.5,
            )
        return enforce_linebreaks(tweet_text)
    except Exception as e:
        logger.error(f"国内農林業インサイトツイート生成エラー ({provider}): {e}")
        raise


# =========================================================
# ツイート生成（夜20時: 産業・経営トレンド × 林業への示唆）
# =========================================================
def _industry_system_prompt():
    max_chars = get_max_post_chars()
    return f"""
あなたは新潟で1,500ha規模の森林経営計画を管理し、将来的に3,000haへの拡大を見据える林業経営者「岸本一夫」として、
産業・経営・経済・テクノロジー分野の複数トレンド記事を読み、林業経営への示唆を含むX投稿を作成します。

【投稿の目的】
複数の産業・経営トレンドを引用し、「林業経営ではこう読み替える」示唆を必ず入れる。
国内農林業ニュースの単なる紹介に終始しない。
締めは読者への問いかけではなく、一人称の意志・方針にする。

{VOICE_FIRST_PERSON_PROMPT}

{MISLEAD_GUARD_PROMPT}

{PRIVACY_GUARD_PROMPT}

【構成】
1. 複数トレンドの要点
2. 林業・森林経営への示唆（必須）
3. 「私は〜と考えます」「このように思います」など一人称の意志（問いかけ禁止）
4. 末尾ハッシュタグ #林業 #森林 #forest（URLは付けない）

【文字数】
・X Premium 前提。目安 {max_chars} 文字以内。無理に短縮しない。
・URLは含めない。
"""


INDUSTRY_TREND_SYSTEM_PROMPT = None  # 実行時に _industry_system_prompt() を使う


def generate_industry_trend_tweet(
    sources,
    provider="openai",
    article_title=None,
    article_snippet=None,
    obsidian_context=None,
):
    """
    夜20時枠: 複数の産業・経営トレンドを踏まえ、林業への示唆付き投稿を生成する。
    """
    if not sources and (article_title or article_snippet):
        sources = [{"title": article_title or "", "snippet": article_snippet or "", "url": "", "source": ""}]
    if not sources:
        raise ValueError("sources が空です")

    system_prompt = _industry_system_prompt()
    if obsidian_context is None:
        obsidian_context = load_obsidian_context()
    user_content = f"""
以下は産業・経営・経済・テクノロジー分野のソース（{len(sources)}件）です。
国内農林業ニュースの要約だけにしないでください。
林業経営への読み替えを必ず含め、複数ソースに触れてください。
読者への問いかけはせず、一人称の意志で締めてください。
氏名フル・メール・事務所住所／電話は本文に書かないでください。

{format_sources_for_prompt(sources)}

{format_obsidian_for_prompt(obsidian_context)}
"""

    try:
        tweet_text = chat_complete(provider, system_prompt, user_content, temperature=0.75)
        soft = get_max_post_chars()
        if len(tweet_text) > soft:
            tweet_text = chat_complete(
                provider,
                system_prompt,
                user_content + f"\n\n（再生成）直前案は{len(tweet_text)}文字でした。{soft}文字以内に収めてください。",
                temperature=0.5,
            )
        return enforce_linebreaks(tweet_text)
    except Exception as e:
        logger.error(f"産業・経営トレンドツイート生成エラー ({provider}): {e}")
        raise


# =========================================================
# X投稿
# =========================================================
HASHTAGS = "#林業 #forest"

def build_tweet_payload(tweet_text, article_url=None):
    """
    投稿本文を組み立てる（副作用なし）。
    article_url は str または URL の list を受け付ける。
    X Premium 前提でソフト上限 get_max_post_chars() のみ適用。
    """
    if not tweet_text:
        raise ValueError("投稿テキストが空です")

    import re
    clean_body = re.sub(r'#\S+', '', tweet_text).rstrip()
    hashtag_str = HASHTAGS
    urls = normalize_article_urls(article_url)

    # URLはX上で短縮カウントされるが、ソフト上限は最終テキスト長で見る
    parts = [clean_body, hashtag_str]
    if urls:
        parts.extend(urls)
    full_text = "\n".join(parts)

    soft = get_max_post_chars()
    if len(full_text) > soft:
        # 本文だけ削る（URL・タグは残す）
        overhead = len(hashtag_str) + 1 + sum(len(u) + 1 for u in urls)
        max_body = max(50, soft - overhead)
        if len(clean_body) > max_body:
            clean_body = clean_body[: max_body - 1] + "…"
        parts = [clean_body, hashtag_str]
        if urls:
            parts.extend(urls)
        full_text = "\n".join(parts)
    return full_text, clean_body


def post_to_x(tweet_text, article_url=None):
    """
    Xにツイートを投稿する。
    - 末尾に HASHTAGS を付ける
    - article_url（単一または複数）を末尾に付ける
    """
    try:
        full_text, _ = build_tweet_payload(tweet_text, article_url)
    except ValueError as e:
        logger.error(str(e))
        return False

    urls = normalize_article_urls(article_url)
    if urls:
        logger.info(f"記事URL付き投稿 ({len(urls)}件): {urls}")

    try:
        creds = get_x_credentials()
        client = tweepy.Client(
            consumer_key=creds["consumer_key"],
            consumer_secret=creds["consumer_secret"],
            access_token=creds["access_token"],
            access_token_secret=creds["access_token_secret"],
        )
        response = client.create_tweet(text=full_text)
        tweet_id = response.data['id']
        logger.info(f"投稿成功！ Tweet ID: {tweet_id}")
        logger.info(f"投稿内容: {full_text}")
        logger.info(f"文字数(最終): {len(full_text)} / soft_max={get_max_post_chars()}")
        return True
    except Exception as e:
        logger.error(f"X投稿エラー: {e}")
        return False


def ensure_post_ready(tweet, article_url):
    """URL・本文・投稿成否を検査し、失敗時は RuntimeError を送出する。"""
    urls = normalize_article_urls(article_url)
    if not urls:
        raise RuntimeError("記事URLが取得できないため投稿を中止しました")
    if not tweet:
        raise RuntimeError("投稿文を生成できませんでした")
    if not post_to_x(tweet, urls):
        raise RuntimeError("Xへの投稿に失敗しました")


def print_draft_for_human(draft: dict):
    """Cursor / ログ向けに二系統案を見やすく出す。"""
    logger.info("=" * 60)
    logger.info(f"DRAFT_ID: {draft['id']}")
    logger.info(f"SLOT: {draft['slot']}")
    logger.info(f"STATUS: {draft.get('status')}")
    sources = draft.get("sources") or []
    if sources:
        logger.info(f"SOURCES ({len(sources)}):")
        for i, s in enumerate(sources, 1):
            logger.info(f"  {i}. [{s.get('label') or s.get('source')}] {s.get('title')}")
            logger.info(f"     {s.get('url')}")
    else:
        art = draft.get("article") or {}
        logger.info(f"ARTICLE: {art.get('title')}")
        logger.info(f"URL: {art.get('url')}")
    for provider in VALID_PROVIDERS:
        cand = (draft.get("candidates") or {}).get(provider) or {}
        logger.info("-" * 40)
        logger.info(f"[{provider}] guard_ok={cand.get('guard_ok')} flags={cand.get('flags')}")
        if cand.get("error"):
            logger.info(f"[{provider}] error: {cand['error']}")
        else:
            text = cand.get("text") or ""
            logger.info(f"[{provider}] chars={len(text)}\n{text}")
    logger.info("=" * 60)
    logger.info(
        "承認後の投稿例: CONFIRM_LIVE_POST=1 python forestry_bot.py approve "
        f"{draft['id']} openai|grok"
    )


def build_dual_candidates(sources, generator):
    """同一ネタ（複数ソース）で openai / grok の案を作る。"""
    candidates = {}
    for provider in VALID_PROVIDERS:
        model = get_openai_model() if provider == "openai" else get_grok_model()
        try:
            text = generator(sources, provider=provider)
            if not text or not str(text).strip():
                raise RuntimeError(f"{provider} が空の本文を返しました (model={model})")
            ok_m, flags_m = check_mislead_risk(text)
            ok_p, flags_p = check_privacy_risk(text)
            # empty_text は mislead 側と重複しうるので privacy 側を優先マージ
            flags = list(flags_m)
            for f in flags_p:
                if f not in flags:
                    flags.append(f)
            ok = ok_m and ok_p
            candidates[provider] = {
                "text": text,
                "guard_ok": ok,
                "flags": flags,
                "model": model,
                "error": None,
                "char_count": len(text),
            }
        except Exception as e:
            err = str(e)
            logger.error(f"候補生成失敗 provider={provider} model={model}: {err}")
            candidates[provider] = {
                "text": None,
                "guard_ok": False,
                "flags": ["generation_error"],
                "model": model,
                "error": err,
                "char_count": 0,
            }
    return candidates


def create_dual_draft(slot: str, sources, generator) -> dict:
    urls = sources_as_urls(sources)
    if not urls:
        raise RuntimeError("記事URLが取得できないため下書きを中止しました")

    # 両モデル生成前に Obsidian を一度だけ読む（無い場合は warn で空コンテキスト）
    obsidian = load_obsidian_context()
    logger.info(
        f"Obsidian 参照: status={obsidian.get('status')} "
        f"path={obsidian.get('path')} files={len(obsidian.get('files_used') or [])}"
    )

    def _accepts_obsidian(fn) -> bool:
        try:
            return "obsidian_context" in inspect.signature(fn).parameters
        except (TypeError, ValueError):
            return False

    def generator_with_obsidian(src, provider="openai"):
        if _accepts_obsidian(generator):
            return generator(src, provider=provider, obsidian_context=obsidian)
        return generator(src, provider=provider)

    candidates = build_dual_candidates(sources, generator_with_obsidian)
    any_ok = any(
        (c.get("text") and not c.get("error")) for c in candidates.values()
    )
    primary = sources[0]
    draft = {
        "id": new_draft_id(slot),
        "slot": slot,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending" if any_ok else "failed",
        "sources": sources,
        "urls": urls,
        # 後方互換
        "article": {
            "title": primary.get("title"),
            "snippet": (primary.get("snippet") or "")[:500],
            "url": primary.get("url"),
            "source": primary.get("source") or primary.get("label"),
        },
        "candidates": candidates,
        "limits": {
            "max_post_chars": get_max_post_chars(),
            "max_sources": get_max_sources(),
        },
        "obsidian": {
            "status": obsidian.get("status"),
            "path": obsidian.get("path"),
            "files_used": obsidian.get("files_used") or [],
            "warning": obsidian.get("warning"),
            "char_count": len(obsidian.get("text") or ""),
        },
    }
    path = save_draft(draft)
    logger.info(f"下書きを保存しました: {path} status={draft['status']} sources={len(sources)}")
    print_draft_for_human(draft)
    if not any_ok:
        errors = {p: (candidates[p] or {}).get("error") for p in VALID_PROVIDERS}
        raise RuntimeError(
            "OpenAI / Grok の両方が生成に失敗しました。"
            f" details={errors}"
        )
    return draft


def collect_noon_article():
    """後方互換: 先頭ソースの (title, snippet, url) を返す。"""
    sources = collect_noon_sources()
    s = sources[0]
    return s.get("title"), s.get("snippet"), s.get("url")


def collect_noon_sources():
    """昼12時: 国内農林業・木材系の複数ソース。"""
    return collect_sources_from_catalog(NOON_SOURCE_QUERIES, "昼12時")


def collect_evening_article():
    """後方互換。"""
    sources = collect_evening_sources()
    s = sources[0]
    return s.get("title"), s.get("snippet"), s.get("url")


def collect_evening_sources():
    """夜20時: 産業・経営トレンドの複数ソース。"""
    return collect_sources_from_catalog(EVENING_SOURCE_QUERIES, "夜20時")


def noon_job():
    """昼12時: 下書きのみ（投稿しない）。"""
    logger.info("=== 昼12時 下書き生成（OpenAI + Grok / 複数ソース）===")
    sources = collect_noon_sources()
    return create_dual_draft("12:00", sources, generate_buzz_insight_tweet)


def pre_evening_job():
    """夜20時: 下書きのみ（投稿しない）。"""
    logger.info("=== 夜20時 下書き生成（OpenAI + Grok / 複数ソース）===")
    sources = collect_evening_sources()
    for s in sources:
        logger.info(f"取得: [{s.get('label')}] {s.get('title')}")
    return create_dual_draft("20:00", sources, generate_industry_trend_tweet)


def approve_and_post(draft_id: str, provider: str):
    """人間が選んだ provider の案だけを投稿する。"""
    require_live_post_confirmation()
    provider = provider.lower()
    if provider not in VALID_PROVIDERS:
        raise ValueError(f"provider は openai または grok です: {provider}")

    draft = load_draft(draft_id)
    if draft.get("status") != "pending":
        raise RuntimeError(f"下書き状態が pending ではありません: {draft.get('status')}")

    cand = (draft.get("candidates") or {}).get(provider) or {}
    tweet = cand.get("text")
    urls = draft.get("urls") or sources_as_urls(draft.get("sources") or [])
    if not urls:
        url = (draft.get("article") or {}).get("url")
        urls = normalize_article_urls(url)
    if not tweet:
        raise RuntimeError(f"{provider} の投稿案がありません: {cand.get('error')}")
    if cand.get("guard_ok") is False:
        logger.warning(
            f"投稿前警告フラグあり（ミスリード／個人情報など）: {cand.get('flags')} — "
            "CONFIRM_LIVE_POST=1 でも続行しますが、内容を再確認してください。"
            "氏名・メール・事務所連絡先が本文に無いことを特に確認してください。"
        )

    ensure_post_ready(tweet, urls)
    mark_draft_posted(draft_id, provider)
    logger.info(f"承認投稿完了: draft={draft_id} provider={provider} urls={len(urls)}")


# 互換: 旧ジョブ名は下書きのみ（実投稿しない）
def early_morning_job():
    raise RuntimeError("旧スロットは廃止。draft 12:00 / 20:00 を使ってください。")


def morning_job():
    raise RuntimeError("旧スロットは廃止。draft 12:00 / 20:00 を使ってください。")


def evening_job():
    raise RuntimeError("旧スロットは廃止。draft 12:00 / 20:00 を使ってください。")


def setup_scheduler():
    """ローカル常駐は下書き生成のみ（投稿しない）。"""
    schedule.every().day.at("03:00").do(noon_job)
    schedule.every().day.at("11:00").do(pre_evening_job)
    logger.info("スケジューラー設定完了（下書きのみ: 12:00 / 20:00 JST）")


def run_scheduler():
    """下書き用ローカルスケジューラ（CONFIRM 不要・投稿しない）。"""
    setup_scheduler()
    logger.info("下書きボット起動。投稿は approve コマンドのみ。")
    while True:
        schedule.run_pending()
        time.sleep(30)


# =========================================================
# メイン
# =========================================================
if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    cmd = args[0] if args else ""

    if cmd in {"approve", "test_all", "test", "test_quote", "run-live"}:
        # approve 以外の旧ライブ系は拒否
        if cmd != "approve":
            raise RuntimeError(
                f"コマンド '{cmd}' による即時投稿は廃止しました。"
                "draft → approve を使ってください。"
            )

    if cmd == "draft":
        slot = args[1] if len(args) > 1 else ""
        if slot == "12:00":
            noon_job()
        elif slot == "20:00":
            pre_evening_job()
        else:
            raise SystemExit("用法: python forestry_bot.py draft 12:00|20:00")
    elif cmd in {"12:00", "20:00"}:
        # 互換: スロット指定は下書きのみ
        logger.info(f"=== {cmd} は下書き生成のみ（投稿しません）===")
        if cmd == "12:00":
            noon_job()
        else:
            pre_evening_job()
    elif cmd == "list-drafts":
        for d in list_drafts("pending"):
            art = d.get("article") or {}
            logger.info(f"{d['id']} slot={d['slot']} title={art.get('title')}")
    elif cmd == "show-draft":
        if len(args) < 2:
            raise SystemExit("用法: python forestry_bot.py show-draft <draft_id>")
        print_draft_for_human(load_draft(args[1]))
    elif cmd == "approve":
        if len(args) < 3:
            raise SystemExit(
                "用法: CONFIRM_LIVE_POST=1 python forestry_bot.py approve <draft_id> openai|grok"
            )
        approve_and_post(args[1], args[2])
    elif cmd == "run":
        run_scheduler()
    elif cmd == "dry-run-format":
        sample = "山を経営資源として見る視点が大切です。\n現場の感覚も忘れません。"
        url = "https://news.google.com/articles/example"
        full, _ = build_tweet_payload(sample, url)
        logger.info(f"dry-run payload:\n{full}")
        assert HASHTAGS in full
        assert url in full
        logger.info("dry-run-format OK")
    elif cmd == "check-mislead":
        sample = args[1] if len(args) > 1 else "木材価格が高騰しています。"
        ok_m, flags_m = check_mislead_risk(sample)
        ok_p, flags_p = check_privacy_risk(sample)
        flags = list(flags_m)
        for f in flags_p:
            if f not in flags:
                flags.append(f)
        ok = ok_m and ok_p
        logger.info(f"ok={ok} flags={flags} text={sample}")
    else:
        logger.info(
            "用法:\n"
            "  python forestry_bot.py draft 12:00|20:00\n"
            "  python forestry_bot.py list-drafts\n"
            "  python forestry_bot.py show-draft <id>\n"
            "  CONFIRM_LIVE_POST=1 python forestry_bot.py approve <id> openai|grok\n"
            "  （主経路は GitHub Actions「林業X承認投稿」。スマホ Cursor で openai|grok を送信）\n"
            "  python forestry_bot.py dry-run-format"
        )
