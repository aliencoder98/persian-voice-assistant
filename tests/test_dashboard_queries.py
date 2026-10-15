import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.dashboard import demo, queries as q
from app.dashboard.queries import CallRow, LogFilters, RunRow
from app.db.models import (
    AgentRun,
    AuditEvent,
    Intent,
    LlmCall,
    LlmPurpose,
    RunFeedback,
    RunStatus,
    ToolCall,
    User,
)

TODAY = dt.date(2026, 10, 14)
UTC = dt.timezone.utc
NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=UTC)  # 12:30 Tehran


def call(day, model="m1", purpose="intent", p=1000, c=100, cached=0, cost=0.01, lat=1000, status="ok"):
    return CallRow(day, model, purpose, p, c, cached, cost, lat, status)


def run(id=1, day=TODAY, model="m1", status="executed", **kw):
    return RunRow(id=id, day=day, created_at=dt.datetime.now(UTC), model=model, intent="schedule", status=status, **kw)


def d(n):
    return TODAY - dt.timedelta(days=n)


# ---------------- pure aggregation


def test_percentile_interpolates_like_sql():
    assert q.percentile([], 0.5) is None
    assert q.percentile([5], 0.95) == 5
    assert q.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert q.percentile([0, 100], 0.95) == pytest.approx(95)
    assert q.percentile([3, 1, 2], 0.5) == 2


def test_kpi_windows_today_7_30_days():
    calls = [call(d(0), p=1000, c=0), call(d(6), p=100, c=0), call(d(7), p=10, c=0), call(d(29), p=1, c=0), call(d(30), p=5000, c=0)]
    k = q.build_kpis(calls, [], TODAY)
    assert k["tokens"] == {"today": 1000, "d7": 1100, "d30": 1111}  # day -7 is outside "7 days"; day -30 outside 30


def test_failed_calls_cost_but_do_not_count_tokens_or_latency():
    calls = [call(d(0), cost=0.5, lat=100), call(d(0), status="error", p=0, c=0, cost=0.25, lat=9999)]
    k = q.build_kpis(calls, [], TODAY)
    assert k["tokens"]["today"] == 1100 and k["cost"]["today"] == pytest.approx(0.75)
    assert k["latency_p50"] == 100 and k["latency_p95"] == 100


def test_kpi_latency_and_rates():
    calls = [call(d(0), lat=v) for v in (100, 200, 300, 400, 1000)]
    runs = [
        run(1, decision="confirmed"), run(2, decision="confirmed"), run(3, decision="confirmed"),
        run(4, decision="edited"), run(5, decision="cancelled"), run(6, status="failed"), run(7, status="needs_clarification"),
    ]  # fmt: skip
    k = q.build_kpis(calls, runs, TODAY)
    assert k["latency_p50"] == 300 and k["latency_p95"] == pytest.approx(880) and k["latency_avg"] == 400
    assert k["success_rate"] == pytest.approx(6 / 7)
    assert k["decided"] == 5
    assert k["confirmation_rate"] == pytest.approx(3 / 5)
    assert k["edit_rate"] == pytest.approx(1 / 5) and k["cancel_rate"] == pytest.approx(1 / 5)


def test_empty_data_gives_none_not_division_errors():
    k = q.build_kpis([], [], TODAY)
    assert k["success_rate"] is None and k["confirmation_rate"] is None and k["latency_p95"] is None
    assert k["tokens"] == {"today": 0, "d7": 0, "d30": 0}
    s = q.build_series([], [], 7, TODAY)
    assert s["success_rate"] == [None] * 7 and s["latency_p50"] == [None] * 7 and s["tokens_by_model"] == {}
    assert q.quality_by_model([]) == [] and q.most_expensive([]) == []


