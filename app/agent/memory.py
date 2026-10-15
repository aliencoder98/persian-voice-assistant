"""Memory: what goes into each prompt, and what is learned after an action is executed.

Context is deliberately small: date/time (added by the agent), preferences, contacts that match
this message, and the last 5 messages. Never the whole history.
"""

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent.context import AgentContext
from app.agent.schemas import EmailAction, ScheduleAction
from app.db.models import AgentRun, Contact, Intent, MemoryFact, RunStatus, User
from app.tools.registry import ToolContext, run_tool
from app.util.persian import normalize

RECENT_MESSAGES = 5
MAX_PREFERENCES = 20
MAX_CONTACTS = 5
CLARIFICATION_WINDOW = dt.timedelta(minutes=30)
DEFAULT_DURATION_KEY = "default_meeting_duration_min"


def matching_contacts(session: Session, user: User, text: str) -> list[Contact]:
    words = set(normalize(text).split())
    found = []
    for c in session.scalars(select(Contact).where(Contact.user_id == user.id).order_by(Contact.id)):
        names = [normalize(n) for n in [c.name, *c.aliases]]
        if any(n and set(n.split()) <= words for n in names):
            found.append(c)
    return found[:MAX_CONTACTS]


def build_context(session: Session, user: User, text: str, draft: dict | None = None) -> AgentContext:
    prefs = session.scalars(
        select(MemoryFact)
        .where(MemoryFact.user_id == user.id)
        .order_by(MemoryFact.updated_at.desc())
        .limit(MAX_PREFERENCES)
    ).all()

    last = session.scalars(
        select(AgentRun)
        .where(AgentRun.user_id == user.id, AgentRun.input_text.is_not(None))
        .order_by(AgentRun.id.desc())
        .limit(RECENT_MESSAGES)
    ).all()

    pending_question = None
    if last and last[0].status == RunStatus.needs_clarification:
        age = dt.datetime.now(dt.timezone.utc) - last[0].created_at
        if age <= CLARIFICATION_WINDOW:
            pending_question = (last[0].action_json or {}).get("question")

    return AgentContext(
        owner_name=user.name,
        owner_email=user.email,
        preferences={f.key: f.value for f in prefs},
        contacts=[
            {"name": c.name, "aliases": c.aliases, "email": c.email}
            for c in matching_contacts(session, user, text)
        ],
        recent=[r.input_text[:200] for r in reversed(last)],
        draft=draft,
        pending_question=pending_question,
    )


def learn_from_executed(session: Session, ctx: ToolContext, run: AgentRun) -> None:
    """Save useful facts after an executed action. Writes go through the tools only (and are logged)."""
    action = run.action_json or {}
    if run.intent == Intent.schedule:
        parsed = ScheduleAction.model_validate(action)
        people = [(g.name, str(g.email)) for g in parsed.guests]
        _learn_meeting_length(session, ctx, run, parsed.duration_min)
    else:
        parsed = EmailAction.model_validate(action)
        people = [(parsed.to_name, str(parsed.to))]

    known = {
        (c.email or "").lower()
        for c in session.scalars(select(Contact).where(Contact.user_id == ctx.user.id))
    }
    for name, email in people:
        if name and email.lower() not in known and email.lower() != (ctx.user.email or "").lower():
            run_tool(
                ctx,
                "save_contact",
                {"name": name, "email": email},
                idempotency_key=f"run:{run.id}:save_contact:{email.lower()}",
            )


def _learn_meeting_length(session: Session, ctx: ToolContext, run: AgentRun, minutes: int) -> None:
    """Two executed meetings in a row with the same length make it the default."""
    previous = session.scalars(
        select(AgentRun)
        .where(
            AgentRun.user_id == ctx.user.id,
            AgentRun.intent == Intent.schedule,
            AgentRun.status == RunStatus.executed,
            AgentRun.id != run.id,
        )
        .order_by(AgentRun.id.desc())
        .limit(1)
    ).first()
    if previous is None or (previous.action_json or {}).get("duration_min") != minutes:
        return
    current = session.scalar(
        select(MemoryFact.value).where(MemoryFact.user_id == ctx.user.id, MemoryFact.key == DEFAULT_DURATION_KEY)
    )
    if current != str(minutes):
        run_tool(
            ctx,
            "remember",
            {"key": DEFAULT_DURATION_KEY, "value": str(minutes)},
            idempotency_key=f"run:{run.id}:remember:{DEFAULT_DURATION_KEY}",
        )
