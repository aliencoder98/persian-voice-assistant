import asyncio
import logging
from pathlib import Path

from sqlalchemy import select
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from app.bot import messages
from app.config import get_settings
from app.db.models import AuditEvent, User, VoiceMessage
from app.db.session import SessionLocal
from app.stt.vosk_stt import SttError, transcribe_ogg

log = logging.getLogger("bot")


async def guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Runs before every other handler; stops everyone not on the allowlist."""
    user = update.effective_user
    if user is not None and user.id in get_settings().allowed_ids:
        return
    log.warning("Refused user %s", user.id if user else None)
    if update.effective_message is not None:
        await update.effective_message.reply_text(messages.REFUSED)
    raise ApplicationHandlerStop


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.START)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.HELP)


async def echo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(messages.ECHO.format(text=update.message.text))


def _get_or_create_user(session, tg_user) -> User:
    settings = get_settings()
    user = session.scalar(select(User).where(User.telegram_user_id == tg_user.id))
    if user is None:
        user = User(
            telegram_user_id=tg_user.id,
            name=tg_user.full_name,
            timezone=settings.default_timezone,
        )
        session.add(user)
        session.flush()
    return user


def _store_voice(tg_user, file_id: str, duration: int | None, path: str) -> int:
    with SessionLocal() as session:
        user = _get_or_create_user(session, tg_user)
        voice = VoiceMessage(
            user_id=user.id,
            telegram_file_id=file_id,
            duration_s=duration,
            audio_path=path,
        )
        session.add(voice)
        session.flush()
        session.add(
            AuditEvent(
                event_type="voice_received",
                actor=f"telegram:{tg_user.id}",
                payload_json={"voice_message_id": voice.id, "duration_s": duration},
            )
        )
        session.commit()
        return voice.id


def _finish_voice(voice_id: int, text: str, confidence: float) -> None:
    with SessionLocal() as session:
        row = session.get(VoiceMessage, voice_id)
        row.transcript = text
        row.stt_confidence = confidence
        row.audio_path = None  # audio is deleted right after transcription (SPEC)
        session.commit()


async def voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    tg_voice = msg.voice
    audio_dir = Path(get_settings().audio_dir) / str(update.effective_user.id)
    audio_dir.mkdir(parents=True, exist_ok=True)
    path = audio_dir / f"{tg_voice.file_unique_id}.ogg"

    try:
        tg_file = await context.bot.get_file(tg_voice.file_id)
        await tg_file.download_to_drive(path)
        voice_id = await asyncio.to_thread(
            _store_voice, update.effective_user, tg_voice.file_id, tg_voice.duration, str(path)
        )
        try:
            result = await asyncio.to_thread(transcribe_ogg, path)
        except SttError:
            log.exception("STT failed for voice %s", voice_id)
            await msg.reply_text(messages.STT_FAILED)
            return
    finally:
        path.unlink(missing_ok=True)

    await asyncio.to_thread(_finish_voice, voice_id, result.text, result.confidence)
    log.info("Voice %s transcribed (confidence %.2f)", voice_id, result.confidence)
    if not result.usable:
        await msg.reply_text(messages.REPEAT)
        return
    await msg.reply_text(messages.TRANSCRIPT.format(text=result.text))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        await update.effective_message.reply_text(messages.ERROR)


def build_application() -> Application:
    settings = get_settings()
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set.")
    if not settings.allowed_ids:
        raise SystemExit("ALLOWED_USER_IDS is not set; refusing to start an open bot.")

    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(TypeHandler(Update, guard), group=-1)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.VOICE, voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, echo))
    app.add_error_handler(on_error)
    return app


def main() -> None:
    logging.basicConfig(
        level=get_settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # keeps the bot token out of logs
    build_application().run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
