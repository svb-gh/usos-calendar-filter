import copy
import datetime
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event, Timezone

import clean_schedule

WARSAW = ZoneInfo("Europe/Warsaw")
UTC = datetime.timezone.utc


def make_event(
    title: str = "LAB - Test",
    start: datetime.datetime | None = None,
    end: datetime.datetime | None = None,
) -> Event:
    start = start or datetime.datetime(2026, 10, 14, 12, 15, tzinfo=UTC)
    end = end or start + datetime.timedelta(hours=2)
    event = Event()
    event.add("uid", "source-event@example.test")
    event.add("summary", title)
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add(
        "description", "Sala: 101\nBudynek Wydziału Mechatroniki\nhttps://usos.test/x?gr_nr=7"
    )
    return event


class RulesTests(unittest.TestCase):
    def test_custom_event_inherits_template_and_overrides_fields(self) -> None:
        rules = clean_schedule.validate_rules(
            {
                "templates": {
                    "lab": {
                        "title": "LAB - Test",
                        "start_time": "07:15",
                        "end_time": "10:00",
                        "location": "Default room",
                    }
                },
                "custom": [
                    {
                        "template": "lab",
                        "date": "2026-10-14",
                        "location": "Room 101",
                    }
                ],
            }
        )

        self.assertEqual(
            rules["custom"][0],
            {
                "title": "LAB - Test",
                "start_time": "07:15",
                "end_time": "10:00",
                "location": "Room 101",
                "date": "2026-10-14",
            },
        )

    def test_rejects_unknown_custom_template(self) -> None:
        with self.assertRaisesRegex(clean_schedule.RulesValidationError, "unknown template"):
            clean_schedule.validate_rules(
                {"custom": [{"template": "missing", "date": "2026-10-14"}]}
            )

    def test_rejects_unknown_template_field(self) -> None:
        with self.assertRaisesRegex(clean_schedule.RulesValidationError, "unknown field"):
            clean_schedule.validate_rules({"templates": {"lab": {"starttime": "07:15"}}})

    def test_rejects_invalid_custom_time_range(self) -> None:
        with self.assertRaisesRegex(clean_schedule.RulesValidationError, "later than"):
            clean_schedule.validate_rules(
                {
                    "custom": [
                        {
                            "title": "Test",
                            "date": "2026-10-14",
                            "start_time": "14:00",
                            "end_time": "13:00",
                        }
                    ]
                }
            )

    def test_cancel_requires_date(self) -> None:
        with self.assertRaisesRegex(clean_schedule.RulesValidationError, "date is required"):
            clean_schedule.validate_rules({"cancel": [{"match": "Test"}]})

    def test_rejects_duplicate_generated_custom_uid(self) -> None:
        item = {
            "title": "Office hours",
            "date": "2026-10-14",
            "start_time": "11:00",
            "end_time": "12:00",
        }
        with self.assertRaisesRegex(clean_schedule.RulesValidationError, "duplicates"):
            clean_schedule.validate_rules({"custom": [item.copy(), item.copy()]})

    def test_rejects_duplicate_explicit_custom_uid(self) -> None:
        first = {
            "title": "First",
            "date": "2026-10-14",
            "start_time": "11:00",
            "end_time": "12:00",
            "uid": "shared@example.test",
        }
        second = {
            "title": "Second",
            "date": "2026-10-15",
            "start_time": "13:00",
            "end_time": "14:00",
            "uid": "shared@example.test",
        }
        with self.assertRaisesRegex(clean_schedule.RulesValidationError, "shared@example.test"):
            clean_schedule.validate_rules({"custom": [first, second]})


