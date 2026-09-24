"""下書きの保存・読込（人間承認前。git 管理外の drafts/）。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

DRAFTS_DIR = Path(os.environ.get("DRAFTS_DIR", "drafts"))


def ensure_drafts_dir() -> Path:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    return DRAFTS_DIR


def new_draft_id(slot: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_slot = slot.replace(":", "")
    return f"{stamp}-{safe_slot}"


def draft_path(draft_id: str) -> Path:
    return ensure_drafts_dir() / f"{draft_id}.json"


def save_draft(draft: Dict[str, Any]) -> Path:
    path = draft_path(draft["id"])
    path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_draft(draft_id: str) -> Dict[str, Any]:
    path = draft_path(draft_id)
    if not path.exists():
        raise FileNotFoundError(f"下書きが見つかりません: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def list_drafts(status: Optional[str] = "pending") -> List[Dict[str, Any]]:
    ensure_drafts_dir()
    items = []
    for path in sorted(DRAFTS_DIR.glob("*.json"), reverse=True):
        data = json.loads(path.read_text(encoding="utf-8"))
        if status is None or data.get("status") == status:
            items.append(data)
    return items


def mark_draft_posted(draft_id: str, provider: str, tweet_id: Optional[str] = None) -> Dict[str, Any]:
    draft = load_draft(draft_id)
    draft["status"] = "posted"
    draft["posted_provider"] = provider
    draft["posted_at"] = datetime.now(timezone.utc).isoformat()
    if tweet_id:
        draft["tweet_id"] = tweet_id
    save_draft(draft)
    return draft
