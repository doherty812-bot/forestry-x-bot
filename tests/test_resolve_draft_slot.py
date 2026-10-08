#!/usr/bin/env python3
"""resolve_draft_slot のユニットテスト。"""

import unittest

from scripts.resolve_draft_slot import (
    EVENING_CRON,
    EVENING_SLOT,
    NOON_CRON,
    NOON_SLOT,
    resolve_draft_slot,
)


class TestResolveDraftSlot(unittest.TestCase):
    def test_dispatch_input_wins(self):
        self.assertEqual(
            resolve_draft_slot(
                input_slot="20:00",
                schedule_cron=NOON_CRON,
                utc_hour=9,
            ),
            EVENING_SLOT,
        )
        self.assertEqual(
            resolve_draft_slot(input_slot="12:00", schedule_cron=EVENING_CRON),
            NOON_SLOT,
        )

    def test_schedule_cron_ignore_wall_clock(self):
        # Actions が数時間遅延しても cron 文字列で正しい枠になる
        self.assertEqual(
            resolve_draft_slot(schedule_cron=EVENING_CRON, utc_hour=17),
            EVENING_SLOT,
        )
        self.assertEqual(
            resolve_draft_slot(schedule_cron=NOON_CRON, utc_hour=9),
            NOON_SLOT,
        )
        self.assertEqual(
            resolve_draft_slot(schedule_cron=EVENING_CRON, utc_hour=19),
            EVENING_SLOT,
        )

    def test_utc_hour_fallback_bands(self):
        self.assertEqual(resolve_draft_slot(utc_hour=3), NOON_SLOT)
        self.assertEqual(resolve_draft_slot(utc_hour=9), NOON_SLOT)
        self.assertEqual(resolve_draft_slot(utc_hour=11), EVENING_SLOT)
        self.assertEqual(resolve_draft_slot(utc_hour=17), EVENING_SLOT)

    def test_default_noon(self):
        self.assertEqual(resolve_draft_slot(), NOON_SLOT)

    def test_invalid_input(self):
        with self.assertRaises(SystemExit):
            resolve_draft_slot(input_slot="99:00")


if __name__ == "__main__":
    unittest.main()
