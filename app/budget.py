"""Token budgets: usage per Tehran day / Jalali month, one-time alerts at 80% and 100%, optional hard stop."""

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import BudgetAlert, LlmCall
from app.notify import telegram_send
from app.tools.registry import TEHRAN
from app.util.persian import jalali_date, jalali_to_gregorian, to_fa_digits

log = logging.getLogger("budget")
THRESHOLDS = (80, 100)


@dataclass
class Period:
    key: str  # "day:2026-10-15" / "month:1405-07"
    label: str  # Persian
    start: dt.datetime  # tz-aware, Asia/Tehran
    limit: int
    used: int = 0

    @property
    def percent(self) -> float:
        return 100.0 * self.used / self.limit if self.limit else 0.0


def _tehran_day_start(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time.min, tzinfo=TEHRAN)


def periods(now: dt.datetime | None = None) -> list[Period]:
    s = get_settings()
    now = now or dt.datetime.now(TEHRAN)
    today = now.astimezone(TEHRAN).date()
    jy, jm, _ = jalali_date(today)
    month_first = dt.date(*jalali_to_gregorian(jy, jm, 1))
    return [
        Period(f"day:{today.isoformat()}", "روزانه", _tehran_day_start(today), s.daily_token_budget),
        Period(f"month:{jy:04d}-{jm:02d}", "ماهانه", _tehran_day_start(month_first), s.monthly_token_budget),
    ]


def tokens_since(session: Session, start: dt.datetime) -> int:
    total = session.scalar(
        select(func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0)).where(
            LlmCall.created_at >= start, LlmCall.status == "ok"
        )
    )
    return int(total or 0)


def status(session: Session, now: dt.datetime | None = None) -> list[Period]:
    result = periods(now)
    for p in result:
        p.used = tokens_since(session, p.start)
    return result


def is_blocked(session: Session, now: dt.datetime | None = None) -> bool:
    """True when the hard stop is on and any configured budget is used up."""
    if not get_settings().budget_hard_stop:
        return False
    return any(p.limit and p.used >= p.limit for p in status(session, now))


def alert_text(p: Period, threshold: int) -> str:
    used, limit = to_fa_digits(f"{p.used:,}"), to_fa_digits(f"{p.limit:,}")
    stop = ""
    if threshold >= 100:
        stop = (
            "\nجلوگیری از درخواست‌های جدید فعال است؛ تا شروع دورهٔ بعد پاسخی داده نمی‌شود."
            if get_settings().budget_hard_stop
            else "\n(جلوگیری از درخواست‌ها غیرفعال است؛ ربات به کار ادامه می‌دهد.)"
        )
    return f"⚠️ بودجهٔ توکن {p.label} به {to_fa_digits(threshold)}٪ رسید.\nمصرف: {used} از {limit} توکن.{stop}"


def on_usage(
    session: Session,
    now: dt.datetime | None = None,
    send: Callable[[int, str], bool] | None = None,
) -> list[tuple[str, int]]:
    """After each LLM call: announce newly crossed thresholds (once per period). Returns what fired."""
    send = send or telegram_send  # looked up at call time so tests can replace it
    fired = []
    owner = next(iter(sorted(get_settings().allowed_ids)), None)
    for p in status(session, now):
        if not p.limit:
            continue
        for threshold in THRESHOLDS:
            if p.percent < threshold:
                continue
            try:
                with session.begin_nested():
                    session.add(BudgetAlert(period=p.key, threshold=threshold, tokens_used=p.used))
                    session.flush()
            except IntegrityError:
                continue  # already announced
            session.commit()
            fired.append((p.key, threshold))
            if owner is not None:
                send(owner, alert_text(p, threshold))
            log.warning("Budget threshold crossed", extra={"period": p.key, "threshold": threshold})
    return fired
