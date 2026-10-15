import contextlib
import datetime as dt
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import create_engine, select, text

from app.agent import llm_log
from app.agent.agent import process_message
from app.agent.context import AgentContext
from app.agent.llm import DeepSeekProvider, LLMError
from app.agent.llm_log import compute_cost, purpose_var
from app.bot import main as bot
from app.budget import alert_text, is_blocked, on_usage, periods, status
from app.config import get_settings
from app.db.models import AgentRun, BudgetAlert, LlmCall, LlmPurpose, RunFeedback
from app.db.roles import ensure_readonly_role
from app.logging_config import bind_run_id
from tests.conftest import FIXED_NOW
from tests.fakes import ScriptedLLM, call, turn

real_record = llm_log.record_llm_call  # the autouse fixture replaces the module attribute


@pytest.fixture
def prices(monkeypatch):
    monkeypatch.setattr(
        get_settings(),
        "llm_prices_json",
        '{"m1": {"input_miss": 1.0, "input_hit": 0.1, "output": 2.0}, "*": {"input_miss": 3.0, "input_hit": 0.3, "output": 6.0}}',
    )


# ---------------- cost


def test_cost_uses_cache_hit_price_for_cached_tokens(prices):
    # 600k miss * 1.0 + 400k hit * 0.1 + 100k out * 2.0, per million
    assert compute_cost("m1", 1_000_000, 100_000, 400_000) == Decimal("0.840000")


def test_unknown_model_uses_the_fallback_price_and_bad_config_costs_zero(prices, monkeypatch):
    assert compute_cost("other", 1_000_000, 0) == Decimal("3.000000")
    monkeypatch.setattr(get_settings(), "llm_prices_json", "not json")
    assert compute_cost("m1", 1000, 1000) == 0
    monkeypatch.setattr(get_settings(), "llm_prices_json", '{"m1": {"input_miss":1,"input_hit":1,"output":1}}')
    assert compute_cost("zzz", 1000, 1000) == 0


# ---------------- recording


def test_record_writes_a_row_with_run_id_purpose_and_cost(session, user, prices):
    run = AgentRun(user_id=user.id, input_text="x")
    session.add(run)
    session.flush()
    purpose_var.set("intent")
    with bind_run_id(run.id):
        real_record(model="m1", prompt_tokens=2000, completion_tokens=100, cached_tokens=1000,
                    latency_ms=850, session=session)  # fmt: skip
    row = session.scalars(select(LlmCall)).one()
    assert (row.run_id, row.purpose, row.status, row.latency_ms) == (run.id, LlmPurpose.intent, "ok", 850)
    assert (row.prompt_tokens, row.completion_tokens, row.cached_tokens) == (2000, 100, 1000)
    assert row.cost_usd == Decimal("0.001300")
    purpose_var.set("tool_step")


def test_record_never_raises(session):
    real_record(model="m1", purpose="not-a-purpose", session=session)  # invalid enum value is swallowed


def make_provider(handler, rows, retries=1):
    return DeepSeekProvider(
        api_key="k", base_url="https://x.test", model="m1", max_retries=retries,
        client=httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None,
        recorder=lambda **kw: rows.append(kw),
    )  # fmt: skip


def test_provider_records_each_call_with_usage_cache_and_retries():
    rows, n = [], {"i": 0}

    def handler(request):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi"}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 20, "prompt_cache_hit_tokens": 300},
        })  # fmt: skip

    make_provider(handler, rows).chat([], [])
    (r,) = rows
    assert (r["status"], r["retries"], r["prompt_tokens"], r["completion_tokens"], r["cached_tokens"]) == ("ok", 1, 500, 20, 300)
    assert r["model"] == "m1" and r["latency_ms"] >= 0


def test_provider_records_failures_too():
    rows = []
    with pytest.raises(LLMError):
        make_provider(lambda r: httpx.Response(500, text="boom"), rows, retries=1).chat([], [])
    with pytest.raises(LLMError):
        make_provider(lambda r: httpx.Response(401, text="bad key"), rows).chat([], [])
    assert [(r["status"], r["retries"]) for r in rows] == [("error", 1), ("error", 0)]
    assert "HTTP 500" in rows[0]["error"] and "HTTP 401" in rows[1]["error"]


