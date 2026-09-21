#!/usr/bin/env python3
"""
林業Xアカウント自動投稿ボット
岸本一夫さんのXアカウント向け
1日2回（昼12時・夜20時 JST）に林業関連トレンドを投稿する

時間帯別コンテンツ:
  昼12時 : 国内農林業ニュース × 実務コメント（Google News RSS取得）
  夜20時 : 現行は国内農林業ニュース引用 × 現場視座
           （再開前に「産業・経営トレンド」へ戻すか所有者確認が必要）

認証情報は環境変数必須。実投稿CLIは CONFIRM_LIVE_POST=1 が必要。
詳細は README.md / CURSOR_HANDOVER.md を参照。
"""

import os
import random
import time
import logging
from datetime import datetime
import tweepy
from openai import OpenAI
import requests
from bs4 import BeautifulSoup
import schedule
import json

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
# その日のバズ記事取得（夜20時渠用）
# =========================================================
def fetch_todays_buzz_article():
    """
    その日のバズ記事をGoogle News RSSから取得する。
    ビジネス・経済・社会・テクノロジー分野から幅広く取得し、
    林業経営に応用できる記事を選択する。
    (title, snippet, url) のタプルを返す。
    """
    import xml.etree.ElementTree as ET
    import urllib.parse
    
    # 国内農林業系ニュースのクエリリスト
    buzz_queries = [
        "林業 国内 最新",
        "林木 木材 市場",
        "森林 整備 地域",
        "林野庁 政策",
        "木材利用 建築",
        "農業 林業 人手不足",
        "山村 地域振興",
        "林業 機械化 ドローン",
        "国産材 活用 建築",
        "林業 カーボンクレジット",
        "里山 整備 武装化",
        "木材 価格 山林",
    ]
    query = random.choice(buzz_queries)
    logger.info(f"夜20時 国内農林業系ニュース検索クエリ: {query}")
    
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
        
        # 最新記事の先頭3件からランダムに1件選択
        selected = random.choice(items[:3])
        title = selected.find('title')
        link = selected.find('link')
        description = selected.find('description')
        
        title_text = title.text if title is not None else ''
        url_text = link.text if link is not None else None
        snippet_text = (description.text or '')[:300] if description is not None else ''
        
        return title_text, snippet_text, url_text
    except Exception as e:
        logger.warning(f"バズ記事取得エラー: {e}")
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
# ツイート生成（その日のバズ記事引用＋林業経営者視座の深みある投稿）
# =========================================================
def generate_buzz_insight_tweet(article_title, article_snippet):
    """
    その日のバズ記事を引用し、林業経営者の視座で深みのある投稿を生成する（夜20時渠）
    """
    system_prompt = """
あなたは新潟で１，５００ha規模の森林経営計画を管理し、将来的に3，000haへの拡大を見据える林業経営者「岸本一夫」として、
国内の農林業系ニュースを読んで、現場目線の実務的コメントを含むX投稿を作成します。

【人物像】
・山を「所有」ではなく「経営資源」として捉える実務家
・针葉樹・広葉樹の販売先を工場中心に置く現実的な判断力
・地方の人口減少・人手不足を冷静に見据え、AI・ロボット活用を必然的手段として捐える
・現場の泥臭さを知りつつ、森林総合研究所などのエビデンスに基づいた判断を重視

【投稿の目的】
国内の農林業系ニュースを読んで、自分の現場感覚・経営判断・問題意識を包んだ実務的なコメントを語る。
単なるニュースの要約や紹介ではなく、「自分はこう見る」という第一人称の視点を必ず加える。

【文体の特徴（最重要）】
・1文ごとに必ず改行する。句点「。」の後は必ず改行すること
・短文・中文中心（1文あたり20〜40文字程度）
・「です」「ます」調を基本とする。語尾は「〜です。」「〜ます。」「〜ですね。」「〜でしょうか。」「〜かもしれません。」など
・「〜だろう」「〜だな」「〜かな」「〜ですな」などの語尾は使わない
・「海外では〜」「世界では〜」などの海外起起の表現は絶対に使わない
・スマートで知性的な口調を保ちつつ、押しつけがましくない
・絵文字は使わない
・AIが書いたような「〜が重要です」「〜を推進します」などの硬い表現は避ける

【投稿の構成】
1. 記事のテーマを自分の言葉で簡潔に言及（1文）
2. 現場目線の実務的コメントまたは問題意識（1〜2文）
3. 自分の考えや問いかけ（1文）
4. ハッシュタグ

【実際の投稿例（この文体を参考にすること）】
「学校や公共施設への木材活用が進まないと、行政はなかなか動きません。
里山整備と災害対策、同時に進める必要があります。」

「今年の木材価格は少し落ち著きました。
工場との値段交渉が、また気居の悪い季節になりそうです。」

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
「海外では〜」といった表現は絶対に使わないでください。国内の現場感覚で語ってください。
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
        logger.error(f"バズ記事洞察ツイート生成エラー: {e}")
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


# =========================================================
# 時間帯別ジョブ
# =========================================================

def early_morning_job():
    """朝6時の投稿（海外の森林関連バズ記事を日本語で紹介）※現行定期枠外・互換用"""
    logger.info("=== 朝6時 海外バズ記事紹介ジョブ開始 ===")
    query, articles = fetch_global_forest_buzz()
    tweet = generate_global_buzz_tweet(query, articles)
    article_url = None
    if articles:
        article_url = articles[0].get('url')
    ensure_post_ready(tweet, article_url)

def morning_job():
    """朝7時の投稿（国内政策・ニュース系）※現行定期枠外・互換用"""
    logger.info("=== 朝7時 国内政策ジョブ開始 ===")
    category, topic = random.choice(MORNING_TOPICS)
    news, article_url = fetch_forestry_news(f"林業 {topic[:20]} 2025 2026")
    tweet = generate_tweet(category, topic, news)
    ensure_post_ready(tweet, article_url)

def noon_job():
    """昼12時の投稿（国内農林業ニュース×実務コメント）"""
    logger.info("=== 昼12時 国内農林業ニュース×実務コメント ジョブ開始 ===")
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
    if news:
        tweet = generate_buzz_insight_tweet(query, news)
    else:
        logger.warning("ニューススニペットなし。別クエリで再取得します。")
        fallback_queries = ["林業 最新", "国産材 活用", "木材 市場"]
        tweet = None
        for fq in fallback_queries:
            news2, url2 = fetch_forestry_news(fq, retry=False)
            if news2 and url2:
                news, article_url = news2, url2
                break
        category, topic = random.choice(NOON_TOPICS)
        tweet = generate_tweet(category, topic, news)
    ensure_post_ready(tweet, article_url)

def pre_evening_job():
    """
    夜20時の投稿。
    現行実装は国内農林業ニュース引用＋現場コメント。
    再開前に「国内農林業」固定か「産業・経営トレンド」へ戻すかを所有者確認すること。
    """
    logger.info("=== 夜20時 国内農林業ニュース引用・林業経営者視座ジョブ開始 ===")
    title, snippet, article_url = fetch_todays_buzz_article()
    tweet = None
    if title:
        logger.info(f"取得記事: {title}")
        tweet = generate_buzz_insight_tweet(title, snippet)
    else:
        logger.warning("バズ記事取得失敗。国内林業ニュースで代替します。")
        fallback_queries = ["林業 国内 最新", "木材 市場 国産材", "森林 整備 政策"]
        for fq in fallback_queries:
            news2, url2 = fetch_forestry_news(fq, retry=False)
            if news2 and url2:
                article_url = url2
                tweet = generate_buzz_insight_tweet(fq, news2)
                break
        else:
            raise RuntimeError("記事URLが取得できないため投稿を中止しました")
    ensure_post_ready(tweet, article_url)

def evening_job():
    """夜21時の投稿（海外トレンド・研究情報系）※現行定期枠外・互換用"""
    logger.info("=== 夜21時 海外トレンド・研究情報ジョブ開始 ===")
    category, topic = random.choice(EVENING_TOPICS)
    if category == "海外トレンド":
        news, article_url = fetch_forestry_news("forest forestry trend 2025 2026")
    else:
        news, article_url = fetch_forestry_news(f"森林総合研究所 {topic[:15]}")
    tweet = generate_tweet(category, topic, news)
    ensure_post_ready(tweet, article_url)


# =========================================================
# スケジューラー設定（ローカル常駐用・GitHub Actionsでは未使用）
# =========================================================
def setup_scheduler():
    """スケジュールを設定する（JST基準）。本番定期実行はGitHub Actions側。"""
    # 現行の本番枠は昼12時・夜20時のみ。以下は互換・ローカル検証用。
    schedule.every().day.at("03:00").do(noon_job)            # 昼12時 JST
    schedule.every().day.at("11:00").do(pre_evening_job)     # 夜20時 JST

    logger.info("スケジューラー設定完了（本番2枠: 12:00 / 20:00 JST）")
    logger.info("  昼12時 JST (UTC 03:00): 国内農林業ニュース×実務コメント")
    logger.info("  夜20時 JST (UTC 11:00): 国内農林業ニュース引用・現場視座")

def run_scheduler():
    """スケジューラーを実行する（CONFIRM_LIVE_POST=1 必須）"""
    require_live_post_confirmation()
    setup_scheduler()
    logger.info("自動投稿ボット起動。次の投稿時刻を待機中...")
    while True:
        schedule.run_pending()
        time.sleep(30)


# =========================================================
# メイン
# =========================================================
if __name__ == "__main__":
    import sys

    LIVE_POST_ARGS = {"test_all", "test", "test_quote", "12:00", "20:00", "run"}

    if len(sys.argv) > 1 and sys.argv[1] in LIVE_POST_ARGS:
        require_live_post_confirmation()

    if len(sys.argv) > 1 and sys.argv[1] == "test_all":
        logger.info("=== 全ジョブ テストモード実行（実投稿）===")
        early_morning_job()
        morning_job()
        noon_job()
        pre_evening_job()
        evening_job()
    elif len(sys.argv) > 1 and sys.argv[1] == "test":
        logger.info("=== テストモード実行（朝6時ジョブ・実投稿）===")
        early_morning_job()
    elif len(sys.argv) > 1 and sys.argv[1] == "test_quote":
        logger.info("=== テストモード実行（夜20時ジョブ・実投稿）===")
        pre_evening_job()
    elif len(sys.argv) > 1 and sys.argv[1] == "12:00":
        logger.info("=== 昼12時枠 国内農林業ニュース×実務コメント 投稿 ===")
        noon_job()
    elif len(sys.argv) > 1 and sys.argv[1] == "20:00":
        logger.info("=== 夜20時枠 国内農林業ニュース引用 投稿 ===")
        pre_evening_job()
    elif len(sys.argv) > 1 and sys.argv[1] == "run":
        run_scheduler()
    elif len(sys.argv) > 1 and sys.argv[1] == "dry-run-format":
        # 実投稿なし: 本文組み立てのみ検証
        sample = "山を経営資源として見る視点が大切です。\n現場の感覚も忘れません。"
        url = "https://news.google.com/articles/example"
        full, _ = build_tweet_payload(sample, url)
        logger.info(f"dry-run payload:\n{full}")
        assert HASHTAGS in full
        assert url in full
        logger.info("dry-run-format OK")
    else:
        logger.info(
            "実投稿なしの既定モードです。"
            "利用: dry-run-format | CONFIRM_LIVE_POST=1 付きで 12:00 / 20:00 / run など"
        )
        sample = "国産材の需要を現場で感じています。\n価格交渉の季節でもあります。"
        full, _ = build_tweet_payload(sample, "https://example.com/news")
        logger.info(f"サンプル組み立て ({len(full)}文字):\n{full}")
