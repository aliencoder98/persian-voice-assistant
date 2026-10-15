"""Aggregation queries for the dashboard.

Two layers: SQL fetches flat rows (one per LLM call, one per run with its quality signals), and pure
functions compute KPIs, series and tables from them. Only tables/columns the read-only role may see are used.

Definitions (also shown in the README):
- success rate      = runs not in status `failed` / all runs
- decision          = confirmed (action_confirmed/retried), edited (action_superseded) or cancelled (action_rejected)
- confirmation rate = confirmed / decided runs;  edit rate = edited / decided runs
- tool success      = tool calls `ok` / (`ok` + `error`)
- JSON-valid rate   = runs without an invalid or rejected final/tool call / all runs
- steps             = successful LLM calls in a run;  retries = provider retries across its calls
- thumbs-up share   = 👍 / (👍 + 👎)
"""

import datetime as dt
import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Integer, and_, cast, func, select
from sqlalchemy.orm import Session

from app.db.models import AgentRun, AuditEvent, LlmCall, RunFeedback, ToolCall, VoiceMessage

TZ_NAME = "Asia/Tehran"
TEHRAN = ZoneInfo(TZ_NAME)
CONFIRM_EVENTS = ("action_confirmed", "action_retried")


def tehran_day(col):
    return func.date(func.timezone(TZ_NAME, col))


def today_tehran(now: dt.datetime | None = None) -> dt.date:
    return (now or dt.datetime.now(dt.timezone.utc)).astimezone(TEHRAN).date()


def day_start(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=TEHRAN)


# ------------------------------------------------------------------ flat rows


@dataclass
class CallRow:
    day: dt.date
    model: str
    purpose: str
    prompt_tokens: int
    completion_tokens: int
    cached_tokens: int
    cost: float
    latency_ms: int
    status: str

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class RunRow:
    id: int
    day: dt.date
    created_at: dt.datetime
    model: str
    intent: str
    status: str
    tokens: int = 0
    cost: float = 0.0
    latency_ms: int | None = None
    steps: int = 0
    retries: int = 0
    invalid: int = 0
    tool_ok: int = 0
    tool_err: int = 0
    clarifications: int = 0
    decision: str | None = None  # confirmed | edited | cancelled
    up: int = 0
    down: int = 0


def fetch_calls(session: Session, since: dt.datetime) -> list[CallRow]:
    rows = session.execute(
        select(
            tehran_day(LlmCall.created_at),
            LlmCall.model,
            LlmCall.purpose,
            LlmCall.prompt_tokens,
            LlmCall.completion_tokens,
            LlmCall.cached_tokens,
            LlmCall.cost_usd,
            LlmCall.latency_ms,
            LlmCall.status,
        ).where(LlmCall.created_at >= since)
    ).all()
    return [
        CallRow(d, m, p.value if hasattr(p, "value") else p, pt, ct, cat, float(c or 0), lat, st)
        for d, m, p, pt, ct, cat, c, lat, st in rows
    ]


