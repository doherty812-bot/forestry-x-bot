#!/usr/bin/env python3
"""
下書き生成成功後に Cursor Cloud Agent を起動し、スマホ向けレビュー会話を作る。

公式: Cloud Agents API（CURSOR_API_KEY）
  https://cursor.com/docs/cloud-agent/api/endpoints
  https://cursor.com/docs/cli/github-actions

- X への投稿はしない（承認経路は変えない）
- PR は自動作成しない
- 秘密の値はログに出さない
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_API_BASE = "https://api.cursor.com"
DEFAULT_REPO_URL = "https://github.com/doherty812-bot/forestry-x-bot"
PROJECT_STORE_HINT = "/cursor/stores/bc-bbe3beb5-dfb2-4368-a2d8-deba0e793ffc"
PROJECT_ID = "bc-bbe3beb5-dfb2-4368-a2d8-deba0e793ffc"


def resolve_repo_url(repo: Optional[str] = None) -> str:
    raw = (repo or os.environ.get("CURSOR_AGENT_REPOSITORY") or "").strip()
    if not raw:
        gh = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
        if gh:
            raw = gh
    if not raw:
        return DEFAULT_REPO_URL
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw.rstrip("/")
    return f"https://github.com/{raw}"


def resolve_ref() -> str:
    return (
        os.environ.get("CURSOR_AGENT_REF")
        or os.environ.get("GITHUB_REF_NAME")
        or "main"
    ).strip() or "main"


def find_latest_draft_id(drafts_dir: Path) -> Optional[str]:
    if not drafts_dir.is_dir():
        return None
    files = sorted(drafts_dir.glob("*.json"))
    if not files:
        return None
    return files[-1].stem


def deterministic_agent_id(draft_id: str) -> str:
    """同一 draft_id の再起動で重複作成を避ける（v1 agentId）。"""
    u = uuid.uuid5(uuid.NAMESPACE_URL, f"forestry-x-bot/draft-review/{draft_id}")
    return f"bc-{u}"


def build_prompt(
    *,
    draft_id: str,
    run_id: str,
    run_url: str,
    repository: str,
    slot: str = "",
) -> str:
    slot_line = f"- スロット: {slot}\n" if slot else ""
    return f"""あなたは林業Xボットの下書きレビュー担当です。日本語で応答してください。

## 今回の下書き（Actions 成功直後）

- DRAFT_ID: `{draft_id}`
{slot_line}- GitHub run: {run_id or "(不明)"}
- Actions URL: {run_url or "(不明)"}
- リポジトリ: {repository}
- Project id / store: `{PROJECT_ID}`
- Project store ヒント: `{PROJECT_STORE_HINT}`

## やること（必須）

1. ライブ投稿しない。X 承認経路（approve.yml / dispatch_approve.sh）は、ユーザーが `openai` / `grok` を明示するまで起動しない。
2. GitHub Actions の artifact `forestry-drafts` から DRAFT_ID=`{draft_id}` の JSON を取得する（`scripts/fetch_draft_from_artifacts.py` または同等）。
3. 可能なら Project store の `docs/latest-draft-review.md` を更新する（パスが無い場合は会話への報告だけでよい。無理に git commit / PR しない）。
4. ユーザー向けに次をすべて届ける（スマホで読めるよう簡潔に）:
   - DRAFT_ID
   - SOURCES（タイトル・出典・URL）
   - openai 案と grok 案（要約でも全文でも可。長い場合は要約＋全文リンク／折りたたみ）
   - guard / PII / prose flags の要点
   - 選択依頼: `openai` / `grok` / `却下` のいずれかだけ送ってください
5. 完了後、ユーザーがスマホの Cursor アプリで気づけるよう、最終メッセージをはっきり書く。push 通知は端末設定依存である旨を一文添えてよい。

## やってはいけないこと

- X への投稿、`CONFIRM_LIVE_POST`、approve workflow の本番起動
- 秘密（API キー・PAT・X_*）をチャットやファイルに書く
- ユーザー選択前の自動投稿や自動 PR 作成

## ユーザーが選んだあと