def test_series_is_zero_filled_and_split_by_model_and_purpose():
    calls = [
        call(d(0), "m1", "intent", p=100, c=0), call(d(0), "m2", "tool_step", p=50, c=0),
        call(d(2), "m1", "tool_step", p=10, c=0), call(d(40), "m1", p=999, c=0),
    ]  # fmt: skip
    s = q.build_series(calls, [], 3, TODAY)
    assert s["labels"] == [d(2).isoformat(), d(1).isoformat(), d(0).isoformat()]
    assert s["tokens_by_model"] == {"m1": [10, 0, 100], "m2": [0, 0, 50]}
    assert s["tokens_by_purpose"] == {"tool_step": [10, 0, 50], "intent": [0, 0, 100]}
    assert s["cost_total"] == [0.01, 0.0, 0.02]


def test_series_rates_per_day():
    runs = [run(1, d(0), decision="edited"), run(2, d(0), decision="confirmed"), run(3, d(1), status="failed")]
    s = q.build_series([], runs, 2, TODAY)
    assert s["success_rate"] == [0.0, 1.0] and s["edit_rate"] == [None, 0.5] and s["runs"] == [1, 2]


def test_quality_table_per_model():
    runs = [
        run(1, model="a", steps=2, cost=0.02, tool_ok=3, up=1), run(2, model="a", steps=4, cost=0.04, invalid=1, tool_err=1, down=1, decision="edited"),
        run(3, model="b", steps=3, cost=0.10, status="failed", retries=2, clarifications=1),
    ]  # fmt: skip
    a, b = q.quality_by_model(runs)
    assert (a["model"], a["runs"], a["success_rate"], a["json_valid_rate"]) == ("a", 2, 1.0, 0.5)
    assert a["avg_steps"] == 3 and a["avg_cost"] == pytest.approx(0.03) and a["tool_success_rate"] == pytest.approx(3 / 4)
    assert a["edit_rate"] == 1.0 and a["thumbs_up_share"] == 0.5 and a["feedback_count"] == 2
    assert (b["success_rate"], b["avg_retries"], b["clarifications"], b["thumbs_up_share"]) == (0.0, 2, 1, None)


def test_most_expensive_runs_sorted():
    runs = [run(1, cost=0.1), run(2, cost=0.5), run(3, cost=0.3)]
    assert [r.id for r in q.most_expensive(runs, 2)] == [2, 3]


# ---------------- SQL layer


def mk_run(session, user, minutes_ago=5, **kw):
    at = dt.datetime.now(UTC) - dt.timedelta(minutes=minutes_ago)
    kw.setdefault("model", "m1")
    r = AgentRun(user_id=user.id, input_text=kw.pop("input_text", "فردا جلسه"), created_at=at, **kw)
    session.add(r)
    session.flush()
    return r


def mk_call(session, run, tokens=(1000, 100), cost="0.01", **kw):
    session.add(LlmCall(run_id=run.id, model=run.model or "m1", purpose=kw.pop("purpose", LlmPurpose.intent),
                        prompt_tokens=tokens[0], completion_tokens=tokens[1], cost_usd=Decimal(cost), latency_ms=kw.pop("latency_ms", 800), **kw))  # fmt: skip
    session.flush()


def test_fetch_runs_collects_all_quality_signals(session, user):
    r = mk_run(session, user, intent=Intent.schedule, status=RunStatus.executed)
    mk_call(session, r, (1000, 100), "0.010")
    mk_call(session, r, (500, 50), "0.005", purpose=LlmPurpose.tool_step, retries=2)
    mk_call(session, r, (0, 0), "0", status="error")  # failed call: not a step, no tokens
    for name, status in [("find_contact", "ok"), ("propose_schedule", "invalid"), ("propose_schedule", "rejected"),
                         ("propose_schedule", "ok"), ("create_calendar_event", "error"), ("ask_clarification", "ok")]:  # fmt: skip
        session.add(ToolCall(run_id=r.id, tool_name=name, status=status))
    session.add_all([
        AuditEvent(event_type="action_confirmed", actor="a", payload_json={"run_id": r.id}),
        AuditEvent(event_type="action_executed", actor="a", payload_json={"run_id": r.id}),
        AuditEvent(event_type="action_confirmed", actor="a", payload_json={"run_id": 999999}),
        RunFeedback(run_id=r.id, user_id=user.id, rating=1),
    ])  # fmt: skip
    session.flush()

    (row,) = [x for x in q.fetch_runs(session, dt.datetime.now(UTC) - dt.timedelta(days=1)) if x.id == r.id]
    assert (row.steps, row.retries, row.tokens) == (2, 2, 1650)
    assert row.cost == pytest.approx(0.015)
    assert (row.invalid, row.tool_ok, row.tool_err, row.clarifications) == (2, 3, 1, 1)
    assert row.decision == "confirmed" and (row.up, row.down) == (1, 0)
    assert row.model == "m1" and row.status == "executed" and row.intent == "schedule"