def _run_signals_query(since: dt.datetime):
    calls = (
        select(
            LlmCall.run_id.label("run_id"),
            func.count().filter(LlmCall.status == "ok").label("steps"),
            func.coalesce(func.sum(LlmCall.retries), 0).label("retries"),
            func.coalesce(func.sum(LlmCall.cost_usd), 0).label("cost"),
            func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens).filter(LlmCall.status == "ok"), 0).label("tokens"),
        )
        .where(LlmCall.run_id.is_not(None))
        .group_by(LlmCall.run_id)
        .subquery()
    )
    tools = (
        select(
            ToolCall.run_id.label("run_id"),
            func.count().filter(ToolCall.status.in_(["invalid", "rejected"])).label("invalid"),
            func.count().filter(ToolCall.status == "ok").label("tool_ok"),
            func.count().filter(ToolCall.status == "error").label("tool_err"),
            func.count().filter(and_(ToolCall.tool_name == "ask_clarification", ToolCall.status == "ok")).label("clar"),
        )
        .group_by(ToolCall.run_id)
        .subquery()
    )
    ev_run = cast(AuditEvent.payload_json["run_id"].astext, Integer)
    decisions = (
        select(
            ev_run.label("run_id"),
            func.bool_or(AuditEvent.event_type.in_(CONFIRM_EVENTS)).label("confirmed"),
            func.bool_or(AuditEvent.event_type == "action_superseded").label("edited"),
            func.bool_or(AuditEvent.event_type == "action_rejected").label("cancelled"),
        )
        .where(AuditEvent.event_type.in_([*CONFIRM_EVENTS, "action_superseded", "action_rejected"]))
        .where(AuditEvent.payload_json["run_id"].astext.is_not(None))
        .group_by(ev_run)
        .subquery()
    )
    fb = (
        select(
            RunFeedback.run_id.label("run_id"),
            func.count().filter(RunFeedback.rating > 0).label("up"),
            func.count().filter(RunFeedback.rating < 0).label("down"),
        )
        .group_by(RunFeedback.run_id)
        .subquery()
    )
    q = (
        select(
            AgentRun.id,
            tehran_day(AgentRun.created_at).label("day"),
            AgentRun.created_at,
            func.coalesce(AgentRun.model, "unknown"),
            AgentRun.intent,
            AgentRun.status,
            func.coalesce(calls.c.tokens, AgentRun.tokens, 0),
            func.coalesce(calls.c.cost, 0),
            AgentRun.latency_ms,
            func.coalesce(calls.c.steps, 0),
            func.coalesce(calls.c.retries, 0),
            func.coalesce(tools.c.invalid, 0),
            func.coalesce(tools.c.tool_ok, 0),
            func.coalesce(tools.c.tool_err, 0),
            func.coalesce(tools.c.clar, 0),
            decisions.c.confirmed,
            decisions.c.edited,
            decisions.c.cancelled,
            func.coalesce(fb.c.up, 0),
            func.coalesce(fb.c.down, 0),
        )
        .select_from(AgentRun)
        .outerjoin(calls, calls.c.run_id == AgentRun.id)
        .outerjoin(tools, tools.c.run_id == AgentRun.id)
        .outerjoin(decisions, decisions.c.run_id == AgentRun.id)
        .outerjoin(fb, fb.c.run_id == AgentRun.id)
        .where(AgentRun.created_at >= since)
        .order_by(AgentRun.id)
    )
    return q


def fetch_runs(session: Session, since: dt.datetime) -> list[RunRow]:
    out = []
    for r in session.execute(_run_signals_query(since)).all():
        decision = "confirmed" if r[15] else "edited" if r[16] else "cancelled" if r[17] else None
        out.append(
            RunRow(
                id=r[0], day=r[1], created_at=r[2], model=r[3],
                intent=r[4].value if hasattr(r[4], "value") else r[4],
                status=r[5].value if hasattr(r[5], "value") else r[5],
                tokens=int(r[6] or 0), cost=float(r[7] or 0), latency_ms=r[8], steps=int(r[9]),
                retries=int(r[10]), invalid=int(r[11]), tool_ok=int(r[12]), tool_err=int(r[13]),
                clarifications=int(r[14]), decision=decision, up=int(r[18]), down=int(r[19]),
            )
        )  # fmt: skip
    return out


