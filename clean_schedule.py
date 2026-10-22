import copy
import datetime
import itertools
import os
import re
import stat
import tempfile
import uuid
import zoneinfo
from pathlib import Path

import icalendar
import requests
import yaml
from dotenv import load_dotenv
from icalendar import Calendar, Event
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_RULES_FILE_PATH = "rules.yaml"
DEFAULT_OUTPUT_ICS_PATH = "/var/www/calendar/cleaned_schedule.ics"
DEFAULT_HISTORY_ICS_PATH = "/var/lib/uni-schedule/history.ics"
WINTER_SEMESTER_START = (10, 1)
DEFAULT_SUMMER_BREAK_SEARCH_START = "01-20"
DEFAULT_SUMMER_SEMESTER_HARD_START = "03-15"
DEFAULT_MIN_SEMESTER_BREAK_DAYS = 14
MIN_SUMMER_ACTIVITY_DATES = 3
LOCAL_TIMEZONE = zoneinfo.ZoneInfo("Europe/Warsaw")
CUSTOM_UID_NAMESPACE = uuid.UUID("726fc3f2-d8d4-4c86-8a34-e88ef1e74093")
EVENT_SOURCE_PROPERTY = "x-uni-schedule-source"
SEMESTER_START_PROPERTY = "x-uni-schedule-semester-start"

URL_REGEX = re.compile(r"https?://[^\s]+", re.IGNORECASE)
GROUP_REGEX = re.compile(r"[?&]gr_nr=(\d+)", re.IGNORECASE)
TIME_FORMAT = "%H:%M"
DATE_FORMAT = "%Y-%m-%d"
RULE_LIST_SECTIONS = {"drop", "cancel", "override", "custom"}
RULE_SECTIONS = RULE_LIST_SECTIONS | {"templates"}
RULE_FIELDS = {
    "drop": {"match", "date", "start_time"},
    "cancel": {"match", "date", "start_time"},
    "override": {"match", "date", "filter_start_time", "start_time", "end_time"},
}
CUSTOM_EVENT_FIELDS = {
    "title",
    "description",
    "location",
    "date",
    "start_time",
    "end_time",
    "uid",
}


class RulesValidationError(ValueError):
    """Raised when rules.yaml contains an invalid or unsafe rule."""


class CalendarFetchError(RuntimeError):
    """Raised when the source calendar cannot be downloaded safely."""


def _parse_time(value: object, field: str) -> datetime.time:
    if not isinstance(value, str):
        raise RulesValidationError(f"{field} must be a string in HH:MM format.")
    try:
        return datetime.datetime.strptime(value, TIME_FORMAT).time()
    except ValueError as exc:
        raise RulesValidationError(f"{field} must use a valid HH:MM value, got {value!r}.") from exc


def _parse_date(value: object, field: str) -> datetime.date:
    if isinstance(value, datetime.datetime):
        raise RulesValidationError(f"{field} must be a date, not a date-time.")
    if isinstance(value, datetime.date):
        return value
    if not isinstance(value, str):
        raise RulesValidationError(f"{field} must be a string in YYYY-MM-DD format.")
    try:
        return datetime.datetime.strptime(value, DATE_FORMAT).date()
    except ValueError as exc:
        raise RulesValidationError(
            f"{field} must use a valid YYYY-MM-DD value, got {value!r}."
        ) from exc


def _require_non_empty_text(item: dict, field: str, context: str) -> None:
    if not isinstance(item.get(field), str) or not item[field].strip():
        raise RulesValidationError(f"{context}.{field} must be a non-empty string.")


