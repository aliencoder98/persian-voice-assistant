"""Realistic demo data for the dashboard. All rows belong to one demo user so they can be removed.

    python -m app.dashboard.demo seed [--if-empty] [--days 30]
    python -m app.dashboard.demo clear
"""

import argparse
import datetime as dt
import random
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.agent.llm_log import compute_cost
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
    VoiceMessage,
)
from app.db.session import SessionLocal

DEMO_TELEGRAM_ID = -1
TEHRAN = dt.timezone(dt.timedelta(hours=3, minutes=30))

SCHEDULE_TEXTS = [
    "فردا ساعت ده صبح با علی یک جلسه بذار",
    "پنجشنبه ساعت چهار بعدازظهر جلسه مرور پروژه ثبت کن",
    "بیست و پنجم مهر ساعت ۹ با سارا نیم ساعت قرار بذار",
    "دوشنبه آینده ساعت ۱۱ جلسه تیم فنی",
    "پس‌فردا ساعت ۱۶ تماس با مشتری را در تقویم بنویس",
]
EMAIL_TEXTS = [
    "به علی ایمیل بزن که جلسه فردا منتقل شد",
    "برای سارا ایمیل بفرست و بگو گزارش آماده است",
    "به مریم ایمیل بزن که فاکتور را فرستادم",
    "یه ایمیل برای رضا بنویس که پروپوزال تأیید شد",
]
UNCLEAR_TEXTS = ["فردا با علی جلسه بذار", "یه ایمیل بفرست", "هوای فردا چطوره؟", "ساعت پنج"]


def _models(rng: random.Random) -> str:
    return "deepseek-chat" if rng.random() < 0.7 else "deepseek-flash"


def seed_demo(session: Session, days: int = 30, now: dt.datetime | None = None, seed: int = 42) -> dict[str, int]:
    rng = random.Random(seed)
    now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(TEHRAN)
    user = session.scalar(select(User).where(User.telegram_user_id == DEMO_TELEGRAM_ID))
    if user is None:
        user = User(telegram_user_id=DEMO_TELEGRAM_ID, name="Demo")
        session.add(user)
        session.flush()

    counts = {"runs": 0, "llm_calls": 0}
    for offset in range(days - 1, -1, -1):
        day = (now - dt.timedelta(days=offset)).date()
        weekday = day.weekday()  # Friday (4) is the quiet weekend day
        n_runs = rng.randint(2, 6) if weekday == 4 else rng.randint(6, 20)
        for _ in range(n_runs):
            at = dt.datetime.combine(day, dt.time(rng.randint(8, 22), rng.randint(0, 59), rng.randint(0, 59)), TEHRAN)
            if at > now:
                continue
            _make_run(session, rng, user, at)
            counts["runs"] += 1
    session.flush()
    counts["llm_calls"] = session.scalar(
        select(func.count()).select_from(LlmCall).join(AgentRun, AgentRun.id == LlmCall.run_id).where(AgentRun.user_id == user.id)
    )
    session.commit()
    return counts


