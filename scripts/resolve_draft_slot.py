#!/usr/bin/env python3
"""
下書きスロット（12:00 / 20:00）を決定する。

GitHub Actions の schedule は数時間遅延することがある。
壁時計の UTC 時ではなく、トリガー元の cron（github.event.schedule）で判定する。
"""

from __future__ import annotations

import argparse
import os
import sys


NOON_CRON = "0 3 * * *"
EVENING_CRON = "0 11 * * *"
NOON_SLOT = "12:00"
EVENING_SLOT = "20:00"


def resolve_draft_slot(
    *,
    input_slot: str = "",
    schedule_cron: str = "",
    utc_hour: int | None = None,
) -> str:
    """
    優先順位:
      1) workflow_dispatch の inputs.slot（非空）
      2) github.event.schedule の cron 文字列
      3) 未知時のみ UTC 時の粗いフォールバック（テスト・手動用）
    """
    slot = (input_slot or "").strip()
    if slot in {NOON_SLOT, EVENING_SLOT}:
        return slot
    if slot:
        raise SystemExit(f"不正な slot: {slot!r}（12:00 または 20:00）")

    cron = (schedule_cron or "").strip()
    if cron == EVENING_CRON:
        return EVENING_SLOT
    if cron == NOON_CRON:
        return NOON_SLOT

    if utc_hour is not None:
        # schedule 欠落時のみ。03 枠の遅延は〜10時台、11 枠は 11 時以降が多い。
        hour = int(utc_hour)
        if 11 <= hour <= 23:
            return EVENING_SLOT
        return NOON_SLOT

    return NOON_SLOT


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve forestry draft slot")
    parser.add_argument(
        "--input-slot",
        default=os.environ.get("DRAFT_INPUT_SLOT", ""),
        help="workflow_dispatch inputs.slot",
    )
    parser.add_argument(
        "--schedule-cron",
        default=os.environ.get("GITHUB_EVENT_SCHEDULE", ""),
        help="github.event.schedule cron expression",
    )
    parser.add_argument(
        "--utc-hour",
        type=int,
        default=None,
        help="Optional UTC hour fallback when schedule is empty",
    )
    args = parser.parse_args(argv)
    print(resolve_draft_slot(
        input_slot=args.input_slot,
        schedule_cron=args.schedule_cron,
        utc_hour=args.utc_hour,
    ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
