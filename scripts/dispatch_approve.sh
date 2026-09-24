#!/usr/bin/env bash
# 林業X承認投稿 を workflow_dispatch する補助スクリプト。
#
# 秘密は引数に渡さない。次のいずれかが環境変数にあること:
#   WORKFLOW_DISPATCH_PAT  （推奨）
#   GH_PAT
# これらを GH_TOKEN として gh に渡し、既定の integration トークンを上書きする。
#
# 用法:
#   scripts/dispatch_approve.sh --check-auth
#   scripts/dispatch_approve.sh --draft-id ID --provider grok --gate-only
#   scripts/dispatch_approve.sh --draft-id ID --provider grok --confirm
#
# --confirm のときだけ confirm_live_post=true（本番投稿）。
# --gate-only は confirm=false で dispatch 権限だけ確認（X 投稿しない）。

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

WORKFLOW_NAME="${WORKFLOW_NAME:-林業X承認投稿}"
REF="${REF:-main}"
DRAFT_ID=""
PROVIDER=""
MODE=""  # check-auth | gate-only | confirm

usage() {
  cat <<'EOF'
用法:
  scripts/dispatch_approve.sh --check-auth
  scripts/dispatch_approve.sh --draft-id <ID> --provider openai|grok --gate-only
  scripts/dispatch_approve.sh --draft-id <ID> --provider openai|grok --confirm

環境変数（いずれか必須・値はログに出さない）:
  WORKFLOW_DISPATCH_PAT  推奨
  GH_PAT                 代替

任意:
  REF=main
  WORKFLOW_NAME=林業X承認投稿
  REPO=owner/repo        （未設定なら gh のカレントリポジトリ）
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --draft-id)
      DRAFT_ID="${2:-}"
      shift 2
      ;;
    --provider)
      PROVIDER="${2:-}"
      shift 2
      ;;
    --confirm)
      MODE="confirm"
      shift
      ;;
    --gate-only)
      MODE="gate-only"
      shift
      ;;
    --check-auth)
      MODE="check-auth"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "不明な引数: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$MODE" ]]; then
  echo "--check-auth / --gate-only / --confirm のいずれかを指定してください" >&2
  usage >&2
  exit 2
fi

# トークンを GH_TOKEN に載せる（値は echo しない）
TOKEN_EXPORT="$(
  python3 - <<'PY'
import shlex
from scripts.dispatch_token import require_dispatch_token
name, token = require_dispatch_token()
print(f"export GH_TOKEN={shlex.quote(token)}")
print(f"export DISPATCH_TOKEN_NAME={shlex.quote(name)}")
PY
)" || exit $?
eval "$TOKEN_EXPORT"

echo "dispatch token source: ${DISPATCH_TOKEN_NAME} (value not shown)"
echo "workflow: ${WORKFLOW_NAME} ref=${REF}"

REPO_ARGS=()
if [[ -n "${REPO:-}" ]]; then
  REPO_ARGS=(-R "$REPO")
fi

if [[ "$MODE" == "check-auth" ]]; then
  gh "${REPO_ARGS[@]}" api user -q .login
  gh "${REPO_ARGS[@]}" workflow list
  echo "check-auth OK（値は表示していません）"
  exit 0
fi

if [[ -z "$DRAFT_ID" || -z "$PROVIDER" ]]; then
  echo "--draft-id と --provider が必要です" >&2
  exit 2
fi

case "$PROVIDER" in
  openai|grok) ;;
  *)
    echo "provider は openai または grok です: $PROVIDER" >&2
    exit 2
    ;;
esac

if [[ "$MODE" == "confirm" ]]; then
  CONFIRM_VAL="true"
elif [[ "$MODE" == "gate-only" ]]; then
  CONFIRM_VAL="false"
else
  echo "内部エラー: MODE=$MODE" >&2
  exit 2
fi

echo "dispatching: draft_id=${DRAFT_ID} provider=${PROVIDER} confirm_live_post=${CONFIRM_VAL}"

gh "${REPO_ARGS[@]}" workflow run "$WORKFLOW_NAME" \
  --ref "$REF" \
  -f "draft_id=${DRAFT_ID}" \
  -f "provider=${PROVIDER}" \
  -f "confirm_live_post=${CONFIRM_VAL}"

echo "workflow_dispatch を受け付けました。状態確認:"
gh "${REPO_ARGS[@]}" run list --workflow="$WORKFLOW_NAME" --limit 3
