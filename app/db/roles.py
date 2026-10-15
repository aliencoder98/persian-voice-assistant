"""Creates/updates the read-only database role used by the dashboard. Idempotent: runs on every start.

The role can SELECT only what the dashboard needs, never audio paths, Telegram file ids, contacts,
emails or memory, and its sessions are read-only. python -m app.db.roles
"""

import logging

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL

from app.config import get_settings

log = logging.getLogger("roles")

# Whole tables the dashboard may read.
READABLE_TABLES = ["agent_runs", "llm_calls", "tool_calls", "run_feedback", "audit_events", "budget_alerts"]
# Column-level grants: voice_messages without audio_path / telegram_file_id.
READABLE_COLUMNS = {
    "voice_messages": ["id", "user_id", "duration_s", "transcript", "stt_confidence", "created_at", "updated_at"]
}


def ensure_readonly_role(admin_url: URL, role: str, password: str) -> None:
    if not password:
        raise SystemExit("DASHBOARD_DB_PASSWORD is empty: refusing to create a role without a password.")
    if not role.replace("_", "").isalnum():
        raise SystemExit(f"Unsafe role name: {role!r}")
    db = admin_url.database
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).scalar()
        verb = "ALTER" if exists else "CREATE"
        # format(%I, %L) quotes identifiers and literals safely, whatever characters the password has
        stmt = conn.execute(
            text(f"SELECT format('{verb} ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L', CAST(:r AS text), CAST(:p AS text))"),
            {"r": role, "p": password},
        ).scalar()
        conn.execute(text(stmt))
        conn.execute(text(f'ALTER ROLE "{role}" SET default_transaction_read_only = on'))
        conn.execute(text(f'GRANT CONNECT ON DATABASE "{db}" TO "{role}"'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        conn.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM "{role}"'))
        for table in READABLE_TABLES:
            conn.execute(text(f'GRANT SELECT ON "{table}" TO "{role}"'))
        for table, cols in READABLE_COLUMNS.items():
            conn.execute(text(f'GRANT SELECT ({", ".join(cols)}) ON "{table}" TO "{role}"'))
    engine.dispose()
    log.info("Dashboard role ready", extra={"role": role})


def main() -> None:
    s = get_settings()
    ensure_readonly_role(s.database_url, s.dashboard_db_user, s.dashboard_db_password)
    print(f"Role {s.dashboard_db_user} is ready (read-only).")


if __name__ == "__main__":
    main()
