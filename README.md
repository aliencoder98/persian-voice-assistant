# Persian Voice Assistant

A personal Telegram assistant: send a voice message in Persian, confirm the proposed action, and it schedules an event or sends an email. See [docs/SPEC.md](docs/SPEC.md) for the full spec.

This repository is at **batch 01 (Foundation)**: Docker, PostgreSQL schema, Telegram bot skeleton, Resend client. The agent, speech-to-text and calendar come in later batches.

## Quick start

```bash
cp .env.example .env        # then fill in the values
docker compose up --build
```

This starts:

| Service   | What it does                                                        |
|-----------|---------------------------------------------------------------------|
| `db`      | PostgreSQL 16 with a named volume and a healthcheck                 |
| `migrate` | One-shot: runs `alembic upgrade head`, then exits                   |
| `bot`     | Telegram worker (long polling)                                      |
| `api`     | FastAPI on `127.0.0.1:8000` only (`/health`)                        |

## Useful commands

```bash
# Seed the owner and two sample contacts (idempotent)
docker compose run --rm bot python -m app.cli seed

# Send one test email to OWNER_EMAIL to prove the Resend key works
docker compose run --rm bot python -m app.cli send-test-email

# Open a psql shell
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'

# Create a new migration after changing app/db/models.py
docker compose run --rm bot alembic revision --autogenerate -m "describe change"
```

## Configuration

Every setting comes from `.env`; `.env.example` lists all variables. Secrets are never committed.

## Layout

```
app/bot      Telegram handlers
app/api      FastAPI app
app/db       SQLAlchemy models and session
app/tools    External clients (Resend, later Calendar)
app/stt  app/agent  app/mcp   (later batches)
migrations/  Alembic
docs/SPEC.md
```