def test_agent_marks_the_first_call_intent_and_later_ones_tool_step(session, user):
    seen = []

    class Recording(ScriptedLLM):
        def chat(self, messages, tools):
            seen.append(purpose_var.get())
            return super().chat(messages, tools)

    llm = Recording(turn(call("get_current_datetime")), turn(call("ask_clarification", question="؟")))
    process_message(session, llm, user, "x", AgentContext(), now=lambda: FIXED_NOW)
    assert seen == ["intent", "tool_step"]


# ---------------- budgets


@pytest.fixture
def sent(monkeypatch):
    messages = []
    monkeypatch.setattr("app.budget.telegram_send", lambda chat, text: messages.append((chat, text)) or True)
    monkeypatch.setattr(get_settings(), "allowed_user_ids", "111")
    return messages


def add_usage(session, tokens, **kw):
    session.add(LlmCall(model="m1", purpose=LlmPurpose.intent, prompt_tokens=tokens, completion_tokens=0, **kw))
    session.flush()


def test_periods_use_the_tehran_day_and_the_jalali_month(monkeypatch):
    monkeypatch.setattr(get_settings(), "daily_token_budget", 10)
    day, month = periods(dt.datetime(2026, 10, 14, 23, 59, tzinfo=dt.timezone(dt.timedelta(hours=3, minutes=30))))
    assert day.key == "day:2026-10-14" and month.key == "month:1405-07"
    assert month.start.date() == dt.date(2026, 9, 23)  # 1 Mehr 1405
    # 22:00 UTC is already the next day in Tehran
    day2, _ = periods(dt.datetime(2026, 10, 14, 22, 0, tzinfo=dt.timezone.utc))
    assert day2.key == "day:2026-10-15"


def test_alerts_fire_once_per_threshold_and_period(session, monkeypatch, sent):
    s = get_settings()
    monkeypatch.setattr(s, "daily_token_budget", 1000)
    monkeypatch.setattr(s, "monthly_token_budget", 0)
    assert on_usage(session) == []
    add_usage(session, 790)
    assert on_usage(session) == []
    add_usage(session, 20)  # 810 = 81%
    fired = on_usage(session)
    assert [t for _, t in fired] == [80] and len(sent) == 1
    assert sent[0][0] == 111 and "۸۰٪" in sent[0][1] and "۸۱۰" in sent[0][1]
    assert on_usage(session) == [] and len(sent) == 1  # not repeated
    add_usage(session, 400)  # 121%
    assert [t for _, t in on_usage(session)] == [100] and len(sent) == 2
    assert on_usage(session) == []
    assert session.scalars(select(BudgetAlert.threshold).order_by(BudgetAlert.threshold)).all() == [80, 100]


def test_jumping_straight_past_100_announces_both_levels(session, monkeypatch, sent):
    monkeypatch.setattr(get_settings(), "daily_token_budget", 100)
    add_usage(session, 500)
    assert [t for _, t in on_usage(session)] == [80, 100]


def test_monthly_budget_is_tracked_separately(session, monkeypatch, sent):
    s = get_settings()
    monkeypatch.setattr(s, "daily_token_budget", 0)
    monkeypatch.setattr(s, "monthly_token_budget", 1000)
    add_usage(session, 900)
    ((key, threshold),) = on_usage(session)
    assert key.startswith("month:") and threshold == 80
    assert "ماهانه" in sent[0][1]


def test_usage_outside_the_period_and_failed_calls_do_not_count(session, monkeypatch, sent):
    monkeypatch.setattr(get_settings(), "daily_token_budget", 100)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)
    add_usage(session, 10_000, created_at=old)
    session.add(LlmCall(model="m1", purpose=LlmPurpose.intent, prompt_tokens=10_000, status="error"))
    session.flush()
    assert on_usage(session) == []
    assert status(session)[0].used == 0


def test_no_limits_means_no_alerts_and_no_block(session, monkeypatch, sent):
    s = get_settings()
    monkeypatch.setattr(s, "daily_token_budget", 0)
    monkeypatch.setattr(s, "monthly_token_budget", 0)
    monkeypatch.setattr(s, "budget_hard_stop", True)
    add_usage(session, 10**9)
    assert on_usage(session) == [] and not is_blocked(session) and sent == []


