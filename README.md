# Persian Voice Assistant

A personal Telegram assistant: send a voice message in Persian, review the proposed action, tap **Confirm**, and it creates a Google Calendar event or sends an email. Nothing runs without your confirmation. See [docs/SPEC.md](docs/SPEC.md).

```
voice (OGG/Opus) → ffmpeg → Vosk (fa) → transcript ─┐
text message ───────────────────────────────────────┤
                                                    ▼
             DeepSeek agent (≤6 steps, read-only tools, validated JSON)
                                                    ▼
        Persian preview  [✅ تأیید] [✏️ ویرایش] [❌ لغو]
                                                    ▼  (Confirm only)
                    Google Calendar  /  Resend email
```

## Quick start

```bash
cp .env.example .env        # fill in the values (see below)
docker compose up --build
```

| Service   | What it does                                                        |
|-----------|---------------------------------------------------------------------|
| `db`      | PostgreSQL 16, named volume, healthcheck                            |
| `migrate` | One-shot: `alembic upgrade head`, then exits                        |
| `bot`     | Telegram worker (long polling): Vosk STT, agent, confirmation       |
| `api`     | FastAPI on `127.0.0.1:${API_PORT:-8000}` only (`/health`)           |

The Vosk Persian model (`vosk-model-small-fa-0.42`) is downloaded into the image at build time.

## Setup checklist

1. **Telegram**: create a bot with @BotFather → `TELEGRAM_BOT_TOKEN`. Put your numeric ID (from @userinfobot) in `ALLOWED_USER_IDS`.
2. **Resend**: `RESEND_API_KEY`, and `EMAIL_FROM` on a verified domain (or `onboarding@resend.dev` for tests). Check it: `docker compose run --rm bot python -m app.cli send-test-email`.
3. **DeepSeek**: `DEEPSEEK_API_KEY` (base URL and model are configurable).
4. **Google Calendar**: see below.
5. `docker compose run --rm bot python -m app.cli seed` creates the owner and two sample contacts (optional; the owner is also created on first message).

### Google Calendar (one-time OAuth)

