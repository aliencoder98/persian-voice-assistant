"""MCP server: python -m app.mcp.server [--transport stdio|streamable-http]"""

import argparse
import json
import sys

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from app.config import get_settings
from app.logging_config import setup_logging
from app.mcp.service import McpService

INSTRUCTIONS = (
    "Tools for the owner's personal assistant (Persian speaker, timezone Asia/Tehran). "
    "create_calendar_event and send_email change the real world: they do nothing unless confirm=true, "
    "so first show the user what you will do and call again with confirm=true only after they agree. "
    "Never invent email addresses: use find_contact."
)


def build_server(service: McpService | None = None, host: str = "127.0.0.1", port: int = 8765) -> FastMCP:
    svc = service or McpService()
    mcp = FastMCP(
        "voice-assistant",
        instructions=INSTRUCTIONS,
        host=host,
        port=port,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"],
            allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
        ),
    )

    @mcp.tool()
    def find_contact(name: str) -> dict:
        """Find saved contacts by name or alias (Persian or English). Returns name and email; an empty list means unknown."""
        return svc.find_contact(name)

    @mcp.tool()
    def list_calendar_events(date: str) -> dict:
        """List calendar events on one date (YYYY-MM-DD, Asia/Tehran)."""
        return svc.list_calendar_events(date)

    @mcp.tool()
    def create_calendar_event(
        date: str,
        time: str,
        duration_min: int,
        title: str,
        description: str = "",
        guests: list[str] | None = None,
        confirm: bool = False,
    ) -> dict:
        """Create a Google Calendar event (date YYYY-MM-DD, time HH:MM Asia/Tehran) and invite guests by email.
        Requires confirm=true; without it nothing happens and a preview is returned."""
        return svc.create_calendar_event(date, time, duration_min, title, description, guests, confirm)

    @mcp.tool()
    def send_email(to: str, subject: str, body: str, confirm: bool = False) -> dict:
        """Send one email to one recipient. Requires confirm=true; without it nothing is sent."""
        return svc.send_email(to, subject, body, confirm)

    @mcp.resource("calendar://upcoming", mime_type="application/json")
    def upcoming_events() -> str:
        """The owner's calendar events for the next 7 days."""
        return json.dumps(svc.upcoming_events(7), ensure_ascii=False)

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.mcp.server")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    settings = get_settings()
    # stdout carries the protocol on stdio, so logs always go to stderr here.
    setup_logging(settings.log_level, stream=sys.stderr)
    build_server(host=args.host, port=settings.mcp_port).run(transport=args.transport)


if __name__ == "__main__":
    main()
