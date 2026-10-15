"""Evaluation of the agent on 30 Persian transcripts with expected results.

    docker compose run --rm bot python -m app.evals.run [--out docs/EVAL.md] [--ids s01,s02]

Uses the real DeepSeek provider, a throwaway database (<POSTGRES_DB>_eval), a fixed "now"
(Wednesday 2026-10-14 10:30 Asia/Tehran = 22 Mehr 1405) and fixed contacts. Nothing is sent or scheduled.
"""

import argparse
import datetime as dt
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.agent.agent import process_message
from app.agent.llm import LLMProvider
from app.agent.memory import build_context
from app.config import get_settings
from app.db import models  # noqa: F401
from app.db.base import Base
from app.db.models import Contact, User
from app.tools.registry import TEHRAN

DATASET = Path(__file__).with_name("dataset.jsonl")
EVAL_NOW = dt.datetime(2026, 10, 14, 10, 30, tzinfo=TEHRAN)
EVAL_CONTACTS = [
    ("علی رضایی", ["علی", "Ali"], "ali@example.com"),
    ("سارا محمدی", ["سارا", "Sara"], "sara@example.com"),
    ("نگار احمدی", ["نگار", "Negar"], "negar@example.com"),
    ("محمد کریمی", ["محمد", "Mohammad", "کریمی"], "mohammad@example.com"),
]


def load_dataset(path: Path = DATASET) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass
class CaseResult:
    id: str
    tags: list[str]
    transcript: str
    expect: dict[str, Any]
    got_kind: str
    got: dict[str, Any] = field(default_factory=dict)  # action JSON or {"question": ...}
    checks: dict[str, bool] = field(default_factory=dict)  # name -> passed
    tokens: int = 0
    latency_ms: int = 0
    steps: int = 0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def score(case: dict[str, Any], got_kind: str, got: dict[str, Any]) -> dict[str, bool]:
    """Compare one outcome with its expectation. Keys are metric names used in the report."""
    exp = case["expect"]
    checks = {"intent": got_kind == exp["kind"]}
    if exp["kind"] == "schedule":
        action = got if got_kind == "schedule" else {}
        checks["date"] = action.get("date") == exp["date"]
        checks["time"] = (action.get("time") or "")[:5] == exp["time"]
        checks["duration"] = action.get("duration_min") == exp["duration_min"]
        checks["guests"] = sorted(g["email"].lower() for g in action.get("guests", [])) == sorted(exp["guests"])
    elif exp["kind"] == "email":
        action = got if got_kind == "email" else {}
        checks["recipient"] = (action.get("to") or "").lower() == exp["to"]
    else:
        question = got.get("question", "") if got_kind == "clarify" else ""
        checks["question"] = any(s in question for s in exp.get("question_contains_any", [""])) and bool(question)
    return checks


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    def acc(metric: str, only: list[CaseResult] | None = None) -> dict[str, Any]:
        rows = [r for r in (only if only is not None else results) if metric in r.checks]
        ok = sum(r.checks[metric] for r in rows)
        return {"correct": ok, "total": len(rows), "accuracy": (ok / len(rows)) if rows else None}

    return {
        "cases": len(results),
        "all_checks_passed": sum(r.passed for r in results),
        "intent": acc("intent"),
        "date": acc("date"),
        "time": acc("time"),
        "duration": acc("duration"),
        "guests": acc("guests"),
        "recipient": acc("recipient"),
        "clarification": acc("question"),
        "tokens_total": sum(r.tokens for r in results),
        "tokens_avg": statistics.mean(r.tokens for r in results) if results else 0,
        "latency_p50_ms": statistics.median(r.latency_ms for r in results) if results else 0,
        "steps_avg": statistics.mean(r.steps for r in results) if results else 0,
    }


