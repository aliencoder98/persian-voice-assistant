"""MCP tool logic. Thin wrappers over the agent's own tool functions: one implementation, two interfaces."""

import contextlib
from typing import Any, Callable, ContextManager

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import AuditEvent, User
from app.db.session import SessionLocal
from app.tools import impl
from app.tools.registry import ToolContext, run_tool, tehran_now

SessionFactory = Callable[[], ContextManager[Session]]


class McpToolError(ValueError):
    """Raised for failed tool calls; the MCP SDK turns it into an error result for the client."""


class McpService:
    def __init__(self, session_factory: SessionFactory = SessionLocal, now=tehran_now) -> None:
        self._session_factory = session_factory
        self._now = now

    # ---- helpers

    @contextlib.contextmanager
    def _context(self):
        with self._session_factory() as session:
            settings = get_settings()
            tg_id = settings.mcp_owner_telegram_id or next(iter(sorted(settings.allowed_ids)), 0)
            owner = session.scalar(select(User).where(User.telegram_user_id == tg_id)) if tg_id else None
            if owner is None:
                raise McpToolError("The owner has no user record yet: send /start to the bot or run the seed.")
            yield ToolContext(session=session, user=owner, now=self._now)

    @staticmethod
    def _audit(ctx: ToolContext, event: str, payload: dict[str, Any]) -> None:
        ctx.session.add(AuditEvent(event_type=event, actor="mcp", payload_json=payload))
        ctx.session.commit()

    @staticmethod
    def _call(ctx: ToolContext, tool: str, args: dict[str, Any]) -> Any:
        result = run_tool(ctx, tool, args)
        if not result.ok:
            raise McpToolError(result.error)
        return result.data

    # ---- read tools

    def find_contact(self, name: str) -> dict:
        with self._context() as ctx:
            return self._call(ctx, "find_contact", {"name": name})

    def list_calendar_events(self, date: str) -> dict:
        with self._context() as ctx:
            return self._call(ctx, "list_calendar_events", {"date": date})

    def upcoming_events(self, days: int = 7) -> dict:
        with self._context() as ctx:
            today = ctx.now().date()
            return {
                "from": today.isoformat(),
                "days": days,
                "events": impl.calendar().list_range(today, days),
            }

    # ---- write tools: refuse unless confirm=true, and always leave an audit trail

    def _write(self, tool: str, args: dict[str, Any], confirm: bool, summary: dict[str, Any]) -> dict:
        with self._context() as ctx:
            if not confirm:
                self._audit(ctx, "mcp_confirmation_required", {"tool": tool, **summary})
                return {
                    "status": "confirmation_required",
                    "message": f"Nothing was done. Show this to the user and call {tool} again with confirm=true "
                    "only after they approve.",
                    "preview": args,
                }
            try:
                data = self._call(ctx, tool, args)
            except McpToolError as exc:
                self._audit(ctx, "mcp_write_failed", {"tool": tool, **summary, "error": str(exc)[:300]})
                raise
            self._audit(ctx, "mcp_write_executed", {"tool": tool, **summary, "result": data})
            return {"status": "done", **data}

    def create_calendar_event(
        self,
        date: str,
        time: str,
        duration_min: int,
        title: str,
        description: str = "",
        guests: list[str] | None = None,
        confirm: bool = False,
    ) -> dict:
        args = {
            "date": date,
            "time": time,
            "duration_min": duration_min,
            "title": title,
            "description": description,
            "guests": guests or [],
        }
        return self._write(
            "create_calendar_event", args, confirm, {"date": date, "time": time, "title": title, "guests": guests or []}
        )

    def send_email(self, to: str, subject: str, body: str, confirm: bool = False) -> dict:
        args = {"to": to, "subject": subject, "body": body}
        # The body is not copied into the audit log, only its size.
        return self._write("send_email", args, confirm, {"to": to, "subject": subject, "body_chars": len(body)})

