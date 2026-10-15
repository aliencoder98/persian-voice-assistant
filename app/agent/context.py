"""What the agent is told about the owner for one message. Small on purpose: never whole history."""

import re
from dataclasses import dataclass, field
from typing import Any

_TAGS = re.compile(r"<(/?)(transcript|context)>", re.IGNORECASE)


def sanitize(text: str) -> str:
    """Stop untrusted text from closing or opening our data delimiters."""
    return _TAGS.sub(r"‹\1\2›", text)


@dataclass
class AgentContext:
    owner_name: str = ""
    owner_email: str | None = None
    preferences: dict[str, str] = field(default_factory=dict)
    contacts: list[dict[str, Any]] = field(default_factory=list)  # name, aliases, email
    recent: list[str] = field(default_factory=list)  # last messages, oldest first
    draft: dict[str, Any] | None = None  # action being edited by the owner
    pending_question: str | None = None  # clarification the assistant just asked


def render_user_message(text: str, now_iso: str, ctx: AgentContext) -> str:
    lines = ["<context>", f"Current time (Asia/Tehran): {now_iso}"]
    lines.append(f"Owner: {sanitize(ctx.owner_name) or 'unknown'}")
    if ctx.preferences:
        lines.append("Preferences:")
        lines += [f"- {sanitize(k)}: {sanitize(v)}" for k, v in ctx.preferences.items()]
    if ctx.contacts:
        lines.append("Matching contacts:")
        for c in ctx.contacts:
            aliases = ", ".join(c.get("aliases") or [])
            lines.append(f"- {sanitize(c['name'])} ({sanitize(aliases)}): {c.get('email') or 'no email'}")
    if ctx.recent:
        lines.append("Recent messages from the owner (oldest first):")
        lines += [f"- {sanitize(m)}" for m in ctx.recent]
    if ctx.pending_question:
        lines.append(
            f"You just asked the owner: «{sanitize(ctx.pending_question)}». "
            "The message below is their answer; combine it with their previous request."
        )
    if ctx.draft:
        lines.append(
            f"The owner is editing this draft action: {sanitize(str(ctx.draft))}. "
            "The message below is their correction; output the full corrected action."
        )
    lines += ["</context>", "<transcript>", sanitize(text), "</transcript>"]
    return "\n".join(lines)
