"""Records every LLM call into llm_calls, with cost from the per-model price table."""

import contextvars
import json
import logging
from decimal import Decimal
from typing import Callable

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import LlmCall, LlmPurpose
from app.logging_config import run_id_var

log = logging.getLogger("llm")

# Set by the agent per step: "intent" for the first call of a run, "tool_step" afterwards.
purpose_var: contextvars.ContextVar[str] = contextvars.ContextVar("llm_purpose", default="tool_step")

_PER_MILLION = Decimal(1_000_000)


def compute_cost(
    model: str, prompt_tokens: int, completion_tokens: int, cached_tokens: int = 0
) -> Decimal:
    try:
        prices = json.loads(get_settings().llm_prices_json)
    except ValueError:
        log.warning("LLM_PRICES_JSON is not valid JSON; costs are recorded as 0")
        return Decimal(0)
    p = prices.get(model) or prices.get("*")
    if not p:
        return Decimal(0)
    cached = min(cached_tokens, prompt_tokens)
    cost = (
        Decimal(str(p["input_miss"])) * (prompt_tokens - cached)
        + Decimal(str(p["input_hit"])) * cached
        + Decimal(str(p["output"])) * completion_tokens
    ) / _PER_MILLION
    return cost.quantize(Decimal("0.000001"))


def record_llm_call(
    *,
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    latency_ms: int = 0,
    status: str = "ok",
    error: str | None = None,
    retries: int = 0,
    purpose: str | None = None,
    run_id: int | None = None,
    session: Session | None = None,
) -> None:
    """Never raises: losing a metrics row must not break a user's request."""
    try:
        purpose = purpose or purpose_var.get()
        row = LlmCall(
            run_id=run_id if run_id is not None else run_id_var.get(),
            model=model,
            purpose=LlmPurpose(purpose),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
            cost_usd=compute_cost(model, prompt_tokens, completion_tokens, cached_tokens),
            latency_ms=latency_ms,
            status=status,
            error=(error or None) and error[:500],
            retries=retries,
        )
        if session is not None:
            session.add(row)
            session.commit()
            _after(session)
            return
        from app.db.session import SessionLocal

        with SessionLocal() as s:
            s.add(row)
            s.commit()
            _after(s)
    except Exception:
        log.exception("Could not record llm call")


def _after(session: Session) -> None:
    from app.budget import on_usage

    on_usage(session)


Recorder = Callable[..., None]