def _make_run(session: Session, rng: random.Random, user: User, at: dt.datetime) -> None:
    model = _models(rng)
    r = rng.random()
    if r < 0.03:
        kind = "failed"
    elif r < 0.22:
        kind = "clarify"
    elif r < 0.64:
        kind = "schedule"
    else:
        kind = "email"
    text = rng.choice({"schedule": SCHEDULE_TEXTS, "email": EMAIL_TEXTS}.get(kind, UNCLEAR_TEXTS))

    voice_id = None
    if rng.random() < 0.7:
        voice = VoiceMessage(
            user_id=user.id, telegram_file_id=f"demo-{rng.randint(10**8, 10**9)}", duration_s=rng.randint(3, 25),
            transcript=text, stt_confidence=round(rng.uniform(0.62, 0.97), 3), created_at=at, updated_at=at,
        )  # fmt: skip
        session.add(voice)
        session.flush()
        voice_id = voice.id

    run = AgentRun(
        user_id=user.id, voice_message_id=voice_id, input_text=text, prompt_version="v1", model=model,
        intent=Intent.unknown, status=RunStatus.failed, created_at=at, updated_at=at,
    )  # fmt: skip
    session.add(run)
    session.flush()

    # --- LLM calls: first is the intent call, the rest are tool steps; prompts grow with tool results
    steps = {"schedule": rng.choice([2, 2, 3, 3, 4]), "email": rng.choice([2, 2, 3]), "clarify": rng.choice([1, 2, 2, 3]), "failed": 1}[kind]
    total_tokens, total_ms, t = 0, 0, at
    for i in range(steps):
        prompt = int(rng.gauss(2300, 250)) + i * rng.randint(250, 450)
        cached = int(prompt * rng.uniform(0.55, 0.9)) if i else int(prompt * rng.uniform(0.0, 0.5))
        completion = rng.randint(40, 230)
        latency = int(min(rng.lognormvariate(7.2, 0.5), 11000))
        failed_call = kind == "failed" and i == steps - 1
        retries = 1 if rng.random() < 0.06 else 0
        t += dt.timedelta(milliseconds=latency + rng.randint(30, 400))
        session.add(
            LlmCall(
                run_id=run.id, model=model, purpose=LlmPurpose.intent if i == 0 else LlmPurpose.tool_step,
                prompt_tokens=0 if failed_call else prompt, completion_tokens=0 if failed_call else completion,
                cached_tokens=0 if failed_call else cached,
                cost_usd=Decimal(0) if failed_call else compute_cost(model, prompt, completion, cached),
                latency_ms=latency, status="error" if failed_call else "ok",
                error="LLM unavailable after 4 attempts: ConnectError" if failed_call else None,
                retries=3 if failed_call else retries, created_at=t, updated_at=t,
            )
        )  # fmt: skip
        if not failed_call:
            total_tokens += prompt + completion
        total_ms += latency
    run.tokens, run.latency_ms = total_tokens, total_ms + rng.randint(20, 200)

    # --- tool calls
    def tool(name: str, status: str = "ok") -> None:
        session.add(ToolCall(run_id=run.id, tool_name=name, status=status, arguments_json={}, result_json={}, created_at=at, updated_at=at))

    if kind != "failed":
        tool("get_current_datetime")
        if kind != "clarify" or rng.random() < 0.6:
            tool("find_contact")
        if rng.random() < 0.06:
            tool("propose_schedule" if kind == "schedule" else "propose_email", "invalid")
    audit_actor = f"telegram:{user.telegram_user_id}"

    def audit(event: str, extra: dict | None = None, delay_s: int = 25) -> None:
        when = t + dt.timedelta(seconds=delay_s)
        session.add(AuditEvent(event_type=event, actor=audit_actor, payload_json={"run_id": run.id, **(extra or {})}, created_at=when, updated_at=when))

    # --- outcome
    if kind == "failed":
        run.error = "LLM unavailable after 4 attempts: ConnectError"
        return
    if kind == "clarify":
        tool("ask_clarification")
        run.status, run.action_json, run.error = RunStatus.needs_clarification, {"question": "چه ساعتی؟"}, None
        _maybe_feedback(session, rng, run, user, 0.8, t)
        return

    tool("propose_schedule" if kind == "schedule" else "propose_email")
    run.intent = Intent(kind)
    run.action_json = {"intent": kind, "title": "جلسه" if kind == "schedule" else None, "subject": "موضوع" if kind == "email" else None}
    d = rng.random()
    if d < 0.70:
        audit("action_confirmed")
        if rng.random() < 0.93:
            tool("create_calendar_event" if kind == "schedule" else "send_email")
            audit("action_executed", delay_s=28)
            run.status, run.error = RunStatus.executed, None
            _maybe_feedback(session, rng, run, user, 0.85, t)
        else:
            tool("create_calendar_event" if kind == "schedule" else "send_email", "error")
            audit("action_failed", {"error": "provider error"}, delay_s=28)
            run.status, run.error = RunStatus.failed, "RuntimeError: provider error"
            _maybe_feedback(session, rng, run, user, 0.2, t)
    elif d < 0.85:
        audit("action_superseded")
        run.status = RunStatus.rejected
        _maybe_feedback(session, rng, run, user, 0.5, t)
    else:
        audit("action_rejected")
        run.status = RunStatus.rejected
        _maybe_feedback(session, rng, run, user, 0.4, t)


def _maybe_feedback(session: Session, rng: random.Random, run: AgentRun, user: User, p_up: float, at: dt.datetime) -> None:
    if rng.random() < 0.35:
        session.add(RunFeedback(run_id=run.id, user_id=user.id, rating=1 if rng.random() < p_up else -1, created_at=at, updated_at=at))


def has_demo(session: Session) -> bool:
    return session.scalar(select(User.id).where(User.telegram_user_id == DEMO_TELEGRAM_ID)) is not None


def has_real_usage(session: Session) -> bool:
    demo = select(AgentRun.id).join(User, User.id == AgentRun.user_id).where(User.telegram_user_id == DEMO_TELEGRAM_ID)
    return (
        session.scalar(select(func.count()).select_from(LlmCall).where(LlmCall.run_id.not_in(demo) | LlmCall.run_id.is_(None))) or 0
    ) > 0


def clear_demo(session: Session) -> int:
    user = session.scalar(select(User).where(User.telegram_user_id == DEMO_TELEGRAM_ID))
    if user is None:
        return 0
    run_ids = list(session.scalars(select(AgentRun.id).where(AgentRun.user_id == user.id)))
    if run_ids:
        session.execute(delete(LlmCall).where(LlmCall.run_id.in_(run_ids)))  # FK is SET NULL: delete explicitly
        session.execute(delete(AuditEvent).where(AuditEvent.payload_json["run_id"].astext.in_([str(i) for i in run_ids])))
    session.execute(delete(User).where(User.id == user.id))  # cascades runs, tool calls, feedback, voice
    session.commit()
    return len(run_ids)


def main() -> None:
    parser = argparse.ArgumentParser(prog="app.dashboard.demo")
    sub = parser.add_subparsers(dest="cmd", required=True)
    seed = sub.add_parser("seed")
    seed.add_argument("--days", type=int, default=30)
    seed.add_argument("--if-empty", action="store_true", help="do nothing if demo or real LLM usage already exists")
    sub.add_parser("clear")
    args = parser.parse_args()
    with SessionLocal() as session:
        if args.cmd == "clear":
            print(f"Removed demo data ({clear_demo(session)} runs).")
        elif args.if_empty and (has_demo(session) or has_real_usage(session)):
            print("Demo seed skipped: data already present.")
        else:
            print("Seeded demo data:", seed_demo(session, args.days))


if __name__ == "__main__":
    main()
