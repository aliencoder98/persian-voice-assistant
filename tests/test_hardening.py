import asyncio
import io
import json
import logging
import os
from types import SimpleNamespace as N

import pytest
from telegram.ext import ApplicationHandlerStop

from app.agent.agent import process_message
from app.agent.context import AgentContext
from app.bot import main as bot
from app.bot import messages
from app.bot.cleanup import delete_old_audio
from app.bot.ratelimit import RateLimiter
from app.config import get_settings
from app.logging_config import JsonFormatter, bind_run_id, setup_logging
from tests.conftest import FIXED_NOW
from tests.fakes import ScriptedLLM, call, turn


# ---------------- rate limit


def test_rate_limiter_sliding_window():
    t = [0.0]
    rl = RateLimiter(3, 60, clock=lambda: t[0])
    assert [rl.allow(1) for _ in range(4)] == [True, True, True, False]
    assert rl.allow(2)  # other users are unaffected
    t[0] = 59
    assert not rl.allow(1)
    t[0] = 61
    assert rl.allow(1)


class Replies:
    def __init__(self):
        self.texts, self.alerts = [], []

    async def reply_text(self, text, **kw):
        self.texts.append(text)

    async def answer(self, text=None, show_alert=False):
        self.alerts.append(text)


def test_guard_limits_each_user_and_still_refuses_strangers(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "allowed_user_ids", "111")
    monkeypatch.setattr(s, "rate_limit_per_minute", 2)
    monkeypatch.setattr(bot, "_limiter", None)

    def update(uid, callback=False):
        r = Replies()
        return N(effective_user=N(id=uid), effective_message=r, callback_query=r if callback else None), r

    async def go():
        for _ in range(2):
            u, _ = update(111)
            assert await bot.guard(u, None) is None
        u, r = update(111)
        with pytest.raises(ApplicationHandlerStop):
            await bot.guard(u, None)
        assert r.texts == [messages.RATE_LIMITED]
        u, r = update(111, callback=True)
        with pytest.raises(ApplicationHandlerStop):
            await bot.guard(u, None)
        assert r.alerts == [messages.RATE_LIMITED]
        u, r = update(999)
        with pytest.raises(ApplicationHandlerStop):
            await bot.guard(u, None)
        assert r.texts == [messages.REFUSED]

    asyncio.run(go())


# ---------------- voice length


def test_voice_longer_than_the_limit_is_refused_before_download(monkeypatch):
    monkeypatch.setattr(get_settings(), "max_voice_seconds", 60)
    r = Replies()
    downloaded = []

    class Bot:
        async def get_file(self, *a):
            downloaded.append(a)

    update = N(message=N(voice=N(duration=61, file_id="f", file_unique_id="u"), reply_text=r.reply_text),
               effective_user=N(id=111))  # fmt: skip
    asyncio.run(bot.voice(update, N(bot=Bot())))
    assert downloaded == []
    assert r.texts == [messages.VOICE_TOO_LONG.format(max=60)] and "۶۰" not in r.texts[0]


# ---------------- Persian, short errors


def test_unhandled_errors_reach_the_user_as_a_short_persian_message(monkeypatch, caplog):
    class FakeUpdate:  # on_error checks isinstance(update, Update)
        def __init__(self, reply):
            self.effective_message = N(reply_text=reply)

    monkeypatch.setattr(bot, "Update", FakeUpdate)
    r = Replies()
    with caplog.at_level(logging.ERROR):
        asyncio.run(bot.on_error(FakeUpdate(r.reply_text), N(error=RuntimeError("secret detail: sk-123"))))
    assert r.texts == [messages.ERROR]  # short Persian text, nothing from the exception
    assert "secret detail" in caplog.text  # full details stay in the logs


def test_on_error_message_text_has_no_internals():
    assert "Traceback" not in messages.ERROR and messages.ERROR.startswith("متأسفم")


# ---------------- audio cleanup


def test_old_audio_is_deleted_new_audio_and_other_files_stay(tmp_path):
    (tmp_path / "111").mkdir()
    old, new, other = tmp_path / "111" / "old.ogg", tmp_path / "111" / "new.ogg", tmp_path / "111" / "notes.txt"
    for f in (old, new, other):
        f.write_bytes(b"x")
    now = 1_000_000.0
    os.utime(old, (now - 7200, now - 7200))
    os.utime(other, (now - 7200, now - 7200))
    os.utime(new, (now - 600, now - 600))
    deleted = delete_old_audio(tmp_path, 3600, now=lambda: now)
    assert deleted == [old] and not old.exists() and new.exists() and other.exists()
    assert delete_old_audio(tmp_path / "missing", 3600) == []


# ---------------- structured logs and the run_id correlation id


def capture():
    stream = io.StringIO()
    setup_logging("INFO", stream=stream)
    return stream


def lines(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_logs_are_json_and_share_the_run_id_across_agent_and_tools(session, user):
    stream = capture()
    llm = ScriptedLLM(
        turn(call("find_contact", name="علی")),
        turn(call("ask_clarification", question="چه ساعتی؟")),
    )
    run, _ = process_message(session, llm, user, "فردا با علی", AgentContext(), now=lambda: FIXED_NOW)
    records = lines(stream)
    by_logger = {r["logger"] for r in records}
    assert {"agent", "tools"} <= by_logger
    assert all(r["run_id"] == run.id for r in records if r["logger"] in {"agent", "tools"})
    tool = next(r for r in records if r["logger"] == "tools")
    assert tool["tool"] == "find_contact" and tool["ok"] is True
    done = next(r for r in records if r["msg"] == "run finished")
    assert done["kind"] == "clarify" and done["tokens"] == 460


def test_run_id_follows_into_worker_threads_and_resets():
    stream = capture()
    log = logging.getLogger("t")

    async def go():
        with bind_run_id(42):
            await asyncio.to_thread(log.info, "inside thread")
        log.info("after")

    asyncio.run(go())
    a, b = lines(stream)
    assert a["run_id"] == 42 and b["run_id"] is None


def test_json_formatter_keeps_unicode_and_exceptions():
    rec = logging.LogRecord("x", logging.ERROR, "f", 1, "خطا", (), None)
    try:
        1 / 0
    except ZeroDivisionError:
        import sys

        rec.exc_info = sys.exc_info()
    out = json.loads(JsonFormatter().format(rec))
    assert out["msg"] == "خطا" and "ZeroDivisionError" in out["exc"]