def validate_rules(rules: object) -> dict:
    """Validate and return a rules mapping with predictable section types."""
    if rules is None:
        return {}
    if not isinstance(rules, dict):
        raise RulesValidationError("The rules file must contain a YAML mapping.")

    unknown = set(rules) - RULE_SECTIONS
    if unknown:
        raise RulesValidationError(f"Unknown rule section(s): {', '.join(sorted(unknown))}.")

    templates = rules.get("templates", {})
    if templates is None:
        templates = {}
        rules["templates"] = templates
    if not isinstance(templates, dict):
        raise RulesValidationError("templates must be a YAML mapping.")
    for name, template in templates.items():
        context = f"templates.{name}"
        if not isinstance(name, str) or not name.strip():
            raise RulesValidationError("Each template name must be a non-empty string.")
        if not isinstance(template, dict):
            raise RulesValidationError(f"{context} must be a YAML mapping.")
        unknown_fields = set(template) - CUSTOM_EVENT_FIELDS
        if unknown_fields:
            raise RulesValidationError(
                f"{context} has unknown field(s): {', '.join(sorted(unknown_fields))}."
            )

    for section in RULE_LIST_SECTIONS:
        items = rules.get(section, [])
        if items is None:
            rules[section] = []
            continue
        if not isinstance(items, list):
            raise RulesValidationError(f"{section} must be a YAML list.")

        for index, item in enumerate(items, start=1):
            context = f"{section}[{index}]"
            if not isinstance(item, dict):
                raise RulesValidationError(f"{context} must be a YAML mapping.")

            if section == "custom":
                if "template" in item:
                    template_name = item["template"]
                    if not isinstance(template_name, str) or not template_name.strip():
                        raise RulesValidationError(
                            f"{context}.template must be a non-empty string."
                        )
                    if template_name not in templates:
                        raise RulesValidationError(
                            f"{context}.template references unknown template {template_name!r}."
                        )
                    item = {**templates[template_name], **item}
                    item.pop("template")
                    items[index - 1] = item

                unknown_fields = set(item) - CUSTOM_EVENT_FIELDS
                if unknown_fields:
                    raise RulesValidationError(
                        f"{context} has unknown field(s): {', '.join(sorted(unknown_fields))}."
                    )
                for field in ("title", "date", "start_time", "end_time"):
                    if field not in item:
                        raise RulesValidationError(f"{context}.{field} is required.")
                _require_non_empty_text(item, "title", context)
                event_date = _parse_date(item["date"], f"{context}.date")
                start = _parse_time(item["start_time"], f"{context}.start_time")
                end = _parse_time(item["end_time"], f"{context}.end_time")
                if end <= start:
                    raise RulesValidationError(f"{context}.end_time must be later than start_time.")
                item["date"] = event_date.isoformat()
                if "uid" in item:
                    _require_non_empty_text(item, "uid", context)
                continue

            unknown_fields = set(item) - RULE_FIELDS[section]
            if unknown_fields:
                raise RulesValidationError(
                    f"{context} has unknown field(s): {', '.join(sorted(unknown_fields))}."
                )

            selector_time = "filter_start_time" if section == "override" else "start_time"
            if not item.get("match") and not item.get("date") and not item.get(selector_time):
                raise RulesValidationError(f"{context} needs at least one matching condition.")
            if "match" in item:
                _require_non_empty_text(item, "match", context)
            if "date" in item:
                item["date"] = _parse_date(item["date"], f"{context}.date").isoformat()

            time_fields = (
                ["start_time"]
                if section != "override"
                else [
                    "filter_start_time",
                    "start_time",
                    "end_time",
                ]
            )
            for field in time_fields:
                if field in item:
                    _parse_time(item[field], f"{context}.{field}")

            if section == "cancel" and "date" not in item:
                raise RulesValidationError(
                    f"{context}.date is required for a one-off cancellation."
                )
            if section == "override" and not ({"start_time", "end_time"} & set(item)):
                raise RulesValidationError(f"{context} must set start_time and/or end_time.")

    custom_uids: dict[str, int] = {}
    for index, item in enumerate(rules.get("custom", []), start=1):
        uid = _custom_uid(item)
        if uid in custom_uids:
            raise RulesValidationError(
                f"custom[{index}].uid duplicates custom[{custom_uids[uid]}].uid: {uid!r}."
            )
        custom_uids[uid] = index

    return rules


