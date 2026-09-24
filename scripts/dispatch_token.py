#!/usr/bin/env python3
"""workflow_dispatch 用トークン解決（値は返すが、呼び出し側はログに出さないこと）。"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

# 優先順: WORKFLOW_DISPATCH_PAT → GH_PAT
TOKEN_ENV_CANDIDATES = ("WORKFLOW_DISPATCH_PAT", "GH_PAT")


def resolve_dispatch_token(
    environ: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """
    Returns (env_var_name, token) or (None, None).
    Never print the token.
    """
    env = environ if environ is not None else os.environ
    for name in TOKEN_ENV_CANDIDATES:
        raw = env.get(name)
        if raw is None:
            continue
        stripped = str(raw).strip()
        if stripped:
            return name, stripped
    return None, None


def require_dispatch_token(
    environ: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    name, token = resolve_dispatch_token(environ)
    if not name or not token:
        raise SystemExit(
            "WORKFLOW_DISPATCH_PAT または GH_PAT が未設定です。"
            "Cursor Cloud の Secrets（環境変数）に登録してください。"
            "値をチャットや引数に渡さないでください。"
        )
    return name, token
