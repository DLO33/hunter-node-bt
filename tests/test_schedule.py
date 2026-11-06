"""Tests for decoding and editing NODE-BT programs."""

import importlib.util
from pathlib import Path
import sys
import unittest

SCHEDULE_PATH = (
    Path(__file__).parent.parent / "custom_components" / "hunter_node_bt" / "schedule.py"
)
_spec = importlib.util.spec_from_file_location("hunter_schedule", SCHEDULE_PATH)
schedule = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = schedule  # dataclasses look the module up while it loads
_spec.loader.exec_module(schedule)

# Program_A as read from a 2-station controller: 06:30 every day, station 1 for 10 min.
PROGRAM_A = {
    "Name": "",
    "ScheduleType": 0,
    "ScheduleDays": 127,
    "IntervalDayCount": 0,
    "IntervalDayNext": 0,
    "StartTimes": [390, 65535, 65535, 65535, 65535, 65535, 65535, 65535],
    "RunTime": [600, 0],
}


class StartTimeTests(unittest.TestCase):
    def test_parse_and_format_round_trip(self):
        self.assertEqual(schedule.parse_start_time("6:30"), 390)
        self.assertEqual(schedule.parse_start_time("06:30"), 390)
        self.assertEqual(schedule.format_start_time(390), "06:30")

    def test_parse_start_times_sorts_and_removes_duplicates(self):
        self.assertEqual(schedule.parse_start_times("16:00, 06:30, 16:00"), [390, 960])

    def test_off_and_empty_mean_no_starts(self):
        for text in ("off", "", "  ", "OFF"):
            with self.subTest(text=text):
                self.assertEqual(schedule.parse_start_times(text), [])

    def test_invalid_times_are_rejected(self):
        for text in ("24:00", "6:60", "630", "six thirty"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                schedule.parse_start_time(text)


class WaterDaysTests(unittest.TestCase):
    def test_named_schedules(self):
        self.assertEqual(
            schedule.parse_water_days("Every Day"),
            schedule.WaterDays(schedule.SCHEDULE_WEEKDAYS, days_mask=0x7F),
        )
        self.assertEqual(schedule.parse_water_days("odd days").schedule_type, schedule.SCHEDULE_ODD)
        self.assertEqual(schedule.parse_water_days("even").schedule_type, schedule.SCHEDULE_EVEN)

    def test_interval(self):
        self.assertEqual(
            schedule.parse_water_days("every 3 days"),
            schedule.WaterDays(schedule.SCHEDULE_INTERVAL, interval_days=3),
        )
        with self.assertRaises(ValueError):
            schedule.parse_water_days("every 40 days")

    def test_day_names_build_a_sunday_first_mask(self):
        days = schedule.parse_water_days("Mon, Wed, friday")
        # Sunday is bit 0, so Monday is bit 1, Wednesday bit 3 and Friday bit 5.
        self.assertEqual(days.days_mask, (1 << 1) | (1 << 3) | (1 << 5))

    def test_unknown_day_is_rejected(self):
        with self.assertRaises(ValueError):
            schedule.parse_water_days("Mon, Funday")


class ProgramTests(unittest.TestCase):
    def test_decode_program(self):
        program = schedule.parse_program("A", PROGRAM_A)
        self.assertEqual(program.start_times, [390])
        self.assertEqual(program.run_times, {1: 600})
        self.assertTrue(program.is_active)
        self.assertEqual(program.start_times_text, "06:30")
        self.assertEqual(program.water_days_text, "every day")
        self.assertEqual(program.summary(), "Every day at 06:30 (10 min)")

    def test_program_without_starts_is_off(self):
        program = schedule.parse_program("B", {**PROGRAM_A, "StartTimes": [65535] * 8})
        self.assertFalse(program.is_active)
        self.assertEqual(program.start_times_text, "off")
        self.assertEqual(program.summary(), "Off")

    def test_missing_program_decodes_as_empty(self):
        program = schedule.parse_program("C", None)
        self.assertEqual(program.start_times, [])
        self.assertFalse(program.is_active)

    def test_weekday_text_is_monday_first(self):
        program = schedule.parse_program("A", {**PROGRAM_A, "ScheduleDays": 0b0100011})
        # Bits 0, 1 and 5 are Sunday, Monday and Friday.
        self.assertEqual(program.water_days_text, "Mon, Fri, Sun")


class BuildPayloadTests(unittest.TestCase):
    def test_start_times_keep_slot_count_and_unused_marker(self):
        payload = schedule.build_program_payload(PROGRAM_A, start_times=[960, 390])
        self.assertEqual(payload["StartTimes"], [390, 960] + [65535] * 6)
        # Everything else is copied unchanged.
        self.assertEqual(payload["RunTime"], [600, 0])
        self.assertEqual(payload["ScheduleDays"], 127)

    def test_removing_all_starts(self):
        payload = schedule.build_program_payload(PROGRAM_A, start_times=[])
        self.assertEqual(payload["StartTimes"], [65535] * 8)

    def test_too_many_starts_is_rejected(self):
        with self.assertRaises(ValueError):
            schedule.build_program_payload(PROGRAM_A, start_times=list(range(0, 900, 100)))

    def test_water_days(self):
        payload = schedule.build_program_payload(
            PROGRAM_A, water_days=schedule.parse_water_days("Mon, Wed")
        )
        self.assertEqual(payload["ScheduleType"], schedule.SCHEDULE_WEEKDAYS)
        self.assertEqual(payload["ScheduleDays"], (1 << 1) | (1 << 3))

        payload = schedule.build_program_payload(
            PROGRAM_A, water_days=schedule.parse_water_days("every 2 days")
        )
        self.assertEqual(payload["ScheduleType"], schedule.SCHEDULE_INTERVAL)
        self.assertEqual(payload["IntervalDayCount"], 2)

    def test_run_times(self):
        payload = schedule.build_program_payload(PROGRAM_A, run_times={2: 300}, station_count=2)
        self.assertEqual(payload["RunTime"], [600, 300])
        with self.assertRaises(ValueError):
            schedule.build_program_payload(PROGRAM_A, run_times={5: 300}, station_count=2)

    def test_current_section_is_not_modified(self):
        original = dict(PROGRAM_A)
        schedule.build_program_payload(PROGRAM_A, start_times=[420])
        self.assertEqual(PROGRAM_A, original)


if __name__ == "__main__":
    unittest.main()
