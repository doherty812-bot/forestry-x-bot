"""ミスリード抑制用の簡易チェック（価格煽り・過剰断定など）。"""

from __future__ import annotations

import re
from typing import List, Tuple

# 価格・相場の断定・煽り（根拠なし投稿の再発防止）
PRICE_HYPE_PATTERNS = [
    r"高騰",
    r"暴騰",
    r"急騰",
    r"爆上がり",
    r"暴落",
    r"急落",
    r"暴落中",
    r"価格が上がり続け",
    r"相場が急変",
]

ABSOLUTE_PATTERNS = [
    r"必ず",
    r"絶対に",
    r"間違いなく",
    r"確実に上が",
    r"確実に下が",
]

MISLEAD_GUARD_PROMPT = """
【ミスリード防止（厳守）】
・木材価格・相場について「高騰」「暴騰」「急騰」「暴落」「急落」など断定・煽りの表現は使わない
・記事に具体的な価格・統計が明示されていない限り、相場の方向性を断定しない
・「必ず」「絶対」「間違いなく」などの過剰断定は使わない
・不確実なことは「〜かもしれません」「現場では温度差があります」など留保する
・過去の一般論や印象を、あたかも直近の確定事実のように書かない
・センセーショナルな煽りでクリックを誘う文体にしない
"""


def check_mislead_risk(text: str) -> Tuple[bool, List[str]]:
    """
    簡易チェック。問題があれば (False, flags)、問題なしなら (True, [])。
    人間承認の補助であり、最終判断は所有者。
    """
    if not text:
        return False, ["empty_text"]

    flags: List[str] = []
    for pat in PRICE_HYPE_PATTERNS:
        if re.search(pat, text):
            flags.append(f"price_hype:{pat}")
    for pat in ABSOLUTE_PATTERNS:
        if re.search(pat, text):
            flags.append(f"absolute:{pat}")

    return (len(flags) == 0, flags)
