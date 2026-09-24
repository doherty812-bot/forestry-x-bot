#!/usr/bin/env python3
"""
林業Xアカウント投稿ボット（人間承認制）
岸本一夫さんのXアカウント向け

運用:
  1) draft 12:00 / 20:00 で OpenAI と Grok の二系統案を生成（投稿しない）
  2) Cursor 等で人間が確認・選択
  3) CONFIRM_LIVE_POST=1 付きで approve <draft_id> <openai|grok> のみ投稿

時間帯別コンテンツ:
  昼12時 : 国内農林業ニュース × 実務コメント
  夜20時 : 産業・経営トレンド × 林業経営への示唆

schedule は下書き生成のみ。即時ライブ投稿は行わない。
"""

import os
import random
import time
import logging
from datetime import datetime, timezone
import tweepy
from openai import OpenAI
import requests
from bs4 import BeautifulSoup
import schedule
import json

from mislead_guard import MISLEAD_GUARD_PROMPT, check_mislead_risk
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

OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-3-mini")
XAI_BASE_URL = os.environ.get("XAI_BASE_URL", "https://api.x.ai/v1")
VALID_PROVIDERS = ("openai", "grok")


def get_openai_client():
    """OpenAI クライアントを遅延初期化する。"""
    global _openai_client
    if _openai_client is None:
        api_key = _require_env("OPENAI_API_KEY")
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        if base_url:
            _openai_client = OpenAI(api_key=api_key, base_url=base_url)
        else:
            _openai_client = OpenAI(api_key=api_key)
    return _openai_client


def get_xai_api_key():
    """xAI / Grok 用キー。XAI_API_KEY を優先、なければ GROK_API_KEY。"""
    return os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")


def get_grok_client():
    """Grok（xAI OpenAI互換）クライアントを遅延初期化する。"""
    global _grok_client
    if _grok_client is None:
        api_key = get_xai_api_key()
        if not api_key:
            raise RuntimeError("必須環境変数 XAI_API_KEY または GROK_API_KEY が未設定です")
        _grok_client = OpenAI(api_key=api_key, base_url=XAI_BASE_URL)
    return _grok_client


def get_client_and_model(provider: str):
    provider = (provider or "openai").lower()
    if provider == "openai":
        return get_openai_client(), OPENAI_MODEL
    if provider == "grok":
        return get_grok_client(), GROK_MODEL
    raise ValueError(f"未対応の provider: {provider}（openai / grok）")