def load_rules(filepath: str | Path) -> dict:
    """Load and validate configuration rules from a YAML file."""
    path = Path(filepath)
    if not path.is_file():
        raise RulesValidationError(f"Rules file '{path}' does not exist or is not a file.")
    try:
        with path.open("r", encoding="utf-8") as handle:
            return validate_rules(yaml.safe_load(handle))
    except yaml.YAMLError as exc:
        raise RulesValidationError(f"Could not parse rules file '{path}': {exc}") from exc


def fetch_raw_calendar(url: str) -> Calendar:
    """Fetch the USOS feed with retries and parse its original bytes."""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
    )
    with requests.Session() as session:
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.mount("http://", HTTPAdapter(max_retries=retry))
        try:
            response = session.get(url, timeout=(10, 30))
            response.raise_for_status()
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else "unknown"
            raise CalendarFetchError(f"Calendar feed returned HTTP status {status_code}.") from None
        except requests.RequestException:
            raise CalendarFetchError("Could not download the calendar feed.") from None
        return Calendar.from_ical(response.content)


def _event_start_local(event: Event) -> datetime.datetime | None:
    value = event.get("dtstart")
    if value is None or not isinstance(value.dt, datetime.datetime):
        return None
    value = value.dt
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TIMEZONE)
    return value.astimezone(LOCAL_TIMEZONE)


def sanitize_event(event: Event) -> None:
    """Clean up event fields: reformat LOCATION and strip clutter from DESCRIPTION."""
    raw_desc = str(event.get("description", ""))
    lines = [line.strip() for line in raw_desc.splitlines() if line.strip()]

    room = None
    building = None
    for index, line in enumerate(lines):
        if line.lower().startswith("sala:"):
            room = line.split(":", 1)[1].strip()
            if index + 1 < len(lines) and not lines[index + 1].lower().startswith("http"):
                building_raw = lines[index + 1]
                building = re.sub(
                    r"^(?:budynek\s+)?wydziału\b",
                    "Wydział",
                    building_raw,
                    flags=re.IGNORECASE,
                ).strip()
                building = re.sub(r"^budynek\s+", "", building, flags=re.IGNORECASE).strip()
            break

    if room and building:
        event["location"] = f"s. {room}, {building}"
    elif room:
        event["location"] = f"s. {room}"

    url_match = URL_REGEX.search(raw_desc)
    link = url_match.group(0).rstrip(").,;>") if url_match else None
    group_match = GROUP_REGEX.search(link) if link else None

    description_lines = []
    if group_match:
        description_lines.append(f"Grupa: {group_match.group(1)}")
    if link:
        description_lines.append(f"USOS link: {link}")
    event["description"] = "\n".join(description_lines)


def _matches_event(event: Event, rule: dict, time_field: str = "start_time") -> bool:
    summary = str(event.get("summary", ""))
    match_text = rule.get("match")
    if match_text and match_text.casefold() not in summary.casefold():
        return False

    local_start = _event_start_local(event)
    if "date" in rule and (local_start is None or local_start.date().isoformat() != rule["date"]):
        return False
    return not (
        time_field in rule
        and (local_start is None or local_start.strftime(TIME_FORMAT) != rule[time_field])
    )


def should_drop_event(event: Event, drop_rules: list[dict]) -> bool:
    return any(_matches_event(event, rule) for rule in drop_rules)


def should_cancel_event(event: Event, cancel_rules: list[dict]) -> bool:
    return any(_matches_event(event, rule) for rule in cancel_rules)


def _in_original_timezone(
    local_value: datetime.datetime, original: datetime.datetime
) -> datetime.datetime:
    if original.tzinfo is None:
        return local_value.replace(tzinfo=None)
    return local_value.astimezone(original.tzinfo)


