#!/usr/bin/env python3
"""
GitHub Actions の artifact「forestry-drafts」から下書き JSON を取得する。

再現性のため、直近の成功した下書き workflow run を新しい順に走査し、
指定 draft_id の JSON が見つかった時点で drafts/ に配置する。

秘密は扱わない。GITHUB_TOKEN / GH_TOKEN は呼び出し側が渡す。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ARTIFACT_NAME = "forestry-drafts"
DEFAULT_WORKFLOW_FILE = "post.yml"
APPROVE_WORKFLOW_FILE = "approve.yml"


def require_confirm_flag(value: str) -> None:
    """workflow_dispatch の confirm_live_post が true であることを要求する。"""
    normalized = (value or "").strip().lower()
    if normalized not in {"true", "1", "yes"}:
        raise SystemExit(
            "confirm_live_post が true ではありません。"
            "誤投稿防止のため承認ジョブを中止します。"
        )


def validate_provider(provider: str) -> str:
    p = (provider or "").strip().lower()
    if p not in {"openai", "grok"}:
        raise SystemExit(f"provider は openai または grok です: {provider}")
    return p


def find_draft_json(root: Path, draft_id: str) -> Optional[Path]:
    """ディレクトリ木から {draft_id}.json を探す。"""
    target = f"{draft_id}.json"
    matches = sorted(root.rglob(target))
    return matches[0] if matches else None


def parse_repo(repo: Optional[str] = None) -> tuple[str, str]:
    raw = repo or os.environ.get("GITHUB_REPOSITORY") or ""
    if "/" not in raw:
        raise SystemExit(
            "GITHUB_REPOSITORY（owner/repo）が必要です。"
            "例: doherty812-bot/forestry-x-bot"
        )
    owner, name = raw.split("/", 1)
    return owner, name


def _api_request(url: str, token: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "forestry-x-bot-fetch-draft",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API 失敗 {e.code} {url}: {body}") from e


class _StripAuthOnRedirectHandler(urllib.request.HTTPRedirectHandler):
    """
    GitHub artifact ZIP は api.github.com → Azure Blob へ 302 する。
    urllib 既定は Authorization を redirect 先へ持ち越すため、
    SAS 付き Azure URL が 401 になる。redirect 先では Auth を外す。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new_req = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new_req is None:
            return None
        # Request.remove_header は headers / unredirected_hdrs の両方を消す
        for name in ("Authorization", "X-GitHub-Api-Version"):
            try:
                new_req.remove_header(name)
            except KeyError:
                pass
        return new_req


def _download_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_StripAuthOnRedirectHandler)


def _download_bytes(url: str, token: str) -> bytes:
    """
    artifact 等をダウンロードする。初回は Bearer、redirect 先では Auth なし。
    """
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "forestry-x-bot-fetch-draft",
        },
    )
    opener = _download_opener()
    try:
        with opener.open(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"ダウンロード失敗 {e.code} {url}: {body}") from e


def list_workflow_runs(
    owner: str,
    repo: str,
    token: str,
    workflow_file: str,
    *,
    status: str = "success",
    per_page: int = 20,
) -> List[Dict[str, Any]]:
    q = urllib.parse.urlencode({"status": status, "per_page": str(per_page)})
    url = (
        f"https://api.github.com/repos/{owner}/{repo}/actions/workflows/"
        f"{urllib.parse.quote(workflow_file)}/runs?{q}"
    )
    data = _api_request(url, token)
    return list(data.get("workflow_runs") or [])


def list_run_artifacts(owner: str, repo: str, token: str, run_id: int) -> List[Dict[str, Any]]:
    url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts"
    data = _api_request(url, token)
    return list(data.get("artifacts") or [])


