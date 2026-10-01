"""AIっぽさ除去の校正レビュー（ヒューリスティック + 任意の第二 LLM 呼び出し）。

下書き候補（OpenAI / Grok）ごとに、finalize / Cursor 通知の前に通す。
失敗時は原文を残し flags で soft-fail（空本文の silent 化はしない）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 既定: 有効。0 / false / off / no で無効
DEFAULT_ANTI_AI_COPYEDIT = True
DEFAULT_ANTI_AI_COPYEDIT_TEMPERATURE = 0.35
# same = 候補と同じ provider / openai / grok
DEFAULT_ANTI_AI_COPYEDIT_PROVIDER = "same"
# 0=毎回 LLM 校正（既定） / 1=ヒューリスティックが検知したときだけ
DEFAULT_ANTI_AI_COPYEDIT_ON_FLAGS_ONLY = False

# 検知用パターン → flag 名
AI_ISH_PATTERNS: List[Tuple[str, str]] = [
    (r"ではないでしょうか", "rhetorical_deshouka"),
    (r"ではないかと(?:思い|考え)", "hedged_dehanaika"),
    (r"いかがでしょうか", "reader_ikaga"),
    (r"どう思(?:います|われます)か", "reader_question"),
    (r"皆(?:さん|様)は", "reader_address"),
    (r"皆様の現場", "reader_address"),
    (r"どのようにお考えですか", "reader_question"),
    (r"重要です[。．!！]?", "slogan_juuyou"),
    (r"注目されて(?:い)?ます", "slogan_chumoku"),
    (r"注目を集めて(?:い)?ます", "slogan_chumoku"),
    (r"ますます重要", "slogan_masumasu"),
    (r"必要不可欠", "slogan_fukaketsu"),
    (r"持続可能な未来", "abstract_slogan"),
    (r"新たなステージ", "abstract_slogan"),
    (r"パラダイムシフト", "abstract_slogan"),
    (r"変革の波", "abstract_slogan"),
    (r"結論として", "template_wrap"),
    (r"まとめると", "template_wrap"),
    (r"総じて", "template_wrap"),
    (r"それゆえ", "heavy_connective"),
    (r"しかしながら", "heavy_connective"),
    (r"第一に[\s\S]{0,80}第二に", "enum_template"),
    (r"①[\s\S]{0,80}②[\s\S]{0,80}③", "enum_template"),
    (r"また、[\s\S]{0,60}さらに、[\s\S]{0,60}加えて", "connective_stack"),
]

ANTI_AI_COPYEDIT_SYSTEM = """
あなたは日本語のX（Twitter）スレッド校正者です。AIっぽい説明文を、現場の経営者の一人称の声に直します。

【必ずやること】
・「〜ではないでしょうか」「いかがでしょうか」など読者への問いかけを削る／言い切る
・「重要です」「注目されています」「持続可能な未来」など抽象スローガンを具体の現場語に落とす
・過度な列挙テンプレ（第一に／第二に、①②③）と接続詞の積み上げを減らす
・一人称の意志を残す／強める（「私は〜と考えます」「私はこう進めます」）
・短めの文。スマホで読める密度。各ポストの役割（フック→続き→意志）を保つ
・posts の件数と順序を維持する（欠けさせない・勝手に増やしすぎない）
・事実・数字・出典ラベルは原文にあるものだけ使う。数字や価格煽りを新たに作らない

【やらないこと】
・ハッシュタグ・URL を本文に足す
・氏名・メール・事務所住所／電話を足す
・「高騰」「暴騰」「必ず」など価格煽り・過剰断定を足す
・説明だけの長い壁テキストに戻す

