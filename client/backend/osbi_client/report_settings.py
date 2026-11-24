"""Downstream report tasks: shared sections, task overlays, schedules and targets.

Sections are node-local templates (title/channels/query/territory/local rules).
A task board is a read-only overlay (selector/topN/minEdges/purpose/prompt) over
one section; the section itself is never mutated. Server channels remain global;
boards are local report units and may share or combine channels.
"""
from dataclasses import dataclass
from datetime import date, datetime, time
import re
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from osbi_contracts.models import StrictModel

DEFAULT_TERRITORY = ["上海市", "静安区", "临汾路街道"]
DEFAULT_TIMEZONE = "Asia/Shanghai"
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
SELECTORS = ("candidates", "action", "policy", "evidence")
TARGET_TYPES = ("mimicwx", "awada", "wecom-webhook")


class ReportQuery(StrictModel):
    text: str = Field(default="", max_length=1000)
    entityUris: list[str] = Field(default_factory=list, max_length=500)
    contentTypes: list[str] = Field(default_factory=list, max_length=20)
    territoryPreference: list[str] = Field(default_factory=list, max_length=20)
    territoryMode: Literal["prefer", "only"] = "prefer"
    sourceScope: Literal["all", "own"] = "all"


class ReportLocalRules(StrictModel):
    keywords: list[str] = Field(default_factory=list, max_length=50)
    excludeKeywords: list[str] = Field(default_factory=list, max_length=50)
    leaderBoost: bool = True
    localityPenalty: bool = True
    jev: Literal["inherit", "on", "off"] = "inherit"


class ReportSection(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=120)
    channels: list[str] = Field(min_length=1, max_length=8)
    query: ReportQuery = Field(default_factory=ReportQuery)
    localRules: ReportLocalRules = Field(default_factory=ReportLocalRules)

    @field_validator("channels")
    @classmethod
    def clean_channels(cls, value):
        cleaned = [channel.strip() for channel in value]
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", channel) for channel in cleaned):
            raise ValueError("Channel IDs must use lowercase letters, digits, '-' or '_'")
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("Section channels must be unique")
        return cleaned


class ReportBoardOverride(StrictModel):
    section: str = Field(min_length=1, max_length=120)
    selector: Literal["candidates", "action", "policy", "evidence"] = "candidates"
    topN: int = Field(default=3, ge=1, le=20)
    minEdges: int = Field(default=4, ge=1, le=100)
    purpose: str = Field(default="", max_length=4000)
    prompt: str = Field(default="", max_length=8000)


class ReportSchedule(StrictModel):
    time: str = "08:40"
    weekday: Literal["monday", "tuesday", "wednesday", "thursday", "friday",
                     "saturday", "sunday"] | None = None
    timezone: str = DEFAULT_TIMEZONE

    @field_validator("time")
    @classmethod
    def valid_clock(cls, value):
        if len(value) != 5:
            raise ValueError("Schedule time must use HH:MM")
        try:
            time.fromisoformat(value)
        except ValueError as error:
            raise ValueError("Schedule time must use HH:MM") from error
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value):
        try:
            ZoneInfo(value)
        except Exception as error:
            raise ValueError("Unknown schedule timezone") from error
        return value


class ReportTask(StrictModel):
    enabled: bool = True
    schedule: ReportSchedule
    boards: list[ReportBoardOverride] = Field(default_factory=list, max_length=20)
    targetIds: list[str] = Field(default_factory=list, max_length=20)
    dryRun: bool = True

    @model_validator(mode="after")
    def unique_boards(self):
        if len({board.section for board in self.boards}) != len(self.boards):
            raise ValueError("Each section may appear at most once per task")
        if self.enabled and not self.boards:
            raise ValueError("An enabled report task requires at least one section")
        return self