# ------------------------------------------------------------------ pure aggregation


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation, like SQL percentile_cont."""
    if not values:
        return None
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def success_rate(runs: list[RunRow]) -> float | None:
    return _ratio(sum(r.status != "failed" for r in runs), len(runs))


def decision_rates(runs: list[RunRow]) -> dict[str, float | None]:
    decided = [r for r in runs if r.decision]
    n = len(decided)
    return {
        "confirmation_rate": _ratio(sum(r.decision == "confirmed" for r in decided), n),
        "edit_rate": _ratio(sum(r.decision == "edited" for r in decided), n),
        "cancel_rate": _ratio(sum(r.decision == "cancelled" for r in decided), n),
        "decided": n,
    }


def window(days: int, today: dt.date) -> tuple[dt.date, dt.date]:
    return today - dt.timedelta(days=days - 1), today


def _in(day: dt.date, days: int, today: dt.date) -> bool:
    first, last = window(days, today)
    return first <= day <= last


def build_kpis(calls: list[CallRow], runs: list[RunRow], today: dt.date) -> dict[str, Any]:
    ok = [c for c in calls if c.status == "ok"]

    def tokens(days):
        return sum(c.tokens for c in ok if _in(c.day, days, today))

    def cost(days):
        return sum(c.cost for c in calls if _in(c.day, days, today))

    lat = [float(c.latency_ms) for c in ok if _in(c.day, 30, today)]
    runs30 = [r for r in runs if _in(r.day, 30, today)]
    return {
        "tokens": {"today": tokens(1), "d7": tokens(7), "d30": tokens(30)},
        "cost": {"today": cost(1), "d7": cost(7), "d30": cost(30)},
        "latency_avg": (sum(lat) / len(lat)) if lat else None,
        "latency_p50": percentile(lat, 0.5),
        "latency_p95": percentile(lat, 0.95),
        "runs": len(runs30),
        "success_rate": success_rate(runs30),
        **decision_rates(runs30),
    }


def build_series(calls: list[CallRow], runs: list[RunRow], days: int, today: dt.date) -> dict[str, Any]:
    first, _ = window(days, today)
    labels = [first + dt.timedelta(days=i) for i in range(days)]
    index = {d: i for i, d in enumerate(labels)}
    tokens_by_model: dict[str, list[int]] = {}
    tokens_by_purpose: dict[str, list[int]] = {}
    cost_by_model: dict[str, list[float]] = {}
    lat_by_day: list[list[float]] = [[] for _ in labels]
    for c in calls:
        i = index.get(c.day)
        if i is None:
            continue
        cost_by_model.setdefault(c.model, [0.0] * days)[i] += c.cost
        if c.status != "ok":
            continue
        tokens_by_model.setdefault(c.model, [0] * days)[i] += c.tokens
        tokens_by_purpose.setdefault(c.purpose, [0] * days)[i] += c.tokens
        lat_by_day[i].append(float(c.latency_ms))
    runs_by_day: list[list[RunRow]] = [[] for _ in labels]
    for r in runs:
        if r.day in index:
            runs_by_day[index[r.day]].append(r)
    return {
        "labels": [d.isoformat() for d in labels],
        "tokens_by_model": tokens_by_model,
        "tokens_by_purpose": tokens_by_purpose,
        "cost_by_model": {m: [round(x, 6) for x in v] for m, v in cost_by_model.items()},
        "cost_total": [round(sum(v[i] for v in cost_by_model.values()), 6) for i in range(days)],
        "latency_p50": [percentile(v, 0.5) for v in lat_by_day],
        "latency_p95": [percentile(v, 0.95) for v in lat_by_day],
        "success_rate": [success_rate(v) for v in runs_by_day],
        "edit_rate": [decision_rates(v)["edit_rate"] for v in runs_by_day],
        "confirmation_rate": [decision_rates(v)["confirmation_rate"] for v in runs_by_day],
        "runs": [len(v) for v in runs_by_day],
    }


def quality_by_model(runs: list[RunRow]) -> list[dict[str, Any]]:
    groups: dict[str, list[RunRow]] = {}
    for r in runs:
        groups.setdefault(r.model, []).append(r)
    table = []
    for model, rs in sorted(groups.items()):
        with_steps = [r.steps for r in rs if r.steps]
        tool_ok, tool_err = sum(r.tool_ok for r in rs), sum(r.tool_err for r in rs)
        up, down = sum(r.up for r in rs), sum(r.down for r in rs)
        table.append(
            {
                "model": model,
                "runs": len(rs),
                "success_rate": success_rate(rs),
                "json_valid_rate": _ratio(sum(r.invalid == 0 for r in rs), len(rs)),
                "tool_success_rate": _ratio(tool_ok, tool_ok + tool_err),
                "edit_rate": decision_rates(rs)["edit_rate"],
                "confirmation_rate": decision_rates(rs)["confirmation_rate"],
                "avg_steps": (sum(with_steps) / len(with_steps)) if with_steps else None,
                "avg_cost": sum(r.cost for r in rs) / len(rs),
                "avg_retries": sum(r.retries for r in rs) / len(rs),
                "clarifications": sum(r.clarifications for r in rs),
                "thumbs_up_share": _ratio(up, up + down),
                "feedback_count": up + down,
            }
        )
    return table


def most_expensive(runs: list[RunRow], limit: int = 10) -> list[RunRow]:
    return sorted(runs, key=lambda r: r.cost, reverse=True)[:limit]


@dataclass
class Overview:
    days: int
    today: dt.date
    kpis: dict[str, Any]
    series: dict[str, Any]
    quality: list[dict[str, Any]]
    expensive: list[RunRow]
    demo: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def overview(session: Session, days: int = 30, now: dt.datetime | None = None) -> Overview:
    today = today_tehran(now)
    first = today - dt.timedelta(days=max(days, 30) - 1)  # KPIs always need the last 30 days
    since = day_start(first)
    calls, runs = fetch_calls(session, since), fetch_runs(session, since)
    in_window = [r for r in runs if _in(r.day, days, today)]
    return Overview(
        days=days,
        today=today,
        kpis=build_kpis(calls, runs, today),
        series=build_series(calls, runs, days, today),
        quality=quality_by_model(in_window),
        expensive=most_expensive(in_window),
    )


# ------------------------------------------------------------------ logs and run detail


@dataclass
class LogFilters:
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    model: str | None = None
    status: str | None = None
    intent: str | None = None


def _log_query(f: LogFilters):
    cost = (
        select(LlmCall.run_id.label("run_id"), func.coalesce(func.sum(LlmCall.cost_usd), 0).label("cost"))
        .group_by(LlmCall.run_id)
        .subquery()
    )
    q = (
        select(
            AgentRun.id,
            AgentRun.created_at,
            AgentRun.intent,
            AgentRun.status,
            AgentRun.model,
            AgentRun.tokens,
            AgentRun.latency_ms,
            func.left(func.coalesce(AgentRun.input_text, ""), 80).label("preview"),
            AgentRun.error,
            func.coalesce(cost.c.cost, 0).label("cost"),
        )
        .select_from(AgentRun)
        .outerjoin(cost, cost.c.run_id == AgentRun.id)
    )
    conds = []
    if f.date_from:
        conds.append(AgentRun.created_at >= day_start(f.date_from))
    if f.date_to:
        conds.append(AgentRun.created_at < day_start(f.date_to + dt.timedelta(days=1)))
    if f.model:
        conds.append(AgentRun.model == f.model)
    if f.status:
        conds.append(AgentRun.status == f.status)
    if f.intent:
        conds.append(AgentRun.intent == f.intent)
    return q.where(*conds), conds


def list_logs(session: Session, f: LogFilters, limit: int = 25, offset: int = 0) -> tuple[list[dict], int]:
    q, conds = _log_query(f)
    total = session.scalar(select(func.count()).select_from(AgentRun).where(*conds)) or 0
    rows = session.execute(q.order_by(AgentRun.id.desc()).limit(limit).offset(offset)).mappings().all()
    return [_clean(r) for r in rows], total


def export_logs(session: Session, f: LogFilters, limit: int = 10_000) -> list[dict]:
    q, _ = _log_query(f)
    return [_clean(r) for r in session.execute(q.order_by(AgentRun.id.desc()).limit(limit)).mappings().all()]


def _clean(row) -> dict:
    d = dict(row)
    for k in ("intent", "status"):
        d[k] = d[k].value if hasattr(d[k], "value") else d[k]
    d["cost"] = float(d["cost"] or 0)
    return d


def filter_options(session: Session) -> dict[str, list[str]]:
    models = session.scalars(select(AgentRun.model).where(AgentRun.model.is_not(None)).distinct().order_by(AgentRun.model)).all()
    return {"models": list(models)}


def run_detail(session: Session, run_id: int) -> dict[str, Any] | None:
    run = session.get(AgentRun, run_id)
    if run is None:
        return None
    voice = None
    if run.voice_message_id:
        voice = session.execute(
            select(VoiceMessage.transcript, VoiceMessage.stt_confidence, VoiceMessage.duration_s).where(
                VoiceMessage.id == run.voice_message_id
            )
        ).first()
    calls = session.scalars(select(LlmCall).where(LlmCall.run_id == run_id).order_by(LlmCall.id)).all()
    tools = session.scalars(select(ToolCall).where(ToolCall.run_id == run_id).order_by(ToolCall.id)).all()
    feedback = session.scalars(select(RunFeedback.rating).where(RunFeedback.run_id == run_id)).all()
    events = session.scalars(
        select(AuditEvent)
        .where(cast(AuditEvent.payload_json["run_id"].astext, Integer) == run_id)
        .order_by(AuditEvent.id)
    ).all()
    return {
        "run": run,
        "voice": voice,
        "llm_calls": calls,
        "tool_calls": tools,
        "feedback": list(feedback),
        "events": events,
        "cost": sum((c.cost_usd or Decimal(0) for c in calls), Decimal(0)),
        "tokens": sum(c.prompt_tokens + c.completion_tokens for c in calls if c.status == "ok"),
    }