def test_decisions_edited_and_cancelled_and_undecided(session, user):
    ids = {}
    for name, event in [("edited", "action_superseded"), ("cancelled", "action_rejected"), ("retried", "action_retried")]:
        r = mk_run(session, user)
        session.add(AuditEvent(event_type=event, actor="a", payload_json={"run_id": r.id}))
        ids[name] = r.id
    undecided = mk_run(session, user)
    session.flush()
    rows = {x.id: x for x in q.fetch_runs(session, dt.datetime.now(UTC) - dt.timedelta(days=1))}
    assert rows[ids["edited"]].decision == "edited" and rows[ids["cancelled"]].decision == "cancelled"
    assert rows[ids["retried"]].decision == "confirmed" and rows[undecided.id].decision is None


def test_runs_and_calls_are_bucketed_by_the_tehran_day(session, user):
    # 21:00 UTC on the 14th is 00:30 on the 15th in Tehran
    at = dt.datetime(2026, 10, 14, 21, 0, tzinfo=UTC)
    r = AgentRun(user_id=user.id, model="m1", created_at=at)
    session.add(r)
    session.add(LlmCall(run_id=None, model="m1", purpose=LlmPurpose.intent, created_at=at, prompt_tokens=1))
    session.flush()
    since = dt.datetime(2026, 10, 1, tzinfo=UTC)
    assert [x.day for x in q.fetch_runs(session, since) if x.id == r.id] == [dt.date(2026, 10, 15)]
    assert q.fetch_calls(session, since)[-1].day == dt.date(2026, 10, 15)


def test_list_logs_filters_pagination_and_cost(session, user):
    a = mk_run(session, user, intent=Intent.email, status=RunStatus.executed, model="m1", tokens=100, input_text="الف" * 100)
    b = mk_run(session, user, intent=Intent.schedule, status=RunStatus.failed, model="m2", error="boom")
    mk_run(session, user, intent=Intent.schedule, status=RunStatus.executed, model="m2")
    mk_call(session, a, cost="0.25")
    mk_call(session, a, cost="0.25")

    rows, total = q.list_logs(session, LogFilters())
    assert total >= 3
    assert q.list_logs(session, LogFilters(model="m2"))[1] == 2
    assert q.list_logs(session, LogFilters(status="failed"))[0][0]["error"] == "boom"
    assert q.list_logs(session, LogFilters(intent="email"))[1] == 1
    assert q.list_logs(session, LogFilters(model="m2", status="executed", intent="schedule"))[1] == 1
    row = [x for x in q.list_logs(session, LogFilters(intent="email"))[0] if x["id"] == a.id][0]
    assert row["cost"] == pytest.approx(0.5) and len(row["preview"]) == 80
    page1, _ = q.list_logs(session, LogFilters(), limit=2, offset=0)
    page2, _ = q.list_logs(session, LogFilters(), limit=2, offset=2)
    assert len(page1) == 2 and not {r["id"] for r in page1} & {r["id"] for r in page2}
    assert b.id in [r["id"] for r in q.list_logs(session, LogFilters(model="m2"))[0]]


