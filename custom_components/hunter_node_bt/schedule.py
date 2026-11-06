"""Pure helpers for reading and editing NODE-BT programs.

Nothing here talks to Bluetooth or Home Assistant, so it is shared with the
tests. A program section looks like this (see PROTOCOL.md):

    "Program_A": {"Name": "...", "ScheduleType": 0, "ScheduleDays": 127,
                  "IntervalDayCount": 0, "IntervalDayNext": 0,
                  "StartTimes": [390, 65535, ...], "RunTime": [600, 0, ...]}

``StartTimes`` are minutes after local midnight with 65535 marking an unused
slot, and ``RunTime`` is seconds per station (0 = station not in the program).

``ScheduleType`` follows the order of the app's day picker. Weekday schedules
were verified on hardware; ``ScheduleDays`` is a bitmask with bit 0 = Sunday
through bit 6 = Saturday.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

PROGRAM_LETTERS = ("A", "B", "C")

MINUTES_PER_DAY = 1440
UNUSED_START = 0xFFFF
DEFAULT_START_SLOTS = 8
MAX_INTERVAL_DAYS = 31

SCHEDULE_WEEKDAYS = 0
SCHEDULE_ODD = 1
SCHEDULE_EVEN = 2
SCHEDULE_INTERVAL = 3

DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
ALL_DAYS_MASK = 0x7F
# Display order for day lists: Monday first, as most calendars show them.
_DISPLAY_ORDER = (1, 2, 3, 4, 5, 6, 0)

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_INTERVAL_RE = re.compile(r"^every\s+(\d{1,2})\s+days?$")


def _as_int(value: object, default: int = 0) -> int:
    """Return value as an int, or default for anything that isn't a number."""
    if isinstance(value, bool):
        return int(value)
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# -- start times ------------------------------------------------------------


def format_start_time(minutes: int) -> str:
    """390 -> '06:30'."""
    hours, mins = divmod(minutes, 60)
    return f"{hours:02d}:{mins:02d}"


def parse_start_time(text: str) -> int:
    """'6:30' or '06:30' -> 390 (minutes after midnight)."""
    match = _TIME_RE.match(str(text).strip())
    if not match:
        raise ValueError(f"Start time '{text}' must be HH:MM")
    hours, mins = int(match.group(1)), int(match.group(2))
    if hours > 23 or mins > 59:
        raise ValueError(f"Start time '{text}' is not a valid time of day")
    return hours * 60 + mins


def parse_start_times(text: str) -> list[int]:
    """'06:30, 16:00' -> [390, 960]; 'off' or an empty string -> []."""
    cleaned = str(text).strip().lower()
    if cleaned in ("", "off", "none"):
        return []
    return sorted({parse_start_time(part) for part in cleaned.split(",") if part.strip()})


# -- water days ---------------------------------------------------------------


def days_mask_to_names(mask: int) -> list[str]:
    """Day abbreviations set in a ScheduleDays mask, Monday first."""
    return [DAY_NAMES[bit] for bit in _DISPLAY_ORDER if mask & (1 << bit)]


def names_to_days_mask(names: list[str]) -> int:
    """['Mon', 'wednesday', 'fri'] -> ScheduleDays mask."""
    mask = 0
    for name in names:
        key = str(name).strip().lower()[:3]
        for bit, day in enumerate(DAY_NAMES):
            if day.lower() == key:
                mask |= 1 << bit
                break
        else:
            raise ValueError(f"Unknown day '{name}'")
    return mask


@dataclass(frozen=True)
class WaterDays:
    """The day part of a program: which ScheduleType and its parameter."""

    schedule_type: int
    days_mask: int = 0
    interval_days: int = 0


def parse_water_days(text: str) -> WaterDays:
    """Parse the water days text used by the text entity and the set_program action.

    Accepted: 'every day', 'odd days', 'even days', 'every 3 days' and
    comma-separated day names such as 'Mon, Wed, Fri'.
    """
    cleaned = " ".join(str(text).strip().lower().split())
    if cleaned in ("every day", "daily", "all"):
        return WaterDays(SCHEDULE_WEEKDAYS, days_mask=ALL_DAYS_MASK)
    if cleaned in ("odd", "odd days"):
        return WaterDays(SCHEDULE_ODD)
    if cleaned in ("even", "even days"):
        return WaterDays(SCHEDULE_EVEN)
    match = _INTERVAL_RE.match(cleaned)
    if match:
        interval = int(match.group(1))
        if not 1 <= interval <= MAX_INTERVAL_DAYS:
            raise ValueError(f"Interval must be between 1 and {MAX_INTERVAL_DAYS} days")
        return WaterDays(SCHEDULE_INTERVAL, interval_days=interval)
    names = [part for part in cleaned.replace(" ", ",").split(",") if part]
    if not names:
        raise ValueError("Water days can't be empty")
    return WaterDays(SCHEDULE_WEEKDAYS, days_mask=names_to_days_mask(names))


# -- programs -------------------------------------------------------------------


