"""Response models. Deliberately omit audio paths, Telegram file ids and anything secret."""

import datetime as dt
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, EmailStr, Field

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RunSummary(ORM):
    id: int
    user_id: int
    created_at: dt.datetime
    intent: str
    status: str
    model: str | None
    tokens: int | None
    latency_ms: int | None
    input_text: str | None
    error: str | None


class ToolCallOut(ORM):
    id: int
    created_at: dt.datetime
    tool_name: str
    status: str
    arguments_json: dict[str, Any] | None
    result_json: dict[str, Any] | None


class RunDetail(RunSummary):
    transcript: str | None = None
    stt_confidence: float | None = None
    prompt_version: str | None = None
    action_json: dict[str, Any] | None = None
    tool_calls: list[ToolCallOut] = []


class EventOut(ORM):
    id: int
    created_at: dt.datetime
    event_type: str
    actor: str
    payload_json: dict[str, Any] | None


class EmailOut(ORM):
    id: int
    created_at: dt.datetime
    run_id: int
    to_address: str
    subject: str
    body: str
    provider_message_id: str | None
    status: str


class ContactOut(ORM):
    id: int
    user_id: int
    name: str
    aliases: list[str]
    email: str | None


class ContactIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=10)
    email: EmailStr | None = None


class RetryOut(BaseModel):
    run_id: int
    status: str
    message: str


class ComponentHealth(BaseModel):
    ok: bool
    detail: str | None = None


class HealthOut(BaseModel):
    status: str
    database: ComponentHealth
    telegram: ComponentHealth | None = None
    llm: ComponentHealth | None = None
