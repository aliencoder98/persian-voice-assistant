"""Persian previews and the Confirm / Edit / Cancel keyboard."""

import datetime as dt

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.agent.schemas import Action, ScheduleAction
from app.util.persian import jalali_long_fa, to_fa_digits


def _hhmm(t: dt.time) -> str:
    return to_fa_digits(t.strftime("%H:%M"))


def format_preview(action: Action) -> str:
    if isinstance(action, ScheduleAction):
        start = dt.datetime.combine(action.date, action.time)
        end = (start + dt.timedelta(minutes=action.duration_min)).time()
        guests = (
            "\n".join(f"  • {g.name + ' ' if g.name else ''}<{g.email}>" for g in action.guests)
            if action.guests
            else "  —"
        )
        return (
            "📅 پیش‌نمایش رویداد\n\n"
            f"عنوان: {action.title}\n"
            f"تاریخ: {jalali_long_fa(action.date)} ({action.date.isoformat()})\n"
            f"ساعت: {_hhmm(action.time)} تا {_hhmm(end)} ({to_fa_digits(action.duration_min)} دقیقه)\n"
            f"مهمانان:\n{guests}\n"
            f"توضیحات: {action.description or '—'}\n\n"
            "با تأیید شما، رویداد ساخته می‌شود و برای مهمانان دعوت‌نامه ارسال می‌شود."
        )
    return (
        "✉️ پیش‌نمایش ایمیل\n\n"
        f"گیرنده: {action.to_name + ' ' if action.to_name else ''}<{action.to}>\n"
        f"موضوع: {action.subject}\n\n"
        f"{action.body}\n\n"
        "با تأیید شما، این ایمیل ارسال می‌شود."
    )


def confirmation_keyboard(run_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ تأیید", callback_data=f"ok:{run_id}"),
                InlineKeyboardButton("✏️ ویرایش", callback_data=f"ed:{run_id}"),
                InlineKeyboardButton("❌ لغو", callback_data=f"no:{run_id}"),
            ]
        ]
    )