def apply_overrides(event: Event, override_rules: list[dict]) -> bool:
    """Modify event times and report whether either value changed."""
    if event.get("dtstart") is None or event.get("dtend") is None:
        return False

    changed = False
    for rule in override_rules:
        if not _matches_event(event, rule, time_field="filter_start_time"):
            continue

        start_property = event.get("dtstart")
        end_property = event.get("dtend")
        current_start = start_property.dt
        current_end = end_property.dt
        if not isinstance(current_start, datetime.datetime) or not isinstance(
            current_end, datetime.datetime
        ):
            return changed

        local_start = _event_start_local(event)
        assert local_start is not None
        if current_end.tzinfo is None:
            local_end = current_end.replace(tzinfo=LOCAL_TIMEZONE)
        else:
            local_end = current_end.astimezone(LOCAL_TIMEZONE)

        if "start_time" in rule:
            new_time = _parse_time(rule["start_time"], "override.start_time")
            local_start = local_start.replace(hour=new_time.hour, minute=new_time.minute)
        if "end_time" in rule:
            new_time = _parse_time(rule["end_time"], "override.end_time")
            local_end = local_start.replace(hour=new_time.hour, minute=new_time.minute)
        if local_end <= local_start:
            raise RulesValidationError(
                f"Override for {event.get('summary', '<untitled>')!s} makes its end "
                "time earlier than or equal to its start time."
            )

        new_start = _in_original_timezone(local_start, current_start)
        new_end = _in_original_timezone(local_end, current_end)
        if new_start != start_property.dt:
            event["dtstart"] = icalendar.vDDDTypes(new_start)
            changed = True
        if new_end != end_property.dt:
            event["dtend"] = icalendar.vDDDTypes(new_end)
            changed = True

    return changed


def _custom_uid(item: dict) -> str:
    if item.get("uid"):
        return item["uid"]
    identity = "|".join((item["date"], item["start_time"], item["title"].strip()))
    return f"custom-{uuid.uuid5(CUSTOM_UID_NAMESPACE, identity)}@uni-schedule"


def create_custom_event(item: dict) -> Event:
    """Instantiate a new Calendar Event based on a validated YAML entry."""
    event = Event()
    event.add("summary", item["title"])
    if item.get("description"):
        event.add("description", item["description"])
    if item.get("location"):
        event.add("location", item["location"])

    start_dt = datetime.datetime.strptime(
        f"{item['date']} {item['start_time']}", f"{DATE_FORMAT} {TIME_FORMAT}"
    ).replace(tzinfo=LOCAL_TIMEZONE)
    end_dt = datetime.datetime.strptime(
        f"{item['date']} {item['end_time']}", f"{DATE_FORMAT} {TIME_FORMAT}"
    ).replace(tzinfo=LOCAL_TIMEZONE)

    event.add("dtstart", start_dt)
    event.add("dtend", end_dt)
    event.add("dtstamp", datetime.datetime.now(tz=datetime.timezone.utc))
    event.add("uid", _custom_uid(item))
    event.add("status", "CONFIRMED")
    event.add("transp", "OPAQUE")
    event.add(EVENT_SOURCE_PROPERTY, "CUSTOM")
    return event


def build_calendar(raw_cal: Calendar, rules: dict) -> tuple[Calendar, dict[str, int]]:
    """Build a cleaned calendar while preserving non-event ICS components."""
    new_cal = Calendar()
    for key, value in raw_cal.items():
        new_cal.add(key, copy.deepcopy(value))

    counts = {"sanitized": 0, "dropped": 0, "cancelled": 0, "overridden": 0, "injected": 0}
    for component in raw_cal.subcomponents:
        component = copy.deepcopy(component)
        if component.name != "VEVENT":
            new_cal.add_component(component)
            continue
        if should_drop_event(component, rules.get("drop", [])):
            counts["dropped"] += 1
            continue
        if should_cancel_event(component, rules.get("cancel", [])):
            counts["cancelled"] += 1
            continue

        component[EVENT_SOURCE_PROPERTY] = "USOS"
        sanitize_event(component)
        if apply_overrides(component, rules.get("override", [])):
            counts["overridden"] += 1
        new_cal.add_component(component)
        counts["sanitized"] += 1

    for item in rules.get("custom", []):
        new_cal.add_component(create_custom_event(item))
        counts["injected"] += 1
    return new_cal, counts