- `openai` または `grok`: `scripts/dispatch_approve.sh --draft-id {draft_id} --provider <選択> --confirm`
- `却下`: Actions は起動せず、却下を記録して終了
"""


def build_v1_payload(
    *,
    draft_id: str,
    prompt_text: str,
    repo_url: str,
    ref: str,
    model_id: Optional[str] = None,
    idempotent: bool = True,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "prompt": {"text": prompt_text},
        "name": f"林業X下書きレビュー {draft_id}"[:100],
        "repos": [{"url": repo_url, "startingRef": ref}],
        "autoCreatePR": False,
        "workOnCurrentBranch": False,
    }
    if model_id:
        payload["model"] = {"id": model_id}
    if idempotent:
        payload["agentId"] = deterministic_agent_id(draft_id)
    return payload


def build_v0_payload(
    *,
    draft_id: str,
    prompt_text: str,
    repo_url: str,
    ref: str,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "prompt": {"text": prompt_text},
        "source": {"repository": repo_url, "ref": ref},
        "target": {
            "autoCreatePr": False,
            "branchName": f"cursor/draft-review-{draft_id}"[:100],
        },
    }
    if model:
        payload["model"] = model
    return payload


def _request_json(
    method: str,
    url: str,
    api_key: str,
    body: Optional[Dict[str, Any]] = None,
    timeout: int = 60,
) -> Tuple[int, Any]:
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": "forestry-x-bot-draft-notify",
    }
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    # Basic auth: api_key as username, empty password（公式ドキュメント準拠）
    import base64

    token = base64.b64encode(f"{api_key}:".encode("utf-8")).decode("ascii")
    headers["Authorization"] = f"Basic {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            parsed: Any = json.loads(raw) if raw.strip() else {}
            return resp.status, parsed
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body) if err_body.strip() else {"raw": err_body}
        except json.JSONDecodeError:
            parsed = {"raw": err_body[:500]}
        return e.code, parsed


def extract_agent_url(response: Any) -> Optional[str]:
    if not isinstance(response, dict):
        return None
    agent = response.get("agent")
    if isinstance(agent, dict):
        url = agent.get("url")
        if url:
            return str(url)
        aid = agent.get("id")
        if aid:
            return f"https://cursor.com/agents/{aid}"
    if response.get("target") and isinstance(response["target"], dict):
        url = response["target"].get("url")
        if url:
            return str(url)
    aid = response.get("id")
    if aid:
        return f"https://cursor.com/agents?id={aid}"
    return None


def launch_agent(
    *,
    api_key: str,
    payload: Dict[str, Any],
    api_base: str = DEFAULT_API_BASE,
    api_version: str = "v1",
) -> Tuple[int, Any]:
    base = api_base.rstrip("/")
    if api_version == "v0":
        url = f"{base}/v0/agents"
    else:
        url = f"{base}/v1/agents"
    return _request_json("POST", url, api_key, payload)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="下書き成功後に Cursor Cloud Agent を起動（投稿しない）"
    )
    parser.add_argument("--draft-id", default="", help="下書き ID。省略時は drafts/ 最新")
    parser.add_argument("--drafts-dir", default="drafts", help="下書き JSON ディレクトリ")
    parser.add_argument("--run-id", default="", help="GitHub Actions run id")
    parser.add_argument("--run-url", default="", help="Actions run URL")
    parser.add_argument("--slot", default="", help="12:00 / 20:00 など")
    parser.add_argument(
        "--api-version",
        choices=("v1", "v0"),
        default=os.environ.get("CURSOR_AGENT_API_VERSION", "v1"),
        help="Cloud Agents API バージョン（既定 v1）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="API を呼ばずペイロードだけ表示（秘密は出さない）",
    )
    parser.add_argument(
        "--soft-fail",
        "--allow-missing-key",
        dest="soft_fail",
        action="store_true",
        help="キー未設定・API 失敗でも exit 0（下書き本体を落とさない）",
    )
    parser.add_argument(
        "--no-idempotent",
        action="store_true",
        help="v1 の agentId を付けない（毎回新規）",
    )
    args = parser.parse_args(argv)

    draft_id = (args.draft_id or os.environ.get("DRAFT_ID") or "").strip()
    if not draft_id:
        draft_id = find_latest_draft_id(Path(args.drafts_dir)) or ""
    if not draft_id:
        print("DRAFT_ID が分かりません（--draft-id または drafts/*.json）", file=sys.stderr)
        return 1

    run_id = (
        args.run_id or os.environ.get("GITHUB_RUN_ID") or ""
    ).strip()
    run_url = (args.run_url or os.environ.get("GITHUB_RUN_URL") or "").strip()
    if not run_url and run_id and os.environ.get("GITHUB_REPOSITORY"):
        run_url = (
            f"https://github.com/{os.environ['GITHUB_REPOSITORY']}"
            f"/actions/runs/{run_id}"
        )

    repo_url = resolve_repo_url()
    ref = resolve_ref()
    slot = (args.slot or os.environ.get("DRAFT_SLOT") or "").strip()
    model = (os.environ.get("CURSOR_AGENT_MODEL") or "").strip() or None
    api_base = (os.environ.get("CURSOR_API_BASE") or DEFAULT_API_BASE).strip()

    prompt_text = build_prompt(
        draft_id=draft_id,
        run_id=run_id,
        run_url=run_url,
        repository=repo_url,
        slot=slot,
    )

    if args.api_version == "v0":
        payload = build_v0_payload(
            draft_id=draft_id,
            prompt_text=prompt_text,
            repo_url=repo_url,
            ref=ref,
            model=model,
        )
    else:
        payload = build_v1_payload(
            draft_id=draft_id,
            prompt_text=prompt_text,
            repo_url=repo_url,
            ref=ref,
            model_id=model,
            idempotent=not args.no_idempotent,
        )

    print(f"draft_id={draft_id}")
    print(f"api_version={args.api_version}")
    print(f"repo={repo_url}")
    print(f"ref={ref}")
    print(f"run_url={run_url or '(none)'}")
    if args.api_version == "v1" and "agentId" in payload:
        print(f"agentId={payload['agentId']}")

    if args.dry_run:
        # プロンプト全文は長いので長さだけ。秘密フィールドは無い想定。
        safe = dict(payload)
        if "prompt" in safe and isinstance(safe["prompt"], dict):
            text = safe["prompt"].get("text", "")
            safe["prompt"] = {
                "text_chars": len(text),
                "text_preview": text[:240].replace("\n", " ") + ("…" if len(text) > 240 else ""),
            }
        print(json.dumps(safe, ensure_ascii=False, indent=2))
        print("dry-run: API は呼び出しませんでした")
        return 0

    api_key = (os.environ.get("CURSOR_API_KEY") or "").strip()
    if not api_key:
        msg = (
            "CURSOR_API_KEY が未設定です。"
            "Repo Secrets に登録すると下書き後にスマホ向け Cloud Agent が起動します。"
            "手順: docs/cursor-mobile-draft-notify.md"
        )
        print(msg, file=sys.stderr)
        return 0 if args.soft_fail else 1

    status, body = launch_agent(
        api_key=api_key,
        payload=payload,
        api_base=api_base,
        api_version=args.api_version,
    )

    # 409 agent_id_conflict = 同一 draft のエージェント既存 → 成功扱い
    if status == 409 and args.api_version == "v1":
        print("agent_id_conflict: 同一 DRAFT_ID のエージェントが既にあります（重複起動スキップ）")
        print(json.dumps(body, ensure_ascii=False)[:800])
        return 0

    if status >= 400:
        print(f"Cloud Agents API 失敗: HTTP {status}", file=sys.stderr)
        # 秘密を含み得る生レスポンスは短く
        print(json.dumps(body, ensure_ascii=False)[:800], file=sys.stderr)
        if args.soft_fail:
            print("続行: 下書き本体は成功のまま（通知のみ失敗）", file=sys.stderr)
            return 0
        return 1

    agent_url = extract_agent_url(body)
    print(f"Cloud Agent を起動しました: HTTP {status}")
    if agent_url:
        print(f"agent_url={agent_url}")
    # レスポンスから id だけ抜く
    if isinstance(body, dict):
        agent = body.get("agent") if isinstance(body.get("agent"), dict) else body
        if isinstance(agent, dict) and agent.get("id"):
            print(f"agent_id={agent.get('id')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