def extract_zip_to(data: bytes, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        zf.extractall(dest)


def already_approved_in_runs(runs: Iterable[Dict[str, Any]], draft_id: str) -> bool:
    """
    承認 workflow の成功 run 名に draft_id が含まれるか。
    run-name: Approve {draft_id} ({provider})
    """
    needle = draft_id.lower()
    for run in runs:
        if run.get("conclusion") != "success":
            continue
        name = (run.get("name") or run.get("display_title") or "").lower()
        if needle in name:
            return True
    return False


def assert_not_already_approved(
    owner: str,
    repo: str,
    token: str,
    draft_id: str,
    *,
    workflow_file: str = APPROVE_WORKFLOW_FILE,
) -> None:
    runs = list_workflow_runs(owner, repo, token, workflow_file, status="success", per_page=50)
    if already_approved_in_runs(runs, draft_id):
        raise SystemExit(
            f"draft_id={draft_id} は既に成功した承認 run があります。"
            "二重投稿を避けるため中止します。"
        )


def fetch_draft_json(
    draft_id: str,
    out_dir: Path,
    *,
    owner: str,
    repo: str,
    token: str,
    workflow_file: str = DEFAULT_WORKFLOW_FILE,
    max_runs: int = 25,
) -> Path:
    """
    下書き workflow の成功 run を新しい順に見て、forestry-drafts artifact から
    draft_id.json を探し out_dir にコピーしてパスを返す。
    """
    runs = list_workflow_runs(
        owner, repo, token, workflow_file, status="success", per_page=max_runs
    )
    if not runs:
        raise SystemExit(f"成功した下書き run がありません（workflow={workflow_file}）")

    scratch = out_dir.parent / ".artifact-scratch"
    scratch.mkdir(parents=True, exist_ok=True)

    for run in runs:
        run_id = run["id"]
        artifacts = list_run_artifacts(owner, repo, token, run_id)
        targets = [
            a
            for a in artifacts
            if a.get("name") == ARTIFACT_NAME and not a.get("expired")
        ]
        for art in targets:
            art_id = art["id"]
            archive_url = (
                f"https://api.github.com/repos/{owner}/{repo}/actions/artifacts/"
                f"{art_id}/zip"
            )
            dest = scratch / f"run-{run_id}-art-{art_id}"
            if dest.exists():
                for p in dest.rglob("*"):
                    if p.is_file():
                        p.unlink()
            extract_zip_to(_download_bytes(archive_url, token), dest)
            found = find_draft_json(dest, draft_id)
            if found:
                out_dir.mkdir(parents=True, exist_ok=True)
                target = out_dir / f"{draft_id}.json"
                target.write_text(found.read_text(encoding="utf-8"), encoding="utf-8")
                # 簡易検証
                data = json.loads(target.read_text(encoding="utf-8"))
                if data.get("id") != draft_id:
                    raise SystemExit(f"JSON の id が一致しません: {data.get('id')}")
                print(f"下書きを配置しました: {target} (from run {run_id})")
                return target

    raise SystemExit(
        f"draft_id={draft_id} の JSON が直近 {max_runs} 件の "
        f"{ARTIFACT_NAME} artifact から見つかりませんでした。"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Actions artifact から下書き JSON を取得")
    parser.add_argument("--draft-id", required=True)
    parser.add_argument("--out-dir", default="drafts")
    parser.add_argument("--repo", default=None, help="owner/repo（未指定なら GITHUB_REPOSITORY）")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW_FILE, help="下書き workflow ファイル名")
    parser.add_argument(
        "--skip-already-approved-check",
        action="store_true",
        help="二重承認チェックをスキップ（通常は使わない）",
    )
    parser.add_argument("--max-runs", type=int, default=25)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
    if not token:
        raise SystemExit("GITHUB_TOKEN または GH_TOKEN が必要です")

    owner, repo = parse_repo(args.repo)
    if not args.skip_already_approved_check:
        assert_not_already_approved(owner, repo, token, args.draft_id)

    fetch_draft_json(
        args.draft_id,
        Path(args.out_dir),
        owner=owner,
        repo=repo,
        token=token,
        workflow_file=args.workflow,
        max_runs=args.max_runs,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
