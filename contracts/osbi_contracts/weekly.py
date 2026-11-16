"""Transport-only contracts for explicit weekly selection and history operations."""
from datetime import date
from typing import Literal
from pydantic import Field, field_validator, model_validator
from .models import StrictModel

ActionBoard = Literal["基层党建", "社区治理", "数字社区"]


class WeeklyScope(StrictModel):
    profile: str = Field(default="default", pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class WeeklySelect(WeeklyScope):
    fromDate: str
    toDate: str
    board: ActionBoard = "社区治理"
    topN: int = Field(default=2, ge=1, le=20)
    minEdges: int = Field(default=4, ge=1, le=100)
    candidateUris: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("fromDate", "toDate")
    @classmethod
    def iso_date(cls, value):
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("Dates must use YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.fromDate >= self.toDate:
            raise ValueError("fromDate must precede toDate")
        return self


class WeeklyRecord(StrictModel):
    planId: str = Field(min_length=1, max_length=100)


class WeeklyBackfill(WeeklyScope):
    weekEnd: str
    sections: dict[str, list[str]]
    apply: bool = False

    @field_validator("weekEnd")
    @classmethod
    def iso_date(cls, value):
        return WeeklySelect.iso_date(value)

    @model_validator(mode="after")
    def bounded_sections(self):
        if len(self.sections) > 20 or any(len(titles) > 100 for titles in self.sections.values()):
            raise ValueError("Too many report sections")
        if any(len(title) > 2000 for titles in self.sections.values() for title in titles):
            raise ValueError("Report headings are too long")
        return self
