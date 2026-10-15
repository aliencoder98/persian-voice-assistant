"""Persian-locale number and date formatting for templates."""

import datetime as dt
from zoneinfo import ZoneInfo

from app.util.persian import jalali_str, to_fa_digits

TEHRAN = ZoneInfo("Asia/Tehran")
THOUSANDS, DECIMAL = "٬", "٫"
DASH = "—"


def fa_num(value, decimals: int = 0) -> str:
    if value is None:
        return DASH
    text = f"{float(value):,.{decimals}f}"
    whole, _, frac = text.partition(".")
    out = whole.replace(",", THOUSANDS) + (DECIMAL + frac if frac else "")
    return to_fa_digits(out)


def fa_pct(ratio, decimals: int = 1) -> str:
    return DASH if ratio is None else fa_num(ratio * 100, decimals) + "٪"


def fa_usd(value) -> str:
    if value is None:
        return DASH
    value = float(value)
    return "$" + fa_num(value, 2 if abs(value) >= 1 else 4)


def fa_ms(ms) -> str:
    if ms is None:
        return DASH
    ms = float(ms)
    return fa_num(ms / 1000, 1) + " ث" if ms >= 1000 else fa_num(ms) + " م‌ث"


def _local(value: dt.datetime) -> dt.datetime:
    return (value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)).astimezone(TEHRAN)


def fa_date(value: dt.date | dt.datetime | None) -> str:
    if value is None:
        return DASH
    day = _local(value).date() if isinstance(value, dt.datetime) else value
    return to_fa_digits(jalali_str(day))


def fa_dt(value: dt.datetime | None) -> str:
    if value is None:
        return DASH
    local = _local(value)
    return f"{fa_date(local)} {to_fa_digits(local.strftime('%H:%M'))}"


def short_day(iso: str) -> str:
    """Chart axis label: Jalali MM/DD in Persian digits."""
    return to_fa_digits(jalali_str(dt.date.fromisoformat(iso))[5:])


def csv_safe(value) -> str:
    """Stop spreadsheet formula injection from user-controlled text."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
