"""Blocking glue between Telegram updates and the agent. Called via asyncio.to_thread."""

import threading
from dataclasses import dataclass

from sqlalchemy import select

from app import budget
from app.agent.agent import process_message
from app.agent.executor import supersede_run
from app.agent.memory import build_context
from app.agent.llm import DeepSeekProvider, LLMError, LLMProvider
from app.agent.schemas import Action, EmailAction, ScheduleAction
from app.config import get_settings
from app.db.models import AgentRun, RunStatus, User
from app.db.session import SessionLocal

_provider: LLMProvider | None = None
_provider_lock = threading.Lock()


def get_provider() -> LLMProvider:
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = DeepSeekProvider()
        return _provider


def get_or_create_user(session, tg_user) -> User:
    user = session.scalar(select(User).where(User.telegram_user_id == tg_user.id))
    if user is None:
        user = User(
            telegram_user_id=tg_user.id,
            name=tg_user.full_name,
            timezone=get_settings().default_timezone,
        )
        session.add(user)
        session.flush()
    return user


@dataclass
class Reply:
    kind: str  # schedule | email | clarify | failed | blocked
    run_id: int | None = None
    text: str | None = None  # clarification question
    action: Action | None = None


def analyse(
    tg_user,
    text: str,
    *,
    voice_message_id: int | None = None,
    editing_run_id: int | None = None,
) -> Reply:
    with SessionLocal() as session:
        user = get_or_create_user(session, tg_user)
        return analyse_for_user(
            session, user, text, voice_message_id=voice_message_id, editing_run_id=editing_run_id
        )


def analyse_for_user(
    session,
    user: User,
    text: str,
    *,
    voice_message_id: int | None = None,
    editing_run_id: int | None = None,
) -> Reply:
    if budget.is_blocked(session):
        return Reply("blocked")
    draft = None
    if editing_run_id is not None:
        old = session.get(AgentRun, editing_run_id)
        if old and old.user_id == user.id and old.status == RunStatus.pending_confirmation:
            draft = old.action_json

    try:
        provider = get_provider()
    except LLMError:
        return Reply("failed")
    agent_ctx = build_context(session, user, text, draft)
    run, outcome = process_message(
        session, provider, user, text, agent_ctx, voice_message_id=voice_message_id
    )

    if outcome.kind in ("schedule", "email"):
        if draft is not None:
            supersede_run(session, user, editing_run_id)
        action = ScheduleAction if outcome.kind == "schedule" else EmailAction
        return Reply(outcome.kind, run.id, action=action.model_validate(run.action_json))
    if outcome.kind == "clarify":
        return Reply("clarify", run.id, text=outcome.question)
    return Reply("failed", run.id)