1. In [Google Cloud Console](https://console.cloud.google.com/) create or pick a project and **enable the Google Calendar API**.
2. *APIs & Services → OAuth consent screen*: choose **External**, add yourself under **Test users**. (In *Testing* mode refresh tokens expire after 7 days; click *Publish app* to make them long-lived. A personal app that is only used by you does not need verification.)
3. *APIs & Services → Credentials → Create credentials → OAuth client ID → Application type **Desktop app***. Copy the client ID and secret into `.env` as `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
4. On your own computer (it opens a browser, so not inside Docker):
   ```bash
   pip install httpx
   python google_auth.py
   ```
   Sign in, allow the `calendar.events` scope, and copy the printed `GOOGLE_REFRESH_TOKEN=...` line into `.env`.
5. `GOOGLE_CALENDAR_ID` is `primary` by default (or the calendar's ID from its settings page).
6. `docker compose up -d --force-recreate bot` to pick up the new values.

Events are created in `Asia/Tehran` time with `sendUpdates=all`, so guests receive an invitation email from Google.

## How the agent stays safe

- The model only sees **read-only tools** (`get_current_datetime`, `convert_jalali_date`, `find_contact`, `list_calendar_events`) and three final tools (`propose_schedule`, `propose_email`, `ask_clarification`). `create_calendar_event` and `send_email` are not offered to it; only the Confirm button runs them.
- Every email address must come from `find_contact`, the matching-contacts context, the owner's own message, or the owner's address; anything else is rejected and sent back to the model.
- The transcript and all context are wrapped in `<transcript>`/`<context>` blocks, treated as data, and cannot close those blocks.
- One email action has exactly one recipient. Events in the past are refused.
- Each run gets one idempotency key: an atomic status claim, a unique key on `tool_calls`, a deterministic Google event ID and a Resend `Idempotency-Key`. A double tap cannot act twice.
- Every run (tokens, latency, model, prompt version) and every tool call is in `agent_runs` / `tool_calls`; confirmations are in `audit_events`.
- Memory per message: date/time, preferences, contacts mentioned in the message, the last 5 messages. After an executed action, new contacts and the usual meeting length are saved through the `save_contact` / `remember` tools.

## Admin API

`docker compose up` serves FastAPI on `127.0.0.1:${API_PORT:-8000}`. `GET /health` checks the database, Telegram and the LLM provider (`?shallow=true` checks only the database). Everything else needs the header `X-Admin-Key: $ADMIN_API_KEY` (the endpoints are disabled while the variable is empty):

| Endpoint | Purpose |
|---|---|
| `GET /runs?status=&intent=&user_id=&model=&since=&until=&limit=&offset=` | paginated runs |
| `GET /runs/{id}` | transcript, action JSON, tool calls |
| `POST /runs/{id}/retry` | failed during execution: runs it again (it was confirmed; provider idempotency keys prevent duplicates; you get a Telegram message). Failed before an action existed: re-runs the agent and sends the new preview to Telegram, which still needs your Confirm |
| `GET /events`, `GET /emails` | audit events, sent emails |
| `GET /contacts`, `POST /contacts` | list and add contacts |

```bash
curl -H "X-Admin-Key: $ADMIN_API_KEY" "http://127.0.0.1:8000/runs?status=failed&limit=5"
```

Responses never include audio paths, Telegram file ids or configured secrets (errors are scrubbed). OpenAPI docs (`/docs`) exist only when `DEBUG=true`.

## MCP server

The same tools the agent uses, as an [MCP](https://modelcontextprotocol.io) server: `find_contact`, `list_calendar_events`, `create_calendar_event`, `send_email`, plus the resource `calendar://upcoming` (next 7 days). `create_calendar_event` and `send_email` do nothing unless `confirm=true` (otherwise they return a preview), and every write attempt is recorded in `audit_events` with actor `mcp`. It acts as the owner (the first `ALLOWED_USER_IDS` entry).

- **HTTP** (started by `docker compose up`, bound to localhost): `http://127.0.0.1:8765/mcp`
  - Claude Code: `claude mcp add --transport http voice-assistant http://127.0.0.1:8765/mcp`
  - Cursor / VS Code / other IDEs: add a server of type `http` with that URL.
- **stdio** (Claude Desktop): edit `claude_desktop_config.json` (Settings → Developer → Edit Config) and restart Claude Desktop:

```json
{
  "mcpServers": {
    "voice-assistant": {
      "command": "docker",
      "args": ["compose", "-f", "C:/Users/you/Desktop/Personal Assistant/docker-compose.yml",
               "run", "--rm", "-i", "-T", "--no-deps", "mcp",
               "python", "-m", "app.mcp.server", "--transport", "stdio"]
    }
  }
}
```

The database container must be running (`docker compose up -d db`). The HTTP endpoint has no authentication of its own; it is only reachable from your machine, so do not publish that port.

## Tests

```bash
docker compose run --rm bot python -m pytest             # offline: scripted model, test database
docker compose run --rm -e LIVE_LLM=1 bot python -m pytest tests/test_agent_live.py -s   # real DeepSeek
```

The offline suite replays ten Persian sample transcripts (`tests/samples.py`: both intents, a missing time, an unknown contact, a hallucinated address, an injection attempt, out of scope) plus provider, tool, confirmation and memory tests. It uses a throwaway `<POSTGRES_DB>_test` database next to the real one.

## Other commands

```bash
docker compose run --rm bot python -m app.cli seed
docker compose run --rm bot python -m app.cli send-test-email
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" "$POSTGRES_DB"'
docker compose run --rm bot alembic revision --autogenerate -m "describe change"
```

## Layout

```
app/bot      Telegram handlers, previews, pipeline
app/stt      Vosk + ffmpeg
app/agent    prompt, schemas, provider, loop, executor, memory
app/tools    tool registry and implementations, Google Calendar, Resend
app/api      FastAPI app          app/db   models, session, seed
app/util     Persian text and Jalali calendar
migrations/  Alembic              tests/   pytest
google_auth.py  one-time OAuth helper (run on your computer)
```