def test_date_filters_use_inclusive_tehran_days(session, user):
    inside = AgentRun(user_id=user.id, model="zz", created_at=dt.datetime(2026, 10, 14, 21, 0, tzinfo=UTC))  # 15th 00:30
    before = AgentRun(user_id=user.id, model="zz", created_at=dt.datetime(2026, 10, 14, 20, 0, tzinfo=UTC))  # 14th 23:30
    session.add_all([inside, before])
    session.flush()
    f = LogFilters(model="zz", date_from=dt.date(2026, 10, 15), date_to=dt.date(2026, 10, 15))
    assert [r["id"] for r in q.list_logs(session, f)[0]] == [inside.id]
    f = LogFilters(model="zz", date_to=dt.date(2026, 10, 14))
    assert [r["id"] for r in q.list_logs(session, f)[0]] == [before.id]


def test_export_is_capped(session, user):
    for _ in range(5):
        mk_run(session, user, model="cap")
    assert len(q.export_logs(session, LogFilters(model="cap"), limit=3)) == 3


def test_run_detail_has_everything_for_the_owner_page(session, user):
    r = mk_run(session, user, intent=Intent.email, action_json={"intent": "email", "body": "متن کامل"})
    mk_call(session, r, cost="0.02")
    session.add_all([ToolCall(run_id=r.id, tool_name="find_contact", status="ok", arguments_json={"name": "علی"}),
                     AuditEvent(event_type="action_confirmed", actor="a", payload_json={"run_id": r.id})])  # fmt: skip
    session.flush()
    det = q.run_detail(session, r.id)
    assert det["run"].action_json["body"] == "متن کامل" and det["tokens"] == 1100 and det["cost"] == Decimal("0.02")
    assert [t.tool_name for t in det["tool_calls"]] == ["find_contact"] and len(det["events"]) == 1
    assert q.run_detail(session, 99999999) is None


# ---------------- overview and demo data


def test_demo_seed_fills_every_chart_and_looks_realistic(session):
    counts = demo.seed_demo(session, days=30, now=NOW)
    assert counts["runs"] > 150 and counts["llm_calls"] > counts["runs"]
    ov = q.overview(session, 30, now=NOW)
    k, s = ov.kpis, ov.series
    assert k["tokens"]["d30"] > 100_000 and k["tokens"]["d30"] > k["tokens"]["d7"] > 0
    assert 0.9 < k["success_rate"] < 1.0 and 0.5 < k["confirmation_rate"] < 0.9 and 0 < k["edit_rate"] < 0.3
    assert 500 < k["latency_p50"] < 4000 and k["latency_p95"] > k["latency_p50"]
    assert k["cost"]["d30"] > 0
    assert len(s["labels"]) == 30 and set(s["tokens_by_model"]) == {"deepseek-chat", "deepseek-flash"}
    assert set(s["tokens_by_purpose"]) == {"intent", "tool_step"}
    assert sum(1 for v in s["runs"] if v) >= 28  # almost every day has data
    assert len(ov.quality) == 2 and all(r["thumbs_up_share"] is not None for r in ov.quality)
    assert len(ov.expensive) == 10 and ov.expensive[0].cost >= ov.expensive[-1].cost


def test_demo_data_can_be_detected_and_cleared_completely(session):
    assert not demo.has_demo(session) and not demo.has_real_usage(session)
    demo.seed_demo(session, days=5, now=NOW)
    assert demo.has_demo(session) and not demo.has_real_usage(session)
    session.add(LlmCall(run_id=None, model="real", purpose=LlmPurpose.intent, prompt_tokens=1))
    session.flush()
    assert demo.has_real_usage(session)
    removed = demo.clear_demo(session)
    assert removed > 0 and not demo.has_demo(session)
    leftovers = session.scalars(select(LlmCall.model).where(LlmCall.model.like("deepseek-%"))).all()
    assert leftovers == []
    assert session.scalar(select(AuditEvent.id).where(AuditEvent.actor == "telegram:-1")) is None
    assert session.scalars(select(User.id).where(User.telegram_user_id == demo.DEMO_TELEGRAM_ID)).first() is None
    assert demo.clear_demo(session) == 0  # idempotent