def _event_boundary_local(event: Event) -> datetime.datetime | None:
    """Return the local end boundary used to decide whether an event is historical."""
    value = event.get("dtend") or event.get("dtstart")
    if value is None:
        return None
    if isinstance(value.dt, datetime.datetime):
        if value.dt.tzinfo is None:
            return value.dt.replace(tzinfo=LOCAL_TIMEZONE)
        return value.dt.astimezone(LOCAL_TIMEZONE)
    if isinstance(value.dt, datetime.date):
        return datetime.datetime.combine(value.dt, datetime.time.min, tzinfo=LOCAL_TIMEZONE)
    return None


def _event_identity(event: Event) -> tuple[str, ...]:
    """Return a stable identity for deduplicating current and archived events."""
    uid = str(event.get("uid", "")).strip()
    recurrence_id = str(event.get("recurrence-id", "")).strip()
    if uid:
        return ("uid", uid, recurrence_id)
    return (
        "fallback",
        str(event.get("summary", "")),
        str(event.get("dtstart", "")),
        str(event.get("dtend", "")),
    )


def _parse_month_day(value: str, field: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}-\d{2}", value):
        raise ValueError(f"{field} must use MM-DD format.")
    month, day = (int(part) for part in value.split("-"))
    try:
        datetime.date(2000, month, day)
    except ValueError:
        raise ValueError(f"{field} must contain a valid month and day.") from None
    return month, day


def _as_local_datetime(value: datetime.datetime | None = None) -> datetime.datetime:
    """Return an aware Europe/Warsaw datetime."""
    value = value or datetime.datetime.now(tz=LOCAL_TIMEZONE)
    if value.tzinfo is None:
        return value.replace(tzinfo=LOCAL_TIMEZONE)
    return value.astimezone(LOCAL_TIMEZONE)


def _source_event_dates(*calendars: Calendar | None) -> list[datetime.date]:
    dates = set()
    for calendar in calendars:
        if calendar is None:
            continue
        for event in calendar.walk("VEVENT"):
            if str(event.get(EVENT_SOURCE_PROPERTY, "")).upper() != "USOS":
                continue
            start = _event_start_local(event)
            if start is not None:
                dates.add(start.date())
    return sorted(dates)


def _stored_semester_start(calendar: Calendar | None) -> datetime.date | None:
    if calendar is None or calendar.get(SEMESTER_START_PROPERTY) is None:
        return None
    try:
        return datetime.date.fromisoformat(str(calendar[SEMESTER_START_PROPERTY]))
    except ValueError:
        return None