class ReportTarget(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    type: Literal["mimicwx", "awada", "wecom-webhook"]
    label: str = Field(default="", max_length=120)
    enabled: bool = True
    config: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_config(self):
        if self.type == "wecom-webhook":
            # Reserved and not implemented; stored but never sendable yet.
            return self
        if self.type == "mimicwx":
            allowed = {"baseUrl", "base_url", "window", "token", "configPath", "config_path",
                       "tokenConfigured", "tokenSuffix"}
        else:
            allowed = {"configPath", "config_path", "channelId", "channel_id", "fileChannelId", "file_channel_id"}
        unknown = set(self.config) - allowed
        if unknown:
            raise ValueError(f"Unknown {self.type} target fields: {sorted(unknown)}")
        return self

    def public_config(self) -> dict:
        config = dict(self.config)
        for name in ("token", "webhookUrl", "webhook_url"):
            value = config.pop(name, None)
            if name == "token":
                config["tokenConfigured"] = bool(value)
                config["tokenSuffix"] = value[-4:] if value else ""
            elif self.type == "wecom-webhook":
                config["webhookConfigured"] = bool(value)
        return dict(id=self.id, type=self.type, label=self.label, enabled=self.enabled,
                    config=config)


class ReportProfile(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    sections: list[ReportSection] = Field(default_factory=list, max_length=20)
    targets: list[ReportTarget] = Field(default_factory=list, max_length=10)
    topnews: ReportTask
    weeklyreport: ReportTask

    @model_validator(mode="after")
    def validate_profile(self):
        section_ids = {section.id for section in self.sections}
        if len(section_ids) != len(self.sections):
            raise ValueError("Section IDs must be unique")
        if len({section.title for section in self.sections}) != len(self.sections):
            raise ValueError("Section titles must be unique")
        for task_name in ("topnews", "weeklyreport"):
            task = getattr(self, task_name)
            unknown = [board.section for board in task.boards if board.section not in section_ids]
            if unknown:
                raise ValueError(f"{task_name} references unknown sections: {unknown}")
            unknown_targets = [target for target in task.targetIds
                               if target not in {t.id for t in self.targets}]
            if unknown_targets:
                raise ValueError(f"{task_name} references unknown targets: {unknown_targets}")
        if len({target.id for target in self.targets}) != len(self.targets):
            raise ValueError("Target IDs must be unique")
        return self

    def section(self, key: str) -> ReportSection:
        for section in self.sections:
            if section.id == key or section.title == key:
                return section
        raise ValueError(f"Unknown report section: {key}")


@dataclass(frozen=True)
class BoardView:
    """Read-only merged view of one section and its task overlay."""
    id: str
    title: str
    channels: tuple[str, ...]
    query: ReportQuery
    localRules: ReportLocalRules
    selector: str
    topN: int
    minEdges: int
    purpose: str
    prompt: str
    task: str
    profile: str

    @property
    def intent(self) -> str:
        return "\n".join(value for value in (self.purpose, self.title) if value)

    @property
    def query_text(self) -> str:
        return self.query.text


def _task(time_value: str, weekday: str | None, *boards) -> ReportTask:
    return ReportTask(enabled=True, schedule=ReportSchedule(time=time_value, weekday=weekday),
                      boards=list(boards))


def _section(identifier, title, channels, purpose="", **rules) -> ReportSection:
    return ReportSection(id=identifier, title=title, channels=channels,
                         query=ReportQuery(territoryPreference=list(DEFAULT_TERRITORY)),
                         localRules=ReportLocalRules(**rules))


def default_report_profiles() -> list[ReportProfile]:
    """Shanghai/Jing'an parity defaults; deployments replace this in config."""
    sections = [
        _section("party-building", "基层党建", ["party-building"]),
        _section("economy", "经济动态", ["economy"]),
        _section("community", "社区治理", ["community"]),
        _section("digital", "数字社区", ["digital"]),
        _section("policy-latest", "政策速递", ["policy-latest"]),
    ]
    topnews = _task("08:40", None, *[
        ReportBoardOverride(section=section.id, selector="candidates", topN=3, minEdges=4)
        for section in sections])
    weekly = _task("07:50", "monday", *[
        ReportBoardOverride(
            section=section.id,
            selector="action" if section.id in {"party-building", "community", "digital"} else "policy",
            topN=2, minEdges=4)
        for section in sections])
    return [ReportProfile(id="default", name="Default", sections=sections,
                          targets=[], topnews=topnews, weeklyreport=weekly)]


def board_views(config, profile_id: str, task: str) -> list[BoardView]:
    profile = next((p for p in config.reportProfiles if p.id == profile_id), None)
    if profile is None:
        raise ValueError("Report profile not configured on this client")
    task_config = getattr(profile, task)
    views = []
    for board in task_config.boards:
        section = profile.section(board.section)
        views.append(BoardView(
            id=section.id, title=section.title, channels=tuple(section.channels),
            query=section.query, localRules=section.localRules,
            selector=board.selector, topN=board.topN, minEdges=board.minEdges,
            purpose=board.purpose, prompt=board.prompt, task=task, profile=profile.id))
    return views


def resolve_board(config, profile_id, task, board_id, channel):
    """Return the merged BoardView for one task/section, validating the channel."""
    profile = next((p for p in config.reportProfiles if p.id == profile_id), None)
    if profile is None:
        raise ValueError("Report profile not configured on this client")
    task_config = getattr(profile, task)
    override = next((b for b in task_config.boards
                     if b.section == board_id), None)
    if override is None:
        section = next((s for s in profile.sections
                        if s.title == board_id), None)
        override = next((b for b in task_config.boards
                         if section and b.section == section.id), None)
    if override is None:
        raise ValueError("Board is not configured for this task")
    section = profile.section(override.section)
    if channel not in section.channels:
        raise ValueError("Board is not configured for this channel")
    return profile, BoardView(
        id=section.id, title=section.title, channels=tuple(section.channels),
        query=section.query, localRules=section.localRules,
        selector=override.selector, topN=override.topN, minEdges=override.minEdges,
        purpose=override.purpose, prompt=override.prompt, task=task,
        profile=profile.id), override.purpose or section.title


def next_run_at(schedule: ReportSchedule, *, now: datetime | None = None) -> float:
    """Timestamp of the next scheduled occurrence after ``now``."""
    zone = ZoneInfo(schedule.timezone)
    current = (now or datetime.now(zone)).astimezone(zone)
    from datetime import timedelta
    hour, minute = map(int, schedule.time.split(":"))
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if schedule.weekday:
        index = WEEKDAYS.index(schedule.weekday)
        ahead = (index - target.weekday()) % 7
        if ahead == 0 and target <= current:
            ahead = 7
        target += timedelta(days=ahead)
    elif target <= current:
        target += timedelta(days=1)
    return target.timestamp()