def make_eval_engine():
    s = get_settings()
    name = f"{s.postgres_db}_eval"
    admin = create_engine(s.database_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()
    engine = create_engine(s.database_url.set(database=name))
    Base.metadata.create_all(engine)
    return engine, name


def run_case(engine, provider: LLMProvider, case: dict[str, Any]) -> CaseResult:
    """Each case runs in its own rolled-back transaction, so cases cannot influence each other."""
    conn = engine.connect()
    outer = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        user = User(telegram_user_id=1, name="Owner", email="owner@example.com")
        session.add(user)
        session.flush()
        session.add_all([Contact(user_id=user.id, name=n, aliases=a, email=e) for n, a, e in EVAL_CONTACTS])
        session.flush()
        text_in = case["transcript"]
        started = time.perf_counter()
        run, outcome = process_message(
            session, provider, user, text_in, build_context(session, user, text_in), now=lambda: EVAL_NOW
        )
        elapsed = int((time.perf_counter() - started) * 1000)
        got = run.action_json or {}
        kind = outcome.kind
        return CaseResult(
            id=case["id"], tags=case.get("tags", []), transcript=text_in, expect=case["expect"], got_kind=kind,
            got=got, checks=score(case, kind, got), tokens=outcome.tokens, latency_ms=elapsed,
            steps=outcome.steps, error=outcome.error,
        )  # fmt: skip
    finally:
        session.close()
        outer.rollback()
        conn.close()


def run_eval(provider: LLMProvider, cases: list[dict[str, Any]], engine=None) -> list[CaseResult]:
    own_engine = engine is None
    name = None
    if own_engine:
        engine, name = make_eval_engine()
    try:
        return [run_case(engine, provider, c) for c in cases]
    finally:
        if own_engine:
            engine.dispose()
            admin = create_engine(get_settings().database_url, isolation_level="AUTOCOMMIT")
            with admin.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
            admin.dispose()


def _pct(m: dict[str, Any]) -> str:
    return "n/a" if m["accuracy"] is None else f"{m['accuracy'] * 100:.1f}% ({m['correct']}/{m['total']})"


def render_report(results: list[CaseResult], model: str, prompt_version: str) -> str:
    s = summarize(results)
    lines = [
        "# Agent evaluation",
        "",
        f"- Model: `{model}` · prompt `{prompt_version}` · {s['cases']} Persian transcripts · fixed now: Wed 2026-10-14 10:30 Asia/Tehran (22 Mehr 1405)",
        f"- Date generated: {dt.date.today().isoformat()}",
        "",
        "| Metric | Accuracy |",
        "|---|---|",
        f"| **Intent** (schedule / email / clarify) | **{_pct(s['intent'])}** |",
        f"| **Date** (schedule cases) | **{_pct(s['date'])}** |",
        f"| **Time** (schedule cases) | **{_pct(s['time'])}** |",
        f"| Duration (schedule cases) | {_pct(s['duration'])} |",
        f"| Guests (schedule cases) | {_pct(s['guests'])} |",
        f"| Recipient (email cases) | {_pct(s['recipient'])} |",
        f"| Clarifying question asked (clarify cases) | {_pct(s['clarification'])} |",
        f"| Cases with every check passed | {s['all_checks_passed']}/{s['cases']} |",
        "",
        f"Tokens: {s['tokens_total']} total, {s['tokens_avg']:.0f} per case · median latency {s['latency_p50_ms'] / 1000:.1f} s · "
        f"{s['steps_avg']:.1f} LLM steps per case.",
        "",
    ]
    failures = [r for r in results if not r.passed]
    if failures:
        lines += ["## Failures", ""]
        for r in failures:
            bad = [k for k, v in r.checks.items() if not v]
            lines += [
                f"- `{r.id}` — failed: {', '.join(bad)}",
                f"  - input: {r.transcript}",
                f"  - expected: `{json.dumps(r.expect, ensure_ascii=False)}`",
                f"  - got ({r.got_kind}): `{json.dumps(r.got, ensure_ascii=False)}`" + (f" error: {r.error}" if r.error else ""),
            ]
    else:
        lines.append("All cases passed every check.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(prog="app.evals.run")
    parser.add_argument("--out", help="write the Markdown report here")
    parser.add_argument("--ids", help="comma-separated case ids to run")
    parser.add_argument("--json", help="write raw results as JSON here")
    args = parser.parse_args()

    from app.agent.llm import DeepSeekProvider
    from app.agent.prompt import PROMPT_VERSION

    cases = load_dataset()
    if args.ids:
        wanted = set(args.ids.split(","))
        cases = [c for c in cases if c["id"] in wanted]
    usage: list[dict] = []
    provider = DeepSeekProvider(recorder=lambda **kw: usage.append(kw))  # not written to the real llm_calls
    results = run_eval(provider, cases)
    report = render_report(results, provider.model, PROMPT_VERSION)
    print(report)
    if args.out:
        Path(args.out).write_text(report, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(
            json.dumps([r.__dict__ for r in results], ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if all(r.checks.get("intent") for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
