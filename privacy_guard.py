"""X投稿本文への個人情報・連絡先の混入を抑止する簡易ガード。"""

from __future__ import annotations

import re
from typing import List, Tuple

# 投稿本文に出してはいけない既知の氏名フル（ロールプレイ用の内部人物像とは別）
KNOWN_FULL_NAME_TOKENS = (
    "岸本一夫",
)

# メールっぽい文字列（ドメインは問わない）
EMAIL_RE = re.compile(
    r"(?i)\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"
)

# 個人向けフリーメールの明示（部分一致）
PERSONAL_MAIL_MARKERS = (
    "@gmail.",
    "@yahoo.",
    "@outlook.",
    "@hotmail.",
    "@icloud.",
    "@me.com",
)

# 日本の電話番号っぽい並び（ハイフン有無。年月や面積の誤検知を抑える）
PHONE_RE = re.compile(
    r"(?<!\d)"
    r"(?:"
    r"0[789]0(?:[-‐−–—]?\d{4}){2}"  # 携帯
    r"|"
    r"0\d{1,4}[-‐−–—]\d{1,4}[-‐−–—]\d{3,4}"  # 固定（区切り必須）
    r"|"
    r"\(0\d{1,4}\)\s*\d{1,4}[-‐−–—]?\d{3,4}"
    r")"
    r"(?!\d)"
)

# 事務所・所在地・連絡先の明示（屋号そのものはここでは禁止しない）
OFFICE_CONTACT_PATTERNS = [
    r"事務所",
    r"本社所在地",
    r"所在地[：:]",
    r"住所[：:]",
    r"〒\s*\d{3}-?\d{4}",
    r"電話番号",
    r"(?i)\bTEL\b",
    r"(?i)\bFAX\b",
    r"連絡先[：:]",
]

PRIVACY_GUARD_PROMPT = """
【個人情報・連絡先の取り扱い（厳守・両モデル共通）】
・Obsidian（CLAUDE.md / profile.md 等）は文体・事業文脈・方針の背景参照のみに使う
・X投稿本文に次を直接書いてはいけない:
  - 氏名フル（例: フルネームでの自己紹介・署名）
  - 個人メール・メールアドレス全般
  - 事務所の住所・電話・FAX・「事務所」表記での所在案内
・屋号・事業者名（例: 有限会社丸実）は文脈上どうしても必要なときだけ最小限。迷ったら載せない
・事務所の所在地・連絡先は禁止。APIキーや秘密も禁止
・ノートの連絡先ブロックを要約・転記しない
"""


def check_privacy_risk(text: str) -> Tuple[bool, List[str]]:
    """
    簡易チェック。問題があれば (False, flags)、問題なしなら (True, [])。
    人間承認の補助であり、最終判断は所有者。
    """
    if not text:
        return False, ["empty_text"]

    flags: List[str] = []

    for name in KNOWN_FULL_NAME_TOKENS:
        if name and name in text:
            flags.append(f"pii_full_name:{name}")

    if EMAIL_RE.search(text):
        flags.append("pii_email")

    lower = text.lower()
    for marker in PERSONAL_MAIL_MARKERS:
        if marker in lower:
            if "pii_email" not in flags and "pii_personal_mail_marker" not in flags:
                flags.append("pii_personal_mail_marker")
            break

    if PHONE_RE.search(text):
        flags.append("pii_phone")

    for pat in OFFICE_CONTACT_PATTERNS:
        if re.search(pat, text):
            flags.append(f"pii_office_contact:{pat}")

    return (len(flags) == 0, flags)