class CalendarTests(unittest.TestCase):
    def test_custom_uid_is_stable(self) -> None:
        item = {
            "title": "Office hours",
            "date": "2026-10-14",
            "start_time": "11:00",
            "end_time": "12:00",
        }
        first = clean_schedule.create_custom_event(item)
        second = clean_schedule.create_custom_event(item)
        self.assertEqual(str(first["uid"]), str(second["uid"]))
        self.assertEqual(str(first[clean_schedule.EVENT_SOURCE_PROPERTY]), "CUSTOM")

    def test_cancel_only_matching_date(self) -> None:
        event = make_event()
        rules = [{"match": "LAB", "date": "2026-10-14"}]
        self.assertTrue(clean_schedule.should_cancel_event(event, rules))
        self.assertFalse(
            clean_schedule.should_cancel_event(event, [{"match": "LAB", "date": "2026-10-15"}])
        )

    def test_rule_times_are_compared_in_warsaw_timezone(self) -> None:
        event = make_event(start=datetime.datetime(2026, 10, 14, 12, 15, tzinfo=UTC))
        self.assertTrue(
            clean_schedule.should_drop_event(event, [{"match": "LAB", "start_time": "14:15"}])
        )

    def test_warsaw_daylight_saving_offset_is_respected(self) -> None:
        cases = (
            (datetime.datetime(2026, 7, 15, 12, 15, tzinfo=UTC), "14:15"),
            (datetime.datetime(2026, 12, 15, 12, 15, tzinfo=UTC), "13:15"),
        )
        for start, expected_local_time in cases:
            with self.subTest(start=start):
                event = make_event(start=start)
                self.assertTrue(
                    clean_schedule.should_drop_event(
                        event, [{"match": "LAB", "start_time": expected_local_time}]
                    )
                )

    def test_override_changes_end_only_and_reports_change(self) -> None:
        event = make_event()
        changed = clean_schedule.apply_overrides(
            event, [{"match": "LAB", "filter_start_time": "14:15", "end_time": "17:00"}]
        )
        self.assertTrue(changed)
        self.assertEqual(event["dtend"].dt.astimezone(WARSAW).strftime("%H:%M"), "17:00")

    def test_overrides_use_times_changed_by_previous_rule(self) -> None:
        event = make_event()
        changed = clean_schedule.apply_overrides(
            event,
            [
                {"match": "LAB", "end_time": "18:00"},
                {"match": "LAB", "start_time": "17:00"},
            ],
        )
        self.assertTrue(changed)
        self.assertEqual(event["dtstart"].dt.astimezone(WARSAW).strftime("%H:%M"), "17:00")
        self.assertEqual(event["dtend"].dt.astimezone(WARSAW).strftime("%H:%M"), "18:00")

    def test_build_preserves_vtimezone(self) -> None:
        calendar = Calendar()
        calendar.add("prodid", "-//test//EN")
        calendar.add("version", "2.0")
        timezone = Timezone()
        timezone.add("tzid", "Europe/Warsaw")
        calendar.add_component(timezone)
        calendar.add_component(make_event())

        result, counts = clean_schedule.build_calendar(calendar, {})

        self.assertEqual(
            len([item for item in result.subcomponents if item.name == "VTIMEZONE"]), 1
        )
        self.assertEqual(counts["sanitized"], 1)
        event = result.walk("VEVENT")[0]
        self.assertEqual(str(event[clean_schedule.EVENT_SOURCE_PROPERTY]), "USOS")

    def test_sanitizes_location_and_description(self) -> None:
        event = make_event()
        clean_schedule.sanitize_event(event)
        self.assertEqual(str(event["location"]), "s. 101, Wydział Mechatroniki")
        self.assertEqual(
            str(event["description"]), "Grupa: 7\nUSOS link: https://usos.test/x?gr_nr=7"
        )

    def test_atomic_write_produces_parseable_calendar(self) -> None:
        calendar = Calendar()
        calendar.add("prodid", "-//test//EN")
        calendar.add("version", "2.0")
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "calendar.ics"
            clean_schedule.write_calendar_atomic(calendar, output)
            parsed = Calendar.from_ical(output.read_bytes())
            self.assertEqual(str(parsed["version"]), "2.0")


