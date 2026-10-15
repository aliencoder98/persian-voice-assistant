"""Safety net: delete leftover audio files (the happy path deletes right after transcription)."""

import asyncio
import logging
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("cleanup")
AUDIO_SUFFIXES = {".ogg", ".oga", ".opus", ".wav", ".mp3", ".m4a"}


def delete_old_audio(audio_dir: str | Path, max_age_s: int, now: Callable[[], float] = time.time) -> list[Path]:
    root = Path(audio_dir)
    deleted: list[Path] = []
    if not root.is_dir():
        return deleted
    cutoff = now() - max_age_s
    for path in root.rglob("*"):
        try:
            if path.is_file() and path.suffix.lower() in AUDIO_SUFFIXES and path.stat().st_mtime < cutoff:
                path.unlink()
                deleted.append(path)
        except OSError:
            log.warning("Could not delete %s", path.name)
    if deleted:
        log.info("Deleted leftover audio", extra={"count": len(deleted)})
    return deleted


async def cleanup_loop(audio_dir: str, max_age_s: int, interval_s: int = 600) -> None:
    while True:
        try:
            await asyncio.to_thread(delete_old_audio, audio_dir, max_age_s)
        except Exception:
            log.exception("Audio cleanup failed")
        await asyncio.sleep(interval_s)
