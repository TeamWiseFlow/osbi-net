"""Private editorial preferences, independent of shared channels and schemas."""
from typing import Literal
from pydantic import Field, model_validator
from osbi_contracts.models import StrictModel


class ReportBoard(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    title: str = Field(min_length=1, max_length=120)
    channel: str = Field(default="policy", pattern=r"^[a-z][a-z0-9-]{0,63}$")
    focus: str = Field(default="", max_length=4000)
    query: str = Field(default="", max_length=1000)
    topN: int = Field(default=3, ge=1, le=20)
    selector: Literal["evidence", "policy-actions"] = "evidence"
    minEdges: int = Field(default=4, ge=1, le=100)


class ReportProfile(StrictModel):
    id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=120)
    focus: str = Field(default="", max_length=4000)
    topnews: list[ReportBoard] = Field(default_factory=list, max_length=20)
    weeklyreport: list[ReportBoard] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def unique_boards(self):
        for boards in (self.topnews, self.weeklyreport):
            if len({b.id for b in boards}) != len(boards) or len({b.title for b in boards}) != len(boards):
                raise ValueError("Board IDs and titles must be unique within each task")
        return self


def default_report_profiles():
    return [ReportProfile(id="default", name="Default", topnews=[
        ReportBoard(id="policy", title="Policy", channel="policy"),
        ReportBoard(id="economy", title="Economy", channel="economy"),
    ], weeklyreport=[
        ReportBoard(id=key, title=title, selector="policy-actions", topN=2)
        for key, title in (("party-building", "基层党建"), ("community", "社区治理"), ("digital", "数字社区"))
    ])]


def resolve_board(config, profile_id, task, board_id, channel):
    profile = next((p for p in config.reportProfiles if p.id == profile_id), None)
    if profile is None:
        raise ValueError("Report profile not configured on this client")
    boards = getattr(profile, task)
    board = next((b for b in boards if b.id == board_id), None)
    if board is None:
        board = next((b for b in boards if b.title == board_id), None)
    if board is None or board.channel != channel:
        raise ValueError("Board is not configured for this task and channel")
    intent = "\n".join(value for value in (config.focus, profile.focus, board.title, board.focus) if value)
    return profile, board, intent