def chat_complete(provider: str, system_prompt: str, user_content: str, temperature: float = 0.75):
    """指定プロバイダで chat completion を1回実行する。"""
    client, model = get_client_and_model(provider)
    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        max_tokens=200,
        temperature=temperature,
    )
    return response.choices[0].message.content.strip()


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
# ニュース収集（Web検索）
# =========================================================
def fetch_forestry_news(query, retry=True):
    """
    Google News RSSで林業関連ニュースを検索して取得する。
    (snippet_text, article_url) のタプルを返す。
    URLが必ず取得できるようリトライあり。
    """
    import xml.etree.ElementTree as ET
    import urllib.parse
    
    def _fetch(q):
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }
        rss_url = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=ja&gl=JP&ceid=JP:ja"
        response = requests.get(rss_url, headers=headers, timeout=15)
        if response.status_code != 200:
            return "", None
        root = ET.fromstring(response.content)
        items = root.findall('.//item')
        snippets = []
        first_url = None
        for item in items[:5]:  # 最大5件まで確認
            title = item.find('title')
            link = item.find('link')
            description = item.find('description')
            # タイトルをスニペットに追加
            if title is not None and title.text:
                t = title.text.strip()
                # 「 - ソース名」の形式を除去しタイトル本文のみ取得
                if ' - ' in t:
                    t = t.rsplit(' - ', 1)[0].strip()
                if t:
                    snippets.append(t)
            # URL取得（最初の有効URL）
            if first_url is None and link is not None and link.text:
                first_url = link.text.strip()
        snippet_text = " / ".join(snippets[:3]) if snippets else ""
        return snippet_text, first_url
    
    try:
        snippet, url = _fetch(query)
        # URLが取得できなかった場合、別クエリでリトライ
        if url is None and retry:
            fallback_query = "林業 国内 最新"
            logger.warning(f"ニュースURL取得失敗。リトライ: {fallback_query}")
            snippet2, url2 = _fetch(fallback_query)
            if url2:
                return snippet2, url2
        return snippet, url
    except Exception as e:
        logger.warning(f"ニュース取得エラー: {e}")
        return "", None


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
def generate_buzz_insight_tweet(article_title, article_snippet, provider="openai"):
    """
    昼12時枠: 国内農林業ニュースを引用し、現場実務コメント付き投稿を生成する。
    provider: openai | grok
    """
    system_prompt = f"""
あなたは新潟で1,500ha規模の森林経営計画を管理し、将来的に3,000haへの拡大を見据える林業経営者「岸本一夫」として、
国内の農林業系ニュースを読んで、現場目線の実務的コメントを含むX投稿を作成します。

【人物像】
・山を「所有」ではなく「経営資源」として捉える実務家
・針葉樹・広葉樹の販売先を工場中心に置く現実的な判断力
・地方の人口減少・人手不足を冷静に見据え、AI・ロボット活用を必然的手段として捉える
・現場の泥臭さを知りつつ、森林総合研究所などのエビデンスに基づいた判断を重視

【投稿の目的】
国内の農林業系ニュースを読んで、自分の現場感覚・経営判断・問題意識を含んだ実務的なコメントを語る。
単なるニュースの要約や紹介ではなく、「自分はこう見る」という第一人称の視点を必ず加える。

【文体の特徴（最重要）】
・1文ごとに必ず改行する。句点「。」の後は必ず改行すること
・短文・中文中心（1文あたり20〜40文字程度）
・「です」「ます」調を基本とする
・「〜だろう」「〜だな」「〜かな」「〜ですな」などの語尾は使わない
・スマートで知性的な口調を保ちつつ、押しつけがましくない
・絵文字は使わない
・AIが書いたような「〜が重要です」「〜を推進します」などの硬い表現は避ける

{MISLEAD_GUARD_PROMPT}

【投稿の構成】
1. 記事のテーマを自分の言葉で簡潔に言及（1文）
2. 現場目線の実務的コメントまたは問題意識（1〜2文）
3. 自分の考えや問いかけ（1文）
4. ハッシュタグ

【厳守事項】
・文字数は全体で140文字以内（ハッシュタグ・改行含む）
・必ず最後に「#林業 #森林 #forest」を付ける
・URLは含めない
・140文字を超えた場合は必ず短縮すること
"""

    user_content = f"""
記事タイトル: {article_title}
記事の概要: {article_snippet[:200] if article_snippet else '（概要なし）'}

上記の国内農林業系ニュースを読んで、現場目線の実務的コメントを含む投稿を作成してください。
価格・相場は記事に根拠が無い限り断定しないでください。
"""

    try:
        tweet_text = chat_complete(provider, system_prompt, user_content, temperature=0.75)
        if len(tweet_text) > 140:
            tweet_text = chat_complete(
                provider,
                system_prompt,
                user_content + f"\n\n（再生成）直前案は{len(tweet_text)}文字でした。140文字以内に収めてください。",
                temperature=0.5,
            )
        return enforce_linebreaks(tweet_text)
    except Exception as e:
        logger.error(f"国内農林業インサイトツイート生成エラー ({provider}): {e}")
        return None


# =========================================================
# ツイート生成（夜20時: 産業・経営トレンド × 林業への示唆）
# =========================================================
INDUSTRY_TREND_SYSTEM_PROMPT = f"""
あなたは新潟で1,500ha規模の森林経営計画を管理し、将来的に3,000haへの拡大を見据える林業経営者「岸本一夫」として、
産業・経営・経済・テクノロジー分野のトレンド記事を読み、林業経営への示唆を含むX投稿を作成します。

【人物像】
・山を「所有」ではなく「経営資源」として捉える実務家
・他産業の経営トレンドから学び、自社の森林経営に応用する視点を持つ
・地方の人口減少・人手不足を冷静に見据え、AI・ロボット活用を必然的手段として捉える
・押しつけがましくなく、知性的に語る

【投稿の目的】
幅広い産業・経営トレンドを引用し、「林業経営ではこう読み替える」という示唆を必ず添える。
国内農林業ニュースの単なる紹介や現場報告だけに終始しない。
記事は林業以外の分野でもよい。必ず林業・森林経営への接続を1文以上入れる。

【文体の特徴（最重要）】
・1文ごとに必ず改行する。句点「。」の後は必ず改行すること
・短文・中文中心（1文あたり20〜40文字程度）
・「です」「ます」調を基本とする
・「〜だろう」「〜だな」「〜かな」「〜ですな」などの語尾は使わない
・絵文字は使わない
・AIが書いたような「〜が重要です」「〜を推進します」などの硬い表現は避ける

{MISLEAD_GUARD_PROMPT}

【投稿の構成】
1. 産業・経営トレンドの要点を自分の言葉で（1文）
2. 林業経営・森林経営への示唆または読み替え（1〜2文）
3. 短い問いかけまたは一言（1文）
4. ハッシュタグ

【厳守事項】
・文字数は全体で140文字以内（ハッシュタグ・改行含む）
・必ず最後に「#林業 #森林 #forest」を付ける
・URLは含めない
・140文字を超えた場合は必ず短縮すること
"""