@dataclass
class Program:
    """A decoded Program_X section."""

    letter: str
    name: str | None
    schedule_type: int
    days_mask: int
    interval_days: int
    start_times: list[int] = field(default_factory=list)
    run_times: dict[int, int] = field(default_factory=dict)

    @property
    def is_active(self) -> bool:
        """True if the program would ever water."""
        if not self.start_times or not self.run_times:
            return False
        if self.schedule_type == SCHEDULE_WEEKDAYS and not self.days_mask & ALL_DAYS_MASK:
            return False
        return True

    @property
    def run_seconds_per_start(self) -> int:
        """Total watering for one start; stations run one after another."""
        return sum(self.run_times.values())

    @property
    def start_times_text(self) -> str:
        """Start times as the text entity shows them, e.g. '06:30, 16:00'."""
        if not self.start_times:
            return "off"
        return ", ".join(format_start_time(minutes) for minutes in self.start_times)

    @property
    def water_days_text(self) -> str:
        """Water days as the text entity shows them, e.g. 'Mon, Wed, Fri'."""
        if self.schedule_type == SCHEDULE_WEEKDAYS:
            mask = self.days_mask & ALL_DAYS_MASK
            if mask == ALL_DAYS_MASK:
                return "every day"
            if not mask:
                return "no days"
            return ", ".join(days_mask_to_names(mask))
        if self.schedule_type == SCHEDULE_ODD:
            return "odd days"
        if self.schedule_type == SCHEDULE_EVEN:
            return "even days"
        if self.schedule_type == SCHEDULE_INTERVAL:
            count = max(self.interval_days, 1)
            return "every day" if count == 1 else f"every {count} days"
        return f"schedule type {self.schedule_type}"

    def summary(self) -> str:
        """One line for the schedule sensor, e.g. 'Every day at 06:30 (10 min)'."""
        if not self.is_active:
            return "Off"
        minutes = round(self.run_seconds_per_start / 60)
        days = self.water_days_text
        return f"{days[0].upper()}{days[1:]} at {self.start_times_text} ({minutes} min)"


def parse_program(letter: str, raw: dict | None) -> Program:
    """Decode a Program_X section; a missing section decodes as an empty program."""
    raw = raw if isinstance(raw, dict) else {}
    name = raw.get("Name")
    starts = raw.get("StartTimes") if isinstance(raw.get("StartTimes"), list) else []
    run_times = raw.get("RunTime") if isinstance(raw.get("RunTime"), list) else []
    return Program(
        letter=letter,
        name=name.strip() if isinstance(name, str) and name.strip() else None,
        schedule_type=_as_int(raw.get("ScheduleType"), SCHEDULE_WEEKDAYS),
        days_mask=_as_int(raw.get("ScheduleDays")),
        interval_days=_as_int(raw.get("IntervalDayCount")),
        start_times=sorted(
            {m for m in (_as_int(v, -1) for v in starts) if 0 <= m < MINUTES_PER_DAY}
        ),
        # 0 means "not in this program"; very large values are unset slots.
        run_times={
            station: seconds
            for station, seconds in enumerate((_as_int(v) for v in run_times), start=1)
            if 0 < seconds < 24 * 3600
        },
    )


def build_program_payload(
    current: dict,
    *,
    start_times: list[int] | None = None,
    water_days: WaterDays | None = None,
    run_times: dict[int, int] | None = None,
    station_count: int = 0,
) -> dict:
    """Merge changes into a full Program_X section, ready to write back.

    The controller expects the whole section, so everything not being changed is
    copied from what it last reported. Array lengths and the unused-slot value
    are kept as the firmware sent them.
    """
    payload = dict(current)

    if start_times is not None:
        template = current.get("StartTimes") if isinstance(current.get("StartTimes"), list) else []
        slots = len(template) or DEFAULT_START_SLOTS
        cleaned = sorted({int(minutes) for minutes in start_times})
        if any(not 0 <= minutes < MINUTES_PER_DAY for minutes in cleaned):
            raise ValueError("Start times must be between 00:00 and 23:59")
        if len(cleaned) > slots:
            raise ValueError(f"At most {slots} start times are supported")
        payload["StartTimes"] = cleaned + [UNUSED_START] * (slots - len(cleaned))

    if water_days is not None:
        payload["ScheduleType"] = water_days.schedule_type
        if water_days.schedule_type == SCHEDULE_WEEKDAYS:
            payload["ScheduleDays"] = water_days.days_mask
        if water_days.schedule_type == SCHEDULE_INTERVAL:
            payload["IntervalDayCount"] = water_days.interval_days

    if run_times is not None:
        template = current.get("RunTime") if isinstance(current.get("RunTime"), list) else []
        # Only stations the controller reported (or knows it has) can be set.
        length = max(len(template), station_count) or max(run_times, default=0)
        values = [_as_int(v) for v in template] + [0] * (length - len(template))
        for station, seconds in run_times.items():
            if not 1 <= station <= length:
                raise ValueError(f"Station {station} does not exist")
            if not 0 <= seconds < 24 * 3600:
                raise ValueError("Run time must be under 24 hours")
            values[station - 1] = int(seconds)
        payload["RunTime"] = values

    return payload