def determine_semester_start(
    current: Calendar,
    previous: Calendar | None = None,
    now: datetime.datetime | None = None,
    summer_search_start: str = DEFAULT_SUMMER_BREAK_SEARCH_START,
    summer_hard_start: str = DEFAULT_SUMMER_SEMESTER_HARD_START,
    min_break_days: int = DEFAULT_MIN_SEMESTER_BREAK_DAYS,
) -> datetime.datetime:
    """Determine the semester boundary from a fixed winter date or a winter break."""
    if min_break_days < 1:
        raise ValueError("MIN_SEMESTER_BREAK_DAYS must be a positive integer.")

    now_local = _as_local_datetime(now)
    winter_start = datetime.datetime(now_local.year, *WINTER_SEMESTER_START, tzinfo=LOCAL_TIMEZONE)
    if now_local >= winter_start:
        return winter_start

    search_month, search_day = _parse_month_day(summer_search_start, "SUMMER_BREAK_SEARCH_START")
    hard_month, hard_day = _parse_month_day(summer_hard_start, "SUMMER_SEMESTER_HARD_START")
    search_date = datetime.date(now_local.year, search_month, search_day)
    hard_date = datetime.date(now_local.year, hard_month, hard_day)
    if search_date >= hard_date:
        raise ValueError("SUMMER_BREAK_SEARCH_START must be before SUMMER_SEMESTER_HARD_START.")

    stored_start = _stored_semester_start(previous)
    if stored_start is not None and search_date <= stored_start <= hard_date:
        return datetime.datetime.combine(stored_start, datetime.time.min, LOCAL_TIMEZONE)

    source_dates = _source_event_dates(previous, current)
    january_first = datetime.date(now_local.year, 1, 1)
    candidates: list[tuple[int, datetime.date]] = []
    for before, after in itertools.pairwise(source_dates):
        gap_days = (after - before).days
        if (
            before < january_first
            or not search_date <= after <= hard_date
            or after > now_local.date()
            or gap_days < min_break_days
        ):
            continue
        activity_end = after + datetime.timedelta(days=14)
        activity_dates = sum(after <= date <= activity_end for date in source_dates)
        if activity_dates >= MIN_SUMMER_ACTIVITY_DATES:
            candidates.append((gap_days, after))

    if candidates:
        detected_start = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))[1]
        return datetime.datetime.combine(detected_start, datetime.time.min, LOCAL_TIMEZONE)
    if now_local.date() >= hard_date:
        return datetime.datetime.combine(hard_date, datetime.time.min, LOCAL_TIMEZONE)
    return datetime.datetime(now_local.year - 1, *WINTER_SEMESTER_START, tzinfo=LOCAL_TIMEZONE)


def retain_calendar_history(
    current: Calendar,
    previous: Calendar | None,
    semester_start: datetime.datetime | None = None,
    now: datetime.datetime | None = None,
    rules: dict | None = None,
) -> dict[str, int]:
    """Keep past events from the current semester and prune older history."""
    now_local = _as_local_datetime(now)
    cutoff = semester_start or determine_semester_start(current, previous, now_local)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=LOCAL_TIMEZONE)
    else:
        cutoff = cutoff.astimezone(LOCAL_TIMEZONE)
    if cutoff > now_local:
        raise ValueError("semester start cannot be in the future")

    retained_components = []
    identities: set[tuple[str, ...]] = set()
    expired_identities: set[tuple[str, ...]] = set()
    pruned = 0
    for component in current.subcomponents:
        if component.name != "VEVENT":
            retained_components.append(component)
            continue
        boundary = _event_boundary_local(component)
        if boundary is not None and boundary < cutoff:
            expired_identities.add(_event_identity(component))
            pruned += 1
            continue
        retained_components.append(component)
        identities.add(_event_identity(component))
    current.subcomponents = retained_components

    archived = 0
    rules = rules or {}
    if previous is not None:
        for component in previous.walk("VEVENT"):
            boundary = _event_boundary_local(component)
            identity = _event_identity(component)
            if boundary is not None and boundary < cutoff:
                if identity not in expired_identities:
                    expired_identities.add(identity)
                    pruned += 1
                continue
            if (
                boundary is None
                or boundary > now_local
                or identity in identities
                or should_drop_event(component, rules.get("drop", []))
                or should_cancel_event(component, rules.get("cancel", []))
            ):
                continue
            current.add_component(copy.deepcopy(component))
            identities.add(identity)
            archived += 1

    return {"archived": archived, "pruned": pruned}


def load_previous_calendar(calendar_path: str | Path) -> Calendar | None:
    """Load a previously saved calendar when it is available and valid."""
    calendar_file = Path(calendar_path)
    if not calendar_file.exists():
        return None
    try:
        return Calendar.from_ical(calendar_file.read_bytes())
    except (OSError, ValueError) as exc:
        print(f"Warning: Could not read previous calendar history: {type(exc).__name__}.")
        return None


