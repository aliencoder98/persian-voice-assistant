import datetime as dt
import logging
import time
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agent.executor import confirm_run
from app.api.deps import AdminOnly, get_session, scrub
from app.api.schemas import (
    ComponentHealth,
    ContactIn,
    ContactOut,
    EmailOut,
    EventOut,
    HealthOut,
    Page,
    RetryOut,
    RunDetail,
    RunSummary,
    ToolCallOut,
)
from app.config import get_settings
from app.db.models import AgentRun, AuditEvent, Contact, Email, Intent, RunStatus, ToolCall, User, VoiceMessage
from app.db.session import engine
from app.logging_config import setup_logging
from app.notify import telegram_send

log = logging.getLogger("api")
settings = get_settings()
setup_logging(settings.log_level)

app = FastAPI(
    title="Persian Voice Assistant API",
    docs_url="/docs" if settings.debug else None,
    redoc_url="/redoc" if settings.debug else None,
    openapi_url="/openapi.json" if settings.debug else None,
)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception) -> JSONResponse:
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error."}, status_code=500)


# ---------------------------------------------------------------- health

_deep_cache: dict[str, tuple[float, HealthOut]] = {}


def _probe(url: str, headers: dict[str, str] | None = None) -> ComponentHealth:
    """Reachable means the host answered at all (even 401); only network failures and 5xx count as down."""
    s = get_settings()
    try:
        proxy = s.telegram_proxy_url or None if "telegram" in url else None
        r = httpx.get(url, headers=headers, timeout=5, proxy=proxy)
    except httpx.HTTPError as exc:
        return ComponentHealth(ok=False, detail=type(exc).__name__)
    return ComponentHealth(ok=r.status_code < 500, detail=None if r.status_code < 500 else f"HTTP {r.status_code}")


def _check_db() -> ComponentHealth:
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        return ComponentHealth(ok=True)
    except Exception as exc:
        log.warning("Health: database unreachable (%s)", type(exc).__name__)
        return ComponentHealth(ok=False, detail="unreachable")