def test_hard_stop_blocks_only_when_enabled_and_exhausted(session, monkeypatch, sent):
    s = get_settings()
    monkeypatch.setattr(s, "daily_token_budget", 1000)
    monkeypatch.setattr(s, "budget_hard_stop", False)
    add_usage(session, 1500)
    assert not is_blocked(session)
    monkeypatch.setattr(s, "budget_hard_stop", True)
    assert is_blocked(session)


def test_hard_stop_refuses_new_requests_before_the_agent_runs(session, user, monkeypatch, sent):
    from app.bot import pipeline

    s = get_settings()
    monkeypatch.setattr(s, "daily_token_budget", 100)
    monkeypatch.setattr(s, "budget_hard_stop", True)
    add_usage(session, 500)
    monkeypatch.setattr(pipeline, "get_provider", lambda: pytest.fail("the agent must not run"))
    assert pipeline.analyse_for_user(session, user, "فردا جلسه").kind == "blocked"


def test_recording_a_call_triggers_the_alert(session, monkeypatch, sent):
    monkeypatch.setattr(get_settings(), "daily_token_budget", 1000)
    real_record(model="m1", prompt_tokens=900, completion_tokens=0, session=session)
    assert len(sent) == 1 and "۸۰٪" in sent[0][1]


def test_alert_text_mentions_the_hard_stop_state(monkeypatch):
    monkeypatch.setattr(get_settings(), "budget_hard_stop", True)
    p = periods()[0]
    p.limit, p.used = 100, 100
    assert "جلوگیری از درخواست‌های جدید فعال است" in alert_text(p, 100)
    monkeypatch.setattr(get_settings(), "budget_hard_stop", False)
    assert "غیرفعال" in alert_text(p, 100)


# ---------------- thumbs


def test_feedback_is_saved_updated_and_scoped_to_the_owner(session, user, monkeypatch):
    monkeypatch.setattr(bot, "SessionLocal", lambda: contextlib.nullcontext(session))
    run = AgentRun(user_id=user.id, input_text="x")
    session.add(run)
    session.flush()
    tg = type("U", (), {"id": 111, "full_name": "Owner"})()
    assert bot._save_feedback(tg, run.id, 1)
    assert bot._save_feedback(tg, run.id, -1)  # changing your mind updates the same row
    assert [f.rating for f in session.scalars(select(RunFeedback))] == [-1]
    assert not bot._save_feedback(tg, 999999, 1)


# ---------------- read-only role


def test_dashboard_role_can_read_metrics_but_not_audio_contacts_or_write(engine):
    admin_url = engine.url  # the throwaway test database
    role, password = "dash_ro_test", "p@ss'w\"ord $1"
    ensure_readonly_role(admin_url, role, password)
    ensure_readonly_role(admin_url, role, password)  # idempotent
    url = admin_url.set(username=role, password=password)
    try:
        with create_engine(url).connect() as conn:
            for table in ["agent_runs", "llm_calls", "tool_calls", "run_feedback", "audit_events", "budget_alerts"]:
                conn.execute(text(f"SELECT count(*) FROM {table}"))
            conn.execute(text("SELECT transcript, stt_confidence FROM voice_messages"))
            assert conn.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
        denied = [
            "SELECT audio_path FROM voice_messages",
            "SELECT telegram_file_id FROM voice_messages",
            "SELECT * FROM voice_messages",
            "SELECT * FROM emails",
            "SELECT * FROM contacts",
            "SELECT * FROM users",
            "SELECT * FROM memory_facts",
            "INSERT INTO budget_alerts (period, threshold, tokens_used) VALUES ('x', 1, 1)",
            "DELETE FROM agent_runs",
            "UPDATE agent_runs SET error = 'x'",
        ]
        for sql in denied:
            with create_engine(url).connect() as conn:
                with pytest.raises(Exception, match="(?i)permission denied|read-only"):
                    conn.execute(text(sql))
    finally:
        with create_engine(admin_url, isolation_level="AUTOCOMMIT").connect() as conn:
            conn.execute(text(f'DROP OWNED BY "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))


def test_role_creation_refuses_empty_passwords(engine):
    with pytest.raises(SystemExit):
        ensure_readonly_role(engine.url, "x_ro", "")