def write_calendar_atomic(calendar: Calendar, output_path: str | Path) -> None:
    """Atomically replace the output so readers never see a partial ICS file."""
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_mode = stat.S_IMODE(output_file.stat().st_mode)
    except FileNotFoundError:
        existing_mode = None
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=output_file.parent, prefix=f".{output_file.name}.", delete=False
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(calendar.to_ical())
            handle.flush()
            os.fsync(handle.fileno())
        if existing_mode is not None:
            temporary_path.chmod(existing_mode)
        os.replace(temporary_path, output_file)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def process_calendar(
    url: str,
    rules_path: str | Path,
    output_path: str | Path,
    history_path: str | Path = DEFAULT_HISTORY_ICS_PATH,
    summer_break_search_start: str = DEFAULT_SUMMER_BREAK_SEARCH_START,
    summer_semester_hard_start: str = DEFAULT_SUMMER_SEMESTER_HARD_START,
    min_semester_break_days: int = DEFAULT_MIN_SEMESTER_BREAK_DAYS,
) -> dict[str, int]:
    print("Reading configuration rules...")
    rules = load_rules(rules_path)
    history_file = Path(history_path).resolve()
    output_file = Path(output_path).resolve()
    previous_cal = load_previous_calendar(history_file)
    if previous_cal is None and history_file != output_file:
        previous_cal = load_previous_calendar(output_file)
        if previous_cal is not None:
            print("Migrating calendar history from the previous output file...")
    print("Fetching calendar feed from USOS...")
    raw_cal = fetch_raw_calendar(url)
    new_cal, counts = build_calendar(raw_cal, rules)
    semester_start = determine_semester_start(
        new_cal,
        previous_cal,
        summer_search_start=summer_break_search_start,
        summer_hard_start=summer_semester_hard_start,
        min_break_days=min_semester_break_days,
    )
    new_cal[SEMESTER_START_PROPERTY] = semester_start.date().isoformat()
    counts.update(
        retain_calendar_history(
            new_cal,
            previous_cal,
            semester_start=semester_start,
            rules=rules,
        )
    )

    print(f"Sanitized {counts['sanitized']} native events.")
    for key, label in (
        ("dropped", "Dropped"),
        ("cancelled", "Cancelled"),
        ("overridden", "Modified"),
        ("injected", "Injected"),
        ("archived", "Preserved from history"),
        ("pruned", "Removed from before the current semester"),
    ):
        if counts[key]:
            print(f"{label} {counts[key]} event(s).")

    write_calendar_atomic(new_cal, history_file)
    if history_file != output_file:
        write_calendar_atomic(new_cal, output_file)
    print(f"Calendar successfully updated and written to {output_path}.")
    return counts


def main() -> None:
    load_dotenv()
    url = os.getenv("USOS_ICAL_URL")
    if not url:
        raise ValueError("USOS_ICAL_URL is not configured. Set it in .env or the environment.")
    try:
        min_break_days = int(
            os.getenv("MIN_SEMESTER_BREAK_DAYS", str(DEFAULT_MIN_SEMESTER_BREAK_DAYS))
        )
    except ValueError:
        raise ValueError("MIN_SEMESTER_BREAK_DAYS must be an integer.") from None
    process_calendar(
        url=url,
        rules_path=os.getenv("RULES_FILE_PATH", DEFAULT_RULES_FILE_PATH),
        output_path=os.getenv("OUTPUT_ICS_PATH", DEFAULT_OUTPUT_ICS_PATH),
        history_path=os.getenv("HISTORY_ICS_PATH", DEFAULT_HISTORY_ICS_PATH),
        summer_break_search_start=os.getenv(
            "SUMMER_BREAK_SEARCH_START", DEFAULT_SUMMER_BREAK_SEARCH_START
        ),
        summer_semester_hard_start=os.getenv(
            "SUMMER_SEMESTER_HARD_START", DEFAULT_SUMMER_SEMESTER_HARD_START
        ),
        min_semester_break_days=min_break_days,
    )


if __name__ == "__main__":
    main()