def generate_industry_trend_tweet(article_title, article_snippet, provider="openai"):
    """
    夜20時枠: 産業・経営トレンド記事を引用し、林業経営への示唆付き投稿を生成する。
    provider: openai | grok
    """
    user_content = f"""
記事タイトル: {article_title}
記事の概要: {article_snippet[:200] if article_snippet else '（概要なし）'}

上記は産業・経営・経済・テクノロジー分野のトレンド記事です。
国内農林業ニュースの要約だけにしないでください。
このトレンドを林業経営にどう読み替えるかを必ず含めて投稿を作成してください。
価格・相場は記事に根拠が無い限り断定しないでください。
"""

    try:
        tweet_text = chat_complete(provider, INDUSTRY_TREND_SYSTEM_PROMPT, user_content, temperature=0.75)
        if len(tweet_text) > 140:
            tweet_text = chat_complete(
                provider,
                INDUSTRY_TREND_SYSTEM_PROMPT,
                user_content + f"\n\n（再生成）直前案は{len(tweet_text)}文字でした。140文字以内に収めてください。",
                temperature=0.5,
            )
        return enforce_linebreaks(tweet_text)
    except Exception as e:
        logger.error(f"産業・経営トレンドツイート生成エラー ({provider}): {e}")
        return None


# =========================================================
# X投稿
# =========================================================
HASHTAGS = "#林業 #forest"

def build_tweet_payload(tweet_text, article_url=None):
    """
    投稿本文を組み立てる（副作用なし）。
    戻り値: (full_text, clean_body) またはエラー時は ValueError。
    """
    if not tweet_text:
        raise ValueError("投稿テキストが空です")

    import re
    clean_body = re.sub(r'#\S+', '', tweet_text).rstrip()
    hashtag_str = HASHTAGS

    if article_url:
        max_body = 104
        if len(clean_body) > max_body:
            clean_body = clean_body[:max_body - 1] + "…"
        full_text = f"{clean_body}\n{hashtag_str}\n{article_url}"
    else:
        max_body = 130
        if len(clean_body) > max_body:
            clean_body = clean_body[:max_body - 1] + "…"
        full_text = f"{clean_body}\n{hashtag_str}"
    return full_text, clean_body


def post_to_x(tweet_text, article_url=None):
    """
    Xにツイートを投稿する。
    - 本文の末尾に必ず HASHTAGS（#林業 #forest）を付ける
    - article_urlがある場合はさらにURLを付ける
    - X上でURLは23文字としてカウントされるため、本文はそれを考慮して制限する
    """
    try:
        full_text, _ = build_tweet_payload(tweet_text, article_url)
    except ValueError as e:
        logger.error(str(e))
        return False

    if article_url:
        logger.info(f"記事URL付き投稿: {article_url}")

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
        logger.info(f"文字数(本文): {len(tweet_text)}")
        return True
    except Exception as e:
        logger.error(f"X投稿エラー: {e}")
        return False


def ensure_post_ready(tweet, article_url):
    """URL・本文・投稿成否を検査し、失敗時は RuntimeError を送出する。"""
    if not article_url:
        raise RuntimeError("記事URLが取得できないため投稿を中止しました")
    if not tweet:
        raise RuntimeError("投稿文を生成できませんでした")
    if not post_to_x(tweet, article_url):
        raise RuntimeError("Xへの投稿に失敗しました")


def print_draft_for_human(draft: dict):
    """Cursor / ログ向けに二系統案を見やすく出す。"""
    logger.info("=" * 60)
    logger.info(f"DRAFT_ID: {draft['id']}")
    logger.info(f"SLOT: {draft['slot']}")
    logger.info(f"STATUS: {draft.get('status')}")
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
            logger.info(f"[{provider}] text:\n{cand.get('text')}")
    logger.info("=" * 60)
    logger.info(
        "承認後の投稿例: CONFIRM_LIVE_POST=1 python forestry_bot.py approve "
        f"{draft['id']} openai|grok"
    )


