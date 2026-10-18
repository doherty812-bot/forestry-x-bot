"""カスタム sources 注入（DRAFT_SOURCES_*）の単体テスト。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import forestry_bot as bot


def test_load_injected_sources_from_file(tmp_path: Path, monkeypatch):
    payload = [
        {
            "title": "Example TTN",
            "url": "https://example.com/ttn",
            "source": "Example",
            "snippet": "neutral compare",
        }
    ]
    path = tmp_path / "sources.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("DRAFT_SOURCES_FILE", str(path))
    monkeypatch.delenv("DRAFT_SOURCES_JSON", raising=False)

    sources = bot.load_injected_sources()
    assert sources is not None
    assert len(sources) == 1
    assert sources[0]["title"] == "Example TTN"
    assert sources[0]["url"] == "https://example.com/ttn"
    assert sources[0]["label"] == "Example"


def test_load_injected_sources_absent(monkeypatch):
    monkeypatch.delenv("DRAFT_SOURCES_FILE", raising=False)
    monkeypatch.delenv("DRAFT_SOURCES_JSON", raising=False)
    assert bot.load_injected_sources() is None


def test_format_extra_instructions(monkeypatch):
    monkeypatch.setenv("DRAFT_EXTRA_INSTRUCTIONS", "必ず4機能を書く")
    text = bot.format_extra_instructions_for_prompt()
    assert "追加の執筆指示" in text
    assert "必ず4機能を書く" in text


def test_fixture_ttn_sources_parse():
    root = Path(__file__).resolve().parents[1]
    path = root / "fixtures" / "sources" / "ttn-2026-10-18.json"
    assert path.is_file()
    os.environ["DRAFT_SOURCES_FILE"] = str(path)
    try:
        sources = bot.load_injected_sources()
    finally:
        os.environ.pop("DRAFT_SOURCES_FILE", None)
    assert sources is not None
    assert len(sources) == 3
    assert all(s.get("url") for s in sources)
