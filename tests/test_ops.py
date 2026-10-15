import asyncio
import os
import time

import httpx

from app import outbound_check
from app.bot import heartbeat
from app.config import get_settings


def test_heartbeat_file_goes_stale(tmp_path):
    f = str(tmp_path / "hb")
    assert not heartbeat.is_fresh(f)  # missing file: unhealthy
    heartbeat.touch(f)
    assert heartbeat.is_fresh(f)
    old = time.time() - 600
    os.utime(f, (old, old))
    assert not heartbeat.is_fresh(f, max_age_s=120)
    assert heartbeat.is_fresh(f, max_age_s=120, now=old + 60)


def test_heartbeat_loop_keeps_touching(tmp_path):
    f = tmp_path / "hb"

    async def go():
        task = asyncio.create_task(heartbeat.heartbeat_loop(str(f), interval_s=0.05))
        await asyncio.sleep(0.2)
        first = f.stat().st_mtime
        await asyncio.sleep(0.2)
        task.cancel()
        return first, f.stat().st_mtime

    first, last = asyncio.run(go())
    assert last > first


def test_heartbeat_check_exit_code(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "bot_heartbeat_file", str(tmp_path / "hb"))
    assert heartbeat.main() == 1
    heartbeat.touch(str(tmp_path / "hb"))
    assert heartbeat.main() == 0


def test_outbound_targets_follow_gateway_and_proxy_settings(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "telegram_api_base_url", "https://relay.example.com/")
    monkeypatch.setattr(s, "deepseek_base_url", "https://gw.example.com/v1")
    monkeypatch.setattr(s, "telegram_proxy_url", "http://proxy:3128")
    by_name = {t.name: t for t in outbound_check.targets()}
    assert by_name["Telegram Bot API"].url == "https://relay.example.com/"
    assert by_name["Telegram Bot API"].proxy == "http://proxy:3128"
    assert by_name["DeepSeek API"].url == "https://gw.example.com/v1/models"
    assert {"Resend API", "Google OAuth", "Google Calendar API"} <= set(by_name)


def test_outbound_check_counts_any_http_answer_as_reachable():
    target = outbound_check.Target("x", "https://x.test/")

    def client(handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    assert outbound_check.check(target, client(lambda r: httpx.Response(401)))[0]
    assert outbound_check.check(target, client(lambda r: httpx.Response(404)))[0]
    assert not outbound_check.check(target, client(lambda r: httpx.Response(502)))[0]

    def refuse(request):
        raise httpx.ConnectError("blocked")

    ok, detail = outbound_check.check(target, client(refuse))
    assert not ok and "ConnectError" in detail


def test_telegram_gateway_is_used_for_notifications(monkeypatch):
    from app import notify

    s = get_settings()
    monkeypatch.setattr(s, "telegram_bot_token", "123:ABC")
    monkeypatch.setattr(s, "telegram_api_base_url", "https://relay.example.com")
    seen = {}

    def fake_post(url, **kw):
        seen["url"], seen["proxy"] = url, kw.get("proxy")
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(notify.httpx, "post", fake_post)
    monkeypatch.setattr(s, "telegram_proxy_url", "")
    assert notify.telegram_send(1, "hi")
    assert seen["url"] == "https://relay.example.com/bot123:ABC/sendMessage" and seen["proxy"] is None


def test_bot_uses_the_gateway_for_polling(monkeypatch):
    from app.bot import main as bot

    s = get_settings()
    monkeypatch.setattr(s, "telegram_bot_token", "123:ABC")
    monkeypatch.setattr(s, "allowed_user_ids", "1")
    monkeypatch.setattr(s, "telegram_api_base_url", "https://relay.example.com/")
    app = bot.build_application()
    assert app.bot.base_url == "https://relay.example.com/bot123:ABC"


# ---- connection retries (the Docker DNS flake seen in practice)


def test_retry_connect_retries_only_connection_failures():
    from app.tools.http_retry import retry_connect

    sleeps, calls = [], {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("No address associated with hostname")
        return "ok"

    assert retry_connect(flaky, sleep=sleeps.append) == "ok" and calls["n"] == 3 and sleeps == [1.0, 2.0]

    calls["n"] = 0

    def always():
        calls["n"] += 1
        raise httpx.ConnectError("down")

    try:
        retry_connect(always, attempts=2, sleep=lambda s: None)
    except httpx.ConnectError:
        pass
    assert calls["n"] == 2

    # a read timeout may mean the server already acted: never repeat it
    calls["n"] = 0

    def slow():
        calls["n"] += 1
        raise httpx.ReadTimeout("slow")

    try:
        retry_connect(slow, sleep=lambda s: None)
    except httpx.ReadTimeout:
        pass
    assert calls["n"] == 1


def test_resend_survives_a_dns_hiccup_and_sends_once(monkeypatch):
    from app.tools import http_retry, resend_client

    s = get_settings()
    monkeypatch.setattr(s, "resend_api_key", "re_key")
    monkeypatch.setattr(s, "email_from", "a@example.com")
    monkeypatch.setattr(http_retry.time, "sleep", lambda x: None)
    attempts = {"n": 0}

    def fake_post(url, **kw):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("No address associated with hostname")
        return httpx.Response(200, json={"id": "msg_1"}, request=httpx.Request("POST", url))

    monkeypatch.setattr(resend_client.httpx, "post", fake_post)
    assert resend_client.send_email("to@example.com", "s", "b") == "msg_1" and attempts["n"] == 2
