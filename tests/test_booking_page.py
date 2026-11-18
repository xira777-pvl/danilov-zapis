import json
import re
import unittest
from datetime import date, datetime

from brief.booking_page import availability, busy_days, render

TODAY = date(2026, 11, 19)
DAY = TODAY.isoformat()


def visit(start, end=None, timed=True):
    """All fixture timestamps are business-local."""
    return {
        "start": datetime.fromisoformat(start + "+06:00"),
        "end": datetime.fromisoformat(end + "+06:00") if end else None,
        "timed": timed,
        "deal": {"ID": "987654321", "TITLE": "PRIVATE CRM fixture"},
    }


class BookingAvailabilityTests(unittest.TestCase):
    def test_empty_day_offers_only_whole_hours_through_16(self):
        day = availability({}, TODAY)[DAY]
        expected = [f"{hour}:00" for hour in range(10, 17)]
        self.assertEqual(day["starts"], {"300": expected, "360": expected})
        self.assertEqual([(s["start"], s["end"], s["available"]) for s in day["slots"]],
                         [("10:00", "14:00", True), ("15:00", "21:00", True)])

    def test_fixed_session_reserves_full_window_and_cleaning(self):
        after_morning = availability({DAY: [["15:00", "20:00"]]}, TODAY)[DAY]
        self.assertEqual([s["available"] for s in after_morning["slots"]], [True, False])
        too_close = availability({DAY: [["14:59", "20:00"]]}, TODAY)[DAY]
        self.assertEqual([s["available"] for s in too_close["slots"]], [False, False])
        before_daytime = availability({DAY: [["10:00", "14:00"]]}, TODAY)[DAY]
        self.assertEqual([s["available"] for s in before_daytime["slots"]], [False, True])

    def test_premium_can_fit_when_both_fixed_slots_are_blocked(self):
        day = availability({DAY: [["10:00", "11:00"], ["18:00", "20:00"]]}, TODAY)[DAY]
        self.assertEqual([s["available"] for s in day["slots"]], [False, False])
        self.assertEqual(day["starts"], {"300": ["12:00"], "360": []})

    def test_59_minutes_between_visits_is_insufficient(self):
        for spans in [
            [["10:00", "11:01"], ["18:00", "20:00"]],
            [["10:00", "11:00"], ["17:59", "20:00"]],
        ]:
            with self.subTest(spans=spans):
                self.assertEqual(availability({DAY: spans}, TODAY)[DAY]["starts"]["300"], [])

    def test_end_seconds_round_outward_and_exclude_12_from_starts(self):
        busy = busy_days([
            visit(f"{DAY}T10:00:00", f"{DAY}T11:00:01"),
            visit(f"{DAY}T18:00:59", f"{DAY}T20:00:00"),
        ])
        self.assertEqual(availability(busy, TODAY)[DAY]["starts"]["300"], [])

    def test_overnight_visit_blocks_next_morning_until_cleanup(self):
        busy = busy_days([visit("2026-11-18T22:00:00", f"{DAY}T10:15:00")])
        day = availability(busy, TODAY)[DAY]
        self.assertEqual([s["available"] for s in day["slots"]], [False, True])
        self.assertEqual(day["starts"]["360"], [f"{hour}:00" for hour in range(12, 17)])

    def test_missing_time_or_duration_conservatively_blocks_availability(self):
        unknown_time = busy_days([visit(f"{DAY}T00:00:00", timed=False)])
        self.assertEqual(availability(unknown_time, TODAY)[DAY]["starts"], {"300": [], "360": []})
        unknown_end = busy_days([visit(f"{DAY}T17:17:00")])
        self.assertEqual(availability(unknown_end, TODAY)[DAY]["starts"]["360"], ["10:00"])
        late_visit = busy_days([visit(f"{DAY}T22:30:00")])
        self.assertEqual(availability(late_visit, TODAY)[DAY]["starts"], {
            "300": [f"{hour}:00" for hour in range(10, 17)],
            "360": [f"{hour}:00" for hour in range(10, 16)],
        })

    def test_html_publishes_only_derived_availability_and_original_program_ids(self):
        busy = busy_days([visit(f"{DAY}T11:00:00", f"{DAY}T12:17:00")])
        html = render(busy, TODAY, datetime.fromisoformat(f"{DAY}T08:00:00+06:00"))
        self.assertIn('<select id="start-time">', html)
        self.assertNotIn('type="time"', html)
        self.assertNotIn("PRIVATE CRM fixture", html)
        self.assertNotIn("987654321", html)
        data = json.loads(re.search(r"const TODAY = .*?, DAYS = (.*);", html).group(1))
        self.assertEqual(set(data[DAY]), {"slots", "starts"})
        self.assertNotIn("12:17", json.dumps(data[DAY]))
        programs = json.loads(re.search(r"new Map\((\[\[.*\]\])\.map", html).group(1))
        self.assertEqual([(p[1], p[3], p[4]) for p in programs],
                         [("147", 150, 20000), ("149", 180, 25000), ("151", 240, 30000),
                          ("153", 300, 40000), ("141", 300, 60000), ("143", 300, 60000),
                          ("119", 360, 60000), ("121", 360, 60000), ("123", 360, 60000),
                          ("125", 360, 60000)])


if __name__ == "__main__":
    unittest.main()
