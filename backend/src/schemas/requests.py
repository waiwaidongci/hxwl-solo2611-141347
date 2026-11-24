from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field
from src.constants.enums import Review

class Request(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')

class Credentials(Request):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)

class Upload(Request):
    name: str = Field(min_length=1, max_length=120)
    filename: str = Field(min_length=1, max_length=160)
    csv_text: str = Field(min_length=1, max_length=2_000_000)

class RunRequest(Request):
    dataset_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    params: dict = Field(default_factory=dict)

class ReviewRequest(Request):
    review: Review
    note: str = Field(min_length=1, max_length=500)
    decision: str = Field(default='', max_length=120)

class ScheduleRequest(RunRequest):
    recipient: str = Field(min_length=1, max_length=100)
    first_at: datetime
    repeat_daily: bool = False


class RevisionRequest(Request):
    dataset_id: int = Field(gt=0)
    ticket_id: str = Field(min_length=1, max_length=48)
    revision: int = Field(ge=1, le=32767)
    posted_at: str = Field(min_length=8, max_length=10)
    audit_outcome: str = Field(min_length=1, max_length=20)


class RecomputeRequest(RunRequest):
    pass


class DispatchRequest(Request):
    ticket_ids: list[str] = Field(min_length=1, max_length=500)
    recipient: str = Field(min_length=1, max_length=100)
