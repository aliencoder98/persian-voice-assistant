"""Confirmation and execution. Only confirm_run touches the outside world, and only once per run."""

import logging
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agent.schemas import EmailAction, ScheduleAction
from app.db.models import AgentRun, AuditEvent, CalendarEvent, Email, Intent, RunStatus, User
from app.tools import impl as _tool_implementations  # noqa: F401  (registers the tools)
from app.tools.registry import ToolContext, run_tool, tehran_now
from app.util.persian import jalali_long_fa, to_fa_digits

log = logging.getLogger("executor")

ALREADY_HANDLED = "این درخواست قبلاً بررسی شده است."
NOT_FOUND = "این درخواست پیدا نشد."


@dataclass
class ExecResult:
    ok: bool
    message: str


def _claim(session: Session, run_id: int, user_id: int, to: RunStatus) -> AgentRun | None:
    """Atomically move a run out of pending_confirmation. Exactly one caller can win."""
    won = session.execute(
        update(AgentRun)
        .where(
            AgentRun.id == run_id,
            AgentRun.user_id == user_id,
            AgentRun.status == RunStatus.pending_confirmation,
        )
        .values(status=to)
    ).rowcount
    session.commit()
    return session.get(AgentRun, run_id) if won == 1 else None


def _audit(session: Session, event: str, user: User, run: AgentRun, extra: dict | None = None) -> None:
    session.add(
        AuditEvent(
            event_type=event,
            actor=f"telegram:{user.telegram_user_id}",
            payload_json={"run_id": run.id, "intent": run.intent.value, **(extra or {})},
        )
    )
    session.commit()


def cancel_run(session: Session, user: User, run_id: int) -> ExecResult:
    run = _claim(session, run_id, user.id, RunStatus.rejected)
    if run is None:
        return ExecResult(False, ALREADY_HANDLED)
    _audit(session, "action_rejected", user, run)
    return ExecResult(True, "لغو شد. هیچ کاری انجام نشد. ❌")


def supersede_run(session: Session, user: User, run_id: int) -> None:
    """The owner edited a draft: the old one must not stay confirmable."""
    run = _claim(session, run_id, user.id, RunStatus.rejected)
    if run is not None:
        _audit(session, "action_superseded", user, run)


def confirm_run(session: Session, user: User, run_id: int, *, now=tehran_now) -> ExecResult:
    run = _claim(session, run_id, user.id, RunStatus.confirmed)
    if run is None:
        return ExecResult(False, ALREADY_HANDLED)
    _audit(session, "action_confirmed", user, run)

    ctx = ToolContext(session=session, user=user, run_id=run.id, now=now)
    try:
        if run.intent == Intent.schedule:
            result = _execute_schedule(session, ctx, run)
        elif run.intent == Intent.email:
            result = _execute_email(session, ctx, run)
        else:
            raise ValueError(f"run {run.id} has no executable intent")
    except Exception as exc:
        log.exception("Execution failed for run %s", run.id)
        run.status, run.error = RunStatus.failed, f"{type(exc).__name__}: {exc}"
        session.commit()
        _audit(session, "action_failed", user, run, {"error": run.error[:300]})
        return ExecResult(False, "اجرای درخواست با خطا مواجه شد و انجام نشد. لطفاً دوباره درخواست بدهید. ⚠️")

    run.status, run.error = RunStatus.executed, None
    session.commit()
    _audit(session, "action_executed", user, run)

    try:  # learning is best-effort and must never turn a success into a failure
        from app.agent.memory import learn_from_executed

        learn_from_executed(session, ctx, run)
    except Exception:
        log.exception("Memory update failed for run %s", run.id)
        session.rollback()
    return result


def _key(run: AgentRun, tool: str) -> str:
    return f"run:{run.id}:{tool}"


def _execute_schedule(session: Session, ctx: ToolContext, run: AgentRun) -> ExecResult:
    action = ScheduleAction.model_validate(run.action_json)  # re-validated: never trust stored JSON blindly
    r = run_tool(
        ctx,
        "create_calendar_event",
        {
            "date": action.date,
            "time": action.time,
            "duration_min": action.duration_min,
            "title": action.title,
            "description": action.description,
            "guests": [str(g.email) for g in action.guests],
        },
        idempotency_key=_key(run, "create_calendar_event"),
    )
    if not r.ok:
        raise RuntimeError(r.error)
    if session.scalar(select(CalendarEvent.id).where(CalendarEvent.run_id == run.id)) is None:
        session.add(
            CalendarEvent(
                run_id=run.id,
                provider_event_id=r.data["id"],
                date=action.date,
                time=action.time,
                duration_min=action.duration_min,
                title=action.title,
                description=action.description,
                guests=[str(g.email) for g in action.guests],
            )
        )
    session.commit()
    when = f"{jalali_long_fa(action.date)}، ساعت {to_fa_digits(action.time.strftime('%H:%M'))}"
    invited = "\nدعوت‌نامه برای مهمانان ارسال شد." if action.guests else ""
    return ExecResult(True, f"✅ رویداد «{action.title}» در تقویم ثبت شد.\n{when}{invited}")


def _execute_email(session: Session, ctx: ToolContext, run: AgentRun) -> ExecResult:
    action = EmailAction.model_validate(run.action_json)
    r = run_tool(
        ctx,
        "send_email",
        {"to": str(action.to), "subject": action.subject, "body": action.body},
        idempotency_key=_key(run, "send_email"),
    )
    if not r.ok:
        raise RuntimeError(r.error)
    if session.scalar(select(Email.id).where(Email.run_id == run.id)) is None:
        session.add(
            Email(
                run_id=run.id,
                to_address=str(action.to),
                subject=action.subject,
                body=action.body,
                provider_message_id=r.data["provider_message_id"],
                status="sent",
            )
        )
    session.commit()
    return ExecResult(True, f"✅ ایمیل برای {action.to_name or action.to} ارسال شد.")
