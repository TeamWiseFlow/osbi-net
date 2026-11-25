"""Daily source capture slots, independent of report-generation schedules."""
from datetime import datetime, time, timedelta
import re
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from osbi_contracts.models import StrictModel


class CaptureSchedule(StrictModel):
    times: list[str] = Field(default_factory=list, max_length=48)
    timezone: str = "Asia/Shanghai"

    @field_validator("times")
    @classmethod
    def valid_times(cls, values):
        if any(not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value) for value in values):
            raise ValueError("Capture times must use HH:MM in 24-hour time")
        if len(set(values)) != len(values):
            raise ValueError("Capture times must be unique")
        return sorted(values)

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except (ValueError, KeyError) as error:
            raise ValueError("Unknown capture timezone") from error
        return value


def next_capture_at(schedule: CaptureSchedule, *, now: float) -> float:
    """Return the next future slot. Ambiguous local clocks run only once.

    Nonexistent DST clocks are skipped. For a repeated clock, use its earliest
    real occurrence so a daily slot cannot fire twice during the fallback hour.
    """
    if not schedule.times:
        raise ValueError("At least one capture time is required")
    zone = ZoneInfo(schedule.timezone)
    current = datetime.fromtimestamp(now, zone)
    for offset in range(8):
        day = current.date() + timedelta(days=offset)
        candidates = []
        for clock in schedule.times:
            naive = datetime.combine(day, time.fromisoformat(clock))
            valid = []
            for fold in (0, 1):
                stamp = naive.replace(tzinfo=zone, fold=fold).timestamp()
                if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) == naive:
                    valid.append(stamp)
            if valid and min(valid) > now:
                candidates.append(min(valid))
        if candidates:
            return min(candidates)
    raise ValueError("No valid capture slot in the next week")
