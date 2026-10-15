import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.api import main as api
from app.api.deps import get_session
from app.config import get_settings
from app.db.models import (
    AgentRun,
    AuditEvent,
    CalendarEvent,
    Contact,
    Email,
    Intent,
    RunStatus,
    ToolCall,
    VoiceMessage,
)
from app.tools import impl
from tests.fakes import ScriptedLLM, call, turn

KEY = {"X-Admin-Key": "test-admin-key"}


@pytest.fixture
def client(session, user, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "admin_api_key", "test-admin-key")
    monkeypatch.setattr(s, "allowed_user_ids", "111")
    api.app.dependency_overrides[get_session] = lambda: session
    yield TestClient(api.app, raise_server_exceptions=False)
    api.app.dependency_overrides.clear()


def mk_run(session, user, **kw):
    kw.setdefault("input_text", "فردا جلسه")
    run = AgentRun(user_id=user.id, **kw)
    session.add(run)
    session.flush()
    return run


# ---------------- auth


def test_admin_endpoints_require_the_key(client):
    for path in ["/runs", "/runs/1", "/events", "/emails", "/contacts"]:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-Admin-Key": "wrong"}).status_code == 401
    assert client.post("/contacts", json={"name": "x"}).status_code == 401
    assert client.post("/runs/1/retry").status_code == 401
    assert client.get("/runs", headers=KEY).status_code == 200


def test_admin_api_is_disabled_without_a_configured_key(client, monkeypatch):
    monkeypatch.setattr(get_settings(), "admin_api_key", "")
    assert client.get("/runs", headers={"X-Admin-Key": ""}).status_code == 503
    assert client.get("/runs", headers=KEY).status_code == 503


def test_openapi_docs_are_off_unless_debug(client):
    for path in ["/docs", "/redoc", "/openapi.json"]:
        assert client.get(path).status_code == 404


# ---------------- runs