【出力】
次の JSON だけ。前後に説明やコードフェンスを付けない。
{"posts":[{"text":"..."},{"text":"..."}]}
""".strip()


def env_truthy(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def copyedit_enabled() -> bool:
    return env_truthy("ANTI_AI_COPYEDIT", DEFAULT_ANTI_AI_COPYEDIT)


def copyedit_on_flags_only() -> bool:
    return env_truthy(
        "ANTI_AI_COPYEDIT_ON_FLAGS_ONLY", DEFAULT_ANTI_AI_COPYEDIT_ON_FLAGS_ONLY
    )


def copyedit_temperature() -> float:
    raw = os.environ.get("ANTI_AI_COPYEDIT_TEMPERATURE")
    if raw is None or not str(raw).strip():
        return DEFAULT_ANTI_AI_COPYEDIT_TEMPERATURE
    try:
        return max(0.0, min(1.5, float(str(raw).strip())))
    except ValueError:
        return DEFAULT_ANTI_AI_COPYEDIT_TEMPERATURE


def resolve_copyedit_provider(candidate_provider: str) -> str:
    raw = (os.environ.get("ANTI_AI_COPYEDIT_PROVIDER") or DEFAULT_ANTI_AI_COPYEDIT_PROVIDER).strip().lower()
    if raw in ("", "same", "auto"):
        return (candidate_provider or "openai").lower()
    if raw in ("openai", "grok"):
        return raw
    logger.warning(f"不明な ANTI_AI_COPYEDIT_PROVIDER={raw!r} → same 扱い")
    return (candidate_provider or "openai").lower()


def posts_plain_text(posts: List[Dict[str, Any]]) -> str:
    parts = []
    for p in posts or []:
        t = (p.get("text") if isinstance(p, dict) else str(p)) or ""
        t = str(t).strip()
        if t:
            parts.append(t)
    return "\n".join(parts)


def detect_ai_ish(text: str) -> List[str]:
    """AIっぽい表現の簡易検知。戻り値は flag 名のリスト（重複なし）。"""
    if not text or not str(text).strip():
        return []
    flags: List[str] = []
    for pat, name in AI_ISH_PATTERNS:
        if re.search(pat, text):
            flag = f"anti_ai:{name}"
            if flag not in flags:
                flags.append(flag)
    # 読者問いかけの？多用（一人称の意志文以外）
    if re.search(r"(?:でしょう|ですか|ますか)[？?]", text):
        flag = "anti_ai:question_mark"
        if flag not in flags:
            flags.append(flag)
    return flags


def build_copyedit_user_prompt(posts: List[Dict[str, Any]], flags: List[str]) -> str:
    payload = {
        "posts": [{"text": (p.get("text") or "").strip()} for p in posts if (p.get("text") or "").strip()],
        "detected_flags": flags,
    }
    return (
        "次のスレッド案を、AIっぽさを減らした人間の声に校正してください。"
        "事実・数字は増やさず、posts 構造を保ち、JSON だけ返してください。\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )


def _parse_copyedit_posts(raw: str, expected_min: int = 1) -> List[Dict[str, Any]]:
    """LLM 応答を posts に落とす（最小パーサ。forestry_bot.parse_thread_response と互換志向）。"""
    text = (raw or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if not text:
        raise ValueError("校正応答が空です")

    candidates = [text]
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        candidates.insert(0, m.group(0))

    for cand in candidates:
        try:
            data = json.loads(cand)
        except json.JSONDecodeError:
            continue
        posts_raw = None
        if isinstance(data, dict) and isinstance(data.get("posts"), list):
            posts_raw = data["posts"]
        elif isinstance(data, list):
            posts_raw = data
        if not posts_raw:
            continue
        out: List[Dict[str, Any]] = []
        for item in posts_raw:
            if isinstance(item, str) and item.strip():
                out.append({"text": item.strip()})
            elif isinstance(item, dict):
                t = (item.get("text") or item.get("body") or "").strip()
                if t:
                    out.append({"text": t})
        if len(out) >= expected_min:
            return out
        if out:
            return out
    raise ValueError("校正応答から posts を解釈できません")


ChatFn = Callable[..., str]


def review_thread_posts(
    posts: List[Dict[str, Any]],
    *,
    candidate_provider: str,
    chat_complete: ChatFn,
    parse_posts: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
) -> Dict[str, Any]:
    """
    スレッド posts を校正レビューする。

    戻り値:
      {
        "posts": [...],          # 採用本文（失敗時は原文）
        "changed": bool,
        "status": "ok"|"skipped"|"disabled"|"soft_fail",
        "provider": str|None,
        "flags_before": [...],
        "flags_after": [...],
        "error": str|None,
      }
    """
    original = [
        {"text": (p.get("text") or "").strip(), **{k: v for k, v in p.items() if k != "text"}}
        for p in (posts or [])
        if (p.get("text") or "").strip()
    ]
    joined = posts_plain_text(original)
    flags_before = detect_ai_ish(joined)

    meta: Dict[str, Any] = {
        "posts": original,
        "changed": False,
        "status": "disabled",
        "provider": None,
        "flags_before": flags_before,
        "flags_after": flags_before,
        "error": None,
    }

    if not original:
        meta["status"] = "soft_fail"
        meta["error"] = "empty_posts"
        meta["flags_after"] = list(flags_before) + ["anti_ai_copyedit_soft_fail:empty_posts"]
        return meta

    if not copyedit_enabled():
        meta["status"] = "disabled"
        return meta

    if copyedit_on_flags_only() and not flags_before:
        meta["status"] = "skipped"
        return meta

    provider = resolve_copyedit_provider(candidate_provider)
    meta["provider"] = provider
    user_prompt = build_copyedit_user_prompt(original, flags_before)
    parser = parse_posts or _parse_copyedit_posts

    try:
        raw = chat_complete(
            provider,
            ANTI_AI_COPYEDIT_SYSTEM,
            user_prompt,
            temperature=copyedit_temperature(),
        )
        rewritten = parser(raw)
        # 空や極端な欠落は soft-fail
        if not rewritten or not any((p.get("text") or "").strip() for p in rewritten):
            raise ValueError("校正結果が空です")
        cleaned: List[Dict[str, Any]] = []
        for i, p in enumerate(rewritten):
            t = (p.get("text") or "").strip()
            if not t:
                continue
            item: Dict[str, Any] = {"text": t}
            # 元ポストの media 指定があれば先頭側で引き継ぐ
            if i < len(original) and original[i].get("media_source_indexes"):
                item["media_source_indexes"] = original[i]["media_source_indexes"]
            cleaned.append(item)
        if not cleaned:
            raise ValueError("校正後に有効ポストがありません")

        # 件数が大きく減った場合は危険なので soft-fail（原文維持）
        if len(cleaned) < max(1, len(original) - 1):
            raise ValueError(
                f"校正後の件数が減りすぎ: {len(original)} → {len(cleaned)}"
            )

        flags_after = detect_ai_ish(posts_plain_text(cleaned))
        meta.update(
            {
                "posts": cleaned,
                "changed": posts_plain_text(cleaned) != joined,
                "status": "ok",
                "flags_after": flags_after,
                "error": None,
            }
        )
        logger.info(
            f"anti-ai copyedit ok provider={provider} "
            f"flags_before={flags_before} flags_after={flags_after} changed={meta['changed']}"
        )
        return meta
    except Exception as e:
        err = str(e)
        logger.warning(
            f"anti-ai copyedit soft-fail provider={provider}: {err} （原文を維持）"
        )
        soft_flags = list(flags_before)
        soft_flag = f"anti_ai_copyedit_soft_fail:{err[:80]}"
        if soft_flag not in soft_flags:
            soft_flags.append(soft_flag)
        if "anti_ai_copyedit_soft_fail" not in soft_flags:
            soft_flags.append("anti_ai_copyedit_soft_fail")
        meta.update(
            {
                "posts": original,
                "changed": False,
                "status": "soft_fail",
                "flags_after": soft_flags,
                "error": err,
            }
        )
        return meta
