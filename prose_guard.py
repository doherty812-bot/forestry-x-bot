"""スマホ向け文体（短文・改行密度）の正規化と簡易チェック。"""

from __future__ import annotations

import re
from typing import List, Tuple

# 1文の目安上限（これを超えると long_sentence フラグ）
MAX_SENTENCE_CHARS = 80

# 段落として残す空行は最大1つ（\n\n）
_MULTI_BLANK_RE = re.compile(r"\n{3,}")


def _is_joinable_line(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith("#"):
        return False
    if s.startswith("http://") or s.startswith("https://"):
        return False
    return True


def _is_single_short_sentence(block: str) -> bool:
    """空行区切りの一塊が、短い一文だけか。"""
    s = block.strip()
    if not _is_joinable_line(s):
        return False
    # 末尾の句点以外に句点がある＝複数文
    core = s.rstrip("。．！？!?…")
    if re.search(r"[。．！？!?]", core):
        return False
    return len(s) <= 60


def normalize_prose_breaks(text: str) -> str:
    """
    スマホ向けに改行を正規化する。

    - 句点ごとの強制改行はしない（旧 enforce_linebreaks の挿入は廃止）
    - 連続空行は段落区切り1つ（空行1つ）に圧縮
    - 段落内の1文1行バラしは同一段落に結合
    - 短い一文だけの段落が連続する場合は意味のある段落になるまで結合
    - ハッシュタグ行・URL行は結合しない
    - 行末空白を除去
    """
    if not text:
        return text

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.rstrip() for ln in text.split("\n")]

    # 連続空行 → 空行1つ
    compacted: List[str] = []
    prev_blank = False
    for ln in lines:
        blank = ln.strip() == ""
        if blank:
            if not prev_blank:
                compacted.append("")
            prev_blank = True
        else:
            compacted.append(ln)
            prev_blank = False

    # 空行で段落分割
    raw_paras: List[List[str]] = []
    current: List[str] = []
    for ln in compacted:
        if ln == "":
            if current:
                raw_paras.append(current)
                current = []
        else:
            current.append(ln)
    if current:
        raw_paras.append(current)

    def join_para_lines(para_lines: List[str]) -> str:
        chunks: List[str] = []
        buf = ""
        for ln in para_lines:
            if not _is_joinable_line(ln):
                if buf:
                    chunks.append(buf)
                    buf = ""
                chunks.append(ln.strip())
                continue
            piece = ln.strip()
            buf = piece if not buf else buf + piece
        if buf:
            chunks.append(buf)
        return "\n".join(chunks)

    joined = [join_para_lines(p) for p in raw_paras]

    # 短い一文段落の連続を結合（スカスカ防止）。ハッシュタグ等は区切る。
    merged: List[str] = []
    buf: str | None = None
    for block in joined:
        if _is_single_short_sentence(block):
            if buf is None:
                buf = block.strip()
            else:
                buf = buf + block.strip()
            # 適度な塊になったら確定（2〜3文 or 長さ）
            if buf.count("。") + buf.count("！") + buf.count("？") >= 3 or len(buf) >= 120:
                merged.append(buf)
                buf = None
        else:
            if buf is not None:
                merged.append(buf)
                buf = None
            merged.append(block.strip())
    if buf is not None:
        merged.append(buf)

    out = "\n\n".join(m for m in merged if m)
    out = _MULTI_BLANK_RE.sub("\n\n", out)
    return out.strip()


def check_prose_risk(text: str) -> Tuple[bool, List[str]]:
    """
    スマホ向け文体の簡易チェック。

    戻り値の bool は常に True（承認ブロックしない警告フラグ）。
    flags 例: long_sentence:95 / excessive_linebreaks
    """
    if not text or not str(text).strip():
        return True, []

    flags: List[str] = []

    # ハッシュタグ行を除いて文長を見る
    body_lines = []
    for ln in text.split("\n"):
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        body_lines.append(s)
    body = "".join(body_lines) if body_lines else text

    # 句点単位で文を分割（改行は文長に数えない）
    parts = re.split(r"(?<=[。！？!?])", body)
    for part in parts:
        s = part.replace("\n", "").strip()
        if not s:
            continue
        if len(s) > MAX_SENTENCE_CHARS:
            flags.append(f"long_sentence:{len(s)}")
            break

    # 改行過多: 本文行が多く、ほぼ1文1行
    sentence_count = len(
        [s for s in re.split(r"[。！？!?]", re.sub(r"#\S+", "", text)) if s.strip()]
    )
    non_empty = [
        ln
        for ln in text.split("\n")
        if ln.strip() and not ln.strip().startswith("#") and not ln.strip().startswith("http")
    ]
    if len(non_empty) >= 4 and sentence_count >= 3 and len(non_empty) >= sentence_count:
        flags.append("excessive_linebreaks")

    return True, flags