@app.get("/health", response_model=HealthOut)
def health(response: Response, shallow: bool = False) -> HealthOut:
    """Database, Telegram and the LLM provider. `?shallow=true` checks only the database (used by Docker)."""
    db = _check_db()
    if shallow:
        out = HealthOut(status="ok" if db.ok else "degraded", database=db)
    else:
        cached = _deep_cache.get("deep")
        if cached and time.monotonic() - cached[0] < 30 and cached[1].database.ok == db.ok:
            out = cached[1]
        else:
            s = get_settings()
            telegram = (
                _probe(f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe")
                if s.telegram_bot_token
                else ComponentHealth(ok=False, detail="TELEGRAM_BOT_TOKEN not set")
            )
            llm = (
                _probe(f"{s.deepseek_base_url.rstrip('/')}/models", {"Authorization": f"Bearer {s.deepseek_api_key}"})
                if s.deepseek_api_key
                else ComponentHealth(ok=False, detail="DEEPSEEK_API_KEY not set")
            )
            ok = db.ok and telegram.ok and llm.ok
            out = HealthOut(status="ok" if ok else "degraded", database=db, telegram=telegram, llm=llm)
            _deep_cache["deep"] = (time.monotonic(), out)
    if out.status != "ok":
        response.status_code = 503
    return out


# ---------------------------------------------------------------- admin

admin = APIRouter(dependencies=[AdminOnly])
SessionDep = Annotated[Session, Depends(get_session)]
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


def _page(session: Session, stmt, count_stmt, model, limit: int, offset: int, order) -> Page:
    total = session.scalar(count_stmt) or 0
    rows = session.scalars(stmt.order_by(order).limit(limit).offset(offset)).all()
    return Page(items=[model.model_validate(r) for r in rows], total=total, limit=limit, offset=offset)


def _clean_run(r: RunSummary) -> RunSummary:
    r.error = scrub(r.error)
    return r


@admin.get("/runs", response_model=Page[RunSummary])
def list_runs(
    session: SessionDep,
    status: RunStatus | None = None,
    intent: Intent | None = None,
    user_id: int | None = None,
    model: str | None = None,
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> Page[RunSummary]:
    conds = []
    if status:
        conds.append(AgentRun.status == status)
    if intent:
        conds.append(AgentRun.intent == intent)
    if user_id:
        conds.append(AgentRun.user_id == user_id)
    if model:
        conds.append(AgentRun.model == model)
    if since:
        conds.append(AgentRun.created_at >= since)
    if until:
        conds.append(AgentRun.created_at < until)
    page = _page(
        session,
        select(AgentRun).where(*conds),
        select(func.count()).select_from(AgentRun).where(*conds),
        RunSummary,
        limit,
        offset,
        AgentRun.id.desc(),
    )
    page.items = [_clean_run(i) for i in page.items]
    return page


@admin.get("/runs/{run_id}", response_model=RunDetail)
def get_run(run_id: int, session: SessionDep) -> RunDetail:
    run = session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found.")
    detail = RunDetail.model_validate(run)
    if run.voice_message_id:
        voice = session.get(VoiceMessage, run.voice_message_id)
        if voice:
            detail.transcript, detail.stt_confidence = voice.transcript, voice.stt_confidence
    calls = session.scalars(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.id)).all()
    detail.tool_calls = [ToolCallOut.model_validate(c) for c in calls]
    detail.error = scrub(detail.error)
    return detail


@admin.post("/runs/{run_id}/retry", response_model=RetryOut)
def retry_run(run_id: int, session: SessionDep) -> RetryOut:
    """Retry a failed run.

    - Failed during execution (an action exists): re-run it. You confirmed it before and the
      provider idempotency keys prevent duplicates. The owner is told on Telegram.
    - Failed before an action existed (LLM error): run the agent again on the same text; the new
      preview goes to the owner on Telegram and still needs a tap on Confirm.
    """
    from app.bot.pipeline import analyse_for_user
    from app.bot.preview import confirmation_keyboard, format_preview

    run = session.get(AgentRun, run_id)
    if run is None:
        raise HTTPException(404, "Run not found.")
    if run.status != RunStatus.failed:
        raise HTTPException(409, f"Only failed runs can be retried (this one is {run.status.value}).")
    user = session.get(User, run.user_id)

    if (run.action_json or {}).get("intent") in ("schedule", "email"):
        result = confirm_run(session, user, run.id, retry=True)
        telegram_send(user.telegram_user_id, result.message)
        session.refresh(run)
        return RetryOut(run_id=run.id, status=run.status.value, message=result.message)

    if not run.input_text:
        raise HTTPException(422, "This run has no input text to retry.")
    reply = analyse_for_user(session, user, run.input_text, voice_message_id=run.voice_message_id)
    if reply.kind == "failed" or reply.run_id is None:
        raise HTTPException(502, "The agent failed again; see the logs.")
    new = session.get(AgentRun, reply.run_id)
    if reply.kind == "clarify":
        telegram_send(user.telegram_user_id, reply.text)
    else:
        telegram_send(
            user.telegram_user_id,
            format_preview(reply.action),
            confirmation_keyboard(reply.run_id).to_dict(),
        )
    return RetryOut(run_id=new.id, status=new.status.value, message="New run created; the owner was notified on Telegram.")


@admin.get("/events", response_model=Page[EventOut])
def list_events(
    session: SessionDep,
    event_type: str | None = None,
    actor: str | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> Page[EventOut]:
    conds = []
    if event_type:
        conds.append(AuditEvent.event_type == event_type)
    if actor:
        conds.append(AuditEvent.actor == actor)
    return _page(
        session,
        select(AuditEvent).where(*conds),
        select(func.count()).select_from(AuditEvent).where(*conds),
        EventOut,
        limit,
        offset,
        AuditEvent.id.desc(),
    )


@admin.get("/emails", response_model=Page[EmailOut])
def list_emails(
    session: SessionDep, status: str | None = None, limit: Limit = 20, offset: Offset = 0
) -> Page[EmailOut]:
    conds = [Email.status == status] if status else []
    return _page(
        session,
        select(Email).where(*conds),
        select(func.count()).select_from(Email).where(*conds),
        EmailOut,
        limit,
        offset,
        Email.id.desc(),
    )


@admin.get("/contacts", response_model=Page[ContactOut])
def list_contacts(
    session: SessionDep, q: str | None = None, limit: Limit = 50, offset: Offset = 0
) -> Page[ContactOut]:
    conds = [Contact.name.ilike(f"%{q}%")] if q else []
    return _page(
        session,
        select(Contact).where(*conds),
        select(func.count()).select_from(Contact).where(*conds),
        ContactOut,
        limit,
        offset,
        Contact.id,
    )


@admin.post("/contacts", response_model=ContactOut, status_code=201)
def create_contact(body: ContactIn, session: SessionDep) -> Contact:
    ids = sorted(get_settings().allowed_ids)
    owner = session.scalar(select(User).where(User.telegram_user_id == ids[0])) if ids else None
    if owner is None:
        raise HTTPException(409, "The owner has no user row yet: send /start to the bot or run the seed.")
    if body.email and session.scalar(
        select(Contact.id).where(Contact.user_id == owner.id, func.lower(Contact.email) == str(body.email).lower())
    ):
        raise HTTPException(409, "A contact with this email already exists.")
    contact = Contact(user_id=owner.id, name=body.name, aliases=body.aliases, email=str(body.email) if body.email else None)
    session.add(contact)
    session.commit()
    return contact


app.include_router(admin)