class HistoryTests(unittest.TestCase):
    @staticmethod
    def calendar_with(*events: Event) -> Calendar:
        calendar = Calendar()
        calendar.add("prodid", "-//test//EN")
        calendar.add("version", "2.0")
        for event in events:
            calendar.add_component(event)
        return calendar

    @staticmethod
    def event(uid: str, start: datetime.datetime, source: str = "USOS") -> Event:
        event = make_event(start=start, end=start + datetime.timedelta(hours=1))
        event["uid"] = uid
        event[clean_schedule.EVENT_SOURCE_PROPERTY] = source
        return event

    def test_winter_semester_always_starts_on_october_first(self) -> None:
        cases = (
            (datetime.datetime(2026, 1, 15, tzinfo=WARSAW), "2025-10-01"),
            (datetime.datetime(2026, 10, 18, tzinfo=WARSAW), "2026-10-01"),
        )
        for now, expected in cases:
            with self.subTest(now=now):
                start = clean_schedule.determine_semester_start(self.calendar_with(), now=now)
                self.assertEqual(start.date().isoformat(), expected)

    def test_detects_summer_start_after_long_break_in_usos_events(self) -> None:
        now = datetime.datetime(2027, 2, 20, 12, 0, tzinfo=WARSAW)
        previous = self.calendar_with(
            self.event("winter@example.test", datetime.datetime(2027, 1, 25, tzinfo=WARSAW)),
            self.event(
                "custom@example.test",
                datetime.datetime(2027, 2, 10, tzinfo=WARSAW),
                source="CUSTOM",
            ),
        )
        current = self.calendar_with(
            self.event("summer-1@example.test", datetime.datetime(2027, 2, 20, tzinfo=WARSAW)),
            self.event("summer-2@example.test", datetime.datetime(2027, 2, 22, tzinfo=WARSAW)),
            self.event("summer-3@example.test", datetime.datetime(2027, 2, 25, tzinfo=WARSAW)),
        )

        start = clean_schedule.determine_semester_start(current, previous, now=now)

        self.assertEqual(start.date().isoformat(), "2027-02-20")

    def test_uses_hard_march_boundary_when_break_is_not_detected(self) -> None:
        start = clean_schedule.determine_semester_start(
            self.calendar_with(),
            now=datetime.datetime(2027, 3, 15, tzinfo=WARSAW),
        )
        self.assertEqual(start.date().isoformat(), "2027-03-15")

    def test_reuses_detected_semester_start_stored_in_history(self) -> None:
        previous = self.calendar_with()
        previous[clean_schedule.SEMESTER_START_PROPERTY] = "2027-02-22"

        start = clean_schedule.determine_semester_start(
            self.calendar_with(),
            previous,
            now=datetime.datetime(2027, 4, 1, tzinfo=WARSAW),
        )
        self.assertEqual(start.date().isoformat(), "2027-02-22")

    def test_rejects_invalid_detection_window(self) -> None:
        with self.assertRaisesRegex(ValueError, "before"):
            clean_schedule.determine_semester_start(
                self.calendar_with(),
                now=datetime.datetime(2027, 2, 1, tzinfo=WARSAW),
                summer_search_start="03-20",
                summer_hard_start="03-15",
            )

    def test_preserves_only_recent_completed_events(self) -> None:
        now = datetime.datetime(2026, 10, 18, 12, 0, tzinfo=WARSAW)
        current = self.calendar_with(
            self.event("current@example.test", now + datetime.timedelta(days=1))
        )
        previous = self.calendar_with(
            self.event("recent@example.test", now - datetime.timedelta(days=10)),
            self.event("expired@example.test", now - datetime.timedelta(days=20)),
            self.event("removed-future@example.test", now + datetime.timedelta(days=2)),
        )

        counts = clean_schedule.retain_calendar_history(current, previous, now=now)

        uids = {str(event["uid"]) for event in current.walk("VEVENT")}
        self.assertEqual(uids, {"current@example.test", "recent@example.test"})
        self.assertEqual(counts["archived"], 1)

    def test_prunes_expired_events_from_current_calendar(self) -> None:
        now = datetime.datetime(2026, 10, 18, 12, 0, tzinfo=WARSAW)
        current = self.calendar_with(
            self.event("expired@example.test", now - datetime.timedelta(days=20)),
            self.event("recent@example.test", now - datetime.timedelta(days=10)),
        )

        counts = clean_schedule.retain_calendar_history(current, None, now=now)

        self.assertEqual(
            [str(event["uid"]) for event in current.walk("VEVENT")],
            ["recent@example.test"],
        )
        self.assertEqual(counts["pruned"], 1)

    def test_does_not_duplicate_event_already_in_current_feed(self) -> None:
        now = datetime.datetime(2026, 10, 18, 12, 0, tzinfo=WARSAW)
        event = self.event("same@example.test", now - datetime.timedelta(days=1))
        current = self.calendar_with(event)
        previous = self.calendar_with(copy.deepcopy(event))

        counts = clean_schedule.retain_calendar_history(current, previous, now=now)

        self.assertEqual(len(current.walk("VEVENT")), 1)
        self.assertEqual(counts["archived"], 0)

    def test_drop_rules_also_apply_to_archived_events(self) -> None:
        now = datetime.datetime(2026, 10, 18, 12, 0, tzinfo=WARSAW)
        previous = self.calendar_with(
            self.event("dropped@example.test", now - datetime.timedelta(days=1))
        )

        counts = clean_schedule.retain_calendar_history(
            self.calendar_with(),
            previous,
            now=now,
            rules={"drop": [{"match": "LAB - Test"}]},
        )

        self.assertEqual(counts["archived"], 0)

    def test_process_migrates_output_history_to_dedicated_file(self) -> None:
        now = datetime.datetime(2026, 10, 18, 12, 0, tzinfo=WARSAW)
        previous = self.calendar_with(
            self.event("archived@example.test", now - datetime.timedelta(days=1))
        )
        source = self.calendar_with(
            self.event("current@example.test", now + datetime.timedelta(days=1))
        )

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "public" / "calendar.ics"
            history = Path(directory) / "state" / "history.ics"
            clean_schedule.write_calendar_atomic(previous, output)

            with (
                patch("clean_schedule.load_rules", return_value={}),
                patch("clean_schedule.fetch_raw_calendar", return_value=source),
                patch(
                    "clean_schedule.determine_semester_start",
                    return_value=datetime.datetime(2026, 10, 1, tzinfo=WARSAW),
                ),
            ):
                clean_schedule.process_calendar(
                    "https://example.test/calendar.ics?key=secret",
                    "rules.yaml",
                    output,
                    history,
                )

            self.assertTrue(history.exists())
            for path in (history, output):
                calendar = Calendar.from_ical(path.read_bytes())
                self.assertEqual(
                    str(calendar[clean_schedule.SEMESTER_START_PROPERTY]), "2026-10-01"
                )
                self.assertEqual(
                    {str(event["uid"]) for event in calendar.walk("VEVENT")},
                    {"archived@example.test", "current@example.test"},
                )


class FetchTests(unittest.TestCase):
    @patch("clean_schedule.requests.Session")
    def test_fetch_parses_response_bytes(self, session_class: Mock) -> None:
        calendar = Calendar()
        calendar.add("prodid", "-//test//EN")
        calendar.add("version", "2.0")
        response = Mock()
        response.content = calendar.to_ical()
        session_class.return_value.__enter__.return_value.get.return_value = response

        result = clean_schedule.fetch_raw_calendar("https://example.test/calendar.ics")

        response.raise_for_status.assert_called_once_with()
        self.assertEqual(str(result["version"]), "2.0")

    @patch("clean_schedule.requests.Session")
    def test_fetch_error_does_not_expose_feed_url(self, session_class: Mock) -> None:
        secret_url = "https://example.test/calendar.ics?key=TOP_SECRET"
        session_class.return_value.__enter__.return_value.get.side_effect = (
            clean_schedule.requests.ConnectionError(f"Could not connect to {secret_url}")
        )

        with self.assertRaises(clean_schedule.CalendarFetchError) as caught:
            clean_schedule.fetch_raw_calendar(secret_url)

        self.assertNotIn("TOP_SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