def build_dual_candidates(title: str, snippet: str, generator):
    """同一ネタで openai / grok の案を作る。"""
    candidates = {}
    for provider in VALID_PROVIDERS:
        try:
            text = generator(title, snippet, provider=provider)
            ok, flags = check_mislead_risk(text or "")
            candidates[provider] = {
                "text": text,
                "guard_ok": ok,
                "flags": flags,
                "model": OPENAI_MODEL if provider == "openai" else GROK_MODEL,
            }
        except Exception as e:
            candidates[provider] = {
                "text": None,
                "guard_ok": False,
                "flags": ["generation_error"],
                "error": str(e),
            }
    return candidates


def create_dual_draft(slot: str, title: str, snippet: str, article_url: str, generator) -> dict:
    if not article_url:
        raise RuntimeError("記事URLが取得できないため下書きを中止しました")
    candidates = build_dual_candidates(title, snippet or "", generator)
    draft = {
        "id": new_draft_id(slot),
        "slot": slot,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "pending",
        "article": {
            "title": title,
            "snippet": (snippet or "")[:500],
            "url": article_url,
        },
        "candidates": candidates,
    }
    path = save_draft(draft)
    logger.info(f"下書きを保存しました: {path}")
    print_draft_for_human(draft)
    return draft


def collect_noon_article():
    """昼12時用の記事を取得する。(title, snippet, url)"""
    buzz_queries = [
        "林業 国内 最新",
        "木材 市場 国産材",
        "森林 整備 政策",
        "林野庁 新しい林業",
        "木材利用 建築 国産材",
        "林業 機械化 ドローン",
        "森林経営計画 山村",
        "国産材 活用 建築",
        "林業 カーボンクレジット",
        "木材価格 山林 市況",
    ]
    query = random.choice(buzz_queries)
    logger.info(f"昼12時 国内農林業ニュース検索クエリ: {query}")
    news, article_url = fetch_forestry_news(query)
    title = query
    snippet = news
    if not (news and article_url):
        logger.warning("ニュース取得不足。別クエリで再取得します。")
        for fq in ["林業 最新", "国産材 活用", "木材 市場"]:
            news2, url2 = fetch_forestry_news(fq, retry=False)
            if news2 and url2:
                title, snippet, article_url = fq, news2, url2
                break
    if not article_url:
        raise RuntimeError("記事URLが取得できないため下書きを中止しました")
    return title, snippet, article_url


def collect_evening_article():
    """夜20時用の産業・経営トレンド記事を取得する。"""
    title, snippet, article_url = fetch_todays_buzz_article()
    if title and article_url:
        return title, snippet, article_url
    logger.warning("トレンド記事取得失敗。産業・経営系クエリで代替します。")
    for fq in INDUSTRY_TREND_FALLBACK_QUERIES:
        news2, url2 = fetch_forestry_news(fq, retry=False)
        if news2 and url2:
            return fq, news2, url2
    raise RuntimeError("記事URLが取得できないため下書きを中止しました")


def noon_job():
    """昼12時: 下書きのみ（投稿しない）。"""
    logger.info("=== 昼12時 下書き生成（OpenAI + Grok）===")
    title, snippet, article_url = collect_noon_article()
    return create_dual_draft("12:00", title, snippet, article_url, generate_buzz_insight_tweet)


def pre_evening_job():
    """夜20時: 下書きのみ（投稿しない）。"""
    logger.info("=== 夜20時 下書き生成（OpenAI + Grok）===")
    title, snippet, article_url = collect_evening_article()
    logger.info(f"取得記事: {title}")
    return create_dual_draft(
        "20:00", title, snippet, article_url, generate_industry_trend_tweet
    )


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
    url = (draft.get("article") or {}).get("url")
    if not tweet:
        raise RuntimeError(f"{provider} の投稿案がありません: {cand.get('error')}")
    if cand.get("guard_ok") is False:
        logger.warning(
            f"ミスリード警告フラグあり: {cand.get('flags')} — "
            "CONFIRM_LIVE_POST=1 でも続行しますが、内容を再確認してください。"
        )

    ensure_post_ready(tweet, url)
    mark_draft_posted(draft_id, provider)
    logger.info(f"承認投稿完了: draft={draft_id} provider={provider}")


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
        ok, flags = check_mislead_risk(sample)
        logger.info(f"ok={ok} flags={flags} text={sample}")
    else:
        logger.info(
            "用法:\n"
            "  python forestry_bot.py draft 12:00|20:00\n"
            "  python forestry_bot.py list-drafts\n"
            "  python forestry_bot.py show-draft <id>\n"
            "  CONFIRM_LIVE_POST=1 python forestry_bot.py approve <id> openai|grok\n"
            "  python forestry_bot.py dry-run-format"
        )