def test_runs_filters_and_pagination(client, session, user):
    mk_run(session, user, intent=Intent.schedule, status=RunStatus.executed, model="m1", tokens=100)
    mk_run(session, user, intent=Intent.email, status=RunStatus.failed, model="m1", error="boom")
    mk_run(session, user, intent=Intent.email, status=RunStatus.executed, model="m2")
    for i in range(4):
        mk_run(session, user, status=RunStatus.rejected)

    def get(**params):
        r = client.get("/runs", params=params, headers=KEY)
        assert r.status_code == 200, r.text
        return r.json()

    assert get()["total"] == 7
    assert get(status="failed")["total"] == 1
    assert get(intent="email")["total"] == 2
    assert get(model="m2")["total"] == 1
    assert get(status="executed", intent="email")["items"][0]["model"] == "m2"
    assert get(since=(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat())["total"] == 0

    page1 = get(limit=3)
    page2 = get(limit=3, offset=3)
    assert len(page1["items"]) == 3 and page1["total"] == 7
    assert not {i["id"] for i in page1["items"]} & {i["id"] for i in page2["items"]}
    ids = [i["id"] for i in get()["items"]]
    assert ids == sorted(ids, reverse=True)


def test_bad_query_parameters_are_422(client):
    for params in [{"limit": 0}, {"limit": 101}, {"offset": -1}, {"status": "nope"}, {"intent": "x"}]:
        assert client.get("/runs", params=params, headers=KEY).status_code == 422


def test_run_detail_has_transcript_action_and_tool_calls(client, session, user):
    voice = VoiceMessage(
        user_id=user.id, telegram_file_id="SECRET_FILE_ID", transcript="فردا ساعت ده", stt_confidence=0.9,
        audio_path="/data/audio/111/x.ogg",
    )  # fmt: skip
    session.add(voice)
    session.flush()
    run = mk_run(session, user, voice_message_id=voice.id, intent=Intent.schedule,
                 action_json={"intent": "schedule", "title": "جلسه"}, prompt_version="v1")  # fmt: skip
    session.add(ToolCall(run_id=run.id, tool_name="find_contact", arguments_json={"name": "علی"},
                         result_json={"matches": []}, status="ok"))  # fmt: skip
    session.flush()

    r = client.get(f"/runs/{run.id}", headers=KEY)
    body = r.json()
    assert r.status_code == 200
    assert body["transcript"] == "فردا ساعت ده" and body["stt_confidence"] == 0.9
    assert body["action_json"]["title"] == "جلسه"
    assert [c["tool_name"] for c in body["tool_calls"]] == ["find_contact"]
    raw = r.text
    assert "SECRET_FILE_ID" not in raw and "audio_path" not in raw and ".ogg" not in raw


def test_secrets_are_scrubbed_from_errors(client, session, user, monkeypatch):
    monkeypatch.setattr(get_settings(), "deepseek_api_key", "sk-very-secret-key")
    run = mk_run(session, user, status=RunStatus.failed, error="401 for key sk-very-secret-key at host")
    for url in (f"/runs/{run.id}", "/runs?status=failed"):
        assert "sk-very-secret-key" not in client.get(url, headers=KEY).text


def test_unknown_run_is_404(client):
    assert client.get("/runs/999999", headers=KEY).status_code == 404
    assert client.post("/runs/999999/retry", headers=KEY).status_code == 404


def test_unhandled_errors_are_generic(client, monkeypatch):
    monkeypatch.setattr(api, "_page", lambda *a, **k: 1 / 0)
    r = client.get("/events", headers=KEY)
    assert r.status_code == 500 and r.json() == {"detail": "Internal server error."}
    assert "division" not in r.text


# ---------------- retry


@pytest.fixture
def telegram(monkeypatch):
    sent = []
    monkeypatch.setattr(api, "telegram_send", lambda chat, text, markup=None: sent.append((chat, text, markup)) or True)
    return sent


def test_retry_only_applies_to_failed_runs(client, session, user, telegram):
    run = mk_run(session, user, status=RunStatus.executed)
    assert client.post(f"/runs/{run.id}/retry", headers=KEY).status_code == 409


def test_retry_failed_execution_runs_it_once(client, session, user, telegram, monkeypatch):
    calls = []

    class Cal:
        def create_event(self, **kw):
            calls.append(kw)
            return {"id": kw["event_id"], "html_link": None}

    monkeypatch.setattr(impl, "calendar", lambda: Cal())
    run = mk_run(
        session, user, intent=Intent.schedule, status=RunStatus.failed, error="calendar down",
        action_json={"intent": "schedule", "date": "2099-01-01", "time": "10:00:00", "duration_min": 30,
                     "title": "جلسه", "description": "", "guests": []},
    )  # fmt: skip
    # the failed first attempt left an errored idempotency key behind
    session.add(ToolCall(run_id=run.id, tool_name="create_calendar_event", status="error",
                         idempotency_key=f"run:{run.id}:create_calendar_event"))  # fmt: skip
    session.flush()

    r = client.post(f"/runs/{run.id}/retry", headers=KEY)
    assert r.status_code == 200 and r.json()["status"] == "executed"
    assert len(calls) == 1 and telegram and "ثبت شد" in telegram[0][1]
    assert client.post(f"/runs/{run.id}/retry", headers=KEY).status_code == 409  # now executed
    assert len(calls) == 1
    assert session.query(CalendarEvent).filter_by(run_id=run.id).count() == 1


def test_retry_after_agent_failure_asks_for_a_new_confirmation(client, session, user, telegram, monkeypatch):
    llm = ScriptedLLM(turn(call("ask_clarification", question="چه ساعتی؟")))
    monkeypatch.setattr("app.bot.pipeline.get_provider", lambda: llm)
    run = mk_run(session, user, status=RunStatus.failed, error="LLM unavailable")
    r = client.post(f"/runs/{run.id}/retry", headers=KEY)
    assert r.status_code == 200 and r.json()["run_id"] != run.id
    assert telegram[0][0] == 111 and telegram[0][1] == "چه ساعتی؟"


def test_retry_with_a_proposal_sends_buttons_and_does_not_execute(client, session, user, telegram, monkeypatch):
    llm = ScriptedLLM(turn(call("propose_email", to="owner@example.com", subject="s", body="b")))
    monkeypatch.setattr("app.bot.pipeline.get_provider", lambda: llm)
    monkeypatch.setattr(impl, "resend_send", lambda *a, **k: pytest.fail("must wait for confirmation"))
    run = mk_run(session, user, status=RunStatus.failed, error="LLM unavailable")
    r = client.post(f"/runs/{run.id}/retry", headers=KEY)
    assert r.json()["status"] == "pending_confirmation"
    assert telegram[0][2]["inline_keyboard"][0][0]["callback_data"].startswith("ok:")


# ---------------- events, emails, contacts


def test_events_and_emails_filters(client, session, user):
    run = mk_run(session, user)
    session.add_all([
        AuditEvent(event_type="action_confirmed", actor="telegram:111", payload_json={"run_id": run.id}),
        AuditEvent(event_type="action_failed", actor="telegram:111"),
        Email(run_id=run.id, to_address="a@example.com", subject="s", body="b", status="sent"),
        Email(run_id=run.id, to_address="b@example.com", subject="s", body="b", status="failed"),
    ])  # fmt: skip
    session.flush()
    assert client.get("/events", params={"event_type": "action_failed"}, headers=KEY).json()["total"] == 1
    assert client.get("/events", params={"actor": "telegram:111"}, headers=KEY).json()["total"] == 2
    emails = client.get("/emails", params={"status": "sent"}, headers=KEY).json()
    assert emails["total"] == 1 and emails["items"][0]["to_address"] == "a@example.com"


def test_contacts_list_create_and_validation(client, session, user):
    names = [c["name"] for c in client.get("/contacts", headers=KEY).json()["items"]]
    assert "علی رضایی" in names
    assert [c["name"] for c in client.get("/contacts", params={"q": "سارا"}, headers=KEY).json()["items"]] == ["سارا محمدی"]

    r = client.post("/contacts", json={"name": "رضا", "aliases": ["Reza"], "email": "reza@example.com"}, headers=KEY)
    assert r.status_code == 201 and r.json()["aliases"] == ["Reza"]
    assert session.query(Contact).filter_by(email="reza@example.com").count() == 1
    dup = client.post("/contacts", json={"name": "رضا۲", "email": "REZA@example.com"}, headers=KEY)
    assert dup.status_code == 409
    assert client.post("/contacts", json={"name": "x", "email": "nope"}, headers=KEY).status_code == 422
    assert client.post("/contacts", json={"name": ""}, headers=KEY).status_code == 422


# ---------------- health


def test_health_shallow_reflects_the_database(client, monkeypatch):
    monkeypatch.setattr(api, "_check_db", lambda: api.ComponentHealth(ok=True))
    assert client.get("/health", params={"shallow": "true"}).status_code == 200
    monkeypatch.setattr(api, "_check_db", lambda: api.ComponentHealth(ok=False, detail="unreachable"))
    r = client.get("/health", params={"shallow": "true"})
    assert r.status_code == 503 and r.json()["status"] == "degraded"


def test_health_deep_reports_each_component_and_no_secrets(client, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "telegram_bot_token", "123456:SECRET-TOKEN")
    monkeypatch.setattr(s, "deepseek_api_key", "sk-secret-key")
    api._deep_cache.clear()
    monkeypatch.setattr(api, "_check_db", lambda: api.ComponentHealth(ok=True))
    monkeypatch.setattr(api, "_probe", lambda url, headers=None: api.ComponentHealth(ok="telegram" in url))
    r = client.get("/health")
    body = r.json()
    assert r.status_code == 503 and body["telegram"]["ok"] and not body["llm"]["ok"]
    assert "SECRET" not in r.text and "sk-secret" not in r.text
    api._deep_cache.clear()
